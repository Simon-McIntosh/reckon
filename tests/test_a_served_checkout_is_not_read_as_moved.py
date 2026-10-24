"""A checkout that has not moved reads as unmoved.

The MCP's stale-code guard asks one question on every call: has the checkout's
code moved behind the running image? It compares the identity the image
recorded when it imported with the identity of the tree that supplied it. Read
against the wrong directory the comparison never matches, so an untouched
checkout reads as moved and every call is served by a child started over the
same tree.

The identity is never recomputed here. A child interpreter imports the package
from the checkout and prints the stamp the server records at start, and the
case then asserts what the guard answers for that stamp -- unmoved while the
checkout still matches its commit, moved once a commit changes the package.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from reckon import served_code
from tests.test_one_check_trusts_a_moved_checkout import (
    _GIT,
    _committed_checkout,
    _git,
)

_STAMP_PROGRAM = "import reckon.mcp; print(reckon.mcp._SERVER_CODE_STAMP)"


def _recorded_stamp(root: Path, tmp_path: Path) -> str:
    """The stamp a server importing the package from ``root`` records at start.

    The stamp is read from a fresh interpreter rather than from this one: this
    process imported the worktree's package, whereas the question is what a
    server started over ``root`` would record. Its own tree check runs the real
    git binary, so a worker session's ``git`` shim cannot answer it.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "git").symlink_to(_GIT)
    working = tmp_path / "working"
    working.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    completed = subprocess.run(
        [sys.executable, "-c", _STAMP_PROGRAM],
        capture_output=True,
        text=True,
        check=False,
        cwd=working,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHONPATH": str(root),
            "RECKON_HOME": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    return completed.stdout.strip()


def test_an_unmoved_checkout_reads_as_unmoved(tmp_path: Path) -> None:
    """The tree that supplied the image is read as the image's own code."""
    root = _committed_checkout(tmp_path)
    running = _recorded_stamp(root, tmp_path)

    state = served_code.moved_checkout(root=root, running_stamp=running)

    assert state.moved is False, (
        f"a checkout that has not moved must not read as moved, got {state!r}"
    )


def test_a_checkout_whose_package_moved_reads_as_moved(tmp_path: Path) -> None:
    """A commit that changes the package is a move, so the guard says so."""
    root = _committed_checkout(tmp_path)
    running = _recorded_stamp(root, tmp_path)
    target = root / "reckon" / "crew" / "runs.py"
    target.write_text(target.read_text() + "\n# a change the image did not import\n")
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
        "move the code",
    )

    state = served_code.moved_checkout(root=root, running_stamp=running)

    assert state.moved is True, (
        f"a checkout whose package moved must read as moved, got {state!r}"
    )
