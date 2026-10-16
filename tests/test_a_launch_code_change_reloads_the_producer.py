"""One package-wide source stamp reloads the producer when any module changes.

The producer is a long-lived ``reckon crew watch`` process and it launches the
reflex reviews and sweep resumes in-process, reloading itself only when its
source stamp moves. A launch is built from modules outside the CLI set and
``reckon/crew`` — ``_worker_fence.py``, ``_backends.py``, ``nested_launch.py`` —
so the stamp must cover every module under the package, not only the command
modules, or a launch-code repair reaches a running producer only by accident.

A stamp is keyed on file *content*, not on the mtime and size alone: a tool
that rewrites a file with identical bytes must not reload a live producer. Both
former callers, ``source_code_stamp`` and ``follower_code_stamp``, read the one
implementation, so their set cannot drift apart.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from reckon import cli as cli_module
from reckon.crew import follower_registration, obligation_snapshot

# A module a launch is built from and that the command-module set excludes.
LAUNCH_MODULE = "_worker_fence.py"


def _copied_package(tmp_path: Path) -> Path:
    """A temporary copy of the reckon package, isolated from the real tree."""
    source = Path(cli_module.__file__).resolve().parent
    copied = tmp_path / "reckon"
    shutil.copytree(source, copied)
    return copied


def _both_stamps(package_dir: Path) -> tuple[str, str]:
    """Both former callers, computed over ``package_dir``'s bytes.

    ``follower_code_stamp`` resolves its package from
    ``follower_registration.__file__``, so pointing that at the copy runs it
    against the same tree ``source_code_stamp`` is handed.
    """
    source = obligation_snapshot.source_code_stamp(package_dir)
    original = follower_registration.__file__
    try:
        follower_registration.__file__ = str(
            package_dir / "crew" / "follower_registration.py"
        )
        follower = follower_registration.follower_code_stamp()
    finally:
        follower_registration.__file__ = original
    return source, follower


def test_stamp_advances_when_a_launch_module_changes(tmp_path: Path) -> None:
    """A content change to a non-CLI module moves the stamp."""
    package_dir = _copied_package(tmp_path)
    module = package_dir / LAUNCH_MODULE
    baseline = _both_stamps(package_dir)

    original = module.read_bytes()
    module.write_bytes(original + b"\n# launch-code probe\n")
    try:
        changed = _both_stamps(package_dir)
    finally:
        module.write_bytes(original)

    assert changed[0] != baseline[0], (
        f"source_code_stamp did not move when {LAUNCH_MODULE} changed"
    )
    assert changed[1] != baseline[1], (
        f"follower_code_stamp did not move when {LAUNCH_MODULE} changed"
    )


def test_stamp_ignores_an_mtime_only_change(tmp_path: Path) -> None:
    """A touched file with identical bytes is not new code."""
    package_dir = _copied_package(tmp_path)
    module = package_dir / LAUNCH_MODULE
    baseline = _both_stamps(package_dir)

    stat = module.stat()
    os.utime(module, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))

    assert _both_stamps(package_dir) == baseline, (
        f"an mtime-only change to {LAUNCH_MODULE} moved the stamp"
    )


def test_both_callers_agree_on_one_tree(tmp_path: Path) -> None:
    """The two former callers return the same stamp for one tree."""
    package_dir = _copied_package(tmp_path)
    source, follower = _both_stamps(package_dir)

    assert source == follower, (
        "the two callers disagree on one tree; they read different source"
    )
