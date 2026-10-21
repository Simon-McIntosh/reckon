"""A warm child serves every call on a moved checkout, not only the failed ones.

A call answered by the running image returns behaviour the tree no longer has,
and nothing in that answer says so. Once the checkout has moved and can be
trusted and the child has answered its
readiness probe, every call of every tool is served by the child — writes
included, from a tool's first call — so the answer carries
``served_by_code_stamp``. Two failures escaped the guard entirely and are
answered here as well: a call that raised while the child was still cold became
a raw exception with neither stamp nor remedy, and a schema or configuration
failure embedded in an otherwise successful answer — the ``review_owed_error``
an ``edit_plan`` write reports — was passed on as that answer. The embedded
read is a named list of fields carrying a sub-computation's exception, matched
on the leading exception type alone, so an error of the call's own in one of
those fields, or any error outside them, stands.

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

import pytest

from reckon import mcp as mcp_module
from reckon import served_code

#: The fake child. It answers the readiness probe unless it is told to stay
#: cold, and answers every tool call with the configured answer, so a case can
#: say what the current code would say without importing a tree.
_FAKE_CHILD = r"""
import json, os, sys

cold = os.environ.get("FAKE_CHILD_COLD") == "1"
silent = os.environ.get("FAKE_CHILD_SILENT") == "1"
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
        if silent:
            # Warm, but it never answers a call: a child the guard has asked
            # and has no answer from.
            continue
        reply = {"id": ident, "op": "call", "reached": True, "answer": answer}
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
"""


class Spawner:
    """The spawn hook a rig is built with: counts children, starts the fake."""

    def __init__(
        self, *, answer: dict | None = None, cold: bool = False, silent: bool = False
    ) -> None:
        self.count = 0
        self.children: list[subprocess.Popen] = []
        self.answer = json.dumps(
            {"ok": True, "served_by": "current code"} if answer is None else answer
        )
        self.cold = cold
        self.silent = silent

    def __call__(self, root: Path) -> subprocess.Popen:
        self.count += 1
        environment = {
            **os.environ,
            "FAKE_CHILD_ANSWER": self.answer,
            "FAKE_CHILD_COLD": "1" if self.cold else "0",
            "FAKE_CHILD_SILENT": "1" if self.silent else "0",
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
#: code change would answer it. Its text carries pydantic's message on purpose:
#: a match on the message alone would read this as the guard's case, and the
#: leading exception type is what says it is not one.
_UNRELATED_NOTE = (
    "the config could not be read; Extra inputs are not permitted was never reached"
)
_UNRELATED_ERROR = f"OSError: {_UNRELATED_NOTE}"


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
        silent: bool = False,
    ) -> None:
        self.root = tmp_path / "temporary-tree"
        self.root.mkdir()
        self.spawner = Spawner(answer=answer, cold=cold, silent=silent)
        self.server = served_code.CurrentCodeServer(
            self.root, spawn=self.spawner, timeout_s=timeout_s
        )
        #: The stamp of the code the tree now holds, and of the image this
        #: process started with. They differ, so an answer carrying the wrong
        #: one of the two is a failure and not a coincidence.
        self.stamp = "1" * 64
        self.image_stamp = "0" * 64
        self.moved = moved
        monkeypatch.setattr(mcp_module, "_current_code_server", lambda: self.server)
        monkeypatch.setattr(mcp_module, "_moved_checkout_state", self._state)
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
            running_stamp=self.image_stamp,
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

    def child_stamp(self) -> str | None:
        """The stamp the live child was started for, read from the server.

        Read from the object that decided it rather than from a constant this
        case injected, so an answer stamped with the image's code — or with
        nothing — fails here.
        """
        return self.server.current_stamp()


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
        assert result.get("served_by_code_stamp") == rig.child_stamp(), (
            f"the answer carries the stamp of the code that served it: {result!r}"
        )
        assert rig.child_stamp() != rig.image_stamp, (
            f"the child answers for the moved tree, not the running image: {result!r}"
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


def test_a_raise_after_the_warm_child_fell_back_stays_the_calls_own_failure(
    tmp_path, monkeypatch
):
    """A raise from an image call the guard never routed is not a refusal.

    The child is warm — it answered its probe — and made no answer to this
    call, so the image ran it and raised. The refusal says the current code
    failed the call too; that would be false here, because the current code was
    never asked to take the call over. So the failure stands as the call's own.
    """
    rig = Rig(tmp_path, monkeypatch, silent=True, timeout_s=0.3)
    guarded, image = rig.tool("roadmap", raises=RuntimeError("config unreadable"))
    try:
        assert rig.warm(), "the child must answer its probe"
        with pytest.raises(RuntimeError) as raised:
            call(guarded, project="reckon")

        assert "config unreadable" in str(raised.value), raised.value
        assert image, "the image runs the call the warm child did not answer"
    finally:
        rig.server.stop()


def test_the_first_write_of_a_tool_is_served_by_the_warm_child(tmp_path, monkeypatch):
    """A write is routed on its first call, not only after the tool has failed."""
    rig = Rig(tmp_path, monkeypatch)
    guarded, image = rig.tool(
        "edit_plan",
        answer={"ok": True, "version": 4, "served_by": "the image"},
    )
    try:
        assert rig.warm(), "the child must answer its probe"
        result = call(guarded, project="reckon", slug="a-plan", op="set")

        assert result.get("served_by") == "current code", (
            f"the first write of a tool is served by the warm child: {result!r}"
        )
        assert result.get("served_by_code_stamp") == rig.child_stamp(), (
            f"a write carries the stamp of the code that served it: {result!r}"
        )
        assert rig.child_stamp() != rig.image_stamp, (
            f"the child answers for the moved tree, not the running image: {result!r}"
        )
        assert image == [], (
            f"the image never runs a write the warm child answers: {image!r}"
        )
    finally:
        rig.server.stop()


# ── a schema failure embedded in a successful answer ───────────────────────


def test_an_embedded_schema_failure_is_not_passed_off_as_an_answer(
    tmp_path, monkeypatch
):
    """A schema failure beside the image's successful write keeps both stamps."""
    rig = Rig(tmp_path, monkeypatch, cold=True, timeout_s=0.3)
    guarded, image = rig.tool(
        "edit_plan",
        answer={"ok": True, "review_owed": None, "review_owed_error": _SCHEMA_ERROR},
    )
    try:
        result = call(guarded, project="reckon")

        assert image, "the image's own answer is what carries the embedded error"
        assert result.get("ok") is False, (
            f"a schema failure the image could not see is not its answer: {result!r}"
        )
        assert result.get("error") == "ValidationError", result
        assert result.get("detail") == _SCHEMA_ERROR, result
        assert result.get("code_stamp_now") == rig.stamp, result
        assert result.get("code_stamp_at_start") == rig.image_stamp, result
        assert rig.stamp != rig.image_stamp, (
            f"the two stamps are distinct, so the pair above pins both: {result!r}"
        )
        assert "current_code_note" in result, result
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
        assert result.get("ok") is True, result
    finally:
        rig.server.stop()


def test_an_error_in_a_field_outside_the_guard_s_own_is_not_read(tmp_path, monkeypatch):
    """A schema error the call reports in its own field is the caller's answer."""
    rig = Rig(tmp_path, monkeypatch, cold=True, timeout_s=0.3)
    guarded, _ = rig.tool(
        "roadmap",
        answer={"ok": True, "plan_error": _SCHEMA_ERROR},
    )
    try:
        result = call(guarded, project="reckon")

        assert result.get("ok") is True, result
        assert result.get("plan_error") == _SCHEMA_ERROR, result
        assert "code_stamp_now" not in result, result
    finally:
        rig.server.stop()
