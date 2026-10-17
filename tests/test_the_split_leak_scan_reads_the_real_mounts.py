"""The split leak scan reads the operator's mount registry, whatever home is set.

``tests/test_split_existing_runs.py`` watches the workstation's own mounted
projects so a migration that resolved to a real project is caught. The registry
it must consult is the operator's, captured when the conftest is imported, before
any fixture replaces the configuration home. This test empties that home and
asserts the reader still returns the operator's mounts; the vacuity guard fires
if it returns none, which is exactly what the live-resolving reader did once a
peer's throwaway home was in force.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import workstation_mounts


def test_the_reader_keeps_the_operators_mounts_under_an_empty_home(
    tmp_path, monkeypatch
) -> None:
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(empty_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(empty_home / "mounts.json"))

    mounts = workstation_mounts()

    assert mounts, "the leak scan's reader returned no mounts under an empty home"
    assert any(
        (Path(docs) / "state" / project).exists() for project, docs in mounts.items()
    ), (
        "the leak scan sees no state under the workstation's mounts, so its "
        "untouched assertion would be vacuous"
    )
