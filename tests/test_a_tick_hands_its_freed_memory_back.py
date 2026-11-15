"""A station tick hands the memory it freed back to the kernel.

The station's tick decodes gigabytes of stream and review-index JSON and holds a
few hundred megabytes afterwards, and the allocator keeps the arenas the pass
freed rather than returning them. Unreleased, the station's RSS tracks the
largest tick it has ever run instead of the memory it still holds, which is what
drove the fleet job's cgroup to its limit and had the station OOM-killed.

The release is the C library's own ``malloc_trim``, asked for once after the pass
has done its work — and after a pass that raises as much as after one that
returns, because a failed tick has freed its memory too. A platform whose C
library does not offer the call runs the tick unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import station, station_store


@pytest.fixture()
def node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> station.Station:
    """A station over a temporary crew home and its own store."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(tmp_path / "mounts.json"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    (tmp_path / "mounts.json").write_text("{}", encoding="utf-8")
    (tmp_path / "runtime").mkdir()
    store = station_store.StationStore(tmp_path / "runtime")
    return station.Station(store, home=tmp_path / "crew" / "home", projects=dict)


def _stand_in(events: list[str], *, failure: BaseException | None = None):
    """A tick body that records its own work and optionally raises."""

    def work(*, now=None):
        events.append("tick")
        if failure is not None:
            raise failure
        return []

    return work


def test_each_tick_releases_after_its_own_work(
    node: station.Station, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    def trim(pad: int) -> int:
        events.append("release")
        assert pad == 0, "the release needs no padding to hand memory back"
        return 1

    monkeypatch.setattr(station, "_malloc_trim", lambda: trim)
    monkeypatch.setattr(node, "_tick", _stand_in(events))

    for _ in range(3):
        assert node.tick() == []

    assert events == ["tick", "release"] * 3


def test_a_tick_that_raises_still_releases(
    node: station.Station, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    trim = lambda pad: events.append("release") or 1  # noqa: E731 - a recorder
    monkeypatch.setattr(station, "_malloc_trim", lambda: trim)
    monkeypatch.setattr(node, "_tick", _stand_in(events, failure=RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        node.tick()

    assert events == ["tick", "release"]


def test_a_library_without_the_call_runs_the_tick_unchanged(
    node: station.Station, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    monkeypatch.setattr(station, "_malloc_trim", lambda: None)
    monkeypatch.setattr(node, "_tick", _stand_in(events))

    assert node.tick() == []
    assert events == ["tick"]
    assert station.release_freed_memory() is False
