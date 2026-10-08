"""The arming guard recognises a running pytest session, not only a name.

The throwaway-home guard once stood down whenever a test session ran under a
custom ``--basetemp``: its only signal was a ``pytest-of-*`` directory above
the home, and a base temp named ``/tmp/anything`` carries none. The guard now
also reads the temporary root the running session declares on its own command
line, so the session itself is recognised whatever the directory is called.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import reckon.crew.dispatch_watch as dispatch_watch_module

dispatch_module = importlib.import_module("reckon.crew.dispatch")
WATCH_ARMING_ENV = dispatch_module.WATCH_ARMING_ENV

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

_DRIVER_SOURCE = """\
import importlib
import os
import pathlib


def test_the_guard_sees_the_sessions_basetemp():
    from reckon import crew

    dispatch = importlib.import_module("reckon.crew.dispatch")

    basetemp = pathlib.Path(os.environ["CUSTOM_BASETEMP"])
    home = basetemp / "reckon-home"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["RECKON_HOME"] = str(home)
    os.environ.pop(dispatch.WATCH_ARMING_ENV, None)

    verdict = pathlib.Path(os.environ["GUARD_VERDICT"])
    try:
        dispatch._refuse_arming_under_a_throwaway_home("sample")
    except crew.CrewError as refusal:
        verdict.write_text("refused: " + str(refusal), encoding="utf-8")
    else:
        verdict.write_text("armed", encoding="utf-8")
"""


def _child_env(basetemp: Path, verdict: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(PACKAGE_ROOT), env.get("PYTHONPATH", "")) if part
    )
    for name in ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT"):
        env.pop(name, None)
    env["CUSTOM_BASETEMP"] = str(basetemp)
    env["GUARD_VERDICT"] = str(verdict)
    return env


def test_the_guard_refuses_a_session_with_a_custom_basetemp(tmp_path: Path) -> None:
    """A base temp named ``/tmp/anything`` no longer makes the guard stand down.

    The base temp sits directly under the system temp directory, so no
    ``pytest-of-*`` ancestor names it and the session's own ``--basetemp`` is
    the only signal that can refuse the home placed inside it.
    """
    driver = tmp_path / "test_guard_driver.py"
    driver.write_text(_DRIVER_SOURCE, encoding="utf-8")
    verdict = tmp_path / "verdict.txt"
    root = Path(tempfile.mkdtemp(prefix="reckon-custom-basetemp-"))
    try:
        session = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-q",
                f"--basetemp={root}",
                str(driver),
            ],
            cwd=str(tmp_path),
            env=_child_env(root, verdict),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120.0,
            check=False,
        )

        assert session.returncode == 0, session.stdout
        recorded = verdict.read_text(encoding="utf-8")
        assert recorded.startswith("refused:"), recorded
        assert str(root) in recorded
        assert "outlive" in recorded
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_an_ordinary_home_outside_the_session_still_arms(monkeypatch) -> None:
    """The guard is bound to the throwaway home, not to arming itself."""
    home = Path(tempfile.mkdtemp(prefix="reckon-guard-ordinary-"))
    try:
        monkeypatch.setenv("RECKON_HOME", str(home))
        monkeypatch.delenv(WATCH_ARMING_ENV, raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.delenv("PYTEST_VERSION", raising=False)
        monkeypatch.setattr(dispatch_watch_module, "_declared_basetemp", lambda: None)

        dispatch_module._refuse_arming_under_a_throwaway_home("sample")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_the_declared_basetemp_is_read_from_the_session_command_line() -> None:
    """The session's own ``--basetemp=`` names the throwaway root."""
    root = Path(tempfile.gettempdir()) / "no-pytest-name"
    argv = [["pytest", "-q", f"--basetemp={root}", "tests/"]]
    with mock.patch.object(
        dispatch_watch_module, "_current_and_ancestor_argvs", return_value=argv
    ):
        assert dispatch_module._declared_basetemp() == root


def test_a_home_under_the_declared_root_is_a_throwaway_home() -> None:
    """The declared root is the boundary, so a home outside it still arms."""
    base = Path(tempfile.gettempdir())
    root = base / "reckon-guard-plain-root"
    inside = root / "config"
    outside = base / "reckon-guard-elsewhere"

    with mock.patch.object(dispatch_watch_module, "_declared_basetemp", return_value=root):
        assert dispatch_module._temporary_home_root(inside) == root
        assert dispatch_module._temporary_home_root(outside) is None
