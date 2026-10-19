"""A reckon MCP server answers on the checkout's current code, once it moves.

The server holds the classes it imported at start for the life of the process,
while reading its configuration from disk on each call. A checkout that moves
past those imports — a shipped default the running image's schema did not know
when it started — is therefore validated against classes that no longer match
it, and a call fails with a schema error that names nothing about the restart
owed. The guard answers on failure rather than before the call: most commits
move the code without breaking a running image, so a call that returns normally
must be returned untouched.

When a call does fail and the checkout has moved and can be trusted, the server
answers it on the current code rather than refusing: a stdio server's client
holds the session this process initialised, so the image cannot be replaced the
way the HTTP server replaces its own, and the call is run against the checkout
in a child interpreter whose answer stands in for the stale image's. Whatever
that child answers is the caller's answer, a failure as much as a success,
because a reconnect would only deliver the current code's same failure a turn
later. A tool whose parameters changed is the one refusal, naming that tool,
because the client holds the schema it connected with.

Each test runs a server over its own copy of the package in a temp directory,
in a fresh interpreter, so nothing here reads the checkout it lives in.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE_PACKAGE = Path(__file__).resolve().parents[1] / "reckon"

_DRIVER = r'''
import asyncio, json, pathlib, subprocess, sys

root = pathlib.Path(sys.argv[1])
mode = sys.argv[2]
git = sys.argv[3]

import reckon.mcp as reckon_mcp


def structured(result):
    if isinstance(result, tuple):
        for item in result:
            if isinstance(item, dict):
                return item
        result = result[0]
    if isinstance(result, list):
        for block in result:
            text = getattr(block, "text", None)
            if text:
                return json.loads(text)
    return {"unstructured": repr(result)}


async def edit_plan():
    """A call the running image fails: the checkout carries no such plan."""
    try:
        blocks = await reckon_mcp.mcp.call_tool(
            "edit_plan",
            {"project": "reckon", "slug": "no-such-plan", "expected_version": 1},
        )
    except Exception as exc:
        return {"ok": False, "error": "tool_error", "detail": f"{type(exc).__name__}: {exc}"}
    return structured(blocks)


async def crew():
    """A call the running image fails: no project names a crew read view."""
    try:
        blocks = await reckon_mcp.mcp.call_tool("crew", {"view": "summary"})
    except Exception as exc:
        return {"ok": False, "error": "tool_error", "detail": f"{type(exc).__name__}: {exc}"}
    return structured(blocks)


# The change a checkout ahead of the running image carries. ``answers`` makes
# edit_plan return an answer the running image never could; ``signature``
# changes the tool's parameter list, which no in-process answer can cover.
# The answer is patched below the registered tool, on the sync function the
# tool calls: wrapping the registered tool itself changes how the tool manager
# classifies it and the call is then never awaited.
_ANSWERS = """

# the checkout ahead of the running image answers this call
_edit_plan_before_the_change = _edit_plan
def _edit_plan(*args, **kwargs):  # noqa: F811
    result = _edit_plan_before_the_change(*args, **kwargs)
    if isinstance(result, dict) and result.get("ok") is False:
        return {"ok": True, "served_by": "current code", "was": result.get("error")}
    return result
"""

_SIGNATURE = """

# the checkout ahead of the running image adds a parameter to the tool
_edit_plan_tool_before_the_change = _edit_plan_tool
async def _edit_plan_tool(*args, added_parameter=None, **kwargs):  # noqa: F811
    return await _edit_plan_tool_before_the_change(*args, **kwargs)
"""

# The crew tool's equivalents. ``crew_answers`` fails the call the way the
# current code fails it, so the answer the caller gets is a failure too. The
# other two take the child away from the tool's answer without changing the
# registered parameters: one leaves no crew tool to reach, the other leaves no
# answer at all.
_CREW_ANSWERS = """

# The checkout ahead of the running image serves a crew tool of its own, so the
# call fails the way the current code fails it. The tool is re-registered: the
# registration above captured the body it was built with, so rebinding the name
# alone would leave that body answering. functools.wraps keeps the published
# parameters, which the registered tool's schema still has to match.
import functools

_crew_before_the_change = _crew


@functools.wraps(_crew_before_the_change)
def _crew(*args, **kwargs):  # noqa: F811
    return {
        "ok": False,
        "error": "crew_view_refused",
        "detail": "the current code refused this view",
    }


mcp.remove_tool("crew")
mcp.tool(name="crew")(_crew)
"""

_CREW_REMOVED = """

# the checkout ahead of the running image no longer serves the crew tool, so a
# child that goes looking for it never reaches it
mcp.remove_tool("crew")
"""

_CREW_SILENT = """

