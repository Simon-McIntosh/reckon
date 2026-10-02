"""The stop hook answers from the session's snapshot, or a bounded derivation.

The stop path reads the same snapshot the prompt path reads: a fresh one decides
the verdict, so the stop blocks exactly when the snapshot lists outstanding
obligations. A snapshot that is not fresh for any reason sends the hook to the
derivation, imported lazily and run under a budget; when that budget runs out
within the derivation, the stop is allowed with one line naming the not-fresh
reason, because no producer state may trap a coordinator.

Within its bound a review whose dispatch the lane refused as paused reads as
queued, naming the refusal's instant and the session's published worker slots;
it is listed and does not hold the turn open. Past the bound the duty reads
missing again and blocks, which is what makes the queued reading a bound rather
than a suppression.

Every case drives the hook as the harness does: a subprocess with hook JSON on
stdin against a synthesised configuration home, except the timed case, which
drives the stop function in-process so the alarm can be shown cutting a slow
derivation rather than trusted to.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import obligation_snapshot, paid_lanes, runs
from reckon.crew.obligations import (
    REVIEW_QUEUED_BOUND_SECONDS,
    REVIEW_QUEUED_DUTY_KIND,
)
from reckon.crew.obligations import obligations as obligations_view
from reckon.hooks import coordinator_obligations
from reckon.hooks.coordinator_obligations import format_checklist, not_fresh_line

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"
PROJECT = "stop-hook-fixture"
SESSION = "stop-hook-session"
RUN_ID = "run-stop-hook-blocked"
NODE_ID = "stop-hook-blocked-node"
SCORING_RUN_ID = "run-stop-hook-scoring"
SCORING_NODE_ID = "stop-hook-scoring-node"
SNAPSHOT_ONLY_RUN_ID = "run-only-in-the-snapshot"
REMEDY = f"reckon crew watch --ensure --project {PROJECT}"
SESSION_WORKER_SLOTS = 3
LANE_WORKER_SLOTS = 9

# A pid no process can hold, so the drive's session resolves through the
# registration's own name rather than the runner's ambient harness identity.
_ABSENT_HARNESS_PID = 2_147_483_647


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    return home


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
        ("commit", "-q", "-m", "test: seed stop hook fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


@pytest.fixture()
def follower(config_home: Path):
    """Register the session exactly as a real follower arm would."""
    with runs.follower_claim(PROJECT, SESSION) as claim:
        assert claim[0] is True, claim[1]
        yield


def _live_run(
    repository: Path,
    tmp_path: Path,
    *,
    run_id: str,
    node_id: str,
    status: str = "blocked",
    commits: bool = False,
) -> dict[str, object]:
    """Record one live run, scoring when it names a head no review has read."""
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    head = _git(repository, "rev-parse", "HEAD")
    body = f"node: {node_id}\nstatus: {status}\n"
    if commits:
        body += f"commits: [{head}]\n"
    manifest.write_text(body, encoding="utf-8")
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repository),
        "worktree": str(repository),
        "base_sha": head,
        "process_alive": False,
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": node_id,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
    }
    _write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _record_review_refusal(run_id: str, *, at: datetime, head: str) -> None:
    """Write the lane-paused attempt record the reflex writes on a refusal."""
    pointer = json.loads(runs.pointer_path(run_id).read_text(encoding="utf-8"))
    pointer["review_dispatch"] = {
        "status": "lane-paused",
        "reason": "the lane's fair share holds no worker slot for this session",
        "run_id": None,
        "backend": "codex",
        "head": head,
        "at": at.isoformat(),
        "attempt": 1,
    }
    _write_json(runs.pointer_path(run_id), pointer)


def _producer(**overrides: object) -> dict[str, object]:
    producer = {
        "pid": os.getpid(),
        "pid_start_time": obligation_snapshot.process_start_time(os.getpid()),
        "started_at": datetime.now(tz=UTC).isoformat(),
        "code_stamp": obligation_snapshot.source_code_stamp(),
    }
    producer.update(overrides)
    return producer


def _publish(payload: dict[str, object], **producer_overrides: object) -> Path:
    document = obligation_snapshot.document_for(
        payload,
        computed_at=datetime.now(tz=UTC),
        stream_offset=0,
        producer=_producer(**producer_overrides),
    )
    return obligation_snapshot.write_snapshot(PROJECT, SESSION, document)


def _stop_payload(repository: Path) -> dict[str, object]:
    return {"session_id": SESSION, "cwd": str(repository)}


def _drive(
    mode: str,
    payload: dict[str, object],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["CLAUDE_PID"] = str(_ABSENT_HARNESS_PID)
    environment.update(env or {})
    completed = subprocess.run(
        [sys.executable, str(HOOK), "--hook", mode],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert completed.stderr == "", completed.stderr
    return completed


def _empty_payload() -> dict[str, object]:
    return {
        "project": PROJECT,
        "session": SESSION,
        "obligations": [],
        "acknowledged": [],
        "summary": {"count": 0, "oldest_age_seconds": 0, "unreconciled_runs": 0},
    }


def test_a_fresh_snapshot_decides_the_stop(
    repository: Path, tmp_path: Path, follower: None
) -> None:
    """The stop blocks on what the snapshot lists, not the live fleet.

    A snapshot naming a duty the fleet does not hold must block, and a snapshot
    listing nothing while the fleet still owes a duty must not -- otherwise the
    verdict would be the derivation's and the reading untested.
    """
    _live_run(repository, tmp_path, run_id=RUN_ID, node_id=NODE_ID)
    _publish(obligations_view(PROJECT, SESSION))

    blocking = _drive("stop", _stop_payload(repository))

    assert blocking.returncode == 0
    decision = json.loads(blocking.stdout)
    assert decision["decision"] == "block"
    assert RUN_ID in decision["reason"]

    _publish(
        {
            "project": PROJECT,
            "session": SESSION,
            "obligations": [
                {
                    "kind": "blocked",
                    "run_id": SNAPSHOT_ONLY_RUN_ID,
                    "node": "snapshot-only-node",
                    "age_seconds": 4,
                    "next_command": "read the snapshot-only blocker",
                }
            ],
            "acknowledged": [],
            "summary": {"count": 1, "oldest_age_seconds": 4, "unreconciled_runs": 0},
        }
    )
    snapshot_only = _drive("stop", _stop_payload(repository))

    reason = json.loads(snapshot_only.stdout)["reason"]
    assert SNAPSHOT_ONLY_RUN_ID in reason
    assert RUN_ID not in reason, (
        "the live blocked run must not appear: the stop read the snapshot, not "
        "the fleet"
    )

    _publish(_empty_payload())
    silent = _drive("stop", _stop_payload(repository))

    assert silent.returncode == 0
    assert silent.stdout == ""

    assert obligation_snapshot.snapshot_path(PROJECT, SESSION).is_file(), (
        "the snapshot must survive the drives"
    )


def test_a_not_fresh_snapshot_derives_inline_within_the_budget(
    repository: Path, tmp_path: Path, follower: None
) -> None:
    """With no snapshot at all the derivation runs inline and still blocks."""
    _live_run(repository, tmp_path, run_id=RUN_ID, node_id=NODE_ID)
    path = obligation_snapshot.snapshot_path(PROJECT, SESSION)
    assert not path.exists(), "this case needs no snapshot to bite"

    completed = _drive("stop", _stop_payload(repository))

    assert completed.returncode == 0
    decision = json.loads(completed.stdout)
    assert decision["decision"] == "block"
    assert RUN_ID in decision["reason"]
    assert "blocked" in decision["reason"]
    assert not path.exists(), "the stop path must not create the snapshot"


def test_an_over_budget_derivation_allows_the_stop_with_its_reason(
    repository: Path, tmp_path: Path, follower: None
) -> None:
    """The budget running out allows the stop, and the reason is spoken.

    The same fixture blocks on the default budget, so the allowance is the
    override's doing rather than an empty fleet; and the snapshot is still
    absent afterwards, because the hook never writes it.
    """
    _live_run(repository, tmp_path, run_id=RUN_ID, node_id=NODE_ID)
    within = _drive("stop", _stop_payload(repository))
    assert json.loads(within.stdout)["decision"] == "block"

    over = _drive(
        "stop",
        _stop_payload(repository),
        env={coordinator_obligations.STOP_DERIVATION_BUDGET_ENV: "0"},
    )

    assert over.returncode == 0
    emitted = json.loads(over.stdout)
    assert "decision" not in emitted, "the stop must be allowed, not blocked"
    assert "no producer" in emitted["systemMessage"]
    assert REMEDY in emitted["systemMessage"]
    assert not obligation_snapshot.snapshot_path(PROJECT, SESSION).exists()


def test_a_slow_derivation_is_cut_at_the_budget(
    repository: Path,
    tmp_path: Path,
    follower: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The alarm, not a zero shortcut: a slow derivation is interrupted.

    Driven in-process because the cut has to be shown happening to a
    derivation that would otherwise run for far longer than the budget, which
    is the case the budget exists for. The derivation stub sleeps past the
    override and asserts it was never reached, so a hook that waited it out
    would fail here rather than pass silently.
    """
    _live_run(repository, tmp_path, run_id=RUN_ID, node_id=NODE_ID)
    monkeypatch.setenv(coordinator_obligations.STOP_DERIVATION_BUDGET_ENV, "0.2")

    def _slow(project: str, session: str) -> dict[str, object]:
        time.sleep(30)
        raise AssertionError("the budget must cut the derivation before it returns")

    monkeypatch.setattr(coordinator_obligations, "_obligations_view", lambda: _slow)

    started = time.monotonic()
    exit_code = coordinator_obligations._stop(_stop_payload(repository))
    elapsed = time.monotonic() - started

    assert exit_code == 0
    assert elapsed < 10, f"the derivation was not cut ({elapsed:.1f}s)"
    emitted = json.loads(capsys.readouterr().out)
    assert "no producer" in emitted["systemMessage"]


