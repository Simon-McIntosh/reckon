"""A warm child serves every call on a moved checkout, not only the failed ones.

The guard used to answer on the current code only after a call had failed: a
call that still worked returned the running image's answer, which after a
landing is behaviour the tree no longer has, and nothing in the answer said so.
Once the checkout has moved and can be trusted and the child has answered its
readiness probe, every call of every tool is served by the child, so the answer
carries ``served_by_code_stamp``. Two failures escaped the guard entirely and
are answered here as well: a call that raised while the child was still cold
became a raw exception with neither stamp nor remedy, and a schema or
configuration failure embedded in an otherwise successful answer — the
``review_owed_error`` an ``edit_plan`` write reports — was never read as a
failure at all.

An unmoved checkout is the control: it serves in process and starts no child.

Each case runs the guard in this process against a temporary tree, with a fake
child whose probe answer, tool answer and liveness the test controls.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from reckon import mcp as mcp_module
from reckon import served_code

#: The fake child. It answers the readiness probe unless it is told to stay
#: cold, and answers every tool call with the configured answer, so a case can
#: say what the current code would say without importing a tree.
_FAKE_CHILD = r"""
import json, os, sys

cold = os.environ.get("FAKE_CHILD_COLD") == "1"
answer = json.loads(os.environ["FAKE_CHILD_ANSWER"])

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        message = json.loads(line)
    except ValueError:
        continue
    ident = message.get("id")
    if message.get("op") == "probe":
        if cold:
            # A child that never answers its probe stays cold for the life of
            # the process, so every call is served by the running image.
            continue
        reply = {"id": ident, "op": "probe", "params": {}, "required": {}}
    else:
        reply = {"id": ident, "op": "call", "reached": True, "answer": answer}
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
"""


class Spawner:
    """The spawn hook a rig is built with: counts children, starts the fake."""

    def __init__(self, *, answer: dict | None = None, cold: bool = False) -> None:
        self.count = 0
        self.children: list[subprocess.Popen] = []
        self.answer = json.dumps(
            {"ok": True, "served_by": "current code"} if answer is None else answer
        )
        self.cold = cold

    def __call__(self, root: Path) -> subprocess.Popen:
        self.count += 1
        environment = {
            **os.environ,
            "FAKE_CHILD_ANSWER": self.answer,
            "FAKE_CHILD_COLD": "1" if self.cold else "0",
        }
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


#: The error an ``edit_plan`` answer reports in ``review_owed_error`` when the
#: running image's classes cannot validate config the tree has moved past.
_SCHEMA_ERROR = (
    "ValidationError: 1 validation error for FlightConfig\n"
    "budget.advisory_pace_groups\n  Extra inputs are not permitted"
)

#: The error the same field carries when the failure is the call's own and no
#: code change would answer it.
_UNRELATED_ERROR = "OSError: fixture could not be read"


class Rig:
    """The guard under test, over a temporary tree with a controllable child."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch,
        *,
        answer: dict | None = None,
        cold: bool = False,
        moved: bool = True,
        timeout_s: float = 1.0,
    ) -> None:
        self.root = tmp_path / "temporary-tree"
        self.root.mkdir()
        self.spawner = Spawner(answer=answer, cold=cold)
        self.server = served_code.CurrentCodeServer(
            self.root, spawn=self.spawner, timeout_s=timeout_s
        )
        self.stamp = "1" * 64
        self.moved = moved
        monkeypatch.setattr(mcp_module, "_current_code_server", lambda: self.server)
        monkeypatch.setattr(mcp_module, "_moved_checkout_state", self._state)
        monkeypatch.setattr(mcp_module, "_SERVE_ON_CURRENT_CODE", set())
        monkeypatch.setattr(
            mcp_module,
            "_REGISTERED_TOOL_PARAMS",
            dict(mcp_module._REGISTERED_TOOL_PARAMS),
        )

    def _state(self) -> served_code.MovedCheckout:
        return served_code.MovedCheckout(
            moved=self.moved,
            trusted=True,
            detail="",
            running_stamp="0" * 64,
            disk_stamp=self.stamp,
        )

    def tool(
        self,
        name: str = "roadmap",
        *,
        answer: dict | None = None,
        raises: BaseException | None = None,
    ):
        """One guarded tool, whose image answer and calls this case can read."""
        calls: list[dict] = []

        async def inner(**kwargs):
            calls.append(kwargs)
            if raises is not None:
                raise raises
            return (
                dict(answer)
                if answer is not None
                else {"ok": True, "served_by": "current code"}
            )

        mcp_module._REGISTERED_TOOL_PARAMS[name] = ("project",)
        guarded = mcp_module._refuse_when_stale(inner, name=name)
        return guarded, calls

    def warm(self, *, seconds: float = 8.0) -> bool:
        """Run a probe until the child has answered it."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.server.warm(self.stamp) is not None:
                return True
            time.sleep(0.02)
        return False


def call(guarded, **kwargs) -> dict:
    result = asyncio.run(guarded(**kwargs))
    return result if isinstance(result, dict) else {"ok": True, "unstructured": result}


# ── a call that never failed is served by the warm child ───────────────────


def test_a_successful_call_is_served_by_the_current_code(tmp_path, monkeypatch):
    """A tool that never failed runs on the current code once the checkout moved."""
    rig = Rig(tmp_path, monkeypatch)
    guarded, image = rig.tool("roadmap", answer={"ok": True, "served_by": "the image"})
    try:
        assert rig.warm(), "the child must answer its probe"
        result = call(guarded, project="reckon")

        assert result.get("served_by") == "current code", (
            f"a successful call on a moved checkout is served by the child: {result!r}"
        )
        assert result.get("served_by_code_stamp") == rig.stamp, (
            f"the answer carries the stamp of the code that served it: {result!r}"
        )
        assert image == [], (
            f"the image never answers a call the warm child serves: {image!r}"
        )
    finally:
        rig.server.stop()


def test_an_unmoved_checkout_serves_in_process(tmp_path, monkeypatch):
    """The control: a checkout that has not moved starts no child and answers here."""
    rig = Rig(tmp_path, monkeypatch, moved=False)
    guarded, image = rig.tool("roadmap", answer={"ok": True, "served_by": "the image"})
    try:
        result = call(guarded, project="reckon")

        assert result.get("served_by") == "the image", result
        assert "served_by_code_stamp" not in json.dumps(result), result
        assert image == [{"project": "reckon"}], image
    finally:
        rig.server.stop()


# ── a raised failure while the child is cold ───────────────────────────────


def test_a_raised_failure_while_the_child_is_cold_carries_the_remedy(
    tmp_path, monkeypatch
):
    """A cold child leaves a raised failure structured, stamped and remedied."""
    rig = Rig(tmp_path, monkeypatch, cold=True, timeout_s=0.3)
    guarded, image = rig.tool("roadmap", raises=RuntimeError("config unreadable"))
    try:
        result = call(guarded, project="reckon")

        assert image, "the running image serves a call made while the child is cold"
        assert result.get("ok") is False, (
            f"a raised failure is answered, not re-raised raw: {result!r}"
        )
        assert result.get("code_stamp_at_start") != result.get("code_stamp_now"), result
        assert mcp_module._RECONNECT_REMEDY in result.get("detail", ""), result
        assert result.get("reconnect") == mcp_module._RECONNECT_REMEDY, result
        assert "RuntimeError" in result.get("original_error", ""), result
    finally:
        rig.server.stop()


# ── a schema failure embedded in a successful answer ───────────────────────


def test_an_embedded_schema_failure_is_answered_by_the_current_code(
    tmp_path, monkeypatch
):
    """An edit_plan answer whose review_owed_error names a schema failure is re-run."""
    rig = Rig(tmp_path, monkeypatch)
    guarded, image = rig.tool(
        "edit_plan",
        answer={"ok": True, "review_owed": None, "review_owed_error": _SCHEMA_ERROR},
    )
    try:
        assert rig.warm(), "the child must answer its probe"
        result = call(guarded, project="reckon")

        assert result.get("served_by") == "current code", (
            f"a schema failure beside a successful write is the guard's own case: "
            f"{result!r}"
        )
        assert result.get("served_by_code_stamp") == rig.stamp, result
        assert image, "the image's own answer is what carries the embedded error"
    finally:
        rig.server.stop()


def test_an_embedded_error_that_is_not_a_schema_failure_stands(tmp_path, monkeypatch):
    """An error no code change would answer leaves the call's own result alone."""
    rig = Rig(tmp_path, monkeypatch, cold=True, timeout_s=0.3)
    guarded, _ = rig.tool(
        "edit_plan",
        answer={"ok": True, "review_owed": None, "review_owed_error": _UNRELATED_ERROR},
    )
    try:
        result = call(guarded, project="reckon")

        assert result.get("review_owed_error") == _UNRELATED_ERROR, result
        assert "served_by_code_stamp" not in json.dumps(result), result
    finally:
        rig.server.stop()
