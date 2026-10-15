"""A resume never launches beside an attempt this host cannot see.

A worker launched on another machine carries a pid that is meaningless in this
host's process table, so the guard that refuses a proven-live process answers
nothing and a second door starts a worker beside the first. Measured
2026-10-15: a run had two workers writing one tree for about twenty minutes,
its attempts split across two hosts, because every guard a resume had read a
fact local to one kernel.

Two facts cross hosts instead. Each launch reserves its attempt number with an
exclusive create on a claim file in the run directory, which is coherent across
hosts on this home where the pointer's flock is not; and a resume whose
liveness is unknown reads the run's own stream, the same way the stalled-row
reader does — a stream in motion refuses, a stream that has gone quiet licenses
the resume. A claim left by a launcher that never recorded its attempt is
reclaimed once its launch has exceeded the window a fresh dispatch is given, so
an abandoned reservation cannot wedge the run.

The declared mutation removes the exclusive claim, restoring the door that only
ever refused a proven-live process; both doors then launch and the two-door
case fails. The gate logs that mutation verbatim.

Every case points the crew home at a temporary directory and asserts the real
one is untouched afterwards, because the isolation is the test's own claim and
a monitor that reads the machine would pass wherever it was run.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import resumption, runs
from reckon.crew.node import CrewError
from tests import test_a_live_run_never_reads_dead as liveness

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = "remove the exclusive attempt claim; both resume doors must launch"

PROJECT = "proj"
FOREIGN_HOST = "a-launcher-host-that-is-not-this-one"

TWO_DOOR_RUN = "r-two-doors"
REFUSED_RUN = "r-stream-in-motion"
ENDED_RUN = "r-stream-quiet"
RECLAIM_RUN = "r-abandoned-claim"

# Every run id this file mints, so the real-home check names a path only this
# file creates rather than one a live fleet shares.
_RUN_IDS = (TWO_DOOR_RUN, REFUSED_RUN, ENDED_RUN, RECLAIM_RUN)

# How far past the stall window a quiet stream and an old launch are placed, so
# the reading is a fixture fact rather than the wall clock.
LONG_AGO_SECONDS = 3 * 60 * 60

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
        },
    },
    "roles": {"implement": {}},
    "budget": {
        "utilisation_ceiling_pct": 100,
        "resume_reserve_pct": 5,
        "exhausted_statuses": [],
    },
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _real_crew_home() -> Path:
    return Path(os.path.expanduser("~")) / ".config" / "reckon" / "crew"


def _case_artifacts(crew_home: Path) -> list[Path]:
    return [
        *(crew_home / "runs" / run_id for run_id in _RUN_IDS),
        *(crew_home / "live" / f"{run_id}.json" for run_id in _RUN_IDS),
    ]


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run against a temporary crew home, and prove the real one untouched."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    assert runs.crew_home().is_relative_to(tmp_path)
    yield config_home
    landed = [path for path in _case_artifacts(_real_crew_home()) if path.exists()]
    assert not landed, f"the real crew home carries this file's run paths: {landed}"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A throwaway repository carrying the fleet script a launch composes with."""
    root = tmp_path / "repo"
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills/reckon-build/scripts/worktree_fleet.py"
    (scripts / "worktree_fleet.py").write_text(source.read_text())
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return root


def _write_stream(record: dict[str, Any], *, quiet_seconds: int | None) -> Path:
    """A stream carrying one assistant turn, aged ``quiet_seconds`` from now.

    None leaves the stream freshly written — a worker still producing output.
    """
    path = Path(str(record["log_path"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "assistant", "text": "working"}) + "\n", encoding="utf-8"
    )
    if quiet_seconds is not None:
        moment = time.time() - quiet_seconds
        os.utime(path, (moment, moment))
    return path


def _pointer(
    tmp_path: Path,
    repo: Path,
    run_id: str,
    *,
    launcher_host: str | None,
    pid: int | None,
    stream_quiet_seconds: int | None,
    attempt: int = 1,
) -> dict:
    """A stopped run whose worker is on another host, with a stream of ``age``.

    Every field but the stream's quiet time is identical across the cases, so
    the door's decision differs only in the reading the stream supplies.
    """
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    tree = tmp_path / f"{run_id}-tree"
    tree.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text("node: resume-claim\nstatus: waiting\n", encoding="utf-8")
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(tree),
        "launch": "cli",
        "argv": ["codex", "exec"],
        "backend": "alpha",
        "role": "implement",
        "pid": pid,
        "pid_start_time": None,
        "process_alive": None,
        "session_id": "sess-on-the-pointer",
        "created_at": "2026-10-15T09:00:00+00:00",
        "attempt": attempt,
        "log_path": str(directory / "stream.jsonl"),
        "manifest_path": str(manifest),
        "phase": "working",
        "node": {
            "id": run_id,
            "plan": "plan-a",
            "section": "",
            "time_budget": "30m",
            "write_paths": [],
        },
    }
    if launcher_host is not None:
        record["launcher_host"] = launcher_host
    runs._write_json(runs.pointer_path(run_id), record)
    if stream_quiet_seconds is not None:
        _write_stream(record, quiet_seconds=stream_quiet_seconds)
    return record


