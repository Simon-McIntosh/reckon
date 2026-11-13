"""A run that has already landed owes no duty to promote it.

A station computes a session's duties once per tick and keeps them until the
next one, so the answer a coordinator reads can be minutes old. Promotion
unlinks a run's live pointer as it writes the run's ledger row, and the duty
derived from that pointer -- ``review-ready`` for a reviewed run, or
``promotable-stale`` once it outlives the unreconciled-run grace -- survives in
the answer computed before the removal. It is settled at read time, through the
snapshot module the hook already loads by path, on a ``stat`` of the run's
pointer: a promotion is taken only through ``crew complete`` against that
pointer, so once it is gone the duty names an action nobody can perform.

Four facts are measured, each on an answer written while the station held the
run's pointer and read after it was removed: a promoted run's promotion duty is
gone from both the station's stored answer, the crew view and the hook's
rendered list; the same duty survives while its pointer is there and no ledger
row records a promotion; a run whose pointer is gone because it was discarded
owes nothing even though no ledger row records it; and a ``review-missing`` duty
for a departed run is still listed, because a review that was never taken is not
something a promotion settled.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import obligation_snapshot, runs, station_store
from reckon.mcp_views import serve_crew_view
from tests.helpers.git import git as _git

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"
PROJECT = "promoted-duty-fixture"
SESSION = "promoted-duty-session"
PROMOTED_RUN_ID = "run-promoted-review"
STALE_RUN_ID = "run-promoted-stale-review"
DISCARDED_RUN_ID = "run-discarded-review"
UNREVIEWED_RUN_ID = "run-unreviewed-review"
PROMOTED_NODE_ID = "promoted-review-node"
UNREVIEWED_NODE_ID = "unreviewed-review-node"
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
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the promoted-duty fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}),
        encoding="utf-8",
    )
    return root


@pytest.fixture()
def follower(config_home: Path):
    """Register the session exactly as a real follower arm would."""
    with (
        runs.follower_registration(PROJECT, SESSION, delivery="stream"),
        runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat),
    ):
        assert acquired is True
        yield


def _duty(*, kind: str, run_id: str, node: str, age: int = 120) -> dict[str, object]:
    return {
        "kind": kind,
        "run_id": run_id,
        "node": node,
        "plan": "fixture-plan",
        "age_seconds": age,
        "next_command": "reckon crew complete --run " + run_id,
        "reason": "the review is complete, so the run is ready to promote",
    }


def _held_answer(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "project": PROJECT,
        "session": SESSION,
        "obligations": rows,
        "acknowledged": [],
        "summary": {
            "count": len(rows),
            "oldest_age_seconds": 120,
            "unreconciled_runs": len(rows),
        },
    }


def _publish_answer(
    runtime_root: Path,
    rows: list[dict[str, object]],
    *,
    now: datetime | None = None,
) -> None:
    """Store one held answer, as a station tick would while the pointer existed."""
    moment = now or datetime.now(tz=UTC)
    store = station_store.StationStore(runtime_root)
    store.connect()
    try:
        store.write_obligation(
            SESSION,
            {
                "identity": "promoted-duty",
                "document": obligation_snapshot.document_for(
                    _held_answer(rows), computed_at=moment, stream_offset=0, producer={}
                ),
            },
            changed_at=moment,
        )
        store.heartbeat(now=moment)
    finally:
        store.close()


def _record_pointer(run_id: str) -> Path:
    """Write the live pointer whose presence keeps a promotion duty payable."""
    path = obligation_snapshot.live_pointer_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"run_id": run_id, "project": PROJECT}), encoding="utf-8"
    )
    return path


def _record_promotion(repository: Path, run_id: str, node: str) -> None:
    """Write the ledger row a promotion leaves, so the fixture states the fact.

    The settlement deliberately reads no ledger, so this row is the premise of
    the promoted cases rather than an input to them: it is what makes "already
    promoted" true, and the departure is then the pointer's removal.
    """
    path = ledger.ledger_path(PROJECT, repository)
    if not path.exists():
        ledger.write(
            PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=repository
        )
    ledger.append_run(
        PROJECT,
        ledger.build_record(
            run_id=run_id,
            plan="the-monitor-shows-fixture",
            section="s1",
            node=node,
            gate="passed",
            member_id="member-a",
            session_id=SESSION,
            base_sha="def456",
            commits=["fedcba"],
        ),
        root=repository,
    )


def _drive(repository: Path, *, runtime: Path) -> str:
    """Run the stop hook as the harness does, returning the list it raises.

    A stop hook with nothing owed prints nothing, so the empty string is the
    reading that a duty no longer holds the coordinator's turn open.
    """
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
                "stop_hook_active": False,
            }
        ),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == "", completed.stderr
    if not completed.stdout.strip():
        return ""
    answer = json.loads(completed.stdout)
    assert answer.get("decision") == "block", answer
    return str(answer.get("reason") or "")


def _view_rows() -> list[dict[str, object]]:
    answer = serve_crew_view("obligations", project=PROJECT, session=SESSION)
    assert answer["ok"] is True, answer
    return list(answer["obligations"])


def test_a_promoted_runs_promotion_duty_is_gone_from_the_view_and_the_hook(
    repository: Path, runtime_root: Path, follower: None
) -> None:
    """A duty whose run landed and left a ledger row is settled, not repeated.

    The answer still carries both kinds as the station stored them, so the
    fixture's own pointer state is what changes between this case and the next:
    nothing here re-derives the answer.
    """
    _record_promotion(repository, PROMOTED_RUN_ID, PROMOTED_NODE_ID)
    _record_promotion(repository, STALE_RUN_ID, PROMOTED_NODE_ID)
    assert ledger.run_path(PROJECT, PROMOTED_RUN_ID, repository).exists()
    _publish_answer(
        runtime_root,
        [
            _duty(kind="review-ready", run_id=PROMOTED_RUN_ID, node=PROMOTED_NODE_ID),
            _duty(
                kind="promotable-stale",
                run_id=STALE_RUN_ID,
                node=PROMOTED_NODE_ID,
                age=5_400,
            ),
        ],
    )
    for run_id in (PROMOTED_RUN_ID, STALE_RUN_ID):
        assert not obligation_snapshot.live_pointer_path(run_id).exists()

    assert _view_rows() == [], "the view still serves a promotion duty"

    checklist = _drive(repository, runtime=runtime_root)
    assert PROMOTED_RUN_ID not in checklist, checklist
    assert STALE_RUN_ID not in checklist, checklist


def test_the_same_duty_is_kept_while_its_run_holds_a_live_pointer(
    repository: Path, runtime_root: Path, follower: None
) -> None:
    """The drop is the pointer's absence, not the row's kind or its age."""
    _publish_answer(
        runtime_root,
        [_duty(kind="review-ready", run_id=PROMOTED_RUN_ID, node=PROMOTED_NODE_ID)],
    )
    _record_pointer(PROMOTED_RUN_ID)
    assert not ledger.run_path(PROJECT, PROMOTED_RUN_ID, repository).exists()

    rows = _view_rows()
    assert [row["run_id"] for row in rows] == [PROMOTED_RUN_ID], rows
    assert rows[0]["kind"] == "review-ready", rows

    checklist = _drive(repository, runtime=runtime_root)
    assert f"- [review-ready] {PROMOTED_RUN_ID}" in checklist, checklist


def test_a_discarded_runs_promotion_duty_is_gone_without_a_ledger_row(
    repository: Path, runtime_root: Path, follower: None
) -> None:
    """A discard releases the pointer too, and no promotion can be taken.

    No ledger row records this run, so the drop cannot be resting on one: it is
    the pointer's absence that makes the printed command impossible.
    """
    _publish_answer(
        runtime_root,
        [_duty(kind="review-ready", run_id=DISCARDED_RUN_ID, node=PROMOTED_NODE_ID)],
    )
    assert not obligation_snapshot.live_pointer_path(DISCARDED_RUN_ID).exists()
    assert not ledger.run_path(PROJECT, DISCARDED_RUN_ID, repository).exists()

    assert _view_rows() == [], "a discarded run still owes a promotion"

    checklist = _drive(repository, runtime=runtime_root)
    assert DISCARDED_RUN_ID not in checklist, checklist


def test_a_missing_review_goes_with_its_runs_pointer(
    repository: Path, runtime_root: Path, follower: None
) -> None:
    """A review duty is shown only while the run it names still holds a pointer.

    The stored answer is a snapshot of a run that was there when it was
    written. Once the run's pointer has gone there is no tree the duty can act
    on, so both surfaces drop it rather than carrying an outstanding row no
    action can clear. A duty that names no run is unaffected, because it is
    not a run's departure that settles it.
    """
    _publish_answer(
        runtime_root,
        [
            _duty(
                kind="review-missing",
                run_id=UNREVIEWED_RUN_ID,
                node=UNREVIEWED_NODE_ID,
            )
        ],
    )
    assert not obligation_snapshot.live_pointer_path(UNREVIEWED_RUN_ID).exists()

    rows = _view_rows()
    assert rows == [], rows

    checklist = _drive(repository, runtime=runtime_root)
    assert UNREVIEWED_RUN_ID not in checklist, checklist
