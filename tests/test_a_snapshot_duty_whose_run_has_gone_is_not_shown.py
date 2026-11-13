"""A held review duty goes when the run's work it names has moved on.

A station computes a session's duties once per tick and keeps them until the
next one, so the answer a coordinator or the hook reads can be minutes old. A
review duty names a run whose current head is owed a review, and two facts end
that duty between the tick and the read: the run releases its live pointer when
it lands or ends, leaving no tree to review; and the run's pointer records a
dispatch when a review of that head starts, leaving nothing missing.

Four facts are measured, each through the surface that consumes the answer: the
duty of a departed run is gone from both the crew view and the hook's rendered
list; a duty whose review has since been dispatched is gone the same way; a
duty naming no run is kept; and a duty whose run still holds the condition
stands. The second is measured from the session's own published snapshot as
well as the station's answer, because both are read through the same
settlement and a station may be silent while its session's answer is fresh.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import obligation_snapshot, runs, station_store
from reckon.mcp_views import serve_crew_view
from tests.helpers.git import git as _git

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"
PROJECT = "snapshot-duty-fixture"
SESSION = "snapshot-duty-session"
DEPARTED_RUN_ID = "run-departed-review-duty"
DISPATCHED_RUN_ID = "run-dispatched-review-duty"
STANDING_RUN_ID = "run-standing-review-duty"
REFUSED_RUN_ID = "run-refused-review-duty"
RUNLESS_NODE_ID = "runless-index-node"
REVIEW_RUN_ID = "run-the-review-that-was-dispatched"
_REVIEW_HEAD = "1234567890abcdef1234567890abcdef12345678"
_OTHER_HEAD = "fedcba0987654321fedcba0987654321fedcba09"
# A pid no process can hold, so the drive's session resolves through the
# registration's own name rather than the runner's ambient harness identity.
_ABSENT_HARNESS_PID = 2_147_483_647


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    return home


@pytest.fixture()
def runtime_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The node-local root the hook and the station both resolve."""
    root = tmp_path / "runtime"
    root.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(root))
    return root


@pytest.fixture()
def repository(tmp_path: Path, config_home: Path) -> Path:
    root = tmp_path / "repo"
    seed = root / "docs" / "state" / PROJECT
    seed.mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the snapshot-duty fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


@pytest.fixture()
def follower(config_home: Path):
    """Register the session exactly as a live follower arm would."""
    with (
        runs.follower_registration(PROJECT, SESSION, delivery="stream"),
        runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat),
    ):
        assert acquired is True
        yield


def _review_duty(
    *, run_id: str, node: str, head: str = _REVIEW_HEAD, kind: str = "review-missing"
) -> dict:
    return {
        "kind": kind,
        "run_id": run_id,
        "node": node,
        "plan": "fixture-plan",
        "head": head,
        "age_seconds": 120,
        "reason": "no stored review covers the head this run carries",
        "next_command": f"reckon crew review --run {run_id}",
    }


def _runless_duty() -> dict:
    return {
        "kind": "index-lags-head",
        "run_id": "",
        "node": RUNLESS_NODE_ID,
        "plan": "fixture-plan",
        "age_seconds": 300,
        "reason": "the plan index lags the repository head",
        "next_command": "reckon sprint sync",
    }


def _payload(rows: list[dict]) -> dict:
    return {
        "project": PROJECT,
        "session": SESSION,
        "obligations": rows,
        "acknowledged": [],
        "summary": {
            "count": len(rows),
            "oldest_age_seconds": max(int(row["age_seconds"]) for row in rows),
        },
    }


def _duties() -> list[dict]:
    """One duty per fact under test, plus the runless control."""
    return [
        _review_duty(run_id=DEPARTED_RUN_ID, node="departed-review-node"),
        _review_duty(run_id=DISPATCHED_RUN_ID, node="dispatched-review-node"),
        _review_duty(run_id=STANDING_RUN_ID, node="standing-review-node"),
        _runless_duty(),
    ]


def _publish_station_answer(runtime_root: Path, rows: list[dict]) -> None:
    """Store one held answer, as a station tick would."""
    moment = datetime.now(tz=UTC)
    store = station_store.StationStore(runtime_root)
    store.connect()
    try:
        store.write_obligation(
            SESSION,
            {
                "identity": "snapshot-duty",
                "document": obligation_snapshot.document_for(
                    _payload(rows), computed_at=moment, stream_offset=0, producer={}
                ),
            },
            changed_at=moment,
        )
        store.heartbeat(now=moment)
    finally:
        store.close()


def _publish_session_snapshot(rows: list[dict]) -> Path:
    """Publish the session's own snapshot, as its producer sweep would."""
    pid = os.getpid()
    document = obligation_snapshot.document_for(
        _payload(rows),
        computed_at=datetime.now(tz=UTC),
        stream_offset=0,
        producer={
            "pid": pid,
            "pid_start_time": obligation_snapshot.process_start_time(pid),
            "code_stamp": obligation_snapshot.source_code_stamp(),
        },
    )
    return obligation_snapshot.write_snapshot(PROJECT, SESSION, document)


