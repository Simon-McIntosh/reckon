"""An unchanged mount registry is read, and its paths resolved, once.

The registry is the host's repository-authority boundary: every project's docs
directory is found through it, and a producer tick asks for one project's docs
root per unit of work rather than once per tick. Re-reading the file and
running ``realpath`` on every mount entry for each ask is the cost this holds
down, because the registry does not change between asks.

The negative control runs this file against the revision before the memo, where
the same work reads the registry 150 times instead of once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from reckon import file_memo, flight
from reckon._store import _state_root

_PROJECTS = ("alpha", "beta", "gamma")
_CALLS_PER_PROJECT = 50
_DROPPED = "beta"


@dataclass
class _RegistryTraffic:
    """What the code under test asked of the registry while it ran."""

    reads: int = 0
    resolves: int = 0


@dataclass
class _Registry:
    """The registry a test wrote, held beside the file that carries it."""

    mounts_file: Path
    mounts: dict[str, str]


@pytest.fixture()
def registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Registry:
    """A configuration home registering three projects, each with a layer.

    Each project's own flight layer sets a different window, so a resolution
    that reached its mount is visible in the resolved values and not only in a
    path.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    mounts: dict[str, str] = {}
    for index, project in enumerate(_PROJECTS):
        docs = tmp_path / "checkouts" / project / "docs"
        layer = docs / "state" / project
        layer.mkdir(parents=True)
        (layer / "flight.yaml").write_text(f"sprint_recent_days: {index}\n")
        mounts[project] = str(docs)

    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps(mounts))

    state_root = tmp_path / "state"
    state_root.mkdir()
    unregistered = state_root / _DROPPED
    unregistered.mkdir()
    (unregistered / "flight.yaml").write_text("sprint_recent_days: 7\n")

    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    file_memo.clear()
    yield _Registry(mounts_file=mounts_file, mounts=mounts)
    file_memo.clear()


@pytest.fixture()
def traffic(monkeypatch: pytest.MonkeyPatch, registered: _Registry) -> _RegistryTraffic:
    """Count reads of the registry and ``Path.resolve`` calls on mount entries.

    A read is a read of the registry file itself, so a stand-in that never fired
    shows as zero rather than as a passing absence. A resolve is counted only
    for one of the registry's own entry paths, which leaves out the resolves of
    the registry path and of the state root around them.
    """
    counts = _RegistryTraffic()
    entry_paths = {Path(path) for path in registered.mounts.values()}
    read_text = Path.read_text
    resolve = Path.resolve

    def counting_read_text(self, *args, **kwargs):
        if Path(self) == registered.mounts_file:
            counts.reads += 1
        return read_text(self, *args, **kwargs)

    def counting_resolve(self, *args, **kwargs):
        if Path(self) in entry_paths:
            counts.resolves += 1
        return resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)
    monkeypatch.setattr(Path, "resolve", counting_resolve)
    return counts


def _direct(project: str, docs: Path) -> flight.ResolvedFlight:
    """``project``'s layer set resolved by the docs root the registry names."""
    return flight.resolve(
        project, project_path=docs / "state" / project / "flight.yaml"
    )


def test_an_unchanged_registry_is_read_and_resolved_once(
    traffic: _RegistryTraffic, registered: _Registry
) -> None:
    mounted = registered.mounts
    expected = {
        project: _direct(project, Path(mounted[project])) for project in _PROJECTS
    }

    resolved: dict[str, flight.ResolvedFlight] = {}
    for project in _PROJECTS:
        for _ in range(_CALLS_PER_PROJECT):
            resolved[project] = flight.resolve(project)
    # The instrument has to have fired for the one-read count below to mean
    # reuse: a stand-in that never ran would count zero reads, not one.
    assert traffic.reads == 1
    assert traffic.resolves == len(_PROJECTS)
    for project in _PROJECTS:
        assert resolved[project] == expected[project]
        assert flight.project_config_path(project) == (
            Path(mounted[project]) / "state" / project / "flight.yaml"
        )


def test_a_rewritten_registry_is_read_again(
    traffic: _RegistryTraffic, registered: _Registry
) -> None:
    mounted = registered.mounts
    survivors = [project for project in _PROJECTS if project != _DROPPED]
    for project in survivors:
        flight.resolve(project)
    assert traffic.reads == 1
    assert traffic.resolves == len(_PROJECTS)

    registered.mounts_file.write_text(
        json.dumps({project: mounted[project] for project in survivors})
    )

    for project in survivors:
        flight.resolve(project)
    assert traffic.reads == 2
    assert traffic.resolves == len(_PROJECTS) + len(survivors)

    fallback = flight.resolve(_DROPPED)
    assert traffic.reads == 2
    assert traffic.resolves == len(_PROJECTS) + len(survivors)
    # No mount: the same state-root fallback the dropped project had at base.
    assert flight.project_config_path(_DROPPED) == (
        _state_root() / _DROPPED / "flight.yaml"
    )
    assert fallback == _direct(_DROPPED, _state_root().parent)
