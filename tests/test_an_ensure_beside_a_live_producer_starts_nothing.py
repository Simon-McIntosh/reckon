"""An ensure reuses the serving producer regardless of the local unit state."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from reckon import service
from reckon.crew import watch_unit
from reckon.crew.host_lease import HostLease
from tests.test_crew_watch_ensure import FakeWatchService, _backend_bin, _config

PROJECT = "sample"


class RecordingService(FakeWatchService):
    """Record service mutations, including fallback-triggering linger failures."""

    def __init__(self, condition: str) -> None:
        super().__init__()
        self.condition = condition
        self._active = condition in {"changed", "restart"}
        self.linger_attempts = 0

    def write_unit(self, project: str, content: str) -> tuple[Path, bool]:
        self.units[project] = content
        return self.unit_path(project), self.condition == "changed"

    def active(self, project: str) -> bool:
        if self.condition == "unreachable":
            raise service.ServiceError("Failed to connect to bus: Connection refused")
        return super().active(project)

    def lingering(self) -> bool:
        return self.condition != "no-linger"

    def enable_linger(self) -> None:
        self.linger_attempts += 1
        raise service.LingerUnavailableError("Could not enable linger")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(path))
    return path


def _serving_producer(host: str) -> tuple[HostLease, dict]:
    pid = os.getpid() if host == socket.gethostname() else 2_147_483_647
    record = {"project": PROJECT, "host": host, "pid": pid, "job": ""}
    path = watch_unit.watch_lock_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    lease = HostLease(path.parent, path.stem, host, pid, "")
    assert lease.claim()
    assert watch_unit.watch_state(PROJECT)["watcher_live"] is True
    return lease, record


@pytest.mark.parametrize("host", [socket.gethostname(), "producer-elsewhere.invalid"])
@pytest.mark.parametrize(
    "condition", ["inactive", "changed", "restart", "unreachable", "no-linger"]
)
def test_a_serving_producer_prevents_every_arming_path(
    home: Path, tmp_path: Path, host: str, condition: str
) -> None:
    lease, record = _serving_producer(host)
    manager = RecordingService(condition)
    fallbacks = []

    def producer(project: str) -> dict:
        fallbacks.append(project)
        return watch_unit.watch_state(project)

    try:
        result = watch_unit.ensure_watcher_service(
            PROJECT,
            manager=manager,
            config=_config(_backend_bin(tmp_path)),
            restart=condition == "restart",
            producer=producer,
        )
        assert result["started"] is False, result
        assert manager.starts == []
        assert manager.linger_attempts == 0
        assert fallbacks == []
        assert result["watcher_live"] is True
        assert result["watcher"] == record
        assert watch_unit.watch_state(PROJECT)["watcher"] == record
    finally:
        lease.release()


def test_an_absent_producer_still_starts_the_service(
    home: Path, tmp_path: Path
) -> None:
    manager = RecordingService("inactive")
    assert watch_unit.watch_state(PROJECT)["watcher_live"] is False

    result = watch_unit.ensure_watcher_service(
        PROJECT, manager=manager, config=_config(_backend_bin(tmp_path))
    )

    assert result["started"] is True
    assert manager.starts == [(watch_unit.watch_unit_name(PROJECT), False)]
