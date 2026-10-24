"""A checkout that has not moved reads as unmoved.

The MCP's stale-code guard asks one question on every call: has the checkout's
code moved behind the running image? It compares the identity the image
recorded when it imported with the identity of the tree that supplied it. Read
against the wrong directory the comparison never matches, so an untouched
checkout reads as moved and every call is served by a child started over the
same tree.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from reckon import checkout_trust, served_code

SOURCE_PACKAGE = Path(__file__).resolve().parents[1] / "reckon"


def _git(root: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, (
        f"git {' '.join(args)} failed in {root}: {completed.stderr!r}"
    )


def _committed_checkout(tmp_path: Path) -> Path:
    """A self-contained committed copy of the package, as a checkout names it."""
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


def test_an_unmoved_checkout_reads_as_unmoved(tmp_path: Path) -> None:
    """The tree that supplied the image is read as the image's own code.

    The running stamp is read exactly as a server reads it at import, from the
    package directory inside the checkout. An unmoved tree therefore answers
    with that same stamp and the guard serves the call in process.
    """
    root = _committed_checkout(tmp_path)
    running = checkout_trust.source_code_identity(root / "reckon")

    state = served_code.moved_checkout(root=root, running_stamp=running)

    assert state.disk_stamp == running, (
        f"the checkout's stamp is read from the package it holds, got {state!r}"
    )
    assert state.moved is False, (
        f"a checkout that has not moved must not read as moved, got {state!r}"
    )