# The checkout ahead of the running image takes the interpreter down inside the
# crew call, so the child makes no answer at all. Re-registered as above: the
# registration captured the body it was built with.
import functools
import os

_crew_before_the_change = _crew


@functools.wraps(_crew_before_the_change)
def _crew(*args, **kwargs):  # noqa: F811
    os._exit(1)


mcp.remove_tool("crew")
mcp.tool(name="crew")(_crew)
"""

_MOVES = {
    "answers": ("reckon/mcp_edit_plan.py", _ANSWERS),
    "signature": ("reckon/mcp_edit_plan.py", _SIGNATURE),
    "crew_answers": ("reckon/mcp.py", _CREW_ANSWERS),
    "crew_never": ("reckon/mcp.py", _CREW_REMOVED),
    "crew_silent": ("reckon/mcp.py", _CREW_SILENT),
}


def move_the_checkout():
    relative, addition = _MOVES[mode]
    target = root / relative
    target.write_text(target.read_text() + addition)


def move_a_tracked_file():
    """Leave a tracked file modified against HEAD, as a half-edited tree would."""
    target = root / "reckon" / "crew" / "runs.py"
    target.write_text(target.read_text() + "\n# a change the server did not import\n")


def commit_the_move():
    subprocess.run([git, "-C", str(root), "add", "reckon"], check=True)
    subprocess.run(
        [
            git, "-C", str(root),
            "-c", "user.email=probe@example.invalid",
            "-c", "user.name=probe",
            "commit", "-q", "-m", "move the code",
        ],
        check=True,
    )


async def main():
    call = crew if mode.startswith("crew_") else edit_plan
    first = await call()
    if mode == "dirty":
        move_a_tracked_file()
    else:
        move_the_checkout()
        commit_the_move()
    second = await call()
    third = await call()
    print(json.dumps({
        "module": reckon_mcp.__file__,
        "started_at": reckon_mcp._SERVER_STARTED_AT.isoformat(),
        "first": first,
        "second": second,
        "third": third,
    }))


asyncio.run(main())
'''


def _real_git() -> str:
    """The git binary, skipping the PATH shims a worker session installs."""
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(entry) / "git"
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        if "shim" in entry:
            continue
        return str(candidate)
    return "git"


_GIT = _real_git()


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        [_GIT, "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        f"git {' '.join(args)} failed in {root}: {completed.stderr!r}"
    )
    return completed.stdout


def _committed_checkout(tmp_path: Path) -> Path:
    """A self-contained committed git checkout of the package.

    The copy is committed, so ``HEAD`` names its exact starting content, a
    modification is a real difference from it, and a commit moves the code
    identity the server recorded when it imported the copy.
    """
    root = tmp_path / "checkout"
    shutil.copytree(
        SOURCE_PACKAGE,
        root / "reckon",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "reckon")
    _git(
        root,
        "-c",
        "user.email=probe@example.invalid",
        "-c",
        "user.name=probe",
        "commit",
        "-q",
        "-m",
        "base",
    )
    return root


def _run_server(tmp_path: Path, root: Path, mode: str) -> dict:
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER)
    # A directory carrying a real ``git``, so the server's own tree check is not
    # answered by a worker session's shim.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(_GIT)
    # A working directory holding no reckon package, so the copy is what an
    # unscoped import resolves to.
    working = tmp_path / "working"
    working.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHONPATH": str(root),
        "RECKON_HOME": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    completed = subprocess.run(
        [sys.executable, str(driver), str(root), mode, _GIT],
        capture_output=True,
        text=True,
        env=environment,
        cwd=working,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _assert_served_from_the_copy(payload: dict, root: Path) -> None:
    assert Path(payload["module"]).resolve().is_relative_to(root.resolve())


def test_a_call_on_a_moved_checkout_answers_on_the_current_code(
    tmp_path: Path,
) -> None:
    """A commit that changes edit_plan's answer is reached without a reconnect."""
    root = _committed_checkout(tmp_path)
    payload = _run_server(tmp_path, root, "answers")
    _assert_served_from_the_copy(payload, root)

    first, second, third = payload["first"], payload["second"], payload["third"]
    assert first.get("ok") is False, f"the running image must fail the call: {first!r}"
    assert second.get("ok") is True, (
        f"a moved checkout must answer on its current code, got {second!r}"
    )
    assert second.get("served_by") == "current code"
    assert "stale_server_code" not in json.dumps(second), (
        "a call the current code answers is never refused"
    )
    # Later calls run on the current code too, rather than falling back.
    assert third.get("served_by") == "current code", (
        f"later calls must run on the current code: {third!r}"
    )


