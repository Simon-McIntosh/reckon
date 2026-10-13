"""A reckon MCP server answers for the code it actually imported, once a call fails.

The server holds the classes it imported at start for the life of the process,
while reading its configuration from disk on each call. A checkout that moves
past those imports — a shipped default the running image's schema did not know
when it started — is therefore validated against classes that no longer match
it, and a call fails with a schema error that names nothing about the restart
owed. The guard answers on failure rather than before the call: most commits
move the code without breaking a running image, so a call that returns normally
must be returned untouched. When a call fails and the checkout has moved and
still imports, the failure is replaced by one refusal naming the server's start
time, the change and the reconnect remedy, carrying the original error text. A
checkout that does not import defers, so a tree mid-merge never takes a running
server down.

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
import asyncio, json, pathlib, sys

root = pathlib.Path(sys.argv[1])
mode = sys.argv[2]

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


def break_the_tree():
    """Leave conflict markers, as a merge resolving mid-file would."""
    target = root / "reckon" / "crew" / "runs.py"
    target.write_text(
        target.read_text()
        + "\n<<<<<<< HEAD\n_broken = (1\n=======\n>>>>>>> other\n"
    )


def add_a_shipped_default():
    """Add a key the running image's schema rejects, as a later commit would."""
    config = root / "reckon" / "schema" / "flight-defaults.yaml"
    config.write_text(
        config.read_text().replace(
            "  exhausted_statuses: []",
            "  exhausted_statuses: []\n  a_key_from_the_future: 1",
        )
    )


async def main():
    first = await crew_view()
    if mode == "moved_and_broken":
        move_the_stamp()
        add_a_shipped_default()
    elif mode == "moved_and_well":
        move_the_stamp()
    elif mode == "unimportable_and_broken":
        break_the_tree()
        add_a_shipped_default()
    second = await crew_view()
    print(json.dumps({
        "module": reckon_mcp.__file__,
        "started_at": reckon_mcp._SERVER_STARTED_AT.isoformat(),
        "first": first,
        "second": second,
    }))


asyncio.run(main())
'''


def _build_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    shutil.copytree(SOURCE_PACKAGE, root / "reckon")
    return root


def _run_server(tmp_path: Path, root: Path, mode: str) -> dict:
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER)
    # A directory with no reckon package on it, so the copy is what an
    # unscoped import resolves to.
    working = tmp_path / "working"
    working.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        **os.environ,
        "PYTHONPATH": str(root),
        "RECKON_HOME": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    completed = subprocess.run(
        [sys.executable, str(driver), str(root), mode],
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


def test_a_failed_call_on_a_moved_checkout_answers_with_the_remedy(
    tmp_path: Path,
) -> None:
    """A call that fails after the checkout moves names the restart owed."""
    root = _build_checkout(tmp_path)
    payload = _run_server(tmp_path, root, "moved_and_broken")
    _assert_served_from_the_copy(payload, root)

    first, second = payload["first"], payload["second"]
    assert first.get("error") != "stale_server_code", (
        f"a fresh server must answer normally, not refuse: {first!r}"
    )
    assert second.get("error") == "stale_server_code", (
        f"a failed call on a moved checkout must answer with the remedy, got {second!r}"
    )
    detail = second["detail"]
    assert payload["started_at"] in detail, "the error must name the start time"
    assert second["server_started_at"] == payload["started_at"]
    assert second["code_stamp_at_start"] != second["code_stamp_now"]
    assert "changed" in detail.lower(), "the error must name the code change"
    assert "reconnect" in detail.lower(), "the error must name the remedy"
    assert "/mcp" in second["reconnect"]
    # The original failure is carried, so the caller sees what actually failed.
    assert "Extra inputs are not permitted" in second["original_error"]


def test_a_successful_call_on_a_moved_checkout_is_returned_untouched(
    tmp_path: Path,
) -> None:
    """Most commits move the code without breaking the image, so a good call stands."""
    root = _build_checkout(tmp_path)
    payload = _run_server(tmp_path, root, "moved_and_well")
    _assert_served_from_the_copy(payload, root)

    first, second = payload["first"], payload["second"]
    assert first.get("ok") is True, f"a fresh server must answer normally: {first!r}"
    assert second.get("ok") is True, (
        f"a call that succeeds must be returned untouched whatever the stamp reads: {second!r}"
    )
    assert second.get("error") != "stale_server_code"
    assert "stale_server_code" not in json.dumps(second)


def test_a_checkout_that_does_not_import_returns_the_original_failure(
    tmp_path: Path,
) -> None:
    """A tree that will not load never has its failure mistaken for staleness."""
    root = _build_checkout(tmp_path)
    payload = _run_server(tmp_path, root, "unimportable_and_broken")
    _assert_served_from_the_copy(payload, root)

    first, second = payload["first"], payload["second"]
    assert first.get("ok") is True, f"a fresh server must answer normally: {first!r}"
    assert second.get("error") != "stale_server_code", (
        f"a checkout that does not import must not be called stale, got {second!r}"
    )
    # The original failure stands, unchanged.
    assert second.get("error") == "crew_error"
    assert "Extra inputs are not permitted" in second.get("detail", "")
