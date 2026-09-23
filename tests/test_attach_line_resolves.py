"""The follower attach line is runnable by the shell that arms it.

A follower is armed by whatever shell the coordinator's harness exposes, and
that shell need not carry the interpreter's bin directory on PATH -- the
Monitor tool's is the measured case. A line whose first token is the bare word
``reckon`` dies there at exec with exit 127, and a follower that never started
is indistinguishable from a quiet fleet, so the failure hides the very
delivery gap the line exists to close.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from reckon.crew import runs as runs_module

# A PATH that cannot reach the interpreter's bin directory. The composed line
# must still run against it, which is the whole point of naming a path.
RESTRICTED_PATH = "/usr/bin:/bin"


def _first_token(line: str) -> str:
    """Return the command's first token, as a shell would read it."""
    return shlex.split(line)[0]


def test_the_attach_line_runs_under_a_path_that_cannot_reach_the_venv() -> None:
    line = runs_module._watch_attach_line("nova", session="s18-hexgrid")

    token = _first_token(line)
    assert Path(token).is_absolute(), (
        f"the attach line's first token is {token!r}, which the arming shell "
        "resolves through PATH"
    )
    assert Path(token).is_file(), f"the attach line names {token!r}, which is absent"
    assert os.access(token, os.X_OK), f"the attach line names {token!r}, not executable"

    result = subprocess.run(
        [token, "crew", "follow", "--help"],
        env={"PATH": RESTRICTED_PATH},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"{token} crew follow --help exited {result.returncode} under "
        f"PATH={RESTRICTED_PATH}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_composing_the_line_is_refused_when_no_executable_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A line that cannot run is refused, never composed with a bare name."""
    monkeypatch.setattr(
        runs_module.sys, "executable", str(tmp_path / "absent" / "python")
    )
    monkeypatch.setattr(runs_module.shutil, "which", lambda _name: None)

    with pytest.raises(runs_module.CrewError) as refusal:
        runs_module._watch_attach_line("nova", session="s18-hexgrid")

    assert "reckon" in str(refusal.value)
