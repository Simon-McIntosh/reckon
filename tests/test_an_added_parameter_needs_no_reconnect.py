"""An added optional parameter needs no reconnect; a dropped one still does.

After the guard began serving the tree's current code from a child, the only
reconnect it asked for was a changed tool signature — and it asked for any
difference in the parameter list, so a commit whose only change was a new
optional parameter made every session reconnect. The client holds the schema it
connected with, so an added optional parameter leaves every call the client can
make valid on the new code: a reconnect only buys it the new parameter. But a
registered parameter removed or made required gives the client calls the current
code would refuse, and there a reconnect is owed.

The child holds the rule, not the parent, so a later change to it reaches a
running server without one. The parent sends its registered parameters, each
with whether it was required, with the readiness probe; the child answers
whether the change is compatible. A probe from a parent that predates the
verdict carries no registered parameters, gets the answer it already
understands, and compares the parameter names itself.

Each case runs the guard in this process against a temporary tree, with a fake
child whose probed signature and verdict the test controls.
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

#: The worktree root, so the fake child imports the tree's own compatibility
#: rule rather than a copy of it.
_TREE = str(Path(__file__).resolve().parents[1])

#: The fake child. It re-reads its config for every request, so a test can set
#: the signature the probe reports and the verdict it answers. By default it
#: computes the verdict with the tree's own rule, which is what a probe request
#: carrying registered parameters gets; a config value overrides that, standing
#: in for a later change to the rule.
_FAKE_CHILD = r"""
import json, os, sys

sys.path.insert(0, os.environ["RECKON_SERVED_TREE"])
from reckon.served_code import probe_reply, signature_compatible  # noqa: E402

config_path = os.environ["FAKE_CHILD_CONFIG"]


