"""Unit rendering and installation for the systemd user service."""

from pathlib import Path

import pytest

from reckon import service


@pytest.fixture
def executable(tmp_path: Path) -> Path:
    """A stand-in console script so rendering never probes the real install."""
    path = tmp_path / "bin" / "reckon"
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\n")
    return path


def test_unit_runs_serve_on_the_requested_port(executable: Path):
    unit = service.render_unit(port=8766, executable=executable)
    assert f"ExecStart={executable} serve --port 8766" in unit


def test_unit_restarts_on_failure_and_installs_into_the_default_target(
    executable: Path,
):
    unit = service.render_unit(executable=executable)
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit


def test_optional_arguments_are_omitted_when_unset(executable: Path):
    unit = service.render_unit(executable=executable)
    assert "--host" not in unit
    assert "--mounts" not in unit


def test_host_and_mounts_reach_the_command_line(executable: Path, tmp_path: Path):
    mounts = tmp_path / "mounts.json"
    mounts.write_text("{}")
    unit = service.render_unit(
        host="0.0.0.0", mounts_file=mounts, executable=executable
    )
    assert "--host 0.0.0.0" in unit
    assert f"--mounts {mounts.resolve()}" in unit


def test_the_executable_bin_directory_leads_the_search_path(executable: Path):
    unit = service.render_unit(executable=executable)
    assert f'Environment="PATH={executable.parent}:' in unit


def test_an_explicit_config_home_is_forwarded_to_the_unit(
    executable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    unit = service.render_unit(executable=executable)
    assert f'Environment="RECKON_HOME={tmp_path.resolve()}"' in unit


def test_config_home_is_left_to_the_server_when_unset(
    executable: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("RECKON_HOME", raising=False)
    assert "RECKON_HOME" not in service.render_unit(executable=executable)


def test_writing_reports_a_change_only_when_the_content_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executable: Path
):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(service, "server_executable", lambda: executable)

    path, changed = service.write_unit()
    assert changed and path.is_file()

    _, changed_again = service.write_unit()
    assert not changed_again

    _, changed_on_port = service.write_unit(port=8766)
    assert changed_on_port


def test_operations_refuse_to_run_against_a_missing_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert not service.installed()
    with pytest.raises(service.ServiceError, match="not installed"):
        service.require_installed()


def test_output_is_captured_to_a_readable_file(executable: Path):
    unit = service.render_unit(executable=executable)
    log_file = service.log_path()
    assert f"StandardOutput=append:{log_file}" in unit
    assert f"StandardError=append:{log_file}" in unit


def test_installing_creates_the_log_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executable: Path
):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(service, "server_executable", lambda: executable)

    service.write_unit()
    assert service.log_path().parent.is_dir()
