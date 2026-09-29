"""A hand-typed resume waits for an observed end, as the resume sweep does.

The row a coordinator reads promises that recovery waits for a worker's end. The
sweep's lift was given that rule: it refuses a run of unknown liveness, naming
the reading it holds. A direct ``crew resume`` reaches the launcher through the
sweep's own door, so the sweep's refusal was bypassed simply by typing the
command by hand, and a hand-typed resume of a run whose worker lives on another
machine, or whose pointer recorded no process, started a second worker over it.

Two observations license a resume, and they are the sweep's: a pid this host
checked and found dead, or a supervisor's exit record, which stays readable on a
machine that never launched the worker. The launcher reads them through the
sweep's own helper rather than a second composition of it, so the two doors
cannot come to disagree about what an observed end is. A proven-live process is
still refused with the message the guard has always given, ahead of the new
reading, so nothing about that case changes.

The declared mutation removes the observed-end guard from ``resume_plan`` in a
scratch copy, restoring the refusal that only ever refused a proven-live
process. The unknown-liveness case then builds a plan, and its assertion that
the resume is refused fails. The gate that runs it logs that mutation verbatim
as the red log's first line.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew.dispatch import resume_plan
from reckon.crew.node import CrewError
from reckon.crew.runs import _write_json, pointer_path
from tests import test_a_live_run_never_reads_dead as liveness

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = (
    "restore the proven-alive-only refusal in resume_plan in a scratch copy; "
    "the unknown-liveness case must return a plan and fail"
)

PROJECT = "proj"
HOST = socket.gethostname()
FOREIGN_HOST = "a-launcher-host-that-is-not-this-one"

EXIT_RUN = "r-observed-exit"
DEAD_PID_RUN = "r-observed-dead-pid"
UNKNOWN_RUN = "r-unobserved-liveness"
LIVE_RUN = "r-proven-live"

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


def _home_fingerprint(home: Path) -> list[tuple[str, int]]:
    """The real config home's own entries, by name and mtime.

    One directory level only: the point is to catch a write that landed in the
    reader's own home, and a recursive walk of a live fleet's home on GPFS is
    the crawl this check must not itself become.
    """
    if not home.is_dir():
        return []
    return sorted((entry.name, entry.stat().st_mtime_ns) for entry in home.iterdir())


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run against a temporary crew home, and prove the real one untouched."""
    real_home = Path(os.path.expanduser("~")) / ".config" / "reckon"
    before = _home_fingerprint(real_home)
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    assert runs.crew_home().is_relative_to(tmp_path)
    yield config_home
    assert _home_fingerprint(real_home) == before


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


def _write_exit_record(run_id: str) -> None:
    """The supervisor's account of the end, written where its reader looks."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / recovery.EXIT_RECORD_NAME).write_text(
        json.dumps(
            {
                "run_id": run_id,
                "worker_pid": None,
                "launched_at": "2026-09-29T10:00:00Z",
                "exited_at": "2026-09-29T10:30:00Z",
                "exit_code": 0,
                "stream_records_seen": 4,
            }
        ),
        encoding="utf-8",
    )


def _pointer(
    tmp_path: Path,
    repo: Path,
    run_id: str,
    *,
    pid: int | None,
    launcher_host: str | None,
    exit_record: bool = False,
) -> dict:
    """A stopped run as a dispatch leaves it, differing only in what is known of its end.

    Everything but the evidence about the worker's end is identical across the
    four runs, so the launcher's decision differs only in that. The pid is the
    one the record names, and the host that issued it is recorded beside it: a
    pid is meaningful only on the machine that issued it.
    """
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    tree = tmp_path / f"{run_id}-tree"
    tree.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(
        "node: direct-resume-needs-an-observed-death\nstatus: waiting\n",
        encoding="utf-8",
    )
    if exit_record:
        _write_exit_record(run_id)
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
        "pid_start_time": recovery._process_start_time(pid) if pid else None,
        "process_alive": None,
        "session_id": "sess-on-the-pointer",
        "created_at": "2026-09-29T09:59:00+00:00",
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
    _write_json(pointer_path(run_id), record)
    return record


def test_a_supervisor_recorded_exit_licenses_the_resume(
    home: Path, tmp_path: Path, repo: Path
) -> None:
    """The recorded end is an observation readable on a machine that never launched the worker."""
    _pointer(
        tmp_path,
        repo,
        EXIT_RUN,
        pid=liveness._absent_pid(),
        launcher_host=FOREIGN_HOST,
        exit_record=True,
    )

    plan = resume_plan(EXIT_RUN, "continue the same task", config=CONFIG)

    assert plan.resumed_session == "sess-on-the-pointer"
    assert plan.dialect == "codex"


def test_a_pid_this_host_found_dead_licenses_the_resume(
    home: Path, tmp_path: Path, repo: Path
) -> None:
    """A pid the process table answers is an observation taken where the pid means something."""
    _pointer(
        tmp_path,
        repo,
        DEAD_PID_RUN,
        pid=liveness._absent_pid(),
        launcher_host=HOST,
    )

    plan = resume_plan(DEAD_PID_RUN, "continue the same task", config=CONFIG)

    assert plan.resumed_session == "sess-on-the-pointer"
    assert plan.dialect == "codex"


def test_a_run_of_unknown_liveness_is_refused_naming_the_reading(
    home: Path, tmp_path: Path, repo: Path
) -> None:
    """Nothing observed the end, so nothing licenses a second worker over a possibly-live one."""
    _pointer(
        tmp_path,
        repo,
        UNKNOWN_RUN,
        pid=liveness._absent_pid(),
        launcher_host=FOREIGN_HOST,
    )

    with pytest.raises(CrewError) as raised:
        resume_plan(UNKNOWN_RUN, "continue the same task", config=CONFIG)

    refusal = str(raised.value)
    assert UNKNOWN_RUN in refusal
    assert "liveness unknown" in refusal


def test_a_proven_live_process_is_refused_as_before(
    home: Path, tmp_path: Path, repo: Path
) -> None:
    """The case that was already right keeps the message it always gave."""
    with liveness._live_child() as pid:
        _pointer(tmp_path, repo, LIVE_RUN, pid=pid, launcher_host=HOST)

        with pytest.raises(CrewError) as raised:
            resume_plan(LIVE_RUN, "continue the same task", config=CONFIG)

    refusal = str(raised.value)
    assert "still has a live process" in refusal
    assert "before resuming" in refusal
