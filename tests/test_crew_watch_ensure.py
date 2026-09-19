"""The project watcher as an idempotent user service.

A watcher holds the seat that admits dispatches, so the command that starts one
has to be both durable — it must survive the shell that ran it, carrying the
backend directory its lifts need — and safe to run twice, because the refusal a
person acts on names it. Both halves are asserted here with a service manager
that records what it was asked to do, so no unit reaches the real account home
and no systemd manager need exist on the host.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from reckon.crew import runs


def _backend_bin(tmp_path: Path) -> Path:
    """A directory holding one backend executable, on no ambient PATH."""
    directory = tmp_path / "backend-bin"
    directory.mkdir(parents=True, exist_ok=True)
    executable = directory / "codexx"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return directory


def _config(backend_bin: Path) -> dict:
    """A project routing to a backend only reachable through its own PATH."""
    return {
        "default_backend": "alpha",
        "backends": {
            "alpha": {
                "launch": "cli",
                "command": "codexx",
                "model": "some-model",
                "effort": "high",
                "sandbox": "worktree-full",
                "session_reuse": True,
                "time_budget": "25m",
                # The directory is stated by the backend, not inherited: this is
                # the case that failed in the field, where the unit ran for
                # hours with the backend absent from its own PATH.
                "environment": {"PATH": str(backend_bin)},
            }
        },
        "roles": {"implement": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }


class FakeWatchService:
    """A service manager that records every call instead of acting on one."""

    def __init__(self) -> None:
        self.units: dict[str, str] = {}
        self.starts: list[tuple[str, bool]] = []
        self._active = False
        self._linger = True

    def unit_path(self, project: str) -> Path:
        return Path("/nonexistent/systemd") / runs.watch_unit_name(project)

    def installed(self, project: str) -> bool:
        return project in self.units

    def active(self, project: str) -> bool:
        return self._active

    def write_unit(self, project: str, content: str) -> tuple[Path, bool]:
        changed = self.units.get(project) != content
        self.units[project] = content
        return self.unit_path(project), changed

    def start(self, project: str, *, restart: bool) -> None:
        self.starts.append((runs.watch_unit_name(project), restart))
        self._active = True

    def lingering(self) -> bool:
        return self._linger

    def enable_linger(self) -> None:
        self._linger = True


@pytest.fixture()
def service_home(tmp_path: Path, monkeypatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _unit_path_line(content: str) -> str:
    for line in content.splitlines():
        if line.startswith('Environment="PATH='):
            return line
    raise AssertionError(f"the unit carries no PATH line:\n{content}")


def test_ensure_starts_a_unit_carrying_the_backend_directory(
    service_home: Path, tmp_path: Path
) -> None:
    """A host with no unit gets one, whose PATH holds the backend's own dir."""
    backend_bin = _backend_bin(tmp_path)
    assert str(backend_bin) not in (os.environ.get("PATH") or "")
    manager = FakeWatchService()

    result = runs.ensure_watcher_service(
        "sample", manager=manager, config=_config(backend_bin)
    )

    assert result["started"] is True
    assert result["unit"] == runs.watch_unit_name("sample")
    assert manager.starts == [(runs.watch_unit_name("sample"), False)]
    content = manager.units["sample"]
    # The directory the launch would search is the one the unit must carry; the
    # ambient PATH cannot stand in for it, which is why the assertion above is
    # made against a directory that is on no ambient PATH.
    assert str(backend_bin) in _unit_path_line(content)
    # The seat is not held by ensure itself: a service that never comes up must
    # not read as a live watcher.
    assert runs.watch_state("sample")["watcher_live"] is False


def test_a_second_ensure_reports_the_existing_unit_and_starts_nothing(
    service_home: Path, tmp_path: Path
) -> None:
    """The command a refusal names must be safe to run against a live watcher."""
    backend_bin = _backend_bin(tmp_path)
    manager = FakeWatchService()
    runs.ensure_watcher_service("sample", manager=manager, config=_config(backend_bin))

    second = runs.ensure_watcher_service(
        "sample", manager=manager, config=_config(backend_bin)
    )

    assert second["started"] is False
    assert second["unit_changed"] is False
    assert "started nothing" in second["detail"]
    assert manager.starts == [(runs.watch_unit_name("sample"), False)]


def test_a_changed_unit_is_restarted_onto_the_new_definition(
    service_home: Path, tmp_path: Path
) -> None:
    """Idempotent is not inert: a rewritten unit must be picked up."""
    backend_bin = _backend_bin(tmp_path)
    manager = FakeWatchService()
    runs.ensure_watcher_service("sample", manager=manager, config=_config(backend_bin))

    # A second backend directory rewrites the PATH line, so the running unit no
    # longer matches what would be written now.
    other_bin = tmp_path / "other-bin"
    other_bin.mkdir()
    (other_bin / "codexx").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (other_bin / "codexx").chmod(0o755)
    rewritten = _config(backend_bin)
    rewritten["backends"]["alpha"]["environment"] = {"PATH": str(other_bin)}

    result = runs.ensure_watcher_service("sample", manager=manager, config=rewritten)

    assert result["unit_changed"] is True
    assert result["started"] is True
    assert manager.starts[-1] == (runs.watch_unit_name("sample"), True)


def test_the_registration_carries_the_unit_name(
    service_home: Path, tmp_path: Path
) -> None:
    """A reader can tell which unit owns the seat, and that a service will own it."""
    backend_bin = _backend_bin(tmp_path)
    manager = FakeWatchService()

    result = runs.ensure_watcher_service(
        "sample", manager=manager, config=_config(backend_bin)
    )

    assert result["registration"] == {
        "registered": True,
        "reason": "written",
        "unit": runs.watch_unit_name("sample"),
    }
    assert runs.watch_state("sample")["unit"] == runs.watch_unit_name("sample")


def test_a_watcher_claims_its_seat_under_the_unit_that_started_it(
    service_home: Path, monkeypatch, tmp_path: Path
) -> None:
    """The seat a service-armed watcher takes names its own unit.

    Written by the watcher rather than by ensure, because the claim is what
    makes the registration true: a unit that was ensured and never came up must
    not leave a seat record naming it.
    """
    monkeypatch.setenv(runs.WATCH_UNIT_ENV, runs.watch_unit_name("sample"))

    with runs._project_watch_claim("sample", "1h") as (acquired, record):
        assert acquired is True
        assert record["unit"] == runs.watch_unit_name("sample")


def test_an_ensure_line_names_the_command_the_refusal_teaches(
    service_home: Path,
) -> None:
    """The ensure command is a value a caller reads, not prose to retype."""
    line = runs.watcher_ensure_line("sample")

    assert line == "reckon crew watch --ensure --project sample"
    assert runs.watch_state("sample")["ensure_line"] == line
