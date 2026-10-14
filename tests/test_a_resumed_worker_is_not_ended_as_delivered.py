"""A resumed worker is not ended as a delivery it has not made.

A run resumed after it wrote a done manifest still has that manifest on disk
when the new worker starts. The launch supervisor ends a worker whose manifest
reads a done word once it has rested, so the new worker's first edit to any
other field makes the stale delivery look fresh and the worker is ended as
though it had delivered. The reset sets the status word to a non-terminal value
before the new attempt spawns, keeping the delivered text beside it under a
name carrying the attempt it came from.

Each case drives a stub worker that edits a non-status field and then sleeps
far past any grace, so an exit within the bound can only be the supervisor's
act and never the worker's. The declared mutation removes the reset, so the
first case then finds the worker signalled and fails.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

import reckon.crew.dispatch_launch as dispatch_launch_module
import reckon.crew.dispatch_sessions as dispatch_sessions_module
import reckon.crew.resumption as resumption_module
from reckon import _backends
from reckon.crew import runs
from reckon.crew_run_commands import crew_resume
from tests import test_a_live_run_never_reads_dead as liveness

dispatch_module = importlib.import_module("reckon.crew.dispatch")

# A stub worker that leaves the status word alone and edits a non-status field,
# then sleeps far past any grace: exactly the edit the incident made.
STUB_EDIT_NONSTATUS = (
    "import os, time\n"
    "from pathlib import Path\n"
    "p = Path(os.environ['RECKON_MANIFEST'])\n"
    "trigger = Path(os.environ['RECKON_EDIT_TRIGGER'])\n"
    "while not trigger.exists():\n"
    "    time.sleep(0.02)\n"
    "text = p.read_text()\n"
    "text = text.replace('commits: []', 'commits: [abc123]')\n"
    "p.write_text(text)\n"
    "time.sleep(300)\n"
)

# A stub worker that writes complete itself and then sleeps, so the supervisor
# ends it as delivered once the quiet period passes.
STUB_WRITES_COMPLETE = (
    "import os, time\n"
    "from pathlib import Path\n"
    "Path(os.environ['RECKON_MANIFEST']).write_text(\n"
    "    'node: stub-node\\nstatus: complete\\ncommits: []\\n'\n"
    ")\n"
    "time.sleep(300)\n"
)

DECLARED_MUTATION = (
    "remove the status reset, so a resumed worker inherits the delivered "
    "complete status; the first case must then fail with the stub signalled"
)

NEGATIVE_CONTROL_ENV = "RECKON_RESUMED_STATUS_NEGATIVE_CONTROL"
NEGATIVE_CONTROL = os.environ.get(NEGATIVE_CONTROL_ENV, "").strip()

GRACE_SECONDS = 2
QUIET_SECONDS = 0.5
POLICY_SLACK_SECONDS = 8


def _control(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the declared mutation that makes the first case go red."""
    if NEGATIVE_CONTROL in {"reset-removed", "1"}:

        def _never_resets(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(
            resumption_module, "_reset_delivered_manifest_status", _never_resets
        )


def _running(pid: int | None) -> bool:
    """Whether a pid is a live (non-zombie) process."""
    if not pid:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat[stat.rindex(")") + 2 :].split()[0] != "Z"


def _worker_pid(run_id: str) -> int | None:
    path = runs.run_dir(run_id) / dispatch_launch_module.WORKER_RECORD_NAME
    try:
        return int(json.loads(path.read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _wait_until(predicate: Callable[[], Any], *, timeout: float, detail: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"{detail} not met within {timeout:g}s")


def _kill(pid: int | None) -> None:
    if not pid:
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def _drive(spec_path: Path, driver: Callable[[], None]) -> None:
    """Run the supervisor in the main thread while ``driver`` observes."""
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    thread = threading.Thread(target=driver, name="resumed-status-driver")
    thread.start()
    try:
        dispatch_module._run_supervisor(spec_path)
    finally:
        thread.join(timeout=90)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


RUN_ID = "r-resumed-status"


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str,
    stub: str = STUB_EDIT_NONSTATUS,
    trigger: Path | None = None,
) -> dict[str, Any]:
    """A live run whose manifest reads ``status``, and the seam to drive it.

    The pointer and the stub plan carry everything the door needs; the
    supervisor is captured rather than forked so its worker can be observed.
    """
    config_home = tmp_path / "config"
    (config_home / "crew").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv(
        dispatch_launch_module.TERMINAL_MANIFEST_GRACE_ENV, str(GRACE_SECONDS)
    )
    monkeypatch.setattr(
        dispatch_launch_module, "_WORKER_MANIFEST_QUIET_SECONDS", QUIET_SECONDS
    )
    monkeypatch.setattr(dispatch_launch_module, "_WORKER_MANIFEST_POLL_SECONDS", 0.2)
    monkeypatch.setattr(dispatch_launch_module, "_require_fleet_gate_open", dict)
    monkeypatch.setattr(resumption_module, "_require_fleet_gate_open", dict)
    _control(monkeypatch)

    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    manifest = directory / "manifest.md"
    manifest.write_text(
        f"node: stub-node\nstatus: {status}\ncommits: []\n", encoding="utf-8"
    )
    trigger = trigger or (tmp_path / "edit-trigger")
    stream = directory / "stream.jsonl"
    prompt = directory / "prompt.txt"
    prompt.write_text("stub prompt\n", encoding="utf-8")

    pointer: dict[str, Any] = {
        "run_id": RUN_ID,
        "project": "",
        "repo": str(worktree),
        "worktree": str(worktree),
        "backend": "alpha",
        "launch": "cli",
        "dialect": "claude",
        "phase": "working",
        "pid": None,
        "session": "coordinator-fixture",
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "stderr_path": str(directory / "stderr.log"),
        "attempt": 1,
        "attempt_kind": "dispatch",
        "attempt_started_at": "2026-10-02T00:00:00Z",
        "created_at": "2026-10-02T00:00:00Z",
        "node": {
            "id": "stub-node",
            "plan": "fixture",
            "time_budget": "20m",
            "manifest_path": str(manifest),
        },
    }
    runs._write_json(runs.pointer_path(RUN_ID), pointer)

    plan = _backends.LaunchPlan(
        backend="alpha",
        dialect="claude",
        argv=[sys.executable, "-c", stub],
        cwd=str(worktree),
        stdin_text="",
        environment={
            "RECKON_MANIFEST": str(manifest),
            "RECKON_EDIT_TRIGGER": str(trigger),
        },
        final_message_path=None,
        resumed_session=None,
    )
    monkeypatch.setattr(resumption_module, "resume_plan", lambda *a, **k: plan)

    captured: dict[str, Path] = {}

    def _capture(spec_path: Path, _run_directory: Path, _run_id: str) -> int:
        captured["spec_path"] = Path(spec_path)
        return 424242

    monkeypatch.setattr(dispatch_launch_module, "_start_supervisor", _capture)
    return {
        "run_id": RUN_ID,
        "directory": directory,
        "manifest": manifest,
        "trigger": trigger,
        "captured": captured,
    }


def _resume_through_cli(run_id: str) -> None:
    result = CliRunner().invoke(crew_resume, ["--run", run_id, "--advice", "continue"])
    assert result.exit_code == 0, result.output


def _not_signalled_case(
    fixture: dict[str, Any], *, prelaunch: Callable[[str], None]
) -> None:
    """Resume, drive the supervisor, and prove the worker is not signalled."""
    run_id = fixture["run_id"]
    manifest = fixture["manifest"]
    trigger = fixture["trigger"]
    prelaunch(run_id)
    spec_path = fixture["captured"]["spec_path"]

    # The reset ran before the spawn: the delivered manifest has been replaced
    # with an in-progress one, and the delivered text is kept beside it.
    assert manifest.read_text(encoding="utf-8").startswith(
        "node: stub-node\nstatus: in-progress\n"
    ), "the delivered status word was not reset"
    kept = sorted(fixture["directory"].glob("manifest.md.attempt-*.asdelivered"))
    assert kept, "the delivered manifest was not kept under an attempt-named copy"
    assert "status: complete" in kept[0].read_text(encoding="utf-8")

    failures: list[BaseException] = []
    observed: list[str] = []

    def driver() -> None:
        pid: int | None = None
        try:
            _wait_until(
                lambda: _worker_pid(run_id) is not None,
                timeout=15,
                detail="the stub worker's record",
            )
            pid = _worker_pid(run_id)
            # The worker now edits a non-status field. A manifest still carrying
            # its done word would be read as a fresh delivery and the worker
            # ended; one the reset made in-progress is waited on.
            trigger.write_text("go", encoding="utf-8")
            time.sleep(QUIET_SECONDS + GRACE_SECONDS + POLICY_SLACK_SECONDS)
            if _running(pid):
                observed.append("kept")
            else:
                failures.append(
                    AssertionError(
                        "a resumed worker was signalled after editing a non-status "
                        "field; the delivered status word was read as its delivery"
                    )
                )
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            failures.append(exc)
        finally:
            _kill(pid)

    _drive(spec_path, driver)
    assert failures == [], failures
    assert observed == ["kept"]


def test_a_completed_run_resumed_through_crew_resume_is_not_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch, status="complete")
    _not_signalled_case(fixture, prelaunch=_resume_through_cli)


def test_a_completed_run_resumed_through_the_launcher_is_not_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch, status="complete")

    def _resume(run_id: str) -> None:
        record = runs.read_pointer(run_id)
        resumption_module._resume(run_id, record, config=None, advice="continue")

    _not_signalled_case(fixture, prelaunch=_resume)


