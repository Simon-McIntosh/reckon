"""A duty for a run that is gone is not shown, and a working run is drawn working.

A station computes a session's duties once per tick and keeps them until the
next one, so the answer a pane reads can be minutes old. Between the tick and
the read a run lands or is discarded and its live pointer goes away, and the
duty the answer still names has nothing left to act on. A failure duty for the
same run is the same case: once the run's outcome is written to the ledger and
its pointer is gone, the remedy the row publishes cannot apply.

The other half is the reading that never changes: a run whose live pointer reads
a working phase is drawn working from that pointer whatever the payload's age,
and a session holding such a run is not drawn halted.

Three facts are measured here, each through the surface that consumes the
answer: the pane's obligations reader drops a gone run's duty and keeps a
standing one; the live view draws the working pointer working; and the stop
hook's read neither blocks on a promotion-failed nor on a sweep-failed row for a
run with no live pointer whose outcome the ledger already carries, while the
same row for a run still holding its pointer does block.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import obligation_snapshot, runs, station_store, ticker
from reckon.mcp_views import serve_crew_view
from tests.helpers.git import git as _git

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"
PROJECT = "gone-run-duty-fixture"
SESSION = "gone-run-duty-session"
GONE_REVIEW_RUN_ID = "run-gone-review-duty"
GONE_QUEUED_RUN_ID = "run-gone-queued-duty"
STANDING_RUN_ID = "run-standing-duty"
WORKING_RUN_ID = "run-live-working"
GONE_FAILED_RUN_ID = "run-gone-failed-duty"
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
    """The node-local root the station, the hook and the classifier resolve."""
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
        ("commit", "-q", "-m", "test: seed the gone-run duty fixture"),
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


def _duty(kind: str, run_id: str) -> dict:
    return {
        "kind": kind,
        "run_id": run_id,
        "node": run_id,
        "plan": "fixture-plan",
        "age_seconds": 600,
        "reason": "the condition this row names is still owed",
        "next_command": f"reckon crew discard --run {run_id}",
    }


def _failure_duty(kind: str, run_id: str) -> dict:
    return {
        "kind": kind,
        "run_id": run_id,
        "node": run_id,
        "plan": "fixture-plan",
        "age_seconds": 3600,
        "reason": "the automatic step refused",
        "next_command": f"reckon crew complete --run {run_id}",
    }


def _payload(rows: list[dict]) -> dict:
    return {
        "project": PROJECT,
        "session": SESSION,
        "obligations": rows,
        "acknowledged": [],
        "summary": {
            "count": len(rows),
            "oldest_age_seconds": max(
                (int(row["age_seconds"]) for row in rows), default=0
            ),
        },
    }


def _write_pointer(run_id: str, **fields: object) -> Path:
    path = obligation_snapshot.live_pointer_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"run_id": run_id, "project": PROJECT, **fields}),
        encoding="utf-8",
    )
    return path


def _write_working_pointer(run_id: str) -> Path:
    """A pointer the live view reads as a run with a launched worker."""
    return _write_pointer(
        run_id,
        session=SESSION,
        phase="working",
        attempt=1,
        node={"id": run_id, "plan": "fixture-plan", "section": "s1"},
        created_at=datetime.now(tz=UTC).isoformat(),
        process_alive=None,
        pid=_ABSENT_HARNESS_PID,
        launcher_host=None,
        worktree=None,
        base_sha="",
    )


def _publish_station_answer(runtime_root: Path, rows: list[dict]) -> None:
    """Store one held answer, as a station tick would."""
    moment = datetime.now(tz=UTC)
    store = station_store.StationStore(runtime_root)
    store.connect()
    try:
        store.write_obligation(
            SESSION,
            {
                "identity": "gone-run-duty",
                "document": obligation_snapshot.document_for(
                    _payload(rows), computed_at=moment, stream_offset=0, producer={}
                ),
            },
            changed_at=moment,
        )
        store.heartbeat(now=moment)
    finally:
        store.close()


def _read_obligations() -> list[dict]:
    """The session's duties as the pane's own reader serves them."""
    answer = serve_crew_view("obligations", project=PROJECT, session=SESSION)
    assert answer["ok"] is True, answer
    return list(answer["obligations"])


def _read_live_rows() -> list[dict]:
    """The session's live rows as the pane's view serves them."""
    answer = serve_crew_view("live", project=PROJECT, session=SESSION)
    assert answer["ok"] is True, answer
    return [row for row in answer["runs"] if str(row.get("session") or "") == SESSION]


def _drive_stop(repository: Path, *, runtime: Path) -> dict:
    """Run the stop hook as the harness does, returning its JSON answer."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["CLAUDE_PID"] = str(_ABSENT_HARNESS_PID)
    environment["XDG_RUNTIME_DIR"] = str(runtime)
    completed = subprocess.run(
        [sys.executable, str(HOOK), "--hook", "stop"],
        input=json.dumps(
            {
                "session_id": SESSION,
                "cwd": str(repository),
                "hook_event_name": "Stop",
            }
        ),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == "", completed.stderr
    return json.loads(completed.stdout) if completed.stdout else {}


def _write_ledger_row(repository: Path, run_id: str) -> Path:
    """The run's own committed record, beside the project's ledger."""
    path = repository / "docs" / "state" / PROJECT / "runs" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": PROJECT,
                "node": {"id": run_id, "plan": "fixture-plan", "section": "s1"},
                "release": {"worktree_released": True},
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_request_file(config_home: Path, run_id: str, phase: str) -> Path:
    """A promotion request file, as the service leaves one after a failed step."""
    path = config_home / "crew" / "promotion-requests" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": PROJECT,
                "session": SESSION,
                "phase": phase,
                "options": {"gate": "passed"},
                "finished_at": datetime.now(tz=UTC).isoformat(),
                "error": "the automatic step refused",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_a_gone_runs_duty_is_not_shown_and_a_working_run_is(
    config_home: Path, runtime_root: Path
) -> None:
    """Only the standing run has a duty, and the working run is drawn working."""
    _write_pointer(STANDING_RUN_ID, session=SESSION, phase="working")
    _write_working_pointer(WORKING_RUN_ID)
    _publish_station_answer(
        runtime_root,
        [
            _duty("review-missing", GONE_REVIEW_RUN_ID),
            _duty("run-queued", GONE_QUEUED_RUN_ID),
            _duty("review-missing", STANDING_RUN_ID),
        ],
    )

    shown = _read_obligations()
    assert [row["run_id"] for row in shown] == [STANDING_RUN_ID], shown

    # The working run is drawn from its own pointer, whatever the payload says:
    # the payload does not mention it at all.
    live = _read_live_rows()
    by_run = {str(row.get("run_id") or ""): row for row in live}
    assert WORKING_RUN_ID in by_run, live
    assert str(by_run[WORKING_RUN_ID].get("phase") or "") == "working"
    words = {ticker._display_state(row.get("phase")) for row in live}
    assert words == {"working"}, [(row.get("run_id"), row.get("phase")) for row in live]


def test_a_duty_drops_when_its_runs_pointer_goes_between_reads(
    config_home: Path, runtime_root: Path
) -> None:
    """The same held answer is settled again at each read."""
    pointer = _write_pointer(STANDING_RUN_ID, session=SESSION, phase="working")
    _publish_station_answer(runtime_root, [_duty("review-missing", STANDING_RUN_ID)])

    assert [row["run_id"] for row in _read_obligations()] == [STANDING_RUN_ID]
    pointer.unlink()
    assert _read_obligations() == []


def test_the_stop_hook_does_not_block_on_a_failed_row_for_a_gone_run(
    repository: Path, config_home: Path, runtime_root: Path, follower: None
) -> None:
    """A failure row is owed only while its run can still be acted on."""
    runs.join_watch_sweeps()
    _write_request_file(config_home, GONE_FAILED_RUN_ID, "promotion-failed")
    _write_request_file(config_home, GONE_FAILED_RUN_ID, "sweep-failed")
    _write_ledger_row(repository, GONE_FAILED_RUN_ID)
    _publish_station_answer(
        runtime_root,
        [
            _failure_duty("promotion-failed", GONE_FAILED_RUN_ID),
            _failure_duty("sweep-failed", GONE_FAILED_RUN_ID),
        ],
        # The same rows for a run that still holds its pointer block the stop is
        # asserted in the case below.
    )

    answer = _drive_stop(repository, runtime=runtime_root)
    assert "decision" not in answer, answer
    assert "promotion-failed" not in json.dumps(answer)
    assert "sweep-failed" not in json.dumps(answer)


def test_the_stop_hook_still_blocks_here_the_failed_run_holds_its_pointer(
    repository: Path, runtime_root: Path, follower: None
) -> None:
    """The control: a failure row for a live run is still the coordinator's."""
    runs.join_watch_sweeps()
    _write_working_pointer(GONE_FAILED_RUN_ID)
    _publish_station_answer(
        runtime_root,
        [_failure_duty("promotion-failed", GONE_FAILED_RUN_ID)],
    )

    answer = _drive_stop(repository, runtime=runtime_root)
    assert answer.get("decision") == "block", answer
    assert "promotion-failed" in json.dumps(answer), answer