def _write_pointer(run_id: str, **fields: object) -> Path:
    path = obligation_snapshot.live_pointer_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"run_id": run_id, "project": PROJECT, **fields}),
        encoding="utf-8",
    )
    return path


def _drive(repository: Path, *, runtime: Path) -> str:
    """Run the prompt hook as the harness does, returning its injected list."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["CLAUDE_PID"] = str(_ABSENT_HARNESS_PID)
    environment["XDG_RUNTIME_DIR"] = str(runtime)
    completed = subprocess.run(
        [sys.executable, str(HOOK), "--hook", "prompt"],
        input=json.dumps(
            {
                "session_id": SESSION,
                "cwd": str(repository),
                "hook_event_name": "UserPromptSubmit",
            }
        ),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == "", completed.stderr
    answer = json.loads(completed.stdout) if completed.stdout else {}
    output = answer.get("hookSpecificOutput")
    return str(output["additionalContext"]) if isinstance(output, dict) else ""


def _view_rows() -> list[dict]:
    answer = serve_crew_view("obligations", project=PROJECT, session=SESSION)
    assert answer["ok"] is True, answer
    return list(answer["obligations"])


def _settle_rows(rows: list[dict]) -> list[dict]:
    settled = obligation_snapshot.without_departed_run_duties(_payload(rows))
    return list(settled["obligations"])


def _conditions() -> None:
    """Arrange one holding condition and one moved condition per run.

    A departed run has no pointer at all. A dispatched run's pointer records a
    review started for the head its duty names. A standing run's pointer is
    there with no dispatch recorded -- the duty's own condition still holds. A
    refused run's pointer records an attempt the lane refused, which leaves the
    review owed and must keep the duty.
    """
    _write_pointer(
        DISPATCHED_RUN_ID,
        phase="working",
        review_dispatch={
            "status": "dispatched",
            "run_id": REVIEW_RUN_ID,
            "head": _REVIEW_HEAD,
            "at": datetime.now(tz=UTC).isoformat(),
        },
    )
    _write_pointer(STANDING_RUN_ID, phase="working")
    _write_pointer(
        REFUSED_RUN_ID,
        phase="working",
        review_dispatch={
            "status": "refused",
            "run_id": None,
            "head": _REVIEW_HEAD,
            "reason": "the lane is paused",
        },
    )


def test_a_review_duty_of_a_departed_run_is_gone_from_the_held_answer(
    runtime_root: Path, config_home: Path
) -> None:
    """The run released its pointer, so nothing is left for the duty to act on."""
    _conditions()
    rows = [*_duties(), _review_duty(run_id=REFUSED_RUN_ID, node="refused-node")]
    _publish_station_answer(runtime_root, rows)

    settled = _settle_rows(rows)
    settled_runs = [row["run_id"] for row in settled]
    assert DEPARTED_RUN_ID not in settled_runs, settled_runs
    assert DISPATCHED_RUN_ID not in settled_runs, settled_runs
    assert STANDING_RUN_ID in settled_runs, settled_runs
    assert REFUSED_RUN_ID in settled_runs, settled_runs
    assert "" in settled_runs, "a duty naming no run is not a departure"


def test_the_view_and_the_hook_drop_both_duties_without_the_station(
    repository: Path, runtime_root: Path, follower: None
) -> None:
    """Both surfaces serve the station's answer, settled at the read."""
    _conditions()
    _publish_station_answer(runtime_root, _duties())

    view_runs = [row["run_id"] for row in _view_rows()]
    assert view_runs == [STANDING_RUN_ID, ""], view_runs

    checklist = _drive(repository, runtime=runtime_root)
    assert DEPARTED_RUN_ID not in checklist, checklist
    assert DISPATCHED_RUN_ID not in checklist, checklist
    assert STANDING_RUN_ID in checklist, checklist
    assert "index-lags-head" in checklist, checklist
    assert "2 outstanding" in checklist, checklist


def test_the_session_snapshot_is_settled_the_same_way(
    repository: Path, runtime_root: Path, follower: None
) -> None:
    """A silent station leaves the session's own snapshot to answer, and it settles too."""
    _conditions()
    # The producer's own first sweep is a daemon thread, and it publishes an
    # empty list for this fixture's quiet fleet: join it before writing, or it
    # lands over the snapshot under test a few milliseconds later.
    runs.join_watch_sweeps()
    document = _publish_session_snapshot(_duties())
    assert len(json.loads(document.read_text())["obligations"]) == len(_duties())

    checklist = _drive(repository, runtime=runtime_root)
    assert DEPARTED_RUN_ID not in checklist, checklist
    assert DISPATCHED_RUN_ID not in checklist, checklist
    assert STANDING_RUN_ID in checklist, checklist
    assert "index-lags-head" in checklist, checklist