def test_a_dirty_tree_keeps_serving_the_started_image(tmp_path: Path) -> None:
    """A tracked file modified against ``HEAD`` is never loaded over the image."""
    root = _committed_checkout(tmp_path)
    payload = _run_server(tmp_path, root, "dirty")
    _assert_served_from_the_copy(payload, root)

    first, second = payload["first"], payload["second"]
    assert first.get("ok") is False, f"the running image must fail the call: {first!r}"
    assert second.get("error") != "stale_server_code", (
        f"a dirty moved checkout must not be called stale, got {second!r}"
    )
    assert second.get("ok") is False, (
        f"an untrusted tree keeps serving the started image, got {second!r}"
    )
    assert second.get("served_by") != "current code"


def test_a_changed_tool_signature_returns_the_reconnect_refusal(
    tmp_path: Path,
) -> None:
    """A parameter list the client does not hold owes a reconnect naming the tool."""
    root = _committed_checkout(tmp_path)
    payload = _run_server(tmp_path, root, "signature")
    _assert_served_from_the_copy(payload, root)

    second = payload["second"]
    assert second.get("error") == "stale_server_code", (
        f"a changed tool signature must be refused, got {second!r}"
    )
    assert second.get("tool") == "edit_plan", (
        f"the refusal must name the changed tool: {second!r}"
    )
    assert "edit_plan" in second["detail"], second["detail"]
    assert "reconnect" in second["detail"].lower()
    assert second.get("served_by") != "current code"


def test_a_crew_failure_answers_on_the_current_code_with_its_stamp(
    tmp_path: Path,
) -> None:
    """The crew tool's own failure is the caller's answer, stamped, as edit_plan's is."""
    root = _committed_checkout(tmp_path)
    payload = _run_server(tmp_path, root, "crew_answers")
    _assert_served_from_the_copy(payload, root)

    first, second, third = payload["first"], payload["second"], payload["third"]
    assert first.get("ok") is False, f"the running image must fail the call: {first!r}"
    assert first.get("error") == "missing_project", first
    assert second.get("ok") is False, (
        f"the current code's failure is the caller's answer: {second!r}"
    )
    assert second.get("error") == "crew_view_refused", (
        f"the answer must be the current code's own failure: {second!r}"
    )
    stamp = second.get("served_by_code_stamp")
    assert isinstance(stamp, str) and stamp, (
        f"the current code's answer carries its stamp: {second!r}"
    )
    assert "stale_server_code" not in json.dumps(second), (
        "a call the current code answers is never refused"
    )
    # Later calls run on the current code too, rather than falling back.
    assert third.get("error") == "crew_view_refused", third
    assert third.get("served_by_code_stamp") == stamp, third


def test_a_child_that_never_reaches_the_tool_is_not_its_answer(tmp_path: Path) -> None:
    """A child that reached no tool made no answer, and reports no signature."""
    root = _committed_checkout(tmp_path)
    payload = _run_server(tmp_path, root, "crew_never")
    _assert_served_from_the_copy(payload, root)

    first, second = payload["first"], payload["second"]
    assert first.get("ok") is False, f"the running image must fail the call: {first!r}"
    assert second.get("error") == "stale_server_code", (
        f"a child that never reached the tool made no answer, so the caller keeps "
        f"the refusal: {second!r}"
    )
    assert "tool" not in second, (
        f"a child that reached no tool reports no changed signature: {second!r}"
    )
    assert "changed its parameters" not in second["detail"], second
    body = json.dumps(second)
    assert "served_by_code_stamp" not in body, (
        f"the child's own failure is not the current code's answer: {second!r}"
    )
    assert "StopIteration" not in body, second


def test_a_child_that_makes_no_answer_gets_the_refusal(tmp_path: Path) -> None:
    """A child that answers nothing at all owes the reconnect refusal, naming why."""
    root = _committed_checkout(tmp_path)
    payload = _run_server(tmp_path, root, "crew_silent")
    _assert_served_from_the_copy(payload, root)

    first, second = payload["first"], payload["second"]
    assert first.get("ok") is False, f"the running image must fail the call: {first!r}"
    assert second.get("error") == "stale_server_code", (
        f"a child that makes no answer owes the refusal: {second!r}"
    )
    assert second.get("server_started_at") == payload["started_at"], (
        f"the refusal names the image that could not answer: {second!r}"
    )
    assert second.get("code_stamp_at_start") != second.get("code_stamp_now"), second
    assert "reconnect" in second["detail"].lower(), second["detail"]
    assert second.get("original_error"), (
        f"the refusal keeps the failed call: {second!r}"
    )
    assert "served_by_code_stamp" not in json.dumps(second), second