def config():
    try:
        with open(config_path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


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
        params = settings.get("params") or {}
        required = settings.get("required") or {}
        registered = message.get("registered")
        reply = probe_reply(params, required, registered)
        forced = settings.get("compatible")
        if forced is not None and registered:
            reply["compatible"] = {name: bool(forced) for name in registered}
        if settings.get("nest_requiredness"):
            reply["params"] = {
                name: {
                    param: param in set(required.get(name) or [])
                    for param in names
                }
                for name, names in params.items()
            }
            reply.pop("required", None)
        reply.update({"id": ident, "op": "probe"})
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
        continue
    tool = message.get("tool")
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
        self.count = 0
        self.children: list[subprocess.Popen] = []
        self.config = dict(config)
        self.write()

    def write(self) -> None:
        self.path.write_text(json.dumps(self.config))

    def __call__(self, root: Path) -> subprocess.Popen:
        self.count += 1
        environment = {
            **os.environ,
            "FAKE_CHILD_CONFIG": str(self.path),
            "RECKON_SERVED_TREE": _TREE,
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
        monkeypatch.setattr(
            mcp_module,
            "_REGISTERED_TOOL_REQUIRED",
            dict(mcp_module._REGISTERED_TOOL_REQUIRED),
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

    def tool(
        self,
        name: str = "edit_plan",
        *,
        registered: tuple[str, ...] = ("project", "slug", "expected_version"),
        required: tuple[str, ...] = ("project", "slug", "expected_version"),
        answer: dict | None = None,
    ):
        calls: list[dict] = []

        async def inner(**kwargs):
            calls.append(kwargs)
            return (
                dict(answer)
                if answer is not None
                # The running image fails, so the call is re-run on the child.
                else {"ok": False, "error": "version_conflict"}
            )

        mcp_module._REGISTERED_TOOL_PARAMS[name] = registered
        mcp_module._REGISTERED_TOOL_REQUIRED[name] = required
        guarded = mcp_module._refuse_when_stale(inner, name=name)
        return guarded, calls

    def warm(self, *, seconds: float = 8.0) -> bool:
        """Run a probe until the child has answered it."""
        payload = mcp_module._registered_probe_payload()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.server.warm(self.stamp, payload) is not None:
                return True
            time.sleep(0.02)
        return False


def call(guarded, **kwargs) -> dict:
    result = asyncio.run(guarded(**kwargs))
    return result if isinstance(result, dict) else {"ok": True, "unstructured": result}


# ── the declared negative controls ─────────────────────────────────────────


def _apply_control(monkeypatch, control: str | None) -> None:
    """Apply the mutation the node declares as a control, when one is named.

    Each restores one behaviour the matching case exists to forbid, so the case
    fails against it and that run's log is the negative control.
    """
    if control is None:
        return
    if control == "equal-lists":
        # The pre-verdict guard: any difference in the parameter names is a
        # changed signature, an added optional parameter included.
        monkeypatch.setattr(
            mcp_module,
            "_probe_incompatible",
            mcp_module._probe_signature_changed,
        )
    elif control == "decide-in-parent":
        # The parent computes compatibility itself instead of reading the
        # child's verdict, so a rule change in the child never reaches it.
        def in_parent(name, probe):
            params = probe.get("params") or {}
            required = probe.get("required") or {}
            payload = mcp_module._registered_probe_payload().get(name) or {}
            return not served_code.signature_compatible(
                payload, params.get(name), required.get(name)
            )

        monkeypatch.setattr(mcp_module, "_probe_incompatible", in_parent)
    elif control == "nest-requiredness":
        # Requiredness nested inside ``params`` instead of a sibling key, so a
        # parent that compares parameter names reads a mapping and sees no
        # change.
        def nested(params, required, registered):
            reply = {
                "params": {
                    name: {
                        param: param in set((required or {}).get(name) or [])
                        for param in names
                    }
                    for name, names in params.items()
                }
            }
            if registered:
                reply["compatible"] = {
                    name: served_code.signature_compatible(
                        registered.get(name) or {},
                        params.get(name),
                        (required or {}).get(name),
                    )
                    for name in registered
                }
            return reply

        monkeypatch.setattr(served_code, "probe_reply", nested)


@pytest.fixture
def control() -> str | None:
    """The mutation this run applies, named by the driver that keeps the log."""
    return os.environ.get("RECKON_ADDED_PARAM_CONTROL") or None


def _rig(tmp_path, monkeypatch, control: str | None, **config) -> Rig:
    return Rig(tmp_path, monkeypatch, control, **config)


# ── case 1: an added optional parameter is served with a schema note ───────


def test_an_added_optional_parameter_is_served_with_a_schema_note(
    tmp_path, monkeypatch, control
):
    """A failed call re-run on a child with one extra optional parameter is served."""
    rig = _rig(
        tmp_path,
        monkeypatch,
        control,
        params={
            "edit_plan": ["expected_version", "project", "slug", "added_parameter"]
        },
        required={"edit_plan": ["expected_version", "project", "slug"]},
    )
    guarded, _ = rig.tool("edit_plan")

    assert rig.warm(), "the child must answer its probe"
    result = call(guarded, project="reckon", slug="p", expected_version=1)

    assert result.get("served_by") == "current code", result
    assert result.get("served_by_code_stamp"), result
    assert "added_parameter" in str(result.get("schema_note")), (
        f"the answer names the parameter the client cannot send: {result!r}"
    )
    assert "reconnect" not in result, (
        f"a compatible change carries no reconnect field: {result!r}"
    )
    assert "stale_server_code" not in json.dumps(result), result


# ── case 2: a removed parameter still owes a reconnect ─────────────────────


def test_a_removed_parameter_still_owes_a_reconnect(tmp_path, monkeypatch, control):
    """A parameter the client holds and the current code dropped is refused."""
    rig = _rig(
        tmp_path,
        monkeypatch,
        control,
        params={"edit_plan": ["project", "slug"]},
        required={"edit_plan": ["project", "slug"]},
    )
    guarded, image = rig.tool("edit_plan")

    assert rig.warm()
    result = call(guarded, project="reckon", slug="p", expected_version=1)

    assert result.get("error") == "stale_server_code", result
    assert result.get("tool") == "edit_plan", result
    assert "reconnect" in result.get("detail", "").lower(), result
    assert image == [], "the call is refused before it reaches the current code"


# ── case 3: a new required parameter still owes a reconnect ────────────────


def test_a_new_required_parameter_still_owes_a_reconnect(
    tmp_path, monkeypatch, control
):
    """A required parameter the client does not hold is refused."""
    rig = _rig(
        tmp_path,
        monkeypatch,
        control,
        params={"edit_plan": ["expected_version", "project", "slug", "new_required"]},
        required={"edit_plan": ["expected_version", "project", "slug", "new_required"]},
    )
    guarded, _ = rig.tool("edit_plan")

    assert rig.warm()
    result = call(guarded, project="reckon", slug="p", expected_version=1)

    assert result.get("error") == "stale_server_code", result
    assert result.get("tool") == "edit_plan", result


# ── case 4: a registered optional parameter made required still owes one ───


def test_a_registered_optional_made_required_still_owes_a_reconnect(
    tmp_path, monkeypatch, control
):
    """A parameter the client could omit and the current code requires is refused."""
    rig = _rig(
        tmp_path,
        monkeypatch,
        control,
        params={"edit_plan": ["expected_version", "project", "slug", "request_id"]},
        required={"edit_plan": ["expected_version", "project", "slug", "request_id"]},
    )
    guarded, _ = rig.tool(
        "edit_plan",
        registered=("project", "slug", "expected_version", "request_id"),
        required=("project", "slug", "expected_version"),
    )

    assert rig.warm()
    result = call(guarded, project="reckon", slug="p", expected_version=1)

    assert result.get("error") == "stale_server_code", result
    assert result.get("tool") == "edit_plan", result


# ── case 5: the probe stays readable by a parent that predates the verdict ─


def test_the_probe_without_registered_parameters_keeps_its_old_answer(
    tmp_path, monkeypatch, control
):
    """A probe carrying no registered parameters gets names, no verdict."""
    monkeypatch.setattr(
        mcp_module,
        "_REGISTERED_TOOL_PARAMS",
        {"edit_plan": ("project", "slug", "expected_version")},
    )
    probe = served_code.probe_reply(
        {"edit_plan": ["expected_version", "project", "slug", "added_parameter"]},
        {"edit_plan": ["expected_version", "project", "slug"]},
        None,
    )

    assert "compatible" not in probe, probe
    assert probe["params"]["edit_plan"] == [
        "expected_version",
        "project",
        "slug",
        "added_parameter",
    ], probe
    assert mcp_module._probe_signature_changed("edit_plan", probe), (
        f"the pre-verdict comparison still reports the added parameter: {probe!r}"
    )


# ── case 6: the child's verdict governs ────────────────────────────────────


def test_the_childs_verdict_governs_even_when_the_parents_lists_differ(
    tmp_path, monkeypatch, control
):
    """A child that answers compatible makes the parent serve, lists aside."""
    rig = _rig(
        tmp_path,
        monkeypatch,
        control,
        params={"edit_plan": ["project", "slug"]},
        required={"edit_plan": ["project", "slug"]},
        # The child's rule answers compatible for a change the parent's own
        # comparison would call a dropped parameter.
        compatible=True,
    )
    guarded, _ = rig.tool("edit_plan")

    assert rig.warm()
    result = call(guarded, project="reckon", slug="p", expected_version=1)

    assert result.get("served_by") == "current code", result
    assert "stale_server_code" not in json.dumps(result), result
