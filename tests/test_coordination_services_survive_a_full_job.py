"""The coordination services outlive the workers when the job runs out of memory.

The fleet job holds every process of the job in one memory cgroup, so the
kernel's choice of which process to kill decides whether an admission pass
survives a busy hour. These tests read that choice from processes that were
really started -- the service through the supervisor's own service mode, the
worker through dispatch's own detached spawn -- rather than from the constants
the code declares, because a constant that no call site applies would pass a
test that only compared it.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path

from reckon import _backends
from reckon.crew import dispatch_launch as dispatch_module
from reckon.crew import oom_priority

REPO_ROOT = Path(__file__).resolve().parents[1]

# A child that names the adjustment it holds. Read from ``/proc/self`` rather
# than from the module, so the reading is the kernel's answer about a process
# that exists rather than the value a caller hoped to apply.
_ADJUSTMENT_PROBE = (
    "import pathlib;print(pathlib.Path('/proc/self/oom_score_adj').read_text().strip())"
)


def _last_number(output: str) -> int:
    """The last line of a probe's stdout. A probe that printed nothing fails."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    assert lines, "the probe printed no reading at all"
    return int(lines[-1])


def _started_service(tmp_path: Path) -> int:
    """Start one declared service through the supervisor's service mode.

    The service mode takes the service's lock, applies its adjustment and execs
    into the declared argv, so the probe that runs afterwards *is* the service
    process and its reading is the service's own.
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    environ = dict(os.environ)
    environ["PYTHONPATH"] = str(REPO_ROOT)
    environ["FLEET_RUNTIME_DIR"] = str(runtime)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "reckon.crew.fleet_supervisor",
            "service",
            "probe-service",
            sys.executable,
            "-c",
            _ADJUSTMENT_PROBE,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=environ,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"the service exited {result.returncode}: {result.stderr[-2000:]}"
    )
    return _last_number(result.stdout)


def _started_worker(tmp_path: Path) -> int:
    """Start one worker through dispatch's detached spawn, then read its pid."""
    worker = tmp_path / "worker.sh"
    worker.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    worker.chmod(0o755)
    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text("", encoding="utf-8")
    pid = dispatch_module._spawn_detached_worker(
        _backends.LaunchPlan(
            backend="probe",
            dialect="",
            argv=[str(worker)],
            cwd=str(tmp_path),
            stdin_text="",
            environment={},
            final_message_path=None,
            resumed_session=None,
        ),
        log_path=tmp_path / "stream.jsonl",
        stderr_path=tmp_path / "stderr.log",
        prompt_path=prompt_path,
    )
    try:
        # The parent's own write is what is under test, so read the kernel's
        # reading for that pid rather than one the worker reported for itself.
        adjustment = oom_priority.read_adjustment(pid)
        assert adjustment is not None, f"worker {pid} holds no readable adjustment"
        return adjustment
    finally:
        with contextlib.suppress(OSError):
            os.kill(pid, 9)


def test_a_started_service_holds_a_lower_adjustment_than_a_started_worker(
    tmp_path: Path,
) -> None:
    """The deliverable: the kernel reaches a worker before a service."""
    service = _started_service(tmp_path)
    worker = _started_worker(tmp_path)
    assert service < worker, (
        "the kernel would reach the service first: "
        f"service holds {service} while a worker holds {worker}"
    )


def test_a_worker_starts_above_the_value_it_inherited(tmp_path: Path) -> None:
    """A worker's place does not depend on what the job handed down.

    A worker spawned by a process that had itself been lowered would otherwise
    inherit the service's place in the ordering. The spawn raises it above zero,
    which an unprivileged fleet is always permitted to do, so the ordering holds
    even where the service's own lowering was refused.
    """
    worker = _started_worker(tmp_path)
    assert worker >= oom_priority.WORKER_OOM_SCORE_ADJUSTMENT, (
        f"a worker started at {worker}, below the value its spawn applies"
    )
    assert worker > 0


def test_a_refused_lowering_reports_the_value_the_kernel_holds() -> None:
    """``claim`` reports the kernel's answer, not the value it asked for.

    An unprivileged process may raise its own adjustment and may not lower it.
    A test that asserted the preferred value was applied would therefore fail
    on an ordinary workstation and pass only where the capability is held; this
    one asserts that the reported value is the one the kernel holds, and that
    the refused case falls back rather than raising.
    """
    before = oom_priority.read_adjustment()
    assert before is not None, "this kernel exposes no oom_score_adj"
    try:
        applied = oom_priority.claim(oom_priority.SERVICE_OOM_SCORE_ADJUSTMENT)
        held = oom_priority.read_adjustment()
        assert applied == held, "claim reported a value the kernel does not hold"
        assert held is not None
        assert held <= before, "a lowering attempt raised the adjustment"
    finally:
        oom_priority.write_adjustment(before)
