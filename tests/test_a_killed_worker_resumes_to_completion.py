"""A worker killed by a signal is classified, resumed, and lands.

A dispatched worker that is ended by a signal it did not choose used to leave a
pointer reading ``working`` for as long as nobody opened it, because the wait
status the launcher held was discarded and nothing re-derived liveness. The
steps that repair it — the supervisor's exit record, the ``interrupted``
classification, and the resume route — are each covered on their own; this file
drives them as one sequence, because a phase an operator never reaches is worth
nothing.

The whole path runs here against a stub worker in a throwaway repository and
crew home: a run is dispatched through the per-run supervisor, its worker ended
by ``SIGTERM``, the run classified from the supervisor's own exit record as
``interrupted`` with the signal named, offered for resume, resumed through the
resume launcher, and the resumed attempt's manifest carried through promotion
into the ledger.

Each assertion names what only its own step could have produced: the
classification is read from the record the supervisor wrote for a real killed
process, the resume offer comes from the launcher the ``crew resume`` verb
calls, and the promotion is read back from the committed ledger rather than
from the call's return value.
"""

from __future__ import annotations

import importlib
import json
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew import recovery, resumption, runs
from reckon.crew.runs import _write_json, pointer_path, run_dir

dispatch_module = importlib.import_module("reckon.crew.dispatch")
backends_module = importlib.import_module("reckon._backends")

PROJECT = "sample"
PLAN = "plan-a"
RUN_ID = "r-killed-worker-resumes"
NODE_ID = "a-killed-worker-resumes-to-completion"

REVIEW_WAIVER = "fixture: the run's end and its resume are the subject, not repository work"

# The backend the resume launcher composes its plan from. The command is a stub
# so the resumed attempt is a real supervised process that writes the manifest
# itself; the launcher, the supervisor and the exit record are all the ones a
# hand-typed resume reaches.
BACKEND = "stub-alpha"
# The command is a stub on the PATH the lane declares. Its name is one reckon
# can translate, because the dialect is chosen from the command's stem and a
# launch it cannot translate is refused before any plan is composed.
WORKER_BINARY = "codex"

# A worker that writes one turn and then lets a signal end it. The turn is what
# keeps this from being a launch failure: a run that reached a model and was
# killed is the case the phase exists for.
KILLED_WORKER = """#!/bin/sh
echo '{"type": "item.completed", "item": {"type": "agent_message", "text": "carrying the node"}}'
kill -TERM $$
"""

# The resumed worker writes the manifest promotion lands from, and exits of its
# own accord, so the run's second attempt ends the way a finished worker does.
RESUMED_WORKER = """#!/bin/sh
echo '{"type": "item.completed", "item": {"type": "agent_message", "text": "resumed"}}'
cat > "$RECKON_MANIFEST" <<'MANIFEST'
node: %s
status: complete
commits: none
MANIFEST
exit 0
""" % NODE_ID


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home, asserted to be the one the readers resolve."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    assert runs.crew_home().is_relative_to(tmp_path)
    return config_home