def _launcher(pid: int) -> Callable[..., int]:
    """A launcher seam standing in for the supervisor spawn, returning ``pid``."""

    def launcher(plan: Any, **kwargs: Any) -> int:
        return pid

    return launcher


def _launch(run_id: str) -> dict[str, Any]:
    """Resume a run through the real launch path with a launcher seam."""
    return resumption._resume(
        run_id,
        runs.read_pointer(run_id),
        config=CONFIG,
        launcher=_launcher(liveness._absent_pid()),
        advice="continue the same task",
    )


@pytest.fixture(autouse=True)
def _registry_is_clean() -> Any:
    resumption._LAUNCHES_IN_THIS_PROCESS.clear()
    yield
    resumption._LAUNCHES_IN_THIS_PROCESS.clear()


# --- Two doors, one current attempt ----------------------------------------


def test_two_doors_reading_one_attempt_launch_only_once(
    home: Path, tmp_path: Path, repo: Path
) -> None:
    """Of two doors that read the same current attempt, one launches, one refuses.

    The refused door names the run and the in-flight wording, and its advice is
    kept for the run's next resume rather than dropped.
    """
    record = _pointer(
        tmp_path,
        repo,
        TWO_DOOR_RUN,
        launcher_host=None,
        pid=liveness._absent_pid(),
        stream_quiet_seconds=LONG_AGO_SECONDS,
    )
    stale = dict(record)

    launched = resumption._resume(
        TWO_DOOR_RUN,
        stale,
        config=CONFIG,
        launcher=_launcher(liveness._absent_pid()),
        advice="carry on from the same node",
    )
    assert launched["pid"] == liveness._absent_pid(), launched

    with pytest.raises(CrewError) as refused:
        resumption._resume(
            TWO_DOOR_RUN,
            stale,
            config=CONFIG,
            launcher=_launcher(liveness._absent_pid()),
            advice="a second coordinator's answer",
        )

    refusal = str(refused.value)
    assert TWO_DOOR_RUN in refusal, refusal
    assert "already has a resume launching" in refusal, refusal
    kept = resumption.refused_advice_path(TWO_DOOR_RUN)
    assert kept.read_text(encoding="utf-8").strip() == "a second coordinator's answer"


# --- A stream in motion, and a stream gone quiet ----------------------------


def test_a_stream_in_motion_refuses_the_resume(
    home: Path, tmp_path: Path, repo: Path
) -> None:
    """A foreign worker still writing is a life this host can read, so refuse."""
    _pointer(
        tmp_path,
        repo,
        REFUSED_RUN,
        launcher_host=FOREIGN_HOST,
        pid=liveness._absent_pid(),
        stream_quiet_seconds=0,
    )

    with pytest.raises(CrewError) as raised:
        resumption.resume_plan(REFUSED_RUN, "continue the same task", config=CONFIG)

    refusal = str(raised.value)
    assert REFUSED_RUN in refusal, refusal
    assert "still has a live process" in refusal, refusal


def test_a_stream_past_the_window_launches(
    home: Path, tmp_path: Path, repo: Path
) -> None:
    """A foreign worker that wrote and then went quiet licenses the resume."""
    _pointer(
        tmp_path,
        repo,
        ENDED_RUN,
        launcher_host=FOREIGN_HOST,
        pid=liveness._absent_pid(),
        stream_quiet_seconds=LONG_AGO_SECONDS,
    )

    launched = _launch(ENDED_RUN)

    assert launched["pid"] == liveness._absent_pid(), launched


# --- An abandoned claim is reclaimed ---------------------------------------


def test_an_abandoned_claim_is_reclaimed_after_the_launch_window(
    home: Path, tmp_path: Path, repo: Path
) -> None:
    """A claim whose launcher never recorded its attempt is freed once stale."""
    record = _pointer(
        tmp_path,
        repo,
        RECLAIM_RUN,
        launcher_host=None,
        pid=liveness._absent_pid(),
        stream_quiet_seconds=LONG_AGO_SECONDS,
    )
    claim = resumption.attempt_claim_path(RECLAIM_RUN, 2)
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text(json.dumps({"attempt": 2}), encoding="utf-8")
    old = time.time() - (resumption.LAUNCH_WINDOW_SECONDS + 60)
    os.utime(claim, (old, old))

    taken = resumption.claim_next_attempt(RECLAIM_RUN, record)

    assert taken == claim, taken
    assert claim.exists(), claim


def test_a_fresh_claim_is_not_reclaimed(home: Path, tmp_path: Path, repo: Path) -> None:
    """A claim inside the launch window still guards a launcher composing."""
    record = _pointer(
        tmp_path,
        repo,
        RECLAIM_RUN,
        launcher_host=None,
        pid=liveness._absent_pid(),
        stream_quiet_seconds=LONG_AGO_SECONDS,
    )
    claim = resumption.attempt_claim_path(RECLAIM_RUN, 2)
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text(json.dumps({"attempt": 2}), encoding="utf-8")

    with pytest.raises(CrewError) as raised:
        resumption.claim_next_attempt(RECLAIM_RUN, record)

    assert RECLAIM_RUN in str(raised.value)
    assert "already has a resume launching" in str(raised.value)
