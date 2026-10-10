"""User services share the systemd directory and unit installer."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon import service
from reckon.crew import paid_lanes, runs, watch_unit


def test_service_and_watch_units_use_the_xdg_systemd_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "reckon-home"))
    executable = tmp_path / "bin" / "reckon"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(service, "server_executable", lambda: executable)
    monkeypatch.setattr(service, "node_executable", lambda: executable)
    monkeypatch.setattr(watch_unit, "_reckon_console_script", lambda: str(executable))
    monkeypatch.setattr(
        watch_unit,
        "_watcher_service_environment",
        lambda _config: {"PATH": str(executable.parent)},
    )
    monkeypatch.setattr(service, "linger_enabled", lambda: True)
    commands: list[tuple[str, ...]] = []

    def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return subprocess.CompletedProcess(args, 3 if args[0] == "is-active" else 0)

    monkeypatch.setattr(service, "systemctl", systemctl)

    server_path, server_changed = service.write_unit()
    manager = service.SystemdUserUnitManager(runs.watch_unit_name, runs.watch_log_path)
    watch_result = runs.ensure_watcher_service("sample", manager=manager, config={})

    expected = paid_lanes.systemd_user_dir()
    assert expected == config_home / "systemd" / "user"
    assert server_changed
    assert server_path == expected / "reckon.service"
    assert Path(watch_result["unit_path"]) == expected / runs.watch_unit_name("sample")
    assert watch_result["unit_changed"] and watch_result["started"]
    assert "crew watch --project sample" in Path(watch_result["unit_path"]).read_text()
    assert commands == [
        ("is-active", runs.watch_unit_name("sample")),
        ("daemon-reload",),
        ("start", runs.watch_unit_name("sample")),
    ]


def test_paid_lane_units_use_the_same_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    commands: list[list[str]] = []
    first = paid_lanes.install_timer(
        executable=tmp_path / "python", run=commands.append
    )
    second = paid_lanes.install_timer(
        executable=tmp_path / "python", run=commands.append
    )

    expected = paid_lanes.systemd_user_dir()
    assert Path(first["service"]).parent == expected
    assert Path(first["timer"]).parent == expected
    assert first["changed"] is True
    assert second["changed"] is False
    assert commands == [["daemon-reload"], ["enable", "--now", paid_lanes.TIMER_NAME]]