@pytest.fixture()
def repository(tmp_path: Path, home: Path) -> Path:
    """A throwaway repository carrying a plan and a committed head to land into."""
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    (root / "docs" / "plans" / f"{PLAN}.html").write_text(
        _plan_html.write_state(
            bare,
            {"type": "plan", "slug": PLAN, "title": "Plan A", "status": "active"},
        ),
        encoding="utf-8",
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _stub_worker(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _worktree_with_committed_work(repository: Path, tmp_path: Path) -> Path:
    """A worktree holding one commit beyond the run's dispatch base.

    The commit is what makes the run's retained work legible: it is the reading
    that says this run stopped part-way rather than never having started.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "artifact.txt").write_text("base\n", encoding="utf-8")
    _git(worktree, "init", "-q", "-b", "main")
    _git(worktree, "config", "user.email", "worker@example.invalid")
    _git(worktree, "config", "user.name", "Worker")
    _git(worktree, "add", "artifact.txt")
    _git(worktree, "commit", "-q", "-m", "test: fixture base")
    (worktree / "artifact.txt").write_text("base\ndelivered\n", encoding="utf-8")
    _git(worktree, "add", "artifact.txt")
    _git(worktree, "commit", "-q", "-m", "test: fixture delivery")
    return worktree


def _write_pointer(
    repository: Path,
    worktree: Path,
    manifest: Path,
    *,
    bin_directory: Path,
) -> dict:
    directory = run_dir(RUN_ID)
    directory.mkdir(parents=True, exist_ok=True)
    pointer = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(worktree),
        "launch": "cli",
        "role": "implement",
        "backend": BACKEND,
        "argv": [str(bin_directory / WORKER_BINARY)],
        "session_id": "session-that-survives-the-kill",
        "phase": "working",
        "created_at": dispatch_module._utc_now(),
        "manifest_path": str(manifest),
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stderr.log"),
        "node": {
            "id": NODE_ID,
            "plan": PLAN,
            "section": "§5",
            "time_budget": "35m",
            "write_paths": [],
        },
    }
    _write_json(pointer_path(RUN_ID), pointer)
    return pointer


def _dispatch_attempt(plan: backends_module.LaunchPlan, repository: Path, worktree: Path) -> int:
    """Start the run's first attempt the way a dispatch does.

    Dispatch composes the run's supervisor before it returns and writes the
    pointer naming that process, which is why the attempt records are prepared
    here rather than by the supervisor.
    """
    directory = run_dir(RUN_ID)
    prompt = directory / "prompt.txt"
    prompt.write_text("carry the node to its manifest\n", encoding="utf-8")
    attempt_started_at = dispatch_module._utc_now()
    dispatch_module._prepare_attempt_records(
        directory,
        run_id=RUN_ID,
        attempt=1,
        attempt_kind="dispatch",
        attempt_started_at=attempt_started_at,
    )
    spec_path = directory / dispatch_module.SUPERVISOR_SPEC_NAME
    _write_json(
        spec_path,
        dispatch_module._supervisor_spec(
            run_id=RUN_ID,
            run_directory=directory,
            repo_root=repository,
            worktree=worktree,
            plan=plan,
            prompt_path=prompt,
            log_path=directory / "stream.jsonl",
            stderr_path=directory / "stderr.log",
            attempt=1,
            attempt_started_at=attempt_started_at,
        ),
    )
    return dispatch_module._start_supervisor(spec_path, directory, RUN_ID)


def _wait_for_exit_record(*, attempt: int, timeout: float = 20.0) -> dict:
    """Read the exit record the supervisor wrote, or fail naming what it holds."""
    path = run_dir(RUN_ID) / dispatch_module.EXIT_RECORD_NAME
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = None
        if isinstance(record, dict) and int(record.get("attempt") or 0) == attempt:
            return record
        time.sleep(0.05)
    raise AssertionError(
        f"no exit record for attempt {attempt} appeared in {run_dir(RUN_ID)}: "
        f"{sorted(item.name for item in run_dir(RUN_ID).iterdir())}"
    )


def test_a_killed_worker_is_classified_resumed_and_completed(
    home: Path, repository: Path, tmp_path: Path
) -> None:
    worktree = _worktree_with_committed_work(repository, tmp_path)
    manifest = run_dir(RUN_ID) / "manifest.md"
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    _stub_worker(bin_directory, WORKER_BINARY, KILLED_WORKER)
    _write_pointer(repository, worktree, manifest, bin_directory=bin_directory)

    # 1. Dispatch: a stub worker is started under the run's own supervisor and
    #    ended by a signal it did not choose.
    killed_plan = backends_module.LaunchPlan(
        backend=BACKEND,
        dialect="claude",
        argv=["/bin/sh", str(bin_directory / WORKER_BINARY)],
        cwd=str(worktree),
        stdin_text="",
        environment={},
        final_message_path=None,
        resumed_session=None,
    )
    supervisor_pid = _dispatch_attempt(killed_plan, repository, worktree)
    pointer = runs.read_pointer(RUN_ID)
    pointer["pid"] = supervisor_pid
    pointer["pid_start_time"] = dispatch_module._process_start_time(supervisor_pid)
    pointer["launcher_host"] = socket.gethostname()
    _write_json(pointer_path(RUN_ID), pointer)

    # The exit record is written just before the supervisor returns, so the
    # classification that reads it is taken once the supervisor is gone — the
    # state a coordinator reads minutes later, and the one the record marks.
    assert runs.process_alive(supervisor_pid) is not None
    _wait_until_gone(supervisor_pid)

    exit_record = _wait_for_exit_record(attempt=1)
    assert exit_record["signal_name"] == "SIGTERM"
    assert exit_record["exit_code"] is None
    assert exit_record["ended_during"] == "working"

    # 2. Classification: the run reads interrupted, and the signal that ended it
    #    is named. The pointer carries no wait status, so the record the
    #    supervisor wrote is the only thing that can name it.
    row = recovery.classify_pointer(runs.read_pointer(RUN_ID))
    assert row["classification"] == recovery.INTERRUPTED_RUN_PHASE
    assert row["interruption"]["signal"] == 15
    assert row["interruption"]["signal_name"] == "SIGTERM"
    assert "SIGTERM" in row["detail"]

    # 3. Offered for resume: the classifier routes it to resume, and the
    #    launcher the resume verb calls builds its plan rather than refusing.
    assert row["recovery"] == "resume"
    assert "crew resume" in row["next_action"]
    config = _flight_config(bin_directory)
    resumed_plan = dispatch_module.resume_plan(RUN_ID, "continue", config=config)
    assert resumed_plan.argv

    # 4. Resume: the run's second attempt is a real supervised process whose
    #    manifest is the one promotion lands from.
    # The lane's command is a stub, and the second attempt is the one that
    # finishes the work: it writes the manifest and exits of its own accord.
    _stub_worker(bin_directory, WORKER_BINARY, RESUMED_WORKER)
    turn = resumption._resume(RUN_ID, runs.read_pointer(RUN_ID), config=config)
    second = _wait_for_exit_record(attempt=2, timeout=30.0)
    assert second["exit_code"] == 0
    assert manifest.is_file(), "the resumed worker did not write its manifest"
    assert _wait_for_phase(RUN_ID, "stopped", timeout=20.0) is not None
    assert turn["turn"] == 1

    # 5. Promotion completes from that manifest, and the ledger is the proof:
    #    the row is read back from the committed store.
    crew.complete(
        RUN_ID,
        gate="passed",
        outcome="a killed worker was classified, resumed and landed",
        root=repository,
        gate_check=_gate_check(tmp_path / "gate.log"),
        review_waiver=REVIEW_WAIVER,
    )
    row_of_ledger = _committed_row(repository)
    assert row_of_ledger["node"] == NODE_ID
    # The exit the row carries is the resumed attempt's. A promotion that landed
    # the killed attempt's record would name SIGTERM here, so this is the
    # assertion that says the work that landed is the work the resume did.
    assert row_of_ledger["worker_exit"]["exit_code"] == 0


def _wait_until_gone(pid: int, *, timeout: float = 15.0) -> None:
    """Wait until the process table no longer carries this process."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runs.process_alive(pid) is False:
            return
        time.sleep(0.05)
    raise AssertionError(f"the supervisor {pid} was still running after {timeout:g}s")


def _wait_for_phase(run_id: str, phase: str, *, timeout: float) -> dict | None:
    deadline = time.monotonic() + timeout
    record = runs.read_pointer(run_id)
    while time.monotonic() < deadline:
        record = runs.read_pointer(run_id)
        if str(record.get("phase") or "") == phase:
            return record
        time.sleep(0.05)
    return record


def _gate_check(log: Path) -> dict:
    log.write_text("the check ran\nEXIT=0\n", encoding="utf-8")
    return {
        "command": "true",
        "exit_status": 0,
        "log_path": str(log),
        "log_digest": "",
    }


def _committed_row(repository: Path) -> dict:
    data, _version = ledger.load(PROJECT, root=repository)
    row = next(
        (item for item in data["runs"] if str(item.get("run_id") or "") == RUN_ID),
        None,
    )
    assert row is not None, "promotion committed no ledger row for the run"
    return dict(row)


def _flight_config(bin_directory: Path) -> dict:
    """The lane the resumed attempt launches through, with a stub command."""
    path = f"{bin_directory}:{Path(shutil.which('sh') or '/bin/sh').parent}"
    return {
        "default_backend": BACKEND,
        "backends": {
            BACKEND: {
                "launch": "cli",
                "command": WORKER_BINARY,
                "environment": {"PATH": path},
                "time_budget": "35m",
                "session_reuse": True,
            }
        },
        "roles": {"implement": {}},
        "budget": {
            "utilisation_ceiling_pct": 100,
            "resume_reserve_pct": 5,
            "exhausted_statuses": [],
        },
        "fences": {"time_budget": "35m", "needs_help_after_failures": 2},
    }