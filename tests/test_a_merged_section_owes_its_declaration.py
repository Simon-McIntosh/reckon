"""A merged implementation leaves a duty until its section is accounted for."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import _store, ledger
from reckon.crew import queue_admission, runs
from tests import test_a_queued_request_waits_on_its_dependencies as waits
from tests.helpers.git import git

obligations_module = importlib.import_module("reckon.crew.obligations")
PROJECT = waits.PROJECT
PLAN = waits.PLAN
SESSION = waits.SESSION
RUN = "run-upstream"
KIND = "section-declaration-owed"


@pytest.fixture()
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(waits, "CHAIN", {"s2": [f"{PLAN}#s1"], "s3": [f"{PLAN}#s1"]})
    repository = waits.chain_repo.__wrapped__(tmp_path, monkeypatch)
    monkeypatch.setattr(
        obligations_module.flight,
        "resolve",
        lambda _project: SimpleNamespace(config=waits.CONFIG),
    )
    # Other duty kinds are independent of the declaration being measured.
    monkeypatch.setattr(obligations_module, "_classified_rows", lambda _project: [])
    monkeypatch.setattr(obligations_module, "_held_worktrees", lambda *a, **kw: [])
    monkeypatch.setattr(obligations_module, "_sub_floor_items", lambda *a, **kw: [])
    monkeypatch.setattr(obligations_module, "_index_lag_notice", lambda *a, **kw: None)
    queue_admission.queue_requests(
        [waits._node("n2", "s2"), waits._node("n3", "s3")],
        project=PROJECT,
        session="waiting-session",
        repo_root=repository,
        config=waits.CONFIG,
        run_ids={"n2": "r-n2", "n3": "r-n3"},
    )
    return repository


def _record_run(repository: Path, *, merged: bool = True, run_id: str = RUN) -> str:
    tree = git(repository, "rev-parse", "HEAD^{tree}").strip()
    parent = git(repository, "rev-parse", "HEAD").strip()
    revision = (
        parent
        if merged
        else git(
            repository, "commit-tree", tree, "-p", parent, "-m", "test: delivery"
        ).strip()
    )
    record = ledger.build_record(
        run_id=run_id,
        plan=PLAN,
        section="s1",
        node="upstream",
        role="implement",
        gate="passed",
        commits=[revision],
        completed_at="2026-11-10T10:00:00Z",
    )
    record.update(session=SESSION, promoted_revision=revision)
    ledger.append_run(PROJECT, record, root=repository)
    return revision


def _duties(session: str = SESSION, **kwargs) -> list[dict]:
    view = obligations_module.obligations(PROJECT, session, **kwargs)
    return [row for row in view["obligations"] if row["kind"] == KIND]


def _declare(repository: Path, value: str) -> None:
    state, version = _store.read_plan(PROJECT, PLAN, repository)
    state["section_declarations"]["s1"] = value
    _store.write_plan(PROJECT, PLAN, state, version, repository)


def test_a_merged_section_names_its_two_waiters_and_dispatching_session(checkout):
    revision = _record_run(checkout)
    assert git(checkout, "rev-parse", "main").strip() == revision
    assert [runs.read_pointer(name)["phase"] for name in ("r-n2", "r-n3")] == [
        "awaiting-upstream",
        "awaiting-upstream",
    ]
    duties = _duties()
    assert len(duties) == 1, duties
    duty = duties[0]
    assert duty["run_id"] == RUN
    assert duty["plan"] == PLAN
    assert duty["section"] == "§1"
    assert duty["head"] == revision
    assert duty["waiting_requests"] == ["r-n2", "r-n3"]
    assert duty["held_count"] == 2
    assert duty["next_command"] == f"/reckon-edit {PLAN} §1"
    assert "section_declarations.s1" in duty["next_action_form"]
    assert not _duties("other-session")
    assert not _duties("waiting-session")


def test_recording_done_clears_the_duty_and_admission_releases_both_waiters(checkout):
    _record_run(checkout)
    assert len(_duties()) == 1
    _declare(checkout, "done")
    assert not _duties()
    queue_admission.admit_once(config_for=waits._config_for(), launcher=lambda **kw: 0)
    assert all(
        runs.read_pointer(name)["phase"] != "awaiting-upstream"
        for name in ("r-n2", "r-n3")
    )


def test_a_deferred_section_owes_no_declaration(checkout):
    _record_run(checkout)
    assert len(_duties()) == 1
    _declare(checkout, "deferred")
    assert not _duties()


def test_a_partial_delivery_is_scoped_to_the_recorded_run(checkout):
    _record_run(checkout)
    assert len(_duties()) == 1
    state, version = _store.read_plan(PROJECT, PLAN, checkout)
    state.setdefault("comments", {}).setdefault("s1", []).append(
        {
            "id": f"c-partial-{RUN}",
            "who": SESSION,
            "when": "2026-11-15",
            "body": "Only the first of the section's nodes landed; the rest remains.",
        }
    )
    _store.write_plan(PROJECT, PLAN, state, version, checkout)
    assert not _duties()
    assert all(
        runs.read_pointer(name)["phase"] == "awaiting-upstream"
        for name in ("r-n2", "r-n3")
    )
    _record_run(checkout, run_id="run-remainder")
    assert [row["run_id"] for row in _duties()] == ["run-remainder"]


@pytest.mark.parametrize("recorded", [False, True])
def test_a_section_without_a_merged_run_owes_no_declaration(checkout, recorded):
    if recorded:
        revision = _record_run(checkout, merged=False)
        assert git(checkout, "rev-parse", "main").strip() != revision
    assert not _duties()


def test_the_waiter_fields_can_be_requested_when_no_duty_is_owed(checkout):
    assert not _duties(fields=["kind", "waiting_requests", "held_count"])
