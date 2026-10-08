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
from types import SimpleNamespace

import pytest

import reckon.crew.dispatch_sessions as dispatch_sessions_module
from reckon.crew import recovery, runs
from reckon.crew.dispatch import change_lane, resume_plan
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
HARNESS_RUN = "r-in-harness-live"

# Every run id this file mints. A real run's id carries its launch timestamp, so
# a path named for one of these is a path only this test creates.
_RUN_IDS = (EXIT_RUN, DEAD_PID_RUN, UNKNOWN_RUN, LIVE_RUN, HARNESS_RUN)

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
    """The crew home a case writes to if its isolation is not in place."""
    return Path(os.path.expanduser("~")) / ".config" / "reckon" / "crew"


def _case_artifacts(crew_home: Path) -> list[Path]:
    """The paths this file's cases leave under ``crew_home``.

    Named for the run ids above, so the same list describes the real crew home
    and a stand-in: a positive control can plant one and see it found.
    """
    return [
        *(crew_home / "runs" / run_id for run_id in _RUN_IDS),
        *(crew_home / "live" / f"{run_id}.json" for run_id in _RUN_IDS),
    ]


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run against a temporary crew home, and prove the real one untouched.

    The proof is the absence of a path only this test creates, read after the
    case: a live fleet writes into the real home while the case runs, so a
    before-and-after reading of that home's own entries moves under the fleet's
    hand and cannot say who moved it.
    """
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


# --- The third door: a lane change starts a worker too ---------------------


def _stub_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the destination to an in-harness directive, so no executable is needed.

    The refusal under test is decided before the destination is built, so the
    resolution is stubbed to the smallest shape ``change_lane`` reads. The
    launch is in-harness so the spawn path is never reached; the run's own
    reading is what the guard turns on.
    """
    resolution = SimpleNamespace(
        backend="beta",
        launch="in-harness",
        backend_settings={},
        lane_gate={
            "state": "open",
            "gate_path": "fixture-gate.json",
            "paused": False,
            "reason": None,
            "detail": "",
            "path_check": "skipped",
            "path_check_detail": "backend declares no lane document to publish a config_path",
        },
        validation=SimpleNamespace(ok=True, findings=[]),
        competence={"allowed": True},
        authority="a-ledger-authority",
        sandbox_write_roots=None,
    )
    monkeypatch.setattr(dispatch_sessions_module, "plan_dispatch", lambda **kwargs: resolution)
    monkeypatch.setattr(dispatch_sessions_module, "_budget_verdict", lambda **kwargs: {"held": False})
    monkeypatch.setattr(
        dispatch_sessions_module, "resolve_dispatch_ledger_root", lambda authority: authority
    )


def _lane_pointer(
    tmp_path: Path,
    repo: Path,
    run_id: str,
    *,
    pid: int | None,
    launcher_host: str | None,
) -> dict:
    """A stopped run as a lane change found it, differing only in what is known of its end."""
    record = _pointer(tmp_path, repo, run_id, pid=pid, launcher_host=launcher_host)
    prompt = runs.run_dir(run_id) / "prompt.txt"
    prompt.write_text("the original dispatch prompt\n", encoding="utf-8")
    record.update(
        {
            "dialect": "codex",
            "session_harness": "codex",
            "attempt": 1,
            "base_sha": "HEAD",
            "prompt_path": str(prompt),
        }
    )
    _write_json(pointer_path(run_id), record)
    return record


