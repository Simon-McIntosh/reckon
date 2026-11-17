"""Ledger retirement preserves stored history without tracking its projections."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from reckon import ledger, run_store

ROOT = Path(__file__).resolve().parents[1]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_no_ledger_document_is_tracked() -> None:
    assert _git(ROOT, "ls-files", "--", ".gitignore") == ".gitignore"
    tracked = _git(
        ROOT, "ls-files", "--", "docs/state/*/crew.json", "docs/state/*/runs"
    ).splitlines()
    assert not tracked, f"ledger documents remain tracked: {tracked[:5]}"


def test_every_project_ledger_is_ignored_but_other_state_is_kept(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-q")
    (root / ".gitignore").write_text((ROOT / ".gitignore").read_text())
    paths = [
        f"docs/state/{project}/{suffix}"
        for project in ("sample", "another")
        for suffix in ("crew.json", "runs/r-sample.json")
    ]
    assert _git(root, "check-ignore", "--", *paths).splitlines() == paths
    for suffix in (
        "index.json",
        "project.json",
        "reviews/review.json",
        "timeline.html",
    ):
        result = subprocess.run(
            ["git", "-C", str(root), "check-ignore", f"docs/state/sample/{suffix}"],
            capture_output=True,
            check=False,
        )
        assert result.returncode == 1, suffix


@pytest.fixture()
def importer():
    path = ROOT / "scripts" / "import_runs_into_store.py"
    spec = importlib.util.spec_from_file_location("ledger_import_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repository"
    state = root / "docs" / "state" / "sample"
    state.mkdir(parents=True)
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(config / "mounts.json"))
    (config / "mounts.json").write_text(json.dumps({"sample": str(root / "docs")}))
    data = {
        "_version": 7,
        "members": [{"id": "member", "status": "active"}],
        "holds": [{"hold_id": "hold", "reason": "capacity"}],
        "runs": [{"run_id": "r-aggregate", "gate": "passed", "outcome": "retained"}],
    }
    (state / "crew.json").write_text(json.dumps({"project": "sample", "data": data}))
    documents = state / "runs"
    documents.mkdir()
    (documents / "r-document.json").write_text(
        ledger.serialize_run({"run_id": "r-document", "gate": "failed", "tokens": 123})
    )
    (state / "index.json").write_text('{"project":"sample"}\n')
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(
        root,
        "add",
        "docs/state/sample/crew.json",
        "docs/state/sample/runs/r-document.json",
        "docs/state/sample/index.json",
    )
    _git(
        root,
        "commit",
        "-qm",
        "test: seed history\n\nRecord aggregate and per-run history.",
    )
    return root


def _canonical(project: str, root: Path) -> str:
    data, _ = ledger.load(project, root)
    return json.dumps(
        {key: data[key] for key in ("runs", "members", "holds", "auxiliary_version")},
        sort_keys=True,
    )


def test_import_preserves_runs_roster_and_holds_after_committed_removal(
    source: Path, importer, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--project", "sample", "--root", str(source)]
    assert importer.main(args) == 0
    receipt = capsys.readouterr().out
    assert "rows imported: 2" in receipt
    assert "members preserved: 1" in receipt
    assert "holds preserved: 1" in receipt
    assert "run verification: 0 missing, 0 extra, 0 differing" in receipt
    before = _canonical("sample", source)
    assert len(json.loads(before)["runs"]) == 2
    assert importer.main(args) == 0
    assert "rows already present and unchanged: 2" in capsys.readouterr().out
    assert _canonical("sample", source) == before
    _git(
        source,
        "rm",
        "-r",
        "--",
        "docs/state/sample/crew.json",
        "docs/state/sample/runs",
    )
    _git(
        source,
        "commit",
        "-qm",
        "chore: retire ledger files\n\nPreserve history in the store.",
    )
    assert _canonical("sample", source) == before
    assert (
        _git(source, "ls-files", "docs/state/sample") == "docs/state/sample/index.json"
    )
    appended = ledger.append_run(
        "sample", {"run_id": "r-next", "gate": "passed"}, root=source
    )
    assert appended["commit_paths"] == []
    assert appended["path"] == str(run_store.store_path())
    assert not (source / "docs/state/sample/runs").exists()
    assert len(ledger.load("sample", source)[0]["runs"]) == 3


def test_import_never_resurrects_roster_or_holds_from_an_older_aggregate(
    source: Path, importer
) -> None:
    args = ["--project", "sample", "--root", str(source)]
    assert importer.main(args) == 0
    with run_store.RunStore() as store:
        version = store.write_auxiliary("sample", {"members": [], "holds": []}, 7)
    assert importer.main(args) == 0
    assert run_store.read_auxiliary("sample") == ({"members": [], "holds": []}, version)


def test_import_refuses_an_absent_source(source: Path, importer, capsys) -> None:
    _git(
        source,
        "rm",
        "-r",
        "--",
        "docs/state/sample/crew.json",
        "docs/state/sample/runs",
    )
    assert importer.main(["--project", "sample", "--root", str(source)]) == 1
    assert "no ledger documents" in capsys.readouterr().err
    assert not run_store.store_path().exists()


def test_import_refuses_a_failed_parity_check(
    source: Path, importer, monkeypatch, capsys
) -> None:
    comparison = {
        "rows_in_file_absent_from_store": 1,
        "rows_in_store_absent_from_file": 0,
        "rows_present_in_both_disagreeing": 0,
    }
    monkeypatch.setattr(run_store, "compare", lambda *args, **kwargs: comparison)
    assert importer.main(["--project", "sample", "--root", str(source)]) == 1
    assert "ledger verification failed" in capsys.readouterr().err
