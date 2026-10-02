"""A protected bind source that is briefly absent does not abandon a launch.

A writer that replaces a file by rename removes its name for an instant. A
protected path bound during that instant aborts the launch with bubblewrap's
``Can't bind mount ... No such file or directory``, and a path dropped from the
composed set instead would have been left writable to the worker. The fence now
waits, bounded, for every absent protected source: one that appears during the
wait is bound exactly as one that was never absent, one a layer declares but
that never appears refuses the launch naming it, and a shipped default this
machine does not have is left out as before.

Every test here works against a temporary operator home and temporary protected
paths, so none reads or writes the real one.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from reckon import _backends


def _fence(home: Path, declared: list[str], argv: list[str] | None = None) -> list[str]:
    """Compose a fence over a temporary home with a layer's declared paths."""
    return _backends.fence_argv(
        argv or ["true"],
        home=str(home),
        config={"protected_paths": declared},
    )


def _pair(argv: list[str], target: str) -> list[str] | None:
    """Return the ``--ro-bind`` argument triple mounting ``target``, else None."""
    for index, word in enumerate(argv):
        if word == "--ro-bind" and argv[index + 1 : index + 3] == [target, target]:
            return argv[index : index + 3]
    return None


def test_a_protected_file_that_appears_after_a_delay_is_bound(tmp_path) -> None:
    """A path absent when the fence is composed is waited for, then bound."""
    home = tmp_path / "operator-home"
    home.mkdir()
    delayed = home / ".claude.json"
    timer = threading.Timer(0.2, delayed.write_text, args=("{}",))
    timer.start()
    try:
        assert not delayed.exists(), "the path must be absent when the fence is built"
        argv = _fence(home, [str(delayed)])
    finally:
        timer.cancel()

    assert delayed.exists(), "the delayed writer must have run during the wait"
    assert _pair(argv, str(delayed)) == ["--ro-bind", str(delayed), str(delayed)]


def test_a_declared_path_that_never_appears_refuses_the_launch(tmp_path) -> None:
    """A declared protection the fence cannot honour refuses, naming the path."""
    home = tmp_path / "operator-home"
    home.mkdir()
    missing = home / ".never-appears"

    with pytest.raises(_backends.BackendError) as refusal:
        _fence(home, [str(missing)])

    assert str(missing) in str(refusal.value)


def test_an_absent_shipped_default_is_left_out_rather_than_refused(tmp_path) -> None:
    """A default this machine does not have is skipped, not a launch failure."""
    home = tmp_path / "operator-home"
    home.mkdir()
    (home / ".claude").mkdir(parents=True)

    argv = _fence(home, [])

    assert _pair(argv, str(home / ".claude")) == [
        "--ro-bind",
        str(home / ".claude"),
        str(home / ".claude"),
    ]
    assert _pair(argv, str(home / ".netrc")) is None
