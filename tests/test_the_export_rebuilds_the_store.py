"""An external ledger snapshot reconstructs nonempty historical answers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import capabilities, ledger, run_store
from tests.test_revision_reads_restate_against_the_store import (
    PROJECT,
    _build,
    _commit,
    _git,
    _iso,
    _ledger_path,
    _retire,
    _run_path,
)


def _exporter():
    from reckon.crew import export_ledger

    return export_ledger


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("RECKON_RUN_STORE", str(tmp_path / "run_store.db"))
    monkeypatch.delenv("RECKON_STATE_ROOT", raising=False)
    monkeypatch.delenv("RECKON_MOUNTS_PATH", raising=False)
    built = _build(tmp_path)
    for record in built.records:
        record.update(
            project=PROJECT,
            tool_steps=5,
            orientation_input_tokens=120,
            node_definition={
                "goal": "measure work",
                "write_paths": ["reckon/sample.py"],
            },
        )
    data = {
        "_version": 7,
        "members": [{"id": "worker", "harness": "fixture", "description": "méasured"}],
        "holds": [{"hold_id": "held", "reason": "window"}],
        "runs": list(built.records),
    }
    changes = {_ledger_path(): json.dumps({"data": data})}
    changes.update({_run_path(row["run_id"]): json.dumps(row) for row in built.records})
    for plan in ("a-plan", "b-plan"):
        changes[f"docs/plans/{plan}.html"] = (
            '<html><head><meta name="reckon-type" content="plan">'
            f'<meta name="docs-project" content="{PROJECT}">'
            f'<meta name="plan-slug" content="{plan}">'
            '<meta name="plan-effort-hours" content="2.0"></head><body></body></html>'
        )
    built.held_head = _commit(
        built.repo,
        "docs: record measured history\n\nKeep the fixture's observations.",
        7,
        changes,
    )
    run_store.import_ledger(PROJECT, root=built.repo)
    with run_store.RunStore() as store:
        assert store.auxiliary(PROJECT, lambda: (data, data["_version"]))[1] == 7
    return built, data


def _capability_figures(history):
    result = capabilities.derive_capabilities({PROJECT: history.repo / "docs"})

    # Source versions identify a file envelope; the measured figures survive
    # the envelope's retirement and the resulting change of provenance.
    def figures(value):
        if isinstance(value, dict):
            return {
                key: figures(item)
                for key, item in value.items()
                if key not in {"ledger_versions", "source_digest"}
            }
        if isinstance(value, list):
            return [figures(item) for item in value]
        return value

    return figures(result)


def test_the_export_lives_outside_git_and_preserves_the_removed_content(corpus):
    history, expected = corpus
    removed = json.loads(
        _git(history.repo, "show", f"{history.held_head}:{_ledger_path()}")
    )
    _retire(history)
    report = _exporter().export_project(PROJECT)
    target = Path(report["path"])
    assert (
        target == history.root / "config" / "crew" / "exports" / PROJECT / "crew.json"
    )
    assert not target.is_relative_to(history.repo)
    assert _git(history.repo, "ls-files", "docs/state") == ""
    assert not (history.repo / _ledger_path()).exists()
    snapshot = json.loads(target.read_text())
    assert report["rows"] == len(snapshot["data"]["runs"]) == len(expected["runs"]) > 0
    assert _canonical(snapshot["data"]).encode() == _canonical(removed["data"]).encode()
    assert snapshot["run_revisions"] == history.additions
    assert target.stat().st_mode & 0o777 == 0o600


def test_rebuilding_preserves_capabilities_velocity_details_and_versions(
    corpus, monkeypatch
):
    history, expected = corpus
    before = _capability_figures(history)
    assert before["configurations"], "the capability control must see measured runs"
    assert (
        sum(len(row["observations"]) for row in before["configurations"])
        == len(expected["runs"])
        > 0
    )
    _retire(history)
    live = _capability_figures(history)
    assert live == before
    live_capabilities = capabilities.derive_capabilities(
        {PROJECT: history.repo / "docs"}
    )
    figures = history.figures(history.capture(_iso(9)))
    assert len(figures) == len(expected["runs"]) > 0
    snapshot = _exporter().export_project(PROJECT)
    rebuilt = history.root / "rebuilt.db"
    receipt = _exporter().rebuild_store(PROJECT, snapshot["path"], rebuilt)
    assert receipt["rows"] == len(expected["runs"]) > 0
    assert history.figures(history.capture(_iso(9), run_store_db=rebuilt)) == figures
    assert run_store.stored_run_revisions(PROJECT, path=rebuilt) == history.additions
    monkeypatch.setenv("RECKON_RUN_STORE", str(rebuilt))
    assert _capability_figures(history) == live
    assert (
        capabilities.derive_capabilities({PROJECT: history.repo / "docs"})
        == live_capabilities
    )
    actual, _ = ledger.load(PROJECT, root=history.repo)
    assert actual["runs"] == expected["runs"]
    assert actual["members"] == expected["members"]
    assert actual["holds"] == expected["holds"]
    assert actual["auxiliary_version"] == expected["_version"]
    print(
        f"reconstructed_runs={len(actual['runs'])} capability_configurations={len(live['configurations'])} velocity_runs={len(figures)}"
    )


def test_retired_runs_are_exported_with_their_details_and_revisions(corpus):
    history, expected = corpus
    before = datetime(2026, 11, 1, tzinfo=UTC)
    with run_store.RunStore() as store:
        result = store.retire(
            before=before, protected=set(), projects={PROJECT}, apply=True
        )
    assert len(result["retired"]) == len(expected["runs"]) > 0
    report = _exporter().export_project(PROJECT)
    snapshot = json.loads(Path(report["path"]).read_text())
    assert snapshot["data"]["runs"] == expected["runs"]
    rebuilt = history.root / "archive-rebuilt.db"
    _exporter().rebuild_store(PROJECT, report["path"], rebuilt)
    assert run_store.stored_run_rows(PROJECT, path=rebuilt) == expected["runs"]
    assert run_store.stored_run_revisions(PROJECT, path=rebuilt) == history.additions


@pytest.mark.parametrize("location", ["inside", "symlink"])
def test_a_config_home_inside_git_is_refused(corpus, monkeypatch, location):
    history, _ = corpus
    home = history.repo / "config"
    if location == "symlink":
        link = history.root / "linked-config"
        home.mkdir()
        link.symlink_to(home, target_is_directory=True)
        home = link
    monkeypatch.setenv("RECKON_HOME", str(home))
    with pytest.raises(ValueError, match="outside git"):
        _exporter().export_project(PROJECT)
    assert not (history.repo / "config" / "crew" / "exports").exists()


def test_an_empty_export_is_refused_before_creating_a_store(corpus):
    history, _ = corpus
    exporter = _exporter()
    report = exporter.export_project(PROJECT)
    source = Path(report["path"])
    snapshot = json.loads(source.read_text())
    snapshot["data"]["runs"] = []
    source.write_text(json.dumps(snapshot))
    destination = history.root / "empty-rebuilt.db"
    with pytest.raises(ValueError, match="empty ledger export"):
        exporter.rebuild_store(PROJECT, source, destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    "damage", ["project", "duplicate", "revisions", "members", "version"]
)
def test_malformed_exports_are_refused_before_creating_a_store(corpus, damage):
    history, _ = corpus
    exporter = _exporter()
    report = exporter.export_project(PROJECT)
    source = Path(report["path"])
    snapshot = json.loads(source.read_text())
    if damage == "project":
        snapshot["project"] = "foreign"
    elif damage == "duplicate":
        snapshot["data"]["runs"].append(snapshot["data"]["runs"][0])
    elif damage == "revisions":
        snapshot["run_revisions"]["missing"] = "unresolvable"
    elif damage == "members":
        snapshot["data"]["members"] = [{}]
    else:
        snapshot["data"]["_version"] = -1
    source.write_text(json.dumps(snapshot))
    destination = history.root / "invalid-rebuilt.db"
    with pytest.raises(ValueError, match="ledger export"):
        exporter.rebuild_store(PROJECT, source, destination)
    assert not destination.exists()


def test_reconstruction_never_overwrites_an_existing_store(corpus):
    report = _exporter().export_project(PROJECT)
    target = run_store.store_path()
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        _exporter().rebuild_store(PROJECT, report["path"], target)
    assert target.read_bytes() == before


def test_a_failed_publication_keeps_the_previous_export(corpus, monkeypatch):
    exporter = _exporter()
    target = Path(exporter.export_project(PROJECT)["path"])
    before = target.read_bytes()

    def refuse(*_args, **_kwargs):
        raise OSError("publication interrupted")

    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(OSError, match="publication interrupted"):
        exporter.export_project(PROJECT)
    assert target.read_bytes() == before
    assert not list(target.parent.glob(".crew.json.*.tmp"))


def test_the_retention_cadence_exports_and_dry_run_publishes_nothing(
    corpus, monkeypatch
):
    from reckon import flight
    from reckon.crew import gc

    history, expected = corpus
    monkeypatch.setattr(
        flight, "mounted_project_docs", lambda: {PROJECT: history.repo / "docs"}
    )
    monkeypatch.setattr(gc, "settle_run_scratch", lambda **kwargs: {"removed": []})
    monkeypatch.setattr(gc, "_orphan_ledger_indexes", lambda **kwargs: [])
    monkeypatch.setattr(gc, "_expired_picker_views", lambda **kwargs: [])
    now = datetime(2026, 9, 10, tzinfo=UTC)
    target = _exporter().export_path(PROJECT)
    dry = gc.retention_sweep(project=PROJECT, apply=False, now=now)
    assert dry["ledger_exports"] == []
    assert not target.exists()
    applied = gc.retention_sweep(project=PROJECT, apply=True, now=now)
    assert applied["ledger_exports"] == [
        {
            "status": "written",
            "project": PROJECT,
            "path": str(target),
            "rows": len(expected["runs"]),
        }
    ]
    first = target.read_bytes()
    assert (
        gc.retention_sweep(project=PROJECT, apply=True, now=now + timedelta(hours=1))[
            "ledger_exports"
        ]
        == applied["ledger_exports"]
    )
    assert target.read_bytes() == first


def test_the_sweep_reports_export_refusals(corpus, monkeypatch):
    history, _ = corpus
    monkeypatch.setenv("RECKON_HOME", str(history.repo / "config"))
    report = _exporter().export_projects({PROJECT: history.repo}, apply=True)
    assert report[0]["status"] == "failed"
    assert "outside git" in report[0]["error"]
