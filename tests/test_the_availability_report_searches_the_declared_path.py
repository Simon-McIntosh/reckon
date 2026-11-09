"""The availability report searches the declared backend path.

A backend's location is a fact about the host, so a flight layer may declare a
directory that holds its command while the launcher's own PATH does not. The
report must find it through that declaration, after the process PATH, and say
which search held it. Otherwise the report and a launch disagree about the same
backend: a lane that launches is reported missing.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from reckon import flight
from reckon.crew import dispatch_launch

BACKEND = "declared-lane"
COMMAND = "stub-declared-backend"
PROJECT = "report-project"


def _write_stub(directory: Path, name: str = COMMAND) -> Path:
    binary = directory / name
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


def _write_host_layer(tmp_path: Path, *, search_path: list[str] | None = None) -> Path:
    layer = tmp_path / "host-flight.yaml"
    lines = [
        "version: 1\n",
        "backends:\n",
        f"  {BACKEND}:\n",
        "    launch: cli\n",
        f"    command: {COMMAND}\n",
    ]
    if search_path is not None:
        lines.append("    search_path:\n")
        lines.extend(f"      - {entry}\n" for entry in search_path)
    layer.write_text("".join(lines), encoding="utf-8")
    return layer


def _write_project_layer(tmp_path: Path, search_path: list[str]) -> Path:
    state = tmp_path / "checkout" / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    layer = state / "flight.yaml"
    entries = "".join(f"      - {entry}\n" for entry in search_path)
    layer.write_text(
        f"version: 1\nbackends:\n  {BACKEND}:\n    search_path:\n" + entries,
        encoding="utf-8",
    )
    return layer


def _report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, project: bool = False
) -> dict[str, object]:
    """Probe the resolved config with a launch PATH that lacks the stub."""
    launching = tmp_path / "launching-bin"
    launching.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(launching))
    # The instrument must be able to miss: the launcher's own PATH holds no
    # copy of the stub, so only the declared path can find it.
    assert shutil.which(COMMAND) is None
    layers = {
        "host_path": tmp_path / "host-flight.yaml",
        "checkout_path": tmp_path / "checkout",
    }
    config = (
        flight.resolve(project=PROJECT, **layers).config
        if project
        else flight.resolve(**layers).config
    )
    return flight.probe_availability(config)[BACKEND]


def test_a_project_layer_declared_path_locates_the_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory declared only in the project's layer is searched."""
    bindir = tmp_path / "project-backend-bin"
    bindir.mkdir()
    _write_stub(bindir)
    _write_host_layer(tmp_path)
    _write_project_layer(tmp_path, [str(bindir)])

    entry = _report(tmp_path, monkeypatch, project=True)

    assert entry["command_found"] is True
    assert entry["command_source"] == str(bindir)
    assert entry["command_path"] == str(bindir / COMMAND)


def test_a_host_layer_declared_path_locates_the_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory declared in the host layer locates it too."""
    bindir = tmp_path / "host-backend-bin"
    bindir.mkdir()
    _write_stub(bindir)
    _write_host_layer(tmp_path, search_path=[str(bindir)])

    entry = _report(tmp_path, monkeypatch)

    assert entry["command_found"] is True
    assert entry["command_source"] == str(bindir)


def test_the_process_path_wins_over_the_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declaration follows the process PATH, and the report says so."""
    declared = tmp_path / "declared-bin"
    declared.mkdir()
    _write_stub(declared)
    on_path = tmp_path / "on-path-bin"
    on_path.mkdir()
    _write_stub(on_path)
    _write_host_layer(tmp_path, search_path=[str(declared)])
    monkeypatch.setenv("PATH", str(on_path))

    config = flight.resolve(host_path=tmp_path / "host-flight.yaml").config
    entry = flight.probe_availability(config)[BACKEND]

    assert entry["command_found"] is True
    assert entry["command_source"] == "PATH"
    assert entry["command_path"] == str(on_path / COMMAND)


def test_a_backend_on_neither_path_still_reads_found_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declaration that holds nothing leaves the report reporting it missing."""
    empty = tmp_path / "empty-declared"
    empty.mkdir()
    _write_host_layer(tmp_path, search_path=[str(empty)])

    entry = _report(tmp_path, monkeypatch)

    assert entry["command_found"] is False
    assert entry["command_path"] is None
    assert entry["command_source"] is None
    assert "not on PATH" in str(entry["detail"])


def test_a_layer_declaring_no_search_path_reports_the_process_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a declaration the report reads exactly as it did before."""
    _write_host_layer(tmp_path)

    entry = _report(tmp_path, monkeypatch)

    assert entry["command_found"] is False
    assert entry["command_source"] is None
    assert str(entry["detail"]) == f"'{COMMAND}' is not on PATH"


def test_a_declaration_is_kept_out_of_the_environment_untouched(
    tmp_path: Path,
) -> None:
    """The declaration is read, not written: home-relative entries expand only."""
    declared = tmp_path / "declared-bin"
    layer = _write_host_layer(tmp_path, search_path=["~/somewhere", str(declared)])

    resolved = flight.resolve(host_path=layer).config["backends"][BACKEND]

    assert resolved["search_path"] == ["~/somewhere", str(declared)]
    assert dispatch_launch.declared_backend_search_path(resolved) == [
        str(Path("~/somewhere").expanduser()),
        str(declared),
    ]
