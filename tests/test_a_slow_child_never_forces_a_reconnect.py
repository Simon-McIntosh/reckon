"""A slow current-code child never turns a retryable failure into a reconnect.

The guard runs a failed call again on the current code in a child behind the
stdio front. When that child was restarted for every call, and the caller kept
the reconnect refusal whenever the child made no answer, a loaded node turned
every failure -- a version conflict whose remedy is to re-read, a storage
time. It now serves the tree's current code from one persistent child per code
stamp, warmed by a readiness probe rather than by a caller's call, and a call
the child does not answer keeps its own result.

Each case runs the guard in this process against a temporary tree, with a spawn
hook that launches a fake child the test can slow, silence or stop, monkeypatch
by the control named in the node's declared mutation.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon import mcp as mcp_module
from reckon import served_code

#: The fake child. It re-reads its config for every request, so a test can slow,
#: silence or kill a tool between calls, and it records every tool call it was
#: sent, which is what shows that warming used the probe and not a call.
_FAKE_CHILD = r"""
import json, os, sys, time

config_path = os.environ["FAKE_CHILD_CONFIG"]


def config():
    try:
        with open(config_path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def record(name):
    path = config().get("log")
    if path:
        with open(path, "a") as handle:
            handle.write(name + "\n")


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    settings = config()
    try:
        message = json.loads(line)
    except ValueError:
        continue
    ident = message.get("id")
    if message.get("op") == "probe":
        delay = float(settings.get("probe_delay") or 0)
        if delay:
            time.sleep(delay)
        sys.stdout.write(json.dumps({
            "id": ident, "op": "probe", "params": settings.get("params") or {},
        }) + "\n")
        sys.stdout.flush()
        continue
    tool = message.get("tool")
    record(tool)
    behaviour = (settings.get("tools") or {}).get(tool, "answer")
    if behaviour == "silent":
        continue
    if behaviour == "die":
        os._exit(1)
    answer = (settings.get("answers") or {}).get(tool)
    if answer is None:
        answer = {"ok": True, "served_by": "current code"}
    sys.stdout.write(json.dumps({
        "id": ident, "op": "call", "reached": True, "answer": answer,
    }) + "\n")
    sys.stdout.flush()
"""


class Spawner:
    """The spawn hook a rig is built with: counts children and starts the fake."""

    def __init__(self, tmp_path: Path, **config: object) -> None:
        self.path = tmp_path / "child-config.json"
        self.log = tmp_path / "child-calls.log"
        self.count = 0
        self.children: list[subprocess.Popen] = []
        self.config = {"log": str(self.log), **config}
        self.write()

    def write(self) -> None:
        self.path.write_text(json.dumps(self.config))

    def __call__(self, root: Path) -> subprocess.Popen:
        self.count += 1
        environment = {**os.environ, "FAKE_CHILD_CONFIG": str(self.path)}
        process = subprocess.Popen(
            [sys.executable, "-c", _FAKE_CHILD],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=environment,
        )
        self.children.append(process)
        return process

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text().splitlines() if line]


class Rig:
    """The guard under test, over a temporary tree with a controllable child."""

    def __init__(self, tmp_path: Path, monkeypatch, control: str | None, **config):
        self.root = tmp_path / "temporary-tree"
        self.root.mkdir()
        self.spawner = Spawner(tmp_path, **config)
        self.timeout_s = float(config.get("timeout_s", 0.6))
        self.server = served_code.CurrentCodeServer(
            self.root, spawn=self.spawner, timeout_s=self.timeout_s
        )
        self.stamp = "1" * 64
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

    def tool(
        self,
        name: str = "edit_plan",
        *,
        params: tuple[str, ...] = ("project", "slug", "expected_version"),
        answer: dict | None = None,
    ):
        calls: list[dict] = []

        async def inner(**kwargs):
            calls.append(kwargs)
            return (
                dict(answer)
                if answer is not None
                else {"ok": True, "served_by": "image"}
            )

        mcp_module._REGISTERED_TOOL_PARAMS[name] = params
        guarded = mcp_module._refuse_when_stale(inner, name=name)
        return guarded, calls

    def warm(self, *, seconds: float = 8.0) -> bool:
        """Run a guarded call until the child has answered its probe."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.server.warm(self.stamp) is not None:
                return True
            time.sleep(0.02)
        return False


def image_run(inner):
    return inner


def call(guarded, **kwargs) -> dict:
    result = asyncio.run(guarded(**kwargs))
    return result if isinstance(result, dict) else {"ok": True, "unstructured": result}


# ── the declared negative controls ─────────────────────────────────────────


def _apply_control(monkeypatch, control: str | None) -> None:
    """Apply the mutation the node declares as a control, when one is named.

    Each control restores one behaviour the case exists to forbid, so the case
    fails against it and the log of that run is the negative control.
    """
    if control is None:
        return
    if control == "refusal-on-unanswered":

        def refusal(failure, state):
            return {
                "ok": False,
                "error": "stale_server_code",
                "reconnect": "reconnect the reckon MCP server",
                "detail": "This call failed on the current code too.",
            }

        monkeypatch.setattr(mcp_module, "_unanswered_on_current_code", refusal)
    elif control == "child-per-call":
        original = served_code.CurrentCodeServer._child_for

        def every_call(self, stamp):
            with self._lock:
                self._stamp = None
            return original(self, stamp)

        monkeypatch.setattr(served_code.CurrentCodeServer, "_child_for", every_call)
    elif control == "cold-call-waits":

        def waits(self, stamp):
            child = self._child_for(stamp)
            if child is None:
                return None
            answer = child._await(child._probe_id, 30.0)
            return answer if isinstance(answer, dict) else None

        monkeypatch.setattr(served_code.CurrentCodeServer, "warm", waits)
    elif control == "ignore-probe-params":
        monkeypatch.setattr(
            mcp_module, "_probe_signature_changed", lambda name, probe: False
        )
    elif control == "child-outlives-pipe":
        monkeypatch.setattr(served_code.ServedChild, "stop", lambda self: None)


def _wrap_routed(guarded, inner, control: str | None):
    """The two controls that mutate a routed call, which live on the callable."""
    if control not in {"image-and-child", "rerun-write-on-image"}:
        return guarded

    async def wrapped(**kwargs):
        result = await guarded(**kwargs)
        if not isinstance(result, dict):
            return result
        if control == "image-and-child" or (
            result.get("error") == mcp_module.WRITE_MAY_HAVE_LANDED_ERROR
        ):
            await inner(**kwargs)
        return result

    return wrapped


@pytest.fixture
def control() -> str | None:
    """The mutation this run applies, named by the driver that keeps the log."""
    return os.environ.get("RECKON_SLOW_CHILD_CONTROL") or None


def _rig(tmp_path, monkeypatch, control: str | None, **config) -> Rig:
    return Rig(tmp_path, monkeypatch, control, **config)


# ── case 1: an unanswered child leaves the failure alone ───────────────────


def test_a_version_conflict_survives_an_unanswered_child(
    tmp_path, monkeypatch, control
):
    """A retryable failure never becomes a reconnect because the child was slow."""
    rig = _rig(tmp_path, monkeypatch, control, tools={"edit_plan": "silent"})
    guarded, _ = rig.tool(
        "edit_plan", answer={"ok": False, "error": "version_conflict"}
    )

    assert rig.warm(), "the child must answer its probe"
    result = call(guarded, project="reckon", slug="p", expected_version=1)

    assert result.get("error") == "version_conflict", (
        f"a slow child must not replace the call's own result: {result!r}"
    )
    assert result.get("code_stamp_at_start") != result.get("code_stamp_now"), result
    assert "reconnect" not in result, (
        f"an unanswered child must not force a reconnect: {result!r}"
    )
    assert "stale_server_code" not in json.dumps(result), result


# ── case 2: one child per code stamp ───────────────────────────────────────


def test_twenty_calls_start_one_child_and_a_moved_stamp_starts_one_more(
    tmp_path, monkeypatch, control
):
    """A session pays one import per commit, not one per call."""
    rig = _rig(tmp_path, monkeypatch, control)
    guarded, _ = rig.tool("edit_plan")
    guarded = _wrap_routed(guarded, None, control)

    for _ in range(20):
        call(guarded, project="reckon", slug="p", expected_version=1)
    assert rig.spawner.count == 1, (
        f"twenty calls after the stamp moved start one child: {rig.spawner.count}"
    )

    first = rig.spawner.children[0]
    rig.move_the_stamp()
    call(guarded, project="reckon", slug="p", expected_version=1)
    assert rig.spawner.count == 2, (
        f"a further stamp move starts exactly one more child: {rig.spawner.count}"
    )
    deadline = time.monotonic() + 5.0
    while first.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert first.poll() is not None, "the child for the old stamp is stopped"


# ── case 3: a cold call does not wait ──────────────────────────────────────


def test_a_cold_call_is_served_by_the_image_without_waiting(
    tmp_path, monkeypatch, control
):
    """The child is warmed by its probe, never by a caller's call."""
    rig = _rig(tmp_path, monkeypatch, control, probe_delay=1.2)
    guarded, image = rig.tool(
        "edit_plan", answer={"ok": False, "error": "version_conflict"}
    )
    guarded = _wrap_routed(guarded, image, control)

    started = time.monotonic()
    result = call(guarded, project="reckon", slug="p", expected_version=1)
    elapsed = time.monotonic() - started
    assert result is not None
    assert len(image) == 1, "the running image serves a cold call"
    assert elapsed < 0.8, f"a cold call never waits on the child: {elapsed:.2f}s"

    assert rig.warm(), "the child turns warm on its probe answer"
    assert rig.spawner.calls() == [], (
        f"no tool call is sent to warm the child: {rig.spawner.calls()}"
    )


# ── case 4: a routed call runs once, in the child ──────────────────────────


def test_a_routed_call_runs_once_in_the_child(tmp_path, monkeypatch, control):
    """A tool that has fallen back runs in the child, and not on the image."""
    rig = _rig(tmp_path, monkeypatch, control)
    guarded, image = rig.tool(
        "edit_plan", answer={"ok": False, "error": "version_conflict"}
    )
    guarded = _wrap_routed(guarded, image, control)

    assert rig.warm()
    first = call(guarded, project="reckon", slug="p", expected_version=1)
    assert first.get("served_by") == "current code", first
    ran_on_image = len(image)

    second = call(guarded, project="reckon", slug="p", expected_version=1)
    assert second.get("served_by") == "current code", second
    assert len(image) == ran_on_image, (
        f"a routed call does not run on the image too: {len(image)} image calls"
    )
    assert rig.spawner.calls() == ["edit_plan", "edit_plan"], rig.spawner.calls()


# ── case 5: a routed write is never re-run on the image ────────────────────


def test_a_routed_write_the_child_does_not_answer_may_have_landed(
    tmp_path, monkeypatch, control
):
    """A write with no answer is never doubled on the image."""
    rig = _rig(tmp_path, monkeypatch, control)
    guarded, image = rig.tool(
        "edit_plan", answer={"ok": False, "error": "version_conflict"}
    )
    guarded = _wrap_routed(guarded, image, control)

    assert rig.warm()
    call(guarded, project="reckon", slug="p", expected_version=1)
    ran_on_image = len(image)

    rig.spawner.config["tools"] = {"edit_plan": "silent"}
    rig.spawner.write()
    result = call(guarded, project="reckon", slug="p", expected_version=1)

    assert result.get("error") == mcp_module.WRITE_MAY_HAVE_LANDED_ERROR, (
        f"a routed write with no answer names the re-read to make: {result!r}"
    )
    assert len(image) == ran_on_image, (
        f"a routed write is never re-run on the image: {len(image)} image calls"
    )


# ── case 6: a changed tool signature still owes a reconnect ────────────────


def test_a_changed_tool_signature_still_owes_a_reconnect(
    tmp_path, monkeypatch, control
):
    """The one refusal left: the client holds the schema it connected with."""
    rig = _rig(
        tmp_path,
        monkeypatch,
        control,
        params={
            "edit_plan": ["expected_version", "project", "slug", "added_parameter"]
        },
    )
    guarded, _ = rig.tool("edit_plan", answer={"ok": True})
    guarded = _wrap_routed(guarded, None, control)

    assert rig.warm()
    result = call(guarded, project="reckon", slug="p", expected_version=1)
    assert result.get("error") == "stale_server_code", result
    assert result.get("tool") == "edit_plan", result
    assert "reconnect" in json.dumps(result).lower(), result


# ── case 7: closing the pipe ends the child ────────────────────────────────


def test_closing_the_pipe_ends_the_child(tmp_path, monkeypatch, control):
    """The child ends when the server ends: it exits on EOF on its stdin."""
    rig = _rig(tmp_path, monkeypatch, control)
    guarded, _ = rig.tool("edit_plan")
    guarded = _wrap_routed(guarded, None, control)

    call(guarded, project="reckon", slug="p", expected_version=1)
    child = rig.spawner.children[0]
    assert child.poll() is None, "the child is running while the server is"

    rig.server.stop()
    deadline = time.monotonic() + 5.0
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert child.poll() is not None, "closing the pipe ends the child"
