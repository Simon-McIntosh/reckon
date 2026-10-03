"""A late acknowledgement and a skipped record do not hold a stop open.

The stop hook decides from the session's published snapshot, which is what the
project's sweep last computed. Two things change a verdict without a republish.
``crew ack`` writes its deferral on the run's live pointer, so a snapshot
computed seconds earlier goes on listing a duty the coordinator has already
excused; the stop re-reads the deferrals in force and honours one written after
the snapshot was computed. A review record the sweep could not read travels in
the snapshot itself as a finding, rendered into the duty list under its own
kind; it prompts, and it does not refuse the turn, because a file caught
mid-write is not work a coordinator can act on.

Every case drives the hook as the harness does: a subprocess with hook JSON on
stdin against a synthesised configuration home, so the verdict is the one the
wired hook gives rather than one read off a function it calls.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import obligation_snapshot, runs
from reckon.crew.obligations import obligations as obligations_view

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"
PROJECT = "stop-hook-late-state"
SESSION = "stop-hook-late-state-session"
RUN_ID = "run-stop-hook-deferred"
NODE_ID = "stop-hook-deferred-node"
UNREADABLE_KIND = "unreadable-review-record"

# A pid no process can hold, so the drive's session resolves through the
# registration's own name rather than the runner's ambient harness identity.
_ABSENT_HARNESS_PID = 2_147_483_647


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
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
        ("commit", "-q", "-m", "test: seed the late-state fixture"),
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


def _live_run(repository: Path, tmp_path: Path) -> None:
    """Record one live run a coordinator still owns."""
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    head = _git(repository, "rev-parse", "HEAD")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: {NODE_ID}\nstatus: blocked\n", encoding="utf-8")
    _write_json(
        runs.pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": head,
            "process_alive": False,
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {
                "id": NODE_ID,
                "plan": "fixture-plan",
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )


def _producer() -> dict[str, object]:
    return {
        "pid": os.getpid(),
        "pid_start_time": obligation_snapshot.process_start_time(os.getpid()),
        "started_at": datetime.now(tz=UTC).isoformat(),
        "code_stamp": obligation_snapshot.source_code_stamp(),
    }


def _publish(
    payload: dict[str, object],
    *,
    computed_at: datetime | None = None,
    findings: list[dict[str, str]] | None = None,
) -> Path:
    document = obligation_snapshot.document_for(
        payload,
        computed_at=computed_at or datetime.now(tz=UTC),
        stream_offset=0,
        producer=_producer(),
        findings=findings or [],
    )
    return obligation_snapshot.write_snapshot(PROJECT, SESSION, document)


def _stop_payload(repository: Path) -> dict[str, object]:
    return {"session_id": SESSION, "cwd": str(repository)}


def _drive(mode: str, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["CLAUDE_PID"] = str(_ABSENT_HARNESS_PID)
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


def _ack(run_id: str, *, until: datetime, reason: str = "deferred by the coordinator"):
    runs.record_run_acknowledgement(
        run_id, reason, until.isoformat(), project=PROJECT, session=SESSION
    )


def _payload(*, count: int = 0) -> dict[str, object]:
    return {
        "project": PROJECT,
        "session": SESSION,
        "obligations": [],
        "acknowledged": [],
        "summary": {
            "count": count,
            "oldest_age_seconds": 0,
            "unreconciled_runs": 0,
        },
    }


def test_an_ack_recorded_after_the_snapshot_lets_the_stop_through(
    repository: Path, tmp_path: Path, follower: None
) -> None:
    """A deferral the snapshot predates is honoured when the stop reads it.

    The snapshot is published five seconds before the acknowledgement, which is
    the shape observed on 2026-10-03: a snapshot computed nine seconds before
    an ack went on listing the acknowledged duty. The snapshot is asserted to
    still carry that duty when the stop passes, so the allowance is the
    deferral being re-read rather than a republish having emptied the list, and
    the deferral's own deadline is then moved into the past and the same
    snapshot blocks again, which makes the reading bounded rather than a
    suppression.
    """
    _live_run(repository, tmp_path)
    derived = obligations_view(PROJECT, SESSION)
    assert [item["run_id"] for item in derived["obligations"]] == [RUN_ID], derived

    computed_at = datetime.now(tz=UTC) - timedelta(seconds=5)
    _publish(derived, computed_at=computed_at)

    refusing = _drive("stop", _stop_payload(repository))
    assert json.loads(refusing.stdout)["decision"] == "block", refusing.stdout

    _ack(RUN_ID, until=datetime.now(tz=UTC) + timedelta(minutes=5))

    document = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert obligation_snapshot.freshness(document) == obligation_snapshot.FRESH, (
        "the snapshot must still decide the stop, or this case would exercise "
        "the inline derivation instead"
    )
    still_listed = obligation_snapshot.live_payload(document)
    assert [item["run_id"] for item in still_listed["obligations"]] == [RUN_ID], (
        "an acknowledgement republishes nothing, so the duty must still be in "
        "the snapshot for this case to bite"
    )
    recorded = runs.run_acknowledgement(runs.read_pointer(RUN_ID))
    assert recorded is not None
    assert datetime.fromisoformat(
        str(recorded["recorded_at"])
    ) > datetime.fromisoformat(str(document["computed_at"])), (
        "the acknowledgement must postdate the snapshot to be a late one"
    )

    honoured = _drive("stop", _stop_payload(repository))
    assert honoured.returncode == 0
    assert honoured.stdout == "", (
        "a duty the coordinator has already deferred must not hold the stop open: "
        f"{honoured.stdout}"
    )
    assert (
        obligation_snapshot.read_snapshot(PROJECT, SESSION)["computed_at"]
        == (document["computed_at"])
    ), "the stop path writes no snapshot of its own"

    _ack(RUN_ID, until=datetime.now(tz=UTC) - timedelta(minutes=1))

    expired = _drive("stop", _stop_payload(repository))
    assert json.loads(expired.stdout)["decision"] == "block", expired.stdout
    assert RUN_ID in json.loads(expired.stdout)["reason"]


def test_an_unreadable_record_prompts_without_holding_the_stop(
    repository: Path, follower: None, tmp_path: Path
) -> None:
    """A record the sweep could not read is listed and does not refuse a stop.

    The sweep records a review record it could not read as a finding, and the
    reader renders it into the duty list under its own kind. It is a reading of
    a file caught mid-write rather than work a coordinator can act on, so it
    reaches the checklist while the turn may still end. The prompt drive is
    asserted to name the path first, so the silent stop below is a kind that
    does not hold a turn open rather than a row that never reached the list.
    """
    half_written = tmp_path / "reviews" / "review-of-a-run.json"
    half_written.parent.mkdir(parents=True, exist_ok=True)
    half_written.write_text('{"reviewed_run_id": "run-half', encoding="utf-8")
    _publish(
        _payload(count=1),
        findings=[
            {
                "path": str(half_written),
                "error": "JSONDecodeError: Expecting value: line 1 column 21",
            }
        ],
    )

    prompting = _drive("prompt", _stop_payload(repository))
    checklist = json.loads(prompting.stdout)["hookSpecificOutput"]["additionalContext"]
    assert f"[{UNREADABLE_KIND}] {half_written}" in checklist, checklist

    stopping = _drive("stop", _stop_payload(repository))
    assert stopping.returncode == 0
    assert stopping.stdout == "", (
        "a file caught mid-write is not work a coordinator can act on, so it "
        f"must not hold the turn open: {stopping.stdout}"
    )
