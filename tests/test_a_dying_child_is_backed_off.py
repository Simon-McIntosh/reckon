"""A current-code child that keeps dying is backed off within one code stamp.

The guard runs a failed call again on the current code in a persistent child
behind the stdio front. The child was replaced whenever it died, with no
attempt budget: a child that exits right after every spawn therefore started
one fresh interpreter per guarded call, paying the per-call import cost the
persistent child removed, and silently, because the spawn sent the child's
stderr to DEVNULL. The server now bounds the consecutive deaths within one
stamp, serves every call from the running image once the bound is reached,
and keeps the last death's stderr in a bounded file named beside the code.

Each case runs the guard in this process against a temporary tree, with a
spawn hook that launches a child the test forces to die right after every
spawn, and the server's own spawner so the stderr file under test is the one
the product writes.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from reckon import mcp as mcp_module
from reckon import served_code

#: The dying child: it prints a marker to stderr and exits right after every
#: spawn, so every spawn is one consecutive death and the marker is what a
#: reader of the bounded stderr file is expected to find.
_DYING_CHILD = r"""
import os, sys

sys.stderr.write("dying-child-marker\n")
sys.stderr.flush()
os._exit(1)
"""


class Spawner:
    """The spawn hook a rig is built with: counts children, real stderr path.

    It launches through the server's own spawner so the file the case reads is
    the one the product writes, and only the child program is the test's.
    """

    def __init__(self) -> None:
        self.count = 0
        self.children: list[subprocess.Popen] = []

    def __call__(self, root: Path) -> subprocess.Popen:
        process = served_code.spawn_served_child(root)
        # The child dies at once; wait for it so the server's liveness check
        # sees a death on the next call rather than racing a live process.
        process.wait(timeout=10)
        self.count += 1
        self.children.append(process)
        return process


class Rig:
    """The guard under test, over a temporary tree with a child that dies."""

    def __init__(self, tmp_path: Path, monkeypatch, control: str | None):
        self.root = tmp_path / "temporary-tree"
        self.root.mkdir()
        self.spawner = Spawner()
        self.timeout_s = 0.5
        self.server = served_code.CurrentCodeServer(
            self.root, spawn=self.spawner, timeout_s=self.timeout_s
        )
        self.stamp = "1" * 64
        monkeypatch.setattr(served_code, "SERVED_CHILD_PROGRAM", _DYING_CHILD)
        monkeypatch.setattr(mcp_module, "_current_code_server", lambda: self.server)
        monkeypatch.setattr(mcp_module, "_SERVE_ON_CURRENT_CODE", set())
        monkeypatch.setattr(mcp_module, "_moved_checkout_state", self._state)
        monkeypatch.setattr(
            mcp_module,
            "_REGISTERED_TOOL_PARAMS",
            dict(mcp_module._REGISTERED_TOOL_PARAMS),
        )
        _apply_control(monkeypatch, control)

    def _state(self) -> served_code.MovedCheckout:
        return served_code.MovedCheckout(
            moved=True,
            trusted=True,
            detail="",
            running_stamp="0" * 64,
            disk_stamp=self.stamp,
        )

    def move_the_stamp(self) -> None:
        """Move the code identity the guard compares the child against."""
        self.stamp = "2" * 64

    def tool(self, name: str = "edit_plan"):
        calls: list[dict] = []

        async def inner(**kwargs):
            calls.append(kwargs)
            return {"ok": True, "served_by": "image"}

        mcp_module._REGISTERED_TOOL_PARAMS[name] = (
            "project",
            "slug",
            "expected_version",
        )
        guarded = mcp_module._refuse_when_stale(inner, name=name)
        return guarded, calls


def call(guarded, **kwargs) -> dict:
    result = asyncio.run(guarded(**kwargs))
    return result if isinstance(result, dict) else {"ok": True, "unstructured": result}


# ── the declared negative control ──────────────────────────────────────────


def _apply_control(monkeypatch, control: str | None) -> None:
    """Apply the mutation the node declares as a control, when one is named.

    The control removes the bound on consecutive child deaths, restoring the
    per-call respawn the node exists to forbid, so the spawn-count case fails
    against it and the log of that run is the negative control.
    """
    if control is None:
        return
    if control == "no-death-bound":
        original = served_code.CurrentCodeServer._child_for

        def unbounded(self, stamp):
            with self._lock:
                self._deaths = 0
                self._backed_off = False
            return original(self, stamp)

        monkeypatch.setattr(served_code.CurrentCodeServer, "_child_for", unbounded)


@pytest.fixture
def control() -> str | None:
    """The mutation this run applies, named by the driver that keeps the log."""
    return os.environ.get("RECKON_DYING_CHILD_CONTROL") or None


# ── a run of calls starts no more children than the bound, image answers ────


def test_twenty_calls_start_no_more_children_than_the_bound(
    tmp_path, monkeypatch, control
):
    """A child that dies every spawn costs the bound in imports, not one per call."""
    rig = _rig(tmp_path, monkeypatch, control)
    guarded, _ = rig.tool()

    for _ in range(20):
        result = call(guarded, project="reckon", slug="p", expected_version=1)
        assert result == {"ok": True, "served_by": "image"}, (
            f"a backed-off child leaves the call to the running image: {result!r}"
        )

    assert rig.spawner.count <= rig.server.death_bound, (
        f"twenty calls start no more children than the bound "
        f"({rig.server.death_bound}): {rig.spawner.count}"
    )
    assert rig.server.backed_off, "the server backs off once the bound is reached"
    rig.server.stop()


# ── the last death's stderr is readable ────────────────────────────────────


def test_the_last_deaths_stderr_is_readable(tmp_path, monkeypatch, control):
    """The cause of a death is on disk, and the file holds only the last one."""
    rig = _rig(tmp_path, monkeypatch, control)
    guarded, _ = rig.tool()

    for _ in range(5):
        call(guarded, project="reckon", slug="p", expected_version=1)

    path = served_code.child_stderr_path(rig.root)
    assert path.exists(), f"the child's stderr is kept at {path}"
    text = path.read_text()
    assert "dying-child-marker" in text, f"the death's cause is readable: {text!r}"
    assert text.count("dying-child-marker") == 1, (
        f"the file holds the last death only, not every death's output: {text!r}"
    )
    rig.server.stop()


# ── a stamp move resets the count and starts a fresh child ─────────────────


def test_a_stamp_move_resets_the_bound_and_starts_a_fresh_child(
    tmp_path, monkeypatch, control
):
    """The bound is per code stamp: a moved stamp clears it and respawns once."""
    rig = _rig(tmp_path, monkeypatch, control)
    guarded, _ = rig.tool()

    for _ in range(20):
        call(guarded, project="reckon", slug="p", expected_version=1)
    assert rig.server.backed_off
    backed_off_spawns = rig.spawner.count

    rig.move_the_stamp()
    call(guarded, project="reckon", slug="p", expected_version=1)

    assert rig.spawner.count == backed_off_spawns + 1, (
        f"a moved stamp starts exactly one fresh child: {rig.spawner.count}"
    )
    assert not rig.server.backed_off, "the moved stamp clears the backoff"
    rig.server.stop()


def _rig(tmp_path, monkeypatch, control) -> Rig:
    return Rig(tmp_path, monkeypatch, control)
