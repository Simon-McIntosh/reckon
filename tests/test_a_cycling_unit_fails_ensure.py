"""``watch --ensure`` fails on a cycling unit, and the count parse is pinned.

``ensure`` reports a unit whose restart count rose across its two readings as
cycling, but it still exited 0, so a script or hook that reads the status rather
than the JSON saw success. A second gap sat under the first: the count is read
by :class:`reckon.service.SystemdUserUnitManager` from ``systemctl show``
output, and no test fed that method the text systemctl actually emits, so a
format change would read as no restarts and as healthy.

Both are exercised through seams the code already takes: the unit manager and
the sleeper for ``ensure``, and a stubbed ``systemctl`` for the parse. No real
unit is written and no systemd manager need exist on the host.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import service
from reckon.crew import watch_unit


def _refuse_a_real_systemctl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any call to the real systemd client a defect in the test itself."""

    def explode(*args, **kwargs):
        raise AssertionError("a real systemctl was called")

    monkeypatch.setattr(service, "systemctl", explode)


class ScriptedUnitManager:
    """An active, unchanged unit whose restart count is scripted per read.

    ``active`` is true and ``write_unit`` reports the unit already installable
    unchanged, so ``ensure`` finds it active on the same definition and samples
    it in place of restarting it — the state a cycling producer was reported as
    healthy from.
    """

    def __init__(self, counts: list[int]) -> None:
        self._counts = list(counts)
        self._index = 0
        self.starts: list[tuple[str, bool]] = []

    def unit_path(self, project: str) -> Path:
        return Path("/nonexistent/systemd") / watch_unit.watch_unit_name(project)

    def active(self, project: str) -> bool:
        return True

    def lingering(self) -> bool:
        return True

    def enable_linger(self) -> None:
        pass

    def write_unit(self, project: str, content: str) -> tuple[Path, bool]:
        return self.unit_path(project), False

    def start(self, project: str, *, restart: bool) -> None:
        self.starts.append((watch_unit.watch_unit_name(project), restart))

    def restart_count(self, project: str) -> int:
        value = self._counts[min(self._index, len(self._counts) - 1)]
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


def _invoke_ensure(
    monkeypatch: pytest.MonkeyPatch,
    manager: ScriptedUnitManager,
    sleeper: RecordingSleeper,
):
    """Run ``crew watch --ensure`` against an injected manager, once."""
    from reckon.crew import runs as runs_module

    def fake_ensure(project: str, **kwargs):
        return watch_unit.ensure_watcher_service(
            project, manager=manager, config={}, sleeper=sleeper
        )

    monkeypatch.setattr(runs_module, "ensure_watcher_service", fake_ensure)
    return CliRunner().invoke(
        cli_module.main,
        ["crew", "watch", "--ensure", "--project", "sample"],
    )


def test_a_cycling_unit_makes_ensure_fail(
    ensure_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rising count prints ``cycling: true`` and exits non-zero."""
    _refuse_a_real_systemctl(monkeypatch)
    manager = ScriptedUnitManager([3, 5])
    sleeper = RecordingSleeper()

    result = _invoke_ensure(monkeypatch, manager, sleeper)

    assert result.exit_code != 0, result.output
    payload = json.loads(result.output)
    assert payload["cycling"] is True
    assert payload["restart_count_before"] == 3
    assert payload["restart_count_after"] == 5
    assert "cycling" in payload["detail"]
    assert sleeper.waits == [watch_unit.WATCH_RESTART_SAMPLE_INTERVAL]


def test_a_held_count_keeps_ensure_successful(
    ensure_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A count that holds is reported as before, and the exit status is 0."""
    _refuse_a_real_systemctl(monkeypatch)
    manager = ScriptedUnitManager([3, 3])
    sleeper = RecordingSleeper()

    result = _invoke_ensure(monkeypatch, manager, sleeper)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["cycling"] is False
    assert "already active on an unchanged unit" in payload["detail"]
    assert sleeper.waits == [watch_unit.WATCH_RESTART_SAMPLE_INTERVAL]


def _restart_count_from(
    output: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[int | None, list[tuple[str, ...]]]:
    """Read a restart count through the manager, recording the systemctl query."""
    seen: list[tuple[str, ...]] = []

    def fake_systemctl(*args, **kwargs) -> subprocess.CompletedProcess:
        seen.append(args)
        return subprocess.CompletedProcess(
            args=list(args), returncode=0, stdout=output, stderr=""
        )

    monkeypatch.setattr(service, "systemctl", fake_systemctl)
    manager = service.SystemdUserUnitManager(
        watch_unit.watch_unit_name, watch_unit.watch_log_path
    )
    return manager.restart_count("sample"), seen


def test_the_parse_reads_a_counted_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The literal ``NRestarts=`` line systemctl emits reads as its integer."""
    count, seen = _restart_count_from("NRestarts=7\n", monkeypatch)

    assert count == 7
    assert "NRestarts" in seen[-1] and "--property" in seen[-1]


def test_the_parse_reads_a_unit_at_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """``NRestarts=0`` reads as zero, not as unknown."""
    count, _ = _restart_count_from("NRestarts=0\n", monkeypatch)

    assert count == 0


def test_the_parse_reads_absent_output_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Output carrying no NRestarts line is unknown, never zero."""
    count, _ = _restart_count_from(
        "Id=reckon-watch-sample.service\nActiveState=active\n", monkeypatch
    )

    assert count is None
