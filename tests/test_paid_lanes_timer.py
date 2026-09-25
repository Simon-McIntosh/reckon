"""The five-minute refresh deployment: unit contents, calls, and idempotence.

The install writes two user units and brings the timer up through
``systemctl --user``. Every test here runs against a temporary
``XDG_CONFIG_HOME`` and a stub ``systemctl`` placed first on ``PATH``, so no
test reaches the operator's systemd directory or starts a real timer.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import paid_lanes

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The stub records each invocation as one JSON array of its argv, the ``--user``
#: ``service.systemctl`` prepends included, so a test reads back exactly what the
#: command asked the manager to do.
SYSTEMCTL_STUB = """\
#!{interpreter}
import json, os, sys
with open(os.environ["RECKON_SYSTEMCTL_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
"""


def _calls(log: Path) -> list[list[str]]:
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def _exec_start(unit: str) -> list[str]:
    for line in unit.splitlines():
        if line.startswith("ExecStart="):
            return shlex.split(line[len("ExecStart=") :])
    raise AssertionError("unit has no ExecStart line")


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A temporary user-systemd root and a stub systemctl first on PATH.

    ``RECKON_HOME`` is removed so the rendered unit does not depend on whatever
    the surrounding session exported; the current directory is the repository
    root so the module resolves under the interpreter that runs the tests.
    """
    config = tmp_path / "config"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "systemctl.log"
    stub = bin_dir / "systemctl"
    stub.write_text(SYSTEMCTL_STUB.format(interpreter=sys.executable))
    stub.chmod(0o755)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("RECKON_SYSTEMCTL_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv(paid_lanes.RECKON_HOME_ENV, raising=False)
    monkeypatch.chdir(REPO_ROOT)
    return {
        "config": config,
        "log": log,
        "service": config / "systemd" / "user" / paid_lanes.SERVICE_NAME,
        "timer": config / "systemd" / "user" / paid_lanes.TIMER_NAME,
    }


def test_install_writes_both_units_with_the_expected_definition(
    deployment: dict[str, Path],
) -> None:
    result = paid_lanes.install_timer()

    service = deployment["service"].read_text()
    timer = deployment["timer"].read_text()

    assert result["changed"] is True
    assert "Type=oneshot" in service
    assert _exec_start(service) == [
        sys.executable,
        "-m",
        "reckon.crew.paid_lanes",
        "--once",
    ]
    assert "OnBootSec=1min" in timer
    assert "OnUnitActiveSec=5min" in timer
    assert f"Unit={paid_lanes.SERVICE_NAME}" in timer
    assert "WantedBy=timers.target" in timer


def test_install_reloads_and_enables_the_timer(deployment: dict[str, Path]) -> None:
    paid_lanes.install_timer()

    assert _calls(deployment["log"]) == [
        ["--user", "daemon-reload"],
        ["--user", "enable", "--now", paid_lanes.TIMER_NAME],
    ]


def test_unchanged_definition_writes_nothing_and_calls_nothing(
    deployment: dict[str, Path],
) -> None:
    first = paid_lanes.install_timer()
    service_before = deployment["service"].read_text()
    timer_before = deployment["timer"].read_text()
    deployment["log"].unlink()

    second = paid_lanes.install_timer()

    assert first["changed"] is True
    assert second["changed"] is False
    assert deployment["service"].read_text() == service_before
    assert deployment["timer"].read_text() == timer_before
    assert _calls(deployment["log"]) == []


def test_a_changed_interpreter_rewrites_the_service_and_reloads(
    deployment: dict[str, Path],
) -> None:
    paid_lanes.install_timer()
    deployment["log"].unlink()

    result = paid_lanes.install_timer(executable="/opt/elsewhere/bin/python")

    assert result["changed"] is True
    assert (
        _exec_start(deployment["service"].read_text())[0] == "/opt/elsewhere/bin/python"
    )
    assert _calls(deployment["log"]) == [
        ["--user", "daemon-reload"],
        ["--user", "enable", "--now", paid_lanes.TIMER_NAME],
    ]


def test_install_timer_flag_installs_and_reports(
    deployment: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert paid_lanes.main(["--install-timer"]) == 0

    assert deployment["service"].is_file()
    assert deployment["timer"].is_file()
    assert paid_lanes.TIMER_NAME in capsys.readouterr().out


def test_help_lists_install_timer_and_installs_nothing(
    deployment: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert paid_lanes.main(["--help"]) == 0

    out = capsys.readouterr().out
    assert "--install-timer" in out
    assert not deployment["service"].exists()
    assert not deployment["timer"].exists()
    assert _calls(deployment["log"]) == []


def test_reckon_home_is_forwarded_into_the_unit(
    deployment: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(paid_lanes.RECKON_HOME_ENV, str(deployment["config"]))

    paid_lanes.install_timer()

    service = deployment["service"].read_text()
    resolved = Path(str(deployment["config"])).resolve()
    assert f'Environment="RECKON_HOME={resolved}"' in service


def test_the_module_entry_point_installs_under_a_temporary_config_home(
    deployment: dict[str, Path],
) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    completed = subprocess.run(
        [sys.executable, "-m", "reckon.crew.paid_lanes", "--install-timer"],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert deployment["service"].is_file()
    assert deployment["timer"].is_file()
    assert _calls(deployment["log"])[0] == ["--user", "daemon-reload"]