def test_a_resumed_stub_that_writes_complete_is_still_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A done status the new attempt writes is its delivery and ends it."""
    fixture = _fixture(
        tmp_path, monkeypatch, status="complete", stub=STUB_WRITES_COMPLETE
    )
    run_id = fixture["run_id"]

    _resume_through_cli(run_id)
    spec_path = fixture["captured"]["spec_path"]

    failures: list[BaseException] = []
    observed: list[str] = []

    def driver() -> None:
        pid: int | None = None
        try:
            _wait_until(
                lambda: _worker_pid(run_id) is not None,
                timeout=15,
                detail="the stub worker's record",
            )
            pid = _worker_pid(run_id)
            deadline = (
                time.time() + QUIET_SECONDS + GRACE_SECONDS + POLICY_SLACK_SECONDS
            )
            while time.time() < deadline:
                if not _running(pid):
                    observed.append("ended")
                    return
                time.sleep(0.05)
            failures.append(
                AssertionError(
                    "a resumed worker that wrote complete was not ended as delivered"
                )
            )
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
        finally:
            _kill(pid)

    _drive(spec_path, driver)
    assert failures == [], failures
    assert observed == ["ended"]


def test_a_blocked_manifest_keeps_its_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch, status="blocked")
    _resume_through_cli(fixture["run_id"])
    assert (
        fixture["manifest"].read_text(encoding="utf-8").splitlines()[1]
        == "status: blocked"
    ), "a blocked manifest must keep its word through a resume"
    assert not list(fixture["directory"].glob("manifest.md.attempt-*.asdelivered")), (
        "a blocked manifest is not a delivery and must not be kept as one"
    )


def test_the_live_view_classifies_the_working_stub_as_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch, status="complete")
    _resume_through_cli(fixture["run_id"])
    record = runs.read_pointer(fixture["run_id"])
    record["pid"] = os.getpid()
    record["launcher_host"] = __import__("socket").gethostname()
    verdict = importlib.import_module(
        "reckon.crew.recovery_classification"
    ).classify_pointer(record)
    assert verdict["classification"] == "running", verdict


LANE_CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
            "usable_input_window": 512_000,
        },
        "beta": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
            "usable_input_window": 512_000,
        },
    },
    "roles": {"implement": {}},
    "budget": {
        "utilisation_ceiling_pct": 100,
        "resume_reserve_pct": 5,
        "exhausted_statuses": [],
    },
    "fences": {"implement": {"time_budget": "25m"}, "needs_help_after_failures": 2},
}


def test_a_completed_run_relaunched_through_a_lane_change_is_not_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane change resets a delivered manifest before the new worker spawns."""
    home = tmp_path / "operator-home"
    for name in (
        ".claude",
        ".codex",
        ".ssh",
        ".agents",
        ".config/git",
        "public",
        ".local/bin",
    ):
        (home / name).mkdir(parents=True)
    for name in (".claude.json", ".gitconfig"):
        (home / name).write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    config_home = home / ".config" / "reckon"
    (config_home / "crew").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setattr(dispatch_launch_module, "_require_fleet_gate_open", dict)
    _control(monkeypatch)

    run_id = "r-lane-status"
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    manifest = directory / "manifest.md"
    manifest.write_text(
        "node: stub-node\nstatus: complete\ncommits: []\n", encoding="utf-8"
    )
    (directory / "prompt.txt").write_text("stub prompt\n", encoding="utf-8")
    liveness._write_exit_record(run_id)
    record = {
        "run_id": run_id,
        "project": "",
        "repo": str(worktree),
        "worktree": str(worktree),
        "launch": "cli",
        "backend": "alpha",
        "dialect": "codex",
        "role": "implement",
        "fenced": False,
        "pid": liveness._absent_pid(),
        "pid_start_time": None,
        "process_alive": None,
        "session_id": "sess-x",
        "session_harness": "codex",
        "attempt": 1,
        "base_sha": "HEAD",
        "created_at": "2026-10-01T09:59:00+00:00",
        "log_path": str(directory / "stream.jsonl"),
        "manifest_path": str(manifest),
        "prompt_path": str(directory / "prompt.txt"),
        "phase": "working",
        "node": {
            "id": "stub-node",
            "plan": "fixture",
            "time_budget": "20m",
            "manifest_path": str(manifest),
        },
    }
    runs._write_json(runs.pointer_path(run_id), record)

    plan = _backends.LaunchPlan(
        backend="beta",
        dialect="codex",
        argv=[sys.executable, "-c", STUB_EDIT_NONSTATUS],
        cwd=str(worktree),
        stdin_text="",
        environment={"RECKON_MANIFEST": str(manifest)},
        final_message_path=None,
        resumed_session=None,
    )

    resolution = SimpleNamespace(
        backend="beta",
        launch="cli",
        backend_settings=dict(LANE_CONFIG["backends"]["beta"]),
        lane_gate={
            "state": "open",
            "gate_path": None,
            "paused": False,
            "reason": None,
            "detail": "",
            "path_check": "skipped",
            "path_check_detail": "",
        },
        validation=SimpleNamespace(ok=True, findings=[]),
        competence={"allowed": True},
        authority="a-ledger-authority",
        sandbox_write_roots=None,
        route="deterministic",
        route_override=None,
    )
    monkeypatch.setattr(
        dispatch_sessions_module, "plan_dispatch", lambda **kwargs: resolution
    )
    monkeypatch.setattr(
        dispatch_sessions_module, "_budget_verdict", lambda **kwargs: {"held": False}
    )
    monkeypatch.setattr(
        dispatch_sessions_module,
        "resolve_dispatch_ledger_root",
        lambda authority: authority,
    )
    monkeypatch.setattr(
        dispatch_sessions_module,
        "_current_harness_session",
        lambda record, config=None: {
            "resolved": True,
            "session_id": "sess-x",
            "source": "pointer",
            "withheld": None,
            "detail": "",
        },
    )
    monkeypatch.setattr(
        dispatch_sessions_module, "resolve_launch_executable", lambda plan: plan
    )
    monkeypatch.setattr(
        dispatch_sessions_module, "_worker_runtime_plan", lambda plan, **kwargs: plan
    )
    monkeypatch.setattr(
        dispatch_sessions_module, "preflight_launch_command", lambda *a, **k: None
    )
    monkeypatch.setattr(
        dispatch_sessions_module,
        "_backends",
        SimpleNamespace(
            launch_plan=lambda **kwargs: plan,
            dialect_for=lambda backend: SimpleNamespace(name="codex"),
            unknown_budget=lambda detail: {"detail": detail},
            fence_unprotected_paths=lambda config=None: [],
            FENCE_BINARY="reckon-fence",
        ),
    )

    dispatch_module.change_lane(
        run_id,
        "beta",
        "the lane is spent",
        config=LANE_CONFIG,
        advice="continue",
        launch=True,
        launcher=lambda *args, **kwargs: 4242,
    )

    assert manifest.read_text(encoding="utf-8").startswith(
        "node: stub-node\nstatus: in-progress\n"
    ), "a lane change did not reset the delivered status word"
    kept = sorted(directory.glob("manifest.md.attempt-*.asdelivered"))
    assert kept, "a lane change did not keep the delivered manifest under a copy"
    assert "status: complete" in kept[0].read_text(encoding="utf-8")
