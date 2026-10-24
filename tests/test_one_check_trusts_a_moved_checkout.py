"""The MCP and the follower trust the same thing about a moved checkout.

A code stamp that has moved says only that the checkout changed on disk. Whether
that change may be loaded is a second question with two conditions — clean under
``reckon/`` against ``HEAD`` and every module importing — and the follower's
reloader and the MCP's stale response must answer it the same way or the MCP
tells its caller to reconnect onto a tree the followers refuse to load. Held as
separate probes the two disagreed, which is the half-edited tree that took five
producers down.

These cases run a server over its own copy of the package in a temp directory,
in a fresh interpreter, so nothing here reads the checkout it lives in. The
checkout is a real git repository, so a case can leave a tracked file modified
against ``HEAD`` or commit it and watch the trust answer change with it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from reckon.mcp import SERVED_BY_CODE_STAMP

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


async def crew_view():
    """A crew view that resolves the flight config, so a shipped default that
    moved under a running image is the thing a call actually validates."""
    blocks = await reckon_mcp.mcp.call_tool(
        "crew", {"project": "reckon", "view": "flight"}
    )
    return structured(blocks)


def move_the_stamp():
    """Change a module the code stamp digests, as a checkout ahead would."""
    target = root / "reckon" / "crew" / "runs.py"
    target.write_text(target.read_text() + "\n# a change the server did not import\n")


def add_a_shipped_default():
    """Break a declared shipped default, as a later commit the image rejects would.

    The fault has to be one the running image still refuses: a key its schema
    does not declare is ignored and reported rather than raised, so the mover
    breaks the value of a key the schema does declare.
    """
    config = root / "reckon" / "schema" / "flight-defaults.yaml"
    config.write_text(
        config.read_text().replace(
            "  exhausted_statuses: []",
            "  exhausted_statuses: not_a_list",
        )
    )


def commit_the_moved_code():
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


async def warm_the_child():
    """Wait until the guard's persistent child has answered its readiness probe.

    The child is warmed by the probe rather than by a caller's call, so a call
    made while it is still cold is served by the running image. This case is
    about the answer once the current code is serving, so it waits for it.
    """
    for _ in range(1800):
        state = reckon_mcp._moved_checkout_state()
        if state.may_load:
            server = reckon_mcp._current_code_server()
            if server.warm(state.disk_stamp) is not None:
                return
        await asyncio.sleep(0.1)


async def main():
    first = await crew_view()
    move_the_stamp()
    add_a_shipped_default()
    if mode == "clean":
        commit_the_moved_code()
        await warm_the_child()
    second = await crew_view()
    print(json.dumps({
        "module": reckon_mcp.__file__,
        "first": first,
        "second": second,
    }))


asyncio.run(main())
'''


def _real_git() -> str:
    """The git binary, skipping the PATH shims a worker session installs.

    A worker's ``git`` on ``PATH`` refuses mutating verbs aimed outside its own
    worktree, and both ``init`` of a scratch checkout and the server's own tree
    check would meet it. These cases build their checkout with the real binary.
    """
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

    The copy is committed, so ``HEAD`` names its exact starting content and a
    modification is a real difference from it.
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


def test_a_dirty_moved_checkout_keeps_the_original_failure(tmp_path: Path) -> None:
    """A tracked file modified against ``HEAD`` holds the refusal back.

    The code stamp has moved and the call has failed, which is everything the
    stale answer used to require. The tree is half-edited, so the followers
    would refuse to load it; the MCP must not tell its caller to reconnect onto
    it either. The original failure — the one the caller actually hit — stands.
    """
    root = _committed_checkout(tmp_path)
    payload = _run_server(tmp_path, root, "dirty")
    _assert_served_from_the_copy(payload, root)

    first, second = payload["first"], payload["second"]
    assert first.get("ok") is True, f"a fresh server must answer normally: {first!r}"
    assert second.get("error") != "stale_server_code", (
        f"a dirty moved checkout must not be called stale, got {second!r}"
    )
    assert second.get("error") == "crew_error"
    assert "budget.exhausted_statuses" in second.get("detail", "")


def test_a_clean_moved_checkout_answers_on_the_current_code(tmp_path: Path) -> None:
    """A committed, clean, importable checkout is trusted, so its answer is served.

    Committing the move leaves every byte where it was and the tree clean
    against ``HEAD``; the code stamp still differs from the one the server
    imported. The tree can be loaded, so the call is re-run on it and its own
    answer returned. The current code fails this call too, and that failure is
    the caller's real answer — a reconnect would only deliver it a turn later —
    so it stands, marked with the stamp of the code that served it.
    """
    root = _committed_checkout(tmp_path)
    payload = _run_server(tmp_path, root, "clean")
    _assert_served_from_the_copy(payload, root)

    first, second = payload["first"], payload["second"]
    assert first.get("ok") is True, f"a fresh server must answer normally: {first!r}"
    assert second.get("error") != "stale_server_code", (
        f"a clean moved checkout answers on its current code, got {second!r}"
    )
    assert second.get("error") == "crew_error"
    assert "budget.exhausted_statuses" in second.get("detail", "")
    assert second.get(SERVED_BY_CODE_STAMP), (
        f"an answer the current code served names the stamp that served it: {second!r}"
    )


def test_the_source_holds_one_import_probe() -> None:
    """Only one module defines the import proof the readers share."""
    needle = "compile(path.read_bytes(), str(path), 'exec')"
    hits = sorted(
        path for path in SOURCE_PACKAGE.rglob("*.py") if needle in path.read_text()
    )
    assert hits == [SOURCE_PACKAGE / "crew_follow_commands.py"], hits
