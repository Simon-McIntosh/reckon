"""The CLI's modules are declared once and both code stamps read that set.

Two code stamps decide whether a running producer (``source_code_stamp`` in
``reckon/crew/obligation_snapshot.py``) or a running follower
(``follower_code_stamp`` in ``reckon/crew/runs.py``) is on old code. Each used
to list the CLI's module names as its own literals, so a module that carried a
command but was named in one list and not the other reloaded one process and
left the other stale, silently.

The declaration now lives in :data:`reckon.crew.obligation_snapshot.CLI_MODULE_FILES`,
relative to the package directory. The first case walks the click tree from its
root group and derives the modules that actually carry commands, so a new
command module that is not declared is named rather than merely missed. The
second case proves both stamps read the declaration: changing any declared
module's bytes moves both stamps, and changing nothing leaves both where they
were.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import click

from reckon import cli as cli_module
from reckon.crew import obligation_snapshot, runs

ROOT_GROUP = cli_module.main


def _command_modules() -> set[str]:
    """Every module that defines a callback in the CLI's click tree."""
    modules: set[str] = set()
    stack: list[click.Command] = [ROOT_GROUP]
    while stack:
        command = stack.pop()
        callback = getattr(command, "callback", None)
        if callback is not None:
            module = getattr(callback, "__module__", None)
            if module:
                modules.add(module)
        if isinstance(command, click.Group):
            stack.extend(command.commands.values())
    return modules


def _top_level_reckon_file(module: str) -> str | None:
    """The package-relative file name of a top-level ``reckon`` module.

    A module under a sub-package (``reckon.crew.``) is covered by each stamp's
    ``crew/*.py`` glob rather than by the declared tuple, so it resolves to no
    sub-package name here and is left to the glob.
    """
    if not module.startswith("reckon."):
        return None
    remainder = module[len("reckon.") :]
    if not remainder or "." in remainder:
        return None
    return f"{remainder}.py"


def test_every_command_module_is_declared() -> None:
    """No top-level reckon module carries a command the declaration omits."""
    declared = set(obligation_snapshot.CLI_MODULE_FILES)
    collected = _command_modules()

    # A positive control: the walk must see the tree, or an empty result would
    # read as agreement with an empty declaration.
    found = {name for m in collected if (name := _top_level_reckon_file(m))}
    assert found, "the click tree yielded no top-level reckon command modules"

    missing = sorted(name for name in found if name not in declared)
    assert not missing, (
        "the CLI's declared module set omits module(s) that carry commands: "
        f"{missing}; add them to CLI_MODULE_FILES in "
        "reckon/crew/obligation_snapshot.py"
    )


def _copied_package(tmp_path: Path) -> Path:
    source = Path(cli_module.__file__).resolve().parent

    copied = tmp_path / "reckon"
    shutil.copytree(source, copied)
    return copied


def _stamps(package_dir: Path) -> tuple[str, str]:
    """Both code stamps, computed over ``package_dir``'s bytes."""
    source = obligation_snapshot.source_code_stamp(package_dir)
    original = runs.__file__
    try:
        runs.__file__ = str(package_dir / "crew" / "runs.py")
        follower = runs.follower_code_stamp()
    finally:
        runs.__file__ = original
    return source, follower


def test_both_stamps_track_every_declared_module(tmp_path: Path) -> None:
    """Both stamps move on a declared module's edit and hold when nothing moves."""
    package_dir = _copied_package(tmp_path)

    baseline = _stamps(package_dir)
    assert baseline[0] == baseline[1], (
        "the two stamps disagree on an unchanged tree; they read different source"
    )

    # A second read with no edit in between must not move either stamp.
    assert _stamps(package_dir) == baseline

    for name in obligation_snapshot.CLI_MODULE_FILES:
        module = package_dir / name
        original = module.read_bytes()
        module.write_bytes(original + b"\n# stamp probe\n")
        try:
            changed = _stamps(package_dir)
        finally:
            module.write_bytes(original)
        assert changed[0] != baseline[0], (
            f"source_code_stamp did not move when {name} changed"
        )
        assert changed[1] != baseline[1], (
            f"follower_code_stamp did not move when {name} changed"
        )
        assert _stamps(package_dir) == baseline, (
            f"restoring {name}'s bytes did not restore the stamps"
        )