def test_a_dispatch_for_another_head_does_not_settle_the_duty(
    runtime_root: Path, config_home: Path
) -> None:
    """A review of a revision the run has moved past leaves the new head owed."""
    _write_pointer(
        DISPATCHED_RUN_ID,
        phase="working",
        review_dispatch={
            "status": "dispatched",
            "run_id": REVIEW_RUN_ID,
            "head": _OTHER_HEAD,
        },
    )
    rows = [_review_duty(run_id=DISPATCHED_RUN_ID, node="dispatched-review-node")]

    assert [row["run_id"] for row in _settle_rows(rows)] == [DISPATCHED_RUN_ID]


def test_an_unreadable_pointer_keeps_the_duty(
    runtime_root: Path, config_home: Path
) -> None:
    """A file that cannot be read is not evidence the condition moved."""
    path = obligation_snapshot.live_pointer_path(DEPARTED_RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    rows = [_review_duty(run_id=DEPARTED_RUN_ID, node="departed-review-node")]

    assert [row["run_id"] for row in _settle_rows(rows)] == [DEPARTED_RUN_ID]


def test_the_settlement_recomputes_the_summary(config_home: Path) -> None:
    """The header counts the rows actually shown, not the ones stored."""
    rows = [
        _review_duty(run_id=DEPARTED_RUN_ID, node="departed-review-node"),
        _runless_duty(),
    ]
    settled = obligation_snapshot.without_departed_run_duties(_payload(rows))

    assert [row["run_id"] for row in settled["obligations"]] == [""]
    assert settled["summary"]["count"] == 1, settled["summary"]


def test_a_queued_review_duty_settles_the_same_way(config_home: Path) -> None:
    """A review waiting in the lane is owed only while the run it names is there.

    The queued reading is the missing reading inside a wait -- a refusal the
    reflex retries, or a review run the lane has not launched -- so the same two
    facts end it: a departed run leaves nothing to review, and a dispatch
    recorded for the head means the wait is over. A refusal still standing on
    the pointer, or a run still holding its pointer with no dispatch, keeps the
    row.
    """
    _write_pointer(
        DISPATCHED_RUN_ID,
        phase="working",
        review_dispatch={
            "status": "dispatched",
            "run_id": REVIEW_RUN_ID,
            "head": _REVIEW_HEAD,
        },
    )
    _write_pointer(
        STANDING_RUN_ID,
        phase="working",
        review_dispatch={
            "status": "lane-paused",
            "head": _REVIEW_HEAD,
            "at": datetime.now(tz=UTC).isoformat(),
        },
    )
    rows = [
        _review_duty(
            run_id=DEPARTED_RUN_ID, node="departed-review-node", kind="review-queued"
        ),
        _review_duty(
            run_id=DISPATCHED_RUN_ID,
            node="dispatched-review-node",
            kind="review-queued",
        ),
        _review_duty(
            run_id=STANDING_RUN_ID, node="standing-review-node", kind="review-queued"
        ),
    ]

    assert [row["run_id"] for row in _settle_rows(rows)] == [STANDING_RUN_ID]


def test_a_dispatch_without_a_recorded_head_keeps_the_duty(config_home: Path) -> None:
    """An entry that names no head cannot be shown to cover this duty's head."""
    _write_pointer(
        DISPATCHED_RUN_ID,
        phase="working",
        review_dispatch={"status": "dispatched", "run_id": REVIEW_RUN_ID},
    )
    rows = [_review_duty(run_id=DISPATCHED_RUN_ID, node="dispatched-review-node")]

    assert [row["run_id"] for row in _settle_rows(rows)] == [DISPATCHED_RUN_ID]


def test_the_revision_spelling_mirror_agrees_with_its_owner() -> None:
    """The mirror cannot drift from the rule it copies.

    This file is loaded by path, so it may reach no derivation module and
    carries its own spelling of the revision comparison. The comparison is
    checked against the review-subject module's own over the same inputs, which
    is what keeps a copy from quietly answering differently from the rule the
    rest of the crew reads.
    """
    from reckon.crew.recovery_review_subject import same_revision as owner

    full = "1234567890abcdef1234567890abcdef12345678"
    cases = [
        ("", ""),
        ("", full),
        (full, ""),
        (full, full),
        (full, full[:10]),
        (full[:10], full),
        (full.upper(), full),
        (f"  {full[:8]}  ", full),
        (full[:8], full[-8:]),
        ("1234567890abcdef", "1234567890abcdee"),
        ("not-a-sha", "not-a-sha"),
        ("abc", "abcdef"),
    ]

    for left, right in cases:
        assert obligation_snapshot._same_revision(left, right) == owner(left, right), (
            f"the mirrored comparison diverges from the review-subject rule on "
            f"{left!r} against {right!r}"
        )
