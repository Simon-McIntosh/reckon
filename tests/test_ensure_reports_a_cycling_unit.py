"""``watch --ensure`` reports a cycling unit instead of a healthy one.

A watcher that systemd is restarting under a reader is not a live watcher, but
``ensure`` used to answer ``already active on an unchanged unit`` for exactly
that unit, because "active" and "not looping" are different questions and only
the first was asked. The two readings here are supplied through the manager and
sleeper seams ``ensure_watcher_service`` takes, so no unit reaches the real
account and no systemd manager need exist on the host.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import service
from reckon.crew import watch_unit


class CyclingWatchService:
    """A service manager whose unit reports a scripted restart count.

    ``active`` is true from the start so that a second ``ensure`` finds the unit
    already active on an unchanged definition, which is the state a cycling
    producer was reported as healthy from.
    """

    def __init__(self, readings: list[int]) -> None:
        self.units: dict[str, str] = {}
        self.starts: list[tuple[str, bool]] = []
        self._readings = list(readings)
        self._index = 0

    def unit_path(self, project: str) -> Path:
        return Path("/nonexistent/systemd") / watch_unit.watch_unit_name(project)

    def active(self, project: str) -> bool:
        return True

    def lingering(self) -> bool:
        return True

    def enable_linger(self) -> None:
        pass

    def write_unit(self, project: str, content: str) -> tuple[Path, bool]:
        changed = self.units.get(project) != content
        self.units[project] = content
        return self.unit_path(project), changed

    def start(self, project: str, *, restart: bool) -> None:
        self.starts.append((watch_unit.watch_unit_name(project), restart))

    def restart_count(self, project: str) -> int:
        value = self._readings[min(self._index, len(self._readings) - 1)]
        self._index += 1
        return value


class RecordingSleeper:
    """A sleeper that records the intervals it was asked to wait for."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


@pytest.fixture()
def ensure_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setattr(
        watch_unit, "_watcher_service_environment", lambda _config: {"PATH": "/bin"}
    )
    monkeypatch.setattr(
        watch_unit, "_reckon_console_script", lambda: "/nonexistent/bin/reckon"
    )
    return config_home


def _ensure_twice(manager: CyclingWatchService, sleeper: RecordingSleeper) -> dict:
    """Run ``ensure`` twice so the second call meets an unchanged, active unit.

    The first call writes the unit and starts it, so it is not the state under
    test; the second finds it active on the same definition and samples its
    restart count.
    """
    watch_unit.ensure_watcher_service(
        "sample", manager=manager, config={}, sleeper=sleeper
    )
    return watch_unit.ensure_watcher_service(
        "sample", manager=manager, config={}, sleeper=sleeper
    )


def test_a_rising_restart_count_is_reported_as_cycling(
    ensure_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A unit whose count rises between the readings is cycling, not healthy."""
    # Prove the real client is never reached: any call to it is a defect here.
    monkeypatch.setattr(
        service,
        "systemctl",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a real systemctl was called")
        ),
    )
    manager = CyclingWatchService([10, 12])
    sleeper = RecordingSleeper()

    result = _ensure_twice(manager, sleeper)

    assert result["cycling"] is True
    assert result["restart_count_before"] == 10
    assert result["restart_count_after"] == 12
    assert "cycling" in result["detail"]
    # The reported defect: a cycling unit read as healthy because it was active.
    assert "already active on an unchanged unit" not in result["detail"]
    # The two readings are a sample interval apart, and the interval is injected.
    assert sleeper.waits == [watch_unit.WATCH_RESTART_SAMPLE_INTERVAL]
    assert watch_unit.WATCH_RESTART_SAMPLE_INTERVAL > 6.7


def test_a_held_restart_count_is_reported_as_before(
    ensure_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A unit whose count holds across the readings is reported as before."""
    monkeypatch.setattr(
        service,
        "systemctl",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a real systemctl was called")
        ),
    )
    manager = CyclingWatchService([7, 7])
    sleeper = RecordingSleeper()

    result = _ensure_twice(manager, sleeper)

    assert result["cycling"] is False
    assert result["started"] is False
    assert "already active on an unchanged unit" in result["detail"]
    assert sleeper.waits == [watch_unit.WATCH_RESTART_SAMPLE_INTERVAL]


def test_a_freshly_started_unit_is_not_sampled(
    ensure_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A unit this call starts has a fresh count and is not put to two readings."""
    monkeypatch.setattr(
        service,
        "systemctl",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a real systemctl was called")
        ),
    )
    manager = CyclingWatchService([0, 0])
    sleeper = RecordingSleeper()

    result = watch_unit.ensure_watcher_service(
        "sample", manager=manager, config={}, sleeper=sleeper
    )

    assert result["started"] is True
    assert result["cycling"] is False
    assert sleeper.waits == []