def test_a_lane_refused_review_reads_queued_and_returns_after_the_bound(
    repository: Path, tmp_path: Path, follower: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused review is queued within the bound, missing again past it.

    The refusal is the one the reflex records, so the reading is driven through
    the record the writer actually produces. The lane document is planted, so
    the worker-slots figure in the line is one this case chose: the session's
    own share, not the lane-wide figure beside it. The stop is silent while the
    refusal is fresh and blocks once the bound has passed, which is what makes
    the queued reading a bounded wait rather than a suppression.
    """
    head = _git(repository, "rev-parse", "HEAD")
    _live_run(
        repository,
        tmp_path,
        run_id=SCORING_RUN_ID,
        node_id=SCORING_NODE_ID,
        status="complete",
        commits=True,
    )
    lane = tmp_path / "lane.json"
    _write_json(
        lane,
        {
            "observed_at": datetime.now(tz=UTC).isoformat(),
            "suggested_shelf_life_seconds": 300,
            "admission": {
                "worker_slots": LANE_WORKER_SLOTS,
                "new_session_worker_slots": 7,
                "sessions": {
                    SESSION: {"live_runs": 1, "worker_slots": SESSION_WORKER_SLOTS}
                },
                "observed_seconds": 300,
            },
        },
    )
    monkeypatch.setenv(paid_lanes.LOCAL_LANE_DOCUMENT_ENV, str(lane))
    refused_at = datetime.now(tz=UTC) - timedelta(seconds=30)
    _record_review_refusal(SCORING_RUN_ID, at=refused_at, head=head)

    derived = obligations_view(PROJECT, SESSION)
    queued = [
        item
        for item in derived["obligations"]
        if item["kind"] == REVIEW_QUEUED_DUTY_KIND
    ]
    assert len(queued) == 1, derived["obligations"]
    assert queued[0]["refused_at"] == refused_at.isoformat()
    assert queued[0]["worker_slots"] == SESSION_WORKER_SLOTS

    _publish(derived)
    prompting = _drive("prompt", _stop_payload(repository))
    checklist = json.loads(prompting.stdout)["hookSpecificOutput"]["additionalContext"]
    assert f"- [{REVIEW_QUEUED_DUTY_KIND}] {SCORING_RUN_ID}" in checklist
    assert refused_at.isoformat() in checklist, checklist
    assert f"session worker slots: {SESSION_WORKER_SLOTS}" in checklist, checklist

    stopping = _drive("stop", _stop_payload(repository))

    assert stopping.returncode == 0
    assert stopping.stdout == "", (
        "a review the reflex still owns must not hold the turn open"
    )

    expired = datetime.now(tz=UTC) - timedelta(seconds=REVIEW_QUEUED_BOUND_SECONDS + 60)
    _record_review_refusal(SCORING_RUN_ID, at=expired, head=head)
    _publish(obligations_view(PROJECT, SESSION))

    blocking = _drive("stop", _stop_payload(repository))

    decision = json.loads(blocking.stdout)
    assert decision["decision"] == "block", decision
    assert f"- [review-missing] {SCORING_RUN_ID}" in decision["reason"]


def test_the_stale_code_state_has_its_own_line() -> None:
    """A direct caller gets the state it asked about.

    The reload-aware prompt path never reaches this line for a live producer on
    older code, so the state is exercised here as a direct call: the line names
    the stale code rather than dressing the case as no producer, and carries
    the same remedy every other not-fresh state does.
    """
    line = not_fresh_line(
        obligation_snapshot.STALE_CODE,
        project=PROJECT,
        session=SESSION,
        document=None,
    )

    assert "no producer" not in line, line
    assert "older code" in line, line
    assert REMEDY in line, line


def test_an_empty_list_prints_no_oldest_age() -> None:
    """No duty means no age, since the figure would be the snapshot's own.

    Observed on 2026-10-02: an empty list rendered ``0 outstanding, oldest
    1h31m``, where the age was the snapshot's computed_at rather than any
    duty's. The non-empty header is asserted beside it so the absence is a
    rendering rule and not a renderer that stopped printing ages.
    """
    empty = _empty_payload()
    empty["summary"] = {"count": 0, "oldest_age_seconds": 5_460, "unreconciled_runs": 0}

    header = format_checklist(empty).splitlines()[0]

    assert header.endswith("0 outstanding"), header
    assert "oldest" not in header, header

    full = {
        "project": PROJECT,
        "session": SESSION,
        "obligations": [
            {
                "kind": "blocked",
                "run_id": RUN_ID,
                "node": NODE_ID,
                "age_seconds": 300,
                "next_command": "read the manifest",
            }
        ],
        "summary": {"count": 1, "oldest_age_seconds": 300, "unreconciled_runs": 0},
    }
    assert "oldest 5m0s" in format_checklist(full).splitlines()[0]
