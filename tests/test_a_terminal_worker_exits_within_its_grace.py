"""A worker whose manifest turns complete exits within a bounded grace.

A worker kept its process alive one to two hours after writing a terminal
manifest, holding its run's slot long after its work was delivered. The
supervisor notices the manifest, gives the worker the run's grace to exit on
its own, and then ends its process -- writing a sender record through the
shared signal home before it signals. A worker whose manifest is blocked (kept
for resume) or still in progress is waited on as before.

Each case drives a stub worker that writes its manifest and then sleeps far
past the grace, so an exit is the supervisor's act and never the worker's. The
declared mutation removes the grace-period exit, and the complete-manifest case
then finds the worker still alive after the grace and fails.
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
from typing import Any

import pytest

from reckon.crew import routing, runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")

# The stub worker writes the manifest its status names, then sleeps far past any
# grace the test sets, so it never exits on its own.
STUB_WORKER = (
    "import os, time\n"
    "from pathlib import Path\n"
    "manifest = Path(os.environ['RECKON_MANIFEST'])\n"
    "manifest.write_text(\n"
    "    'node: stub-node\\nstatus: ' + os.environ['RECKON_STUB_STATUS']\n"
    "    + '\\ncommits: []\\n'\n"
    ")\n"
    "time.sleep(300)\n"
)

# A stub worker for a resumed run: it writes nothing until released, so the
# manifest left by the previous attempt is the only one present while it waits,
# then it rewrites the manifest complete and sleeps past the stub's own exit.
STUB_RESUME = (
    "import os, time\n"
    "from pathlib import Path\n"
    "trigger = Path(os.environ['RECKON_REWRITE_TRIGGER'])\n"
    "while not trigger.exists():\n"
    "    time.sleep(0.02)\n"
    "Path(os.environ['RECKON_MANIFEST']).write_text(\n"
    "    'node: stub-node\\nstatus: complete\\ncommits: []\\n'\n"
    ")\n"
    "time.sleep(300)\n"
)

# The two declared mutations, verbatim: the strings the promotion audit matches
# against each red log's facts.
DECLARED_MUTATION = (
    "remove the grace-period exit; the stub worker is still alive after the "
    "grace and the test fails"
)
DECLARED_RESUME_MUTATION = (
    "drop the baseline comparison; the resumed-run case is killed and fails"
)

NEGATIVE_CONTROL = os.environ.get("RECKON_TERMINAL_GRACE_NEGATIVE_CONTROL", "").strip()

# The grace the cases run with, and the slack they allow for the supervisor's
# coarse poll and for a loaded machine. The worker's own sleep is far larger,
# so an exit within this bound can only be the supervisor's act.
GRACE_SECONDS = 2
SLACK_SECONDS = 15


def _control(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the declared mutation that makes a case go red."""
    if NEGATIVE_CONTROL in {"grace-exit", "1"}:
        # Remove the grace-period exit: the supervisor waits for the worker's
        # own exit, so a worker that lingers outlives the grace.
        def _never_reaps(pid: int, **_kwargs: Any) -> int | None:
            try:
                _, status = os.waitpid(pid, 0)
            except (ChildProcessError, OSError):
                status = None
            return status

        monkeypatch.setattr(
            dispatch_module, "_reap_worker_on_its_terminal_manifest", _never_reaps
        )
    elif NEGATIVE_CONTROL == "baseline":
        # Drop the baseline comparison: a manifest left by a previous attempt
        # is read as this attempt's delivery, so a resumed worker is killed.
        monkeypatch.setattr(
            dispatch_module, "_supervisor_manifest_baseline_ns", lambda *a, **k: 0
        )


def _running(pid: int | None) -> bool:
    """Whether a pid is a live (non-zombie) process."""
    if not pid:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    # The command name may hold spaces and parentheses; the state is the field
    # after the final ')'.
    return stat[stat.rindex(")") + 2 :].split()[0] != "Z"


def _worker_pid(run_id: str) -> int | None:
    path = runs.run_dir(run_id) / dispatch_module.WORKER_RECORD_NAME
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


