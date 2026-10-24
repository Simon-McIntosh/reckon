"""A queued run is never told to re-dispatch.

A request refused once at admission keeps its place in the queue, and the
admission pass re-tests it on every pass. The refusal is stored in the
pointer's ``failure`` field, and the classification reads it back to decide
whether the session owes a duty. For a request already queued, a re-dispatch is
never that duty: it discards the request's place, and the lane would have
launched the request by itself.

An ``UnreconciledRuns`` refusal names finished runs beside the action that
settles each one. Naming a run that has since been reconciled — its live
pointer is gone — is not an outstanding duty either: the refusal has already
been answered, so the request keeps the waiting reading rather than publishing
a remedy for work that no longer exists. This is the case that kept a
coordinator complete-ing a run forty-six seconds after its promotion, for two
hours.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew.node import UnreconciledRuns

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "queued-refusal-fixture"
SESSION = "coordinator-fixture"
QUEUED_RUN = "r-queued-request"
OBSERVED_AT = datetime(2026, 10, 23, 15, 45, tzinfo=UTC)

GRACE = "15m"


@pytest.fixture()
def mounted_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home and one mounted project, so nothing real is read."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    docs = repo / "docs"
    (docs / "state" / PROJECT).mkdir(parents=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed queued-refusal fixture"),
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(docs)}), encoding="utf-8"
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return home


def _unreconciled_text(named: list[tuple[str, str]]) -> str:
    """The stamped failure the admission pass stores for this refusal.

    Built from the exception itself so the text carries what the real refusal
    carries, including the ``--allow-unreconciled-runs`` dispatch it quotes as
    its other route. That quoted command is what a text search would publish,
    and it is the reason the class stamp decides this reading instead.
    """
    refusal = UnreconciledRuns(
        [{"run_id": run_id, "next_action": action} for run_id, action in named],
        GRACE,
    )
    return f"UnreconciledRuns: {refusal}"


def _write_live_pointer(run_id: str, *, phase: str = "working") -> dict[str, object]:
    record: dict[str, object] = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": phase,
        "process_alive": True,
        "launcher_host": "some-host.invalid",
    }
    runs._write_json(runs.pointer_path(run_id), record)
    return record


def _write_queued_pointer(failure: str) -> dict[str, object]:
    record = _write_live_pointer(QUEUED_RUN, phase="queued")
    record["failure"] = failure
    runs._write_json(runs.pointer_path(QUEUED_RUN), record)
    return record


def _classify(record: dict[str, object]) -> dict[str, object]:
    return recovery.classify_pointer(record, now_seconds=OBSERVED_AT.timestamp())


def _assert_no_redispatch(row: dict[str, object]) -> None:
    """No published command is a dispatch, and no detail advises one.

    The detail quotes the refusal, and the refusal's own text names
    ``reckon crew dispatch --allow-unreconciled-runs`` as a route a queued run
    cannot take, so the assertion is on the command the row publishes and on
    the advice it gives, not on the refusal's words.
    """
    command = str(row.get("next_command") or "")
    assert "reckon crew dispatch" not in command
    refusal = row.get("session_refusal")
    if isinstance(refusal, dict):
        assert "reckon crew dispatch" not in str(refusal.get("next_command") or "")
        detail = str(refusal.get("detail") or "")
        assert "dispatch again" not in detail
        assert "discard" not in detail


def test_a_live_named_run_keeps_the_queued_request_on_that_runs_command(
    mounted_home: Path,
) -> None:
    action = "reckon crew complete --run run-still-open"
    _write_live_pointer("run-still-open")
    record = _write_queued_pointer(_unreconciled_text([("run-still-open", action)]))

    row = _classify(record)

    assert row["classification"] == "queued"
    refusal = row["session_refusal"]
    assert isinstance(refusal, dict)
    assert refusal["next_command"] == action
    _assert_no_redispatch(row)

    duties = obligations_module.obligations(PROJECT, SESSION)["obligations"]
    assert [item["kind"] for item in duties] == ["queued-awaiting-session"]
    assert duties[0]["next_command"] == action
    _assert_no_redispatch(duties[0])


def test_a_named_run_whose_pointer_is_gone_reads_as_waiting(
    mounted_home: Path,
) -> None:
    action = "reckon crew complete --run run-already-promoted"
    record = _write_queued_pointer(
        _unreconciled_text([("run-already-promoted", action)])
    )

    row = _classify(record)

    assert row["classification"] == "queued"
    assert "session_refusal" not in row
    _assert_no_redispatch(row)

    duties = obligations_module.obligations(PROJECT, SESSION)["obligations"]
    assert [item["kind"] for item in duties] == ["run-queued"]
    _assert_no_redispatch(duties[0])


def test_two_named_runs_are_read_for_the_one_still_live(mounted_home: Path) -> None:
    first_action = "reckon crew complete --run run-promoted-first"
    second_action = "reckon crew complete --run run-still-open"
    _write_live_pointer("run-still-open")
    record = _write_queued_pointer(
        _unreconciled_text(
            [
                ("run-promoted-first", first_action),
                ("run-still-open", second_action),
            ]
        )
    )

    row = _classify(record)

    refusal = row["session_refusal"]
    assert isinstance(refusal, dict)
    assert refusal["next_command"] == second_action
    _assert_no_redispatch(row)


def test_a_plan_review_refusal_keeps_its_review_command(mounted_home: Path) -> None:
    review_command = "reckon crew review --run run-still-open --role plan"
    # The gate quotes its invocation in backticks, which is where the reader
    # takes the command from.
    record = _write_queued_pointer(
        f"PlanReviewMissingError: the plan's review is still owed; run `{review_command}`"
    )

    row = _classify(record)

    refusal = row["session_refusal"]
    assert isinstance(refusal, dict)
    assert refusal["next_command"] == review_command
    _assert_no_redispatch(row)