def test_a_lane_change_of_unknown_liveness_is_refused(
    home: Path, tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backend override must not start a second worker over a run whose end nothing observed."""
    _stub_destination(monkeypatch)
    _lane_pointer(
        tmp_path,
        repo,
        UNKNOWN_RUN,
        pid=liveness._absent_pid(),
        launcher_host=FOREIGN_HOST,
    )

    with pytest.raises(CrewError) as raised:
        change_lane(
            UNKNOWN_RUN, "beta", "the lane is spent", config=CONFIG, launch=True
        )

    refusal = str(raised.value)
    assert UNKNOWN_RUN in refusal
    assert "liveness unknown" in refusal


def test_a_lane_change_of_an_observed_death_proceeds(
    home: Path, tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An observed end licenses the move, and the move is what the run records.

    The stubbed destination fixes the values the plan reports, so asserting only
    those would rest on the test's own stub and hold whatever ``change_lane``
    did with the plan it was handed. The lineage the run now carries is composed
    by the lane change itself — the attempt it opened, the kind of attempt it
    is, and the history naming the lane and reason it left — so the case fails
    if the move is not performed and recorded, not only if the stub is echoed.
    """
    _stub_destination(monkeypatch)
    before = _lane_pointer(
        tmp_path,
        repo,
        DEAD_PID_RUN,
        pid=liveness._absent_pid(),
        launcher_host=HOST,
    )

    moved = change_lane(
        DEAD_PID_RUN, "beta", "the lane is spent", config=CONFIG, launch=True
    )

    assert moved["attempt"] == int(before["attempt"]) + 1
    assert moved["attempt_kind"] == "lane-change"
    assert moved["lane_changes"][-1]["from_backend"] == before["backend"]
    assert moved["lane_changes"][-1]["reason"] == "the lane is spent"
    assert moved["lineage"]["kind"] == "lane-change"
    assert runs.read_pointer(DEAD_PID_RUN)["lane_changes"] == moved["lane_changes"]


def test_a_lane_change_preview_reports_the_refusal_a_real_call_raises(
    home: Path, tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A --print-only lane change names the refusal the launch path would raise.

    The resume sweep reads its gates ahead of its dry-run branch so a prediction
    reads as the thing it predicts. A preview of an unknown-liveness run must
    therefore name the same refusal a real call raises rather than reporting a
    lane change the real call refuses.
    """
    _stub_destination(monkeypatch)
    _lane_pointer(
        tmp_path,
        repo,
        UNKNOWN_RUN,
        pid=liveness._absent_pid(),
        launcher_host=FOREIGN_HOST,
    )

    with pytest.raises(CrewError) as raised:
        change_lane(
            UNKNOWN_RUN, "beta", "the lane is spent", config=CONFIG, launch=False
        )

    refusal = str(raised.value)
    assert UNKNOWN_RUN in refusal
    assert "liveness unknown" in refusal


def test_a_lane_change_preview_of_a_live_harness_task_is_refused(
    home: Path, tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A harness-attached run refuses the move in a preview, not only on a real call.

    The in-harness refusal is a launch-path gate, so a preview must raise it
    rather than report a lane change the real call refuses. Every other case in
    the population presents a source the lane changer launched itself, so without
    this one a preview of a run the harness owns reads as a move it is not.
    """
    _stub_destination(monkeypatch)
    record = _lane_pointer(
        tmp_path,
        repo,
        HARNESS_RUN,
        pid=liveness._absent_pid(),
        launcher_host=HOST,
    )
    record["launch"] = "in-harness"
    record["task"] = "task-live-in-harness"
    record["phase"] = "working"
    _write_json(pointer_path(HARNESS_RUN), record)

    with pytest.raises(CrewError) as raised:
        change_lane(
            HARNESS_RUN, "beta", "the lane is spent", config=CONFIG, launch=False
        )

    refusal = str(raised.value)
    assert HARNESS_RUN in refusal
    assert "attached to live harness task" in refusal


def test_a_lane_change_preview_of_an_observed_death_reports_without_writing(
    home: Path, tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An observed end is licensed in a preview too, and a preview writes nothing."""
    _stub_destination(monkeypatch)
    before = _lane_pointer(
        tmp_path,
        repo,
        DEAD_PID_RUN,
        pid=liveness._absent_pid(),
        launcher_host=HOST,
    )

    preview = change_lane(
        DEAD_PID_RUN, "beta", "the lane is spent", config=CONFIG, launch=False
    )

    assert preview["backend"] == "beta"
    assert preview["launch"] == "in-harness"
    assert preview["run_id"] == DEAD_PID_RUN
    # A preview decides and reports; it stops no worker and records no move.
    assert runs.read_pointer(DEAD_PID_RUN) == before
