"""The XDG config base is resolved below the crew runtime.

``reckon.service`` carries the systemd unit installer, so importing it must not
pull in the crew runtime: the base both consumers derive from is resolved
there, and the crew modules import it rather than the other way round.

The modules under test are imported inside each case rather than at module
level. Importability is one of the facts under test here, and an import that
fails must redden that case rather than abort collection before it runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# The probe runs in a fresh interpreter because the question is what importing
# the module loads, and this process already holds whatever the rest of the
# suite imported. It reports rather than raises, so a failure names the loaded
# modules instead of only a traceback.
_PROBE = """\
import json
import sys

error = None
try:
    import reckon.service
except BaseException as exc:
    error = f"{type(exc).__name__}: {exc}"

loaded = sorted(
    name
    for name in sys.modules
    if name == "reckon.crew" or name.startswith("reckon.crew.")
)
module = sys.modules.get("reckon.service")
print(
    "PROBE:"
    + json.dumps(
        {
            "error": error,
            "loaded": loaded,
            "service_file": getattr(module, "__file__", None),
        }
    )
)
"""


def _probe(*prelude: str) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-c", "\n".join((*prelude, _PROBE))],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    assert completed.returncode == 0, completed.stderr
    rows = [row for row in completed.stdout.splitlines() if row.startswith("PROBE:")]
    assert rows, completed.stdout
    return json.loads(rows[-1].removeprefix("PROBE:"))


def test_importing_the_service_module_loads_no_crew_module() -> None:
    payload = _probe()

    assert payload["error"] is None, payload["error"]
    assert payload["loaded"] == [], payload["loaded"]
    assert (
        Path(payload["service_file"]).resolve()
        == (REPO_ROOT / "reckon" / "service.py").resolve()
    )


def test_the_probe_sees_crew_modules_when_they_are_loaded() -> None:
    # An empty list is only evidence of an absence when the probe reports the
    # modules that are there.
    payload = _probe("import reckon.crew.paid_lanes")

    assert payload["error"] is None, payload["error"]
    assert "reckon.crew.paid_lanes" in payload["loaded"]


def test_the_xdg_base_serves_both_consumers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reckon import service
    from reckon.crew import fleet_supervisor, paid_lanes

    config_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    assert service.xdg_config_home() == config_home
    assert service.systemd_user_dir() == config_home / "systemd" / "user"
    assert paid_lanes.systemd_user_dir() == config_home / "systemd" / "user"
    assert fleet_supervisor.config_directory() == config_home / "fleet"

    explicit = tmp_path / "explicit"
    environ = {"XDG_CONFIG_HOME": str(explicit)}
    assert fleet_supervisor.config_directory(environ) == explicit / "fleet"

    # A second definition would resolve identically here; the identity is what
    # says there is one.
    assert paid_lanes.systemd_user_dir is service.systemd_user_dir


def test_config_directory_reads_the_base_from_the_service_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reckon import service
    from reckon.crew import fleet_supervisor

    base = tmp_path / "sentinel"
    seen: list[Mapping[str, str] | None] = []

    def fake(environ: Mapping[str, str] | None = None) -> Path:
        seen.append(environ)
        return base

    monkeypatch.setattr(service, "xdg_config_home", fake)
    environ = {"XDG_CONFIG_HOME": "/somewhere"}

    assert fleet_supervisor.config_directory(environ) == base / "fleet"
    assert seen == [environ]


def test_without_the_variable_the_base_is_the_home_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reckon import service
    from reckon.crew import fleet_supervisor

    home = tmp_path / "home"
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))

    assert service.xdg_config_home() == home / ".config"
    assert service.systemd_user_dir() == home / ".config" / "systemd" / "user"
    assert fleet_supervisor.config_directory({}) == home / ".config" / "fleet"
