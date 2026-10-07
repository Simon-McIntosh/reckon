"""The session host plugin monitor waits silently until its first request.

The entry point is a shell script Claude Code starts once per session. These
tests drive it in a temporary checkout whose ``.venv/bin/reckon`` is a recording
stub, so nothing on the machine is touched and the exec target is observable.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugins" / "crew-host"
ENTRY_REL = Path("plugins") / "crew-host" / "bin" / "crew-host"

# The one shape Claude Code reads at this path is a bare array of monitor
# entries; every key an entry may carry is in this set.
ALLOWED_MONITOR_KEYS = {"name", "command", "description", "when"}

STUB = """#!/usr/bin/env bash
{
  printf 'arg:%s\\n' "$@"
  fd=""
  prev=""
  for a in "$@"; do
    if [ "$prev" = "--fd" ]; then fd="$a"; fi
    prev="$a"
  done
  if [ -n "$fd" ]; then
    printf 'fd:%s\\n' "$(readlink "/proc/self/fd/$fd" 2>/dev/null)"
  fi
} > "$STUB_RECORD"
exit 0
"""


def _start_owner() -> subprocess.Popen:
    return subprocess.Popen(["sleep", "300"])


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _proc_start(pid: int) -> str:
    """Field 22 of /proc/<pid>/stat, the start time in clock ticks."""
    raw = Path(f"/proc/{pid}/stat").read_bytes()
    fields = raw[raw.rindex(b")") + 2 :].split()
    return fields[19].decode()


def _checkout(tmp_path: Path) -> Path:
    """A temporary checkout holding the plugin and a recording reckon stub."""
    root = tmp_path / "checkout"
    (root / "plugins").mkdir(parents=True)
    subprocess.run(
        ["cp", "-r", str(PLUGIN), str(root / "plugins" / "crew-host")], check=True
    )
    (root / ENTRY_REL).chmod(0o755)
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    stub = venv_bin / "reckon"
    stub.write_text(STUB)
    stub.chmod(0o755)
    return root


def _launch(root: Path, tmp_path: Path, owner_pid: int):
    record = tmp_path / "record.txt"
    runtime = tmp_path / "run"
    runtime.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "CLAUDE_PID": str(owner_pid),
        "XDG_RUNTIME_DIR": str(runtime),
        "RECKON_SESSION_HOST_INTERVAL": "1",
        "STUB_RECORD": str(record),
    }
    proc = subprocess.Popen([str(root / ENTRY_REL)], stdout=subprocess.PIPE, env=env)
    return proc, runtime, record


def _fifo(runtime: Path, pid: int, start: str) -> Path:
    return runtime / "reckon-session-host" / f"{pid}-{start}.fifo"


def _await(condition, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def _value(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_manifest_declares_one_always_armed_monitor() -> None:
    monitors = json.loads((PLUGIN / "monitors" / "monitors.json").read_text())
    # The default path carries a bare array of entries, not an object wrapping
    # them: Claude Code's loader validates the file against an array schema and
    # refuses the whole component when it reads an object instead. See
    # test_plugin_directory_loads_without_errors for the loader's own verdict.
    assert isinstance(monitors, list), "monitors.json must be a bare JSON array"
    assert len(monitors) == 1
    monitor = monitors[0]
    assert set(monitor) <= ALLOWED_MONITOR_KEYS
    assert monitor["when"] == "always"
    assert monitor["description"] == "reckon crew"
    assert "bin/crew-host" in monitor["command"]

    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "reckon-crew-host"


requires_claude = pytest.mark.skipif(
    shutil.which("claude") is None, reason="no claude executable on PATH"
)


@requires_claude
def test_plugin_directory_loads_without_errors() -> None:
    """Claude Code itself loads the plugin directory and reports no errors.

    The shape assertions above are ours; this is the loader's own verdict on
    the manifest and every component under it, which is what a malformed
    monitors file actually breaks.
    """
    result = subprocess.run(
        ["claude", "--plugin-dir", str(PLUGIN), "plugin", "list", "--json"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    entries = json.loads(result.stdout)
    entry = next(e for e in entries if e["id"].startswith("reckon-crew-host"))
    assert entry.get("errors", []) == [], entry.get("errors")


def test_waits_silently_on_a_fifo_named_for_its_owner(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    owner = _start_owner()
    try:
        proc, runtime, _ = _launch(root, tmp_path, owner.pid)
        try:
            fifo = _fifo(runtime, owner.pid, _proc_start(owner.pid))
            assert _await(fifo.exists), f"FIFO never appeared at {fifo}"
            assert fifo.is_fifo()

            # Stays silent while it waits: nothing on stdout, process still alive.
            assert select.select([proc.stdout], [], [], 0.5)[0] == []
            assert proc.poll() is None
        finally:
            _stop(proc)
    finally:
        _stop(owner)


def test_first_request_line_becomes_the_host_command(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    owner = _start_owner()
    try:
        proc, runtime, record = _launch(root, tmp_path, owner.pid)
        try:
            start = _proc_start(owner.pid)
            fifo = _fifo(runtime, owner.pid, start)
            assert _await(fifo.exists), f"FIFO never appeared at {fifo}"

            line = "dispatch the follow"
            with open(fifo, "w") as fh:
                fh.write(line + "\n")

            assert _await(record.exists), "the recording stub never ran"
            proc.wait(timeout=5)
            assert proc.returncode == 0
        finally:
            _stop(proc)

        lines = record.read_text().splitlines()
        args = [ln[len("arg:") :] for ln in lines if ln.startswith("arg:")]
        fd_target = next(
            (ln[len("fd:") :] for ln in lines if ln.startswith("fd:")), None
        )

        assert args[:2] == ["crew", "host"]
        assert _value(args, "--owner-pid") == str(owner.pid)
        assert _value(args, "--owner-start") == start
        assert _value(args, "--fd") == "3"
        assert _value(args, "--first-request") == line
        assert fd_target is not None, "the inherited FIFO descriptor was not open"
        assert Path(fd_target) == fifo
    finally:
        _stop(owner)


def test_exits_within_one_interval_after_its_owner_exits(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    owner = _start_owner()
    proc, runtime, _ = _launch(root, tmp_path, owner.pid)
    try:
        fifo = _fifo(runtime, owner.pid, _proc_start(owner.pid))
        assert _await(fifo.exists), f"FIFO never appeared at {fifo}"

        os.kill(owner.pid, signal.SIGKILL)
        owner.wait(timeout=5)

        # One wait interval is one second here; allow generous slack for a slow
        # timeout fenced by the process start and the exec.
        assert proc.wait(timeout=8) == 0
        assert proc.stdout.read() == b""
    finally:
        _stop(proc)
        _stop(owner)


def test_removes_its_fifo_when_its_owner_exits(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    owner = _start_owner()
    proc, runtime, _ = _launch(root, tmp_path, owner.pid)
    try:
        fifo = _fifo(runtime, owner.pid, _proc_start(owner.pid))
        assert _await(fifo.exists), f"FIFO never appeared at {fifo}"

        os.kill(owner.pid, signal.SIGKILL)
        owner.wait(timeout=5)

        assert proc.wait(timeout=8) == 0
        assert not fifo.exists(), "the entry point left its FIFO behind"
    finally:
        _stop(proc)
        _stop(owner)
