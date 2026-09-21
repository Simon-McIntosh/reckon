"""Arming a watcher when the service manager cannot be reached.

The user manager is a single point of failure for ``reckon crew watch --ensure``:
a watcher has to come up even when ``systemctl --user`` cannot reach its bus,
because a project with no watcher refuses every dispatch. The fallback this
module pins is a plain background process, and the two halves of the measurement
are the bus failing and the bus being healthy — a fallback that fires against a
working manager would replace a live service with a process nobody supervises.

The manager is the fake the watcher-ensure tests already use, so no unit reaches
the real account home and no systemd manager need exist on the host. The
fallback's own arming is injected for the same reason: a case proves the path
was taken without starting a process.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import service
from reckon.crew import runs
from tests.test_crew_watch_ensure import FakeWatchService, _backend_bin, _config

BUS_REFUSED = (
    "systemctl --user daemon-reload failed: Failed to connect to bus: "
    "Connection refused"
)


class UnreachableWatchService(FakeWatchService):
    """A manager whose bus calls fail the way a restarted client's do.

    The unit is still written: the write is a filesystem operation, and the
    failure measured in the field arrived on the bus call that followed it.
    """

    def __init__(self, detail: str = BUS_REFUSED) -> None:
        super().__init__()
        self.detail = detail

    def active(self, project: str) -> bool:
        raise service.ServiceError(self.detail)

    def start(self, project: str, *, restart: bool) -> None:
        raise service.ServiceError(self.detail)


class RecordingProducer:
    """A stand-in for the process arming, recording the call and its answer."""

    def __init__(self, *, live: bool = True) -> None:
        self.calls: list[str] = []
        self.live = live

    def __call__(self, project: str) -> dict:
        self.calls.append(project)
        return {"project": project, "watcher_live": self.live}


class RefusingProducer:
    """A producer that fails loudly if the fallback fires when it must not."""

    def __call__(self, project: str) -> dict:
        raise AssertionError(
            "the process fallback armed a watcher while the service manager was "
            "reachable"
        )


@pytest.fixture()
def service_home(tmp_path: Path, monkeypatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def test_an_unreachable_bus_arms_the_watcher_as_a_process(
    service_home: Path, tmp_path: Path
) -> None:
    """A refused bus call returns an armed watcher instead of raising."""
    backend_bin = _backend_bin(tmp_path)
    manager = UnreachableWatchService()
    producer = RecordingProducer(live=True)

    result = runs.ensure_watcher_service(
        "sample",
        manager=manager,
        config=_config(backend_bin),
        producer=producer,
    )

    assert result["path"] == "fallback"
    assert "connection refused" in result["fallback_reason"].lower()
    assert producer.calls == ["sample"]
    assert result["watcher_live"] is True
    assert result["started"] is True
    assert result["service_active"] is False
    # The path is reported as a field, not only in the sentence: a caller reads
    # the value, and the sentence is left to carry the reason.
    assert result["fallback_reason"] in result["detail"]
    assert manager.starts == []


def test_a_reachable_manager_takes_the_service_path_and_says_so(
    service_home: Path, tmp_path: Path
) -> None:
    """The fallback must not fire while the bus is healthy.

    The producer raises if it is called, so this case fails on the fallback
    firing rather than on a comparison against the result.
    """
    backend_bin = _backend_bin(tmp_path)
    manager = FakeWatchService()

    result = runs.ensure_watcher_service(
        "sample",
        manager=manager,
        config=_config(backend_bin),
        producer=RefusingProducer(),
    )

    assert result["path"] == "service"
    assert result["fallback_reason"] is None
    assert result["started"] is True
    assert manager.starts == [(runs.watch_unit_name("sample"), False)]


def test_a_unit_the_manager_refuses_still_raises(
    service_home: Path, tmp_path: Path
) -> None:
    """Only an unreachable bus falls back; a refusal on the unit's merits does not.

    Otherwise the fallback would answer a malformed or unstartable unit with a
    process nobody asked for, and the refusal a person has to act on would never
    reach them.
    """
    backend_bin = _backend_bin(tmp_path)
    manager = UnreachableWatchService(
        detail="systemctl --user start reckon-watch-sample.service failed: "
        "Unit not found."
    )
    producer = RefusingProducer()

    with pytest.raises(service.ServiceError):
        runs.ensure_watcher_service(
            "sample",
            manager=manager,
            config=_config(backend_bin),
            producer=producer,
        )


def test_a_fallback_that_could_not_start_a_watcher_does_not_report_success(
    service_home: Path, tmp_path: Path
) -> None:
    """The fallback reports liveness, so a producer that failed is not a success."""
    backend_bin = _backend_bin(tmp_path)
    producer = RecordingProducer(live=False)

    result = runs.ensure_watcher_service(
        "sample",
        manager=UnreachableWatchService(),
        config=_config(backend_bin),
        producer=producer,
    )

    assert result["path"] == "fallback"
    assert result["watcher_live"] is False
    assert result["started"] is False
    assert result["fallback_reason"]