def _stub_run(
    tmp_path: Path,
    *,
    status: str,
    stub: str = STUB_WORKER,
    prewrite_mtime_ns: int | None = None,
    baseline_ns: int | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A live pointer, and the spec that supervises a stub worker for it.

    ``prewrite_mtime_ns`` writes a complete manifest and stamps it with that
    mtime, modelling a resumed run whose previous attempt left a terminal
    manifest behind; ``baseline_ns`` is then recorded on the pointer as the
    generation this attempt may claim.
    """
    run_id = "r-terminal-grace"
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    manifest = tmp_path / "manifest.md"
    stream = directory / "stream.jsonl"
    prompt = directory / "prompt.txt"
    prompt.write_text("stub prompt\n", encoding="utf-8")

    if prewrite_mtime_ns is not None:
        manifest.write_text(
            "node: stub-node\nstatus: complete\ncommits: []\n", encoding="utf-8"
        )
        os.utime(manifest, ns=(prewrite_mtime_ns, prewrite_mtime_ns))
        # The baseline is the manifest's own mtime as the filesystem records
        # it, so a coarse mtime granularity cannot place it before the stamp
        # and let the stale manifest read as fresh.
        if baseline_ns is None:
            baseline_ns = manifest.stat().st_mtime_ns

    pointer: dict[str, Any] = {
        "run_id": run_id,
        "project": "terminal-grace-fixture",
        "repo": str(worktree),
        "worktree": str(worktree),
        "backend": "alpha",
        "launch": "cli",
        "dialect": "claude",
        "phase": "starting",
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
    if baseline_ns is not None:
        pointer["manifest_baseline_mtime_ns"] = baseline_ns

    runs._write_json(directory / dispatch_module.ATTEMPT_RECORD_NAME, {"attempt": 1})
    runs._write_json(runs.pointer_path(run_id), pointer)

    environment = {"RECKON_STUB_STATUS": status}
    environment.update(extra_env or {})
    spec = {
        "run_id": run_id,
        "run_directory": str(directory),
        "repo": str(worktree),
        "worktree": str(worktree),
        "fenced": False,
        "plan": {
            "argv": [sys.executable, "-c", stub],
            "cwd": str(worktree),
            "environment": environment,
            "dialect": "claude",
            "backend": "alpha",
        },
        "prompt_path": str(prompt),
        "log_path": str(stream),
        "stderr_path": str(directory / "stderr.log"),
        "attempt": 1,
        "attempt_kind": "dispatch",
        "attempt_started_at": "2026-10-02T00:00:00Z",
        "environment": {},
    }
    spec_path = directory / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return {
        "run_id": run_id,
        "directory": directory,
        "manifest": manifest,
        "spec_path": spec_path,
    }


def _drive(spec_path: Path, driver: Callable[[], None]) -> None:
    """Run the supervisor in the main thread while ``driver`` observes.

    ``_run_supervisor`` installs signal handlers, which only the main thread
    may do, so the supervisor runs here and the observations run beside it.
    """
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    thread = threading.Thread(target=driver, name="terminal-grace-driver")
    thread.start()
    try:
        dispatch_module._run_supervisor(spec_path)
    finally:
        thread.join(timeout=90)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def test_a_complete_manifest_ends_the_worker_within_the_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv(dispatch_module.TERMINAL_MANIFEST_GRACE_ENV, str(GRACE_SECONDS))
    _control(monkeypatch)

    fixture = _stub_run(tmp_path, status="complete")
    run_id = fixture["run_id"]
    manifest = fixture["manifest"]

    failures: list[BaseException] = []
    elapsed_since_write: list[float] = []

    def driver() -> None:
        pid: int | None = None
        try:
            _wait_until(
                lambda: manifest.is_file() and _worker_pid(run_id) is not None,
                timeout=15,
                detail="the stub worker's manifest and record",
            )
            pid = _worker_pid(run_id)
            written = manifest.stat().st_mtime
            deadline = written + GRACE_SECONDS + SLACK_SECONDS
            while time.time() < deadline:
                if not _running(pid):
                    elapsed_since_write.append(time.time() - written)
                    return
                time.sleep(0.05)
            failures.append(
                AssertionError(
                    "the worker was still alive after its grace; the supervisor "
                    "never ended it"
                )
            )
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            failures.append(exc)
        finally:
            # A worker the supervisor did not end (the mutation) is stopped
            # here, so the supervisor can return and the failure is reported.
            _kill(pid)

    _drive(fixture["spec_path"], driver)

    assert failures == [], failures
    assert elapsed_since_write, "the driver never observed the worker's exit"
    assert elapsed_since_write[0] <= GRACE_SECONDS + SLACK_SECONDS
    assert not _running(_worker_pid(run_id))

    # The end was attributable: the run's own directory names the sender, the
    # reason and the target that the grace signal reached.
    records = [
        json.loads(line)
        for line in (fixture["directory"] / routing.SENDER_RECORD_NAME)
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    assert records, "the grace signal left no sender record"
    assert records[-1]["reason"] == "worker-lingered-after-terminal-manifest"


def _keeps_running_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, status: str
) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv(dispatch_module.TERMINAL_MANIFEST_GRACE_ENV, str(GRACE_SECONDS))
    _control(monkeypatch)

    fixture = _stub_run(tmp_path, status=status)
    run_id = fixture["run_id"]
    manifest = fixture["manifest"]

    failures: list[BaseException] = []
    observed: list[str] = []

    def driver() -> None:
        pid: int | None = None
        try:
            _wait_until(
                lambda: manifest.is_file() and _worker_pid(run_id) is not None,
                timeout=15,
                detail="the stub worker's manifest and record",
            )
            pid = _worker_pid(run_id)
            # Wait past the grace, then confirm the worker the supervisor keeps
            # for a non-reap status is still running.
            time.sleep(GRACE_SECONDS + 3)
            if _running(pid):
                observed.append("running")
            else:
                failures.append(
                    AssertionError(
                        f"a {status} manifest must keep the worker running, "
                        "but it had exited"
                    )
                )
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            failures.append(exc)
        finally:
            # The supervisor is waiting on this worker, so ending it lets the
            # supervisor return.
            _kill(pid)

    _drive(fixture["spec_path"], driver)

    assert failures == [], failures
    assert observed == ["running"]


def test_a_blocked_manifest_keeps_the_worker_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keeps_running_case(tmp_path, monkeypatch, status="blocked")


def test_an_in_progress_manifest_keeps_the_worker_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keeps_running_case(tmp_path, monkeypatch, status="in-progress")


def test_a_manifest_left_by_a_previous_attempt_is_not_this_attempts_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed run is not killed on its own previous attempt's manifest.

    A run resumed after it wrote complete — the repair reflex does this to
    answer a review — still has that complete manifest on disk when the new
    worker starts. The supervisor must not read it as this attempt's delivery
    and end the worker on its first poll; only a manifest written after the
    attempt began counts.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv(dispatch_module.TERMINAL_MANIFEST_GRACE_ENV, str(GRACE_SECONDS))
    _control(monkeypatch)

    stale_ns = time.time_ns() - 60 * 1_000_000_000
    trigger = tmp_path / "rewrite"
    fixture = _stub_run(
        tmp_path,
        status="complete",
        stub=STUB_RESUME,
        prewrite_mtime_ns=stale_ns,
        extra_env={"RECKON_REWRITE_TRIGGER": str(trigger)},
    )
    run_id = fixture["run_id"]

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
            # The stale complete manifest must not end the worker: wait past
            # the grace and its coarse poll, then confirm it is still running.
            time.sleep(GRACE_SECONDS + 6)
            if not _running(pid):
                failures.append(
                    AssertionError(
                        "a complete manifest left by a previous attempt was read "
                        "as this attempt's delivery; the resumed worker was killed"
                    )
                )
                return
            observed.append("kept")
            # Now the worker rewrites the manifest complete; it must end within
            # the grace measured from that rewrite.
            trigger.write_text("go", encoding="utf-8")
            deadline = time.time() + GRACE_SECONDS + SLACK_SECONDS
            while time.time() < deadline:
                if not _running(pid):
                    observed.append("ended")
                    return
                time.sleep(0.05)
            failures.append(
                AssertionError(
                    "the resumed worker was not ended within the grace after it "
                    "rewrote the manifest complete"
                )
            )
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            failures.append(exc)
        finally:
            _kill(pid)

    _drive(fixture["spec_path"], driver)

    assert failures == [], failures
    assert observed == ["kept", "ended"]


def test_the_done_status_set_excludes_blocked_and_non_terminal(tmp_path: Path) -> None:
    """Only complete and failed are done; blocked is kept for resume."""
    complete = tmp_path / "complete.md"
    complete.write_text("status: complete\n", encoding="utf-8")
    failed = tmp_path / "failed.md"
    failed.write_text("status: failed\n", encoding="utf-8")
    blocked = tmp_path / "blocked.md"
    blocked.write_text("status: blocked\n", encoding="utf-8")
    in_progress = tmp_path / "in-progress.md"
    in_progress.write_text("status: in-progress\n", encoding="utf-8")
    template = tmp_path / "template.md"
    template.write_text("status: complete | blocked | failed\n", encoding="utf-8")

    assert dispatch_module._worker_manifest_done_status(complete) == "complete"
    assert dispatch_module._worker_manifest_done_status(failed) == "failed"
    assert dispatch_module._worker_manifest_done_status(blocked) == ""
    assert dispatch_module._worker_manifest_done_status(in_progress) == ""
    assert dispatch_module._worker_manifest_done_status(template) == ""
    assert dispatch_module._worker_manifest_done_status(tmp_path / "absent.md") == ""
