"""A launch finds its backend through a search path the host declares.

A host process can be restarted without reading the shell's startup files, so
the PATH it hands a launch can omit the directory an installed backend lives
in. The backend's location is a fact about the host, so the flight layer
declares it: a command the launcher's PATH misses is still resolved through
that declaration, and the composed worker environment carries the directory so
the worker resolves the same command its launcher did. A command on neither
path is refused, and the refusal names both.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import flight
from reckon.crew import dispatch_launch

BACKEND = "stub-lane"
COMMAND = "stub-backend"


def _write_layer(tmp_path: Path, *, search_path: list[str]) -> Path:
    directory = tmp_path / "flight"
    directory.mkdir()
    layer = directory / "flight.yaml"
    entries = "".join(f"      - {entry}\n" for entry in search_path)
    layer.write_text(
        "version: 1\n"
        "backends:\n"
        f"  {BACKEND}:\n"
        "    launch: cli\n"
        f"    command: {COMMAND}\n"
        "    search_path:\n" + entries,
        encoding="utf-8",
    )
    return layer


def _write_stub(directory: Path, name: str = COMMAND) -> Path:
    binary = directory / name
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


@pytest.fixture()
def declared_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A flight layer declaring a directory that holds the stub backend."""
    bindir = tmp_path / "backend-bin"
    bindir.mkdir()
    _write_stub(bindir)
    layer = _write_layer(tmp_path, search_path=[str(bindir)])
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(layer))
    return bindir


def _facts() -> SimpleNamespace:
    return SimpleNamespace(in_allocation=False)


def test_a_launch_resolves_a_backend_the_launching_path_lacks(
    declared_backend: Path,
) -> None:
    """The stub is found through the declared path, not the launching PATH."""
    launching = os.pathsep.join(["/usr/bin", "/bin"])

    resolved = dispatch_launch.preflight_launch_command(
        BACKEND,
        {"command": COMMAND, "environment": {"PATH": launching}},
        fence=False,
        facts=_facts(),
    )

    assert resolved == str(declared_backend / COMMAND)
    assert str(declared_backend) not in launching.split(os.pathsep)


def test_the_composed_worker_environment_carries_the_declared_directory(
    declared_backend: Path,
) -> None:
    """The worker's own PATH carries the directory its command was found in."""
    composed = dispatch_launch._persisted_worker_environment(
        {"PATH": "/usr/bin"}, facts=_facts(), fenced=True, backend_name=BACKEND
    )
    entries = composed["PATH"].split(os.pathsep)

    assert str(declared_backend) in entries
    # The launcher's own PATH is searched first, so the declaration follows it.
    assert entries.index("/usr/bin") < entries.index(str(declared_backend))

    # The declaration is what put it there: a launch naming no backend does not
    # carry another backend's directory.
    undeclared = dispatch_launch._persisted_worker_environment(
        {"PATH": "/usr/bin"}, facts=_facts(), fenced=True
    )
    assert str(declared_backend) not in undeclared["PATH"].split(os.pathsep)


def test_a_backend_on_neither_path_is_refused_naming_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command found nowhere refuses, and the message names both searches."""
    empty_declared = tmp_path / "empty-declared"
    empty_declared.mkdir()
    launching = tmp_path / "launching-bin"
    launching.mkdir()
    layer = _write_layer(tmp_path, search_path=[str(empty_declared)])
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(layer))

    with pytest.raises(dispatch_launch.LaunchResolutionError) as refused:
        dispatch_launch.preflight_launch_command(
            BACKEND,
            {"command": COMMAND, "environment": {"PATH": str(launching)}},
            fence=False,
            facts=_facts(),
        )

    message = str(refused.value)
    assert COMMAND in message
    assert str(launching) in message
    assert str(empty_declared) in message


def test_a_flight_layer_carrying_the_key_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declaration survives validation rather than being dropped."""
    declared = tmp_path / "declared-bin"
    layer = _write_layer(tmp_path, search_path=[str(declared)])

    resolved = flight.resolve(host_path=layer)

    assert resolved.config["backends"][BACKEND]["search_path"] == [str(declared)]
    assert not [key for key in resolved.unknown_keys if "search_path" in key], (
        resolved.unknown_keys
    )
