"""The split leak scan reads the operator's mount registry, whatever home is set.

``tests/test_split_existing_runs.py`` watches the workstation's own mounted
projects so a migration that resolved to a real project is caught. The registry
it must consult is the operator's, captured when the conftest is imported, before
any fixture replaces the configuration home. These tests empty that home and
assert the reader — and the leak-scan fixture built on it — still return the
operator's mounts; the vacuity guard fires if they return none, which is exactly
what a reader resolving the live home did once a peer's throwaway home was in
force.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import workstation_mounts
from tests.test_split_existing_runs import workstation_state


def _empty_configuration_home(tmp_path, monkeypatch) -> None:
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(empty_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(empty_home / "mounts.json"))


def _assert_the_scan_can_see_a_mounted_project(mounts: dict[str, Path]) -> None:
    assert any(
        (Path(docs) / "state" / project).exists() for project, docs in mounts.items()
    ), (
        "the leak scan sees no state under the workstation's mounts, so its "
        "untouched assertion would be vacuous"
    )


def test_the_reader_keeps_the_operators_mounts_under_an_empty_home(
    tmp_path, monkeypatch
) -> None:
    _empty_configuration_home(tmp_path, monkeypatch)

    mounts = workstation_mounts()

    _assert_the_scan_can_see_a_mounted_project(mounts)


def test_the_leak_scan_fixture_reads_the_registry_captured_at_import(
    tmp_path, monkeypatch
) -> None:
    _empty_configuration_home(tmp_path, monkeypatch)

    # The fixture body must read the captured registry, not the configuration
    # home in force here: resolving the live home would return nothing and fire
    # the fixture's own vacuity guard. Calling the wrapped body bypasses the
    # session cache, so this pins the fixture itself rather than a stale mount
    # set from an earlier test.
    mounts = workstation_state.__wrapped__()

    _assert_the_scan_can_see_a_mounted_project(mounts)
