"""A browser test that skips without a browser must not skip with one present.

Every browser-driven test reaches its browser through one seam,
``tests.spa_browser_harness.installed_browser_or_skip``. A guard that never let
a browser test run would be worse than no guard at all: a rendered assertion
would read as green while never being made, and the skip would report a missing
capability the host may well have had.

Two facts are checked here, and the second is not inferred from the first.

* Without any browser on PATH the guard raises ``SkipTest``, and its reason names
  the binaries it looked for, so an environment skip is legible rather than
  silent.
* With a stub browser on PATH the guarded body is reached, so a planted defect
  inside a browser-guarded test fails the test rather than being skipped away.
  That is the negative control: if the guard skipped unconditionally, the planted
  failure would vanish into a skip and the suite would report success it had not
  earned.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import SkipTest

import pytest

from tests.spa_browser_harness import BROWSER_NAMES, installed_browser_or_skip

ROOT = Path(__file__).resolve().parents[1]

# A browser-guarded test whose body plants a defect. Run with a browser on PATH,
# the guard returns, the body's assertion fails, and the run is red.
PLANTED_DEFECT_TEST = (
    "from tests.spa_browser_harness import installed_browser_or_skip\n"
    "\n"
    "\n"
    "def test_a_browser_tests_body_is_reached_and_its_planted_defect_fails():\n"
    "    installed_browser_or_skip()\n"
    "    raise AssertionError(\n"
    '        "planted defect: the browser-guarded body ran, so this must fail"\n'
    "    )\n"
)


def browser_free_path() -> str:
    """PATH with every directory that holds a supported browser stripped out."""

    return os.pathsep.join(
        directory
        for directory in os.environ.get("PATH", "").split(os.pathsep)
        if directory
        and not any(shutil.which(name, path=directory) for name in BROWSER_NAMES)
    )


def stub_browser_directory(tmp_path: Path) -> Path:
    """One executable stub per browser name, so the guard resolves a browser."""

    directory = tmp_path / "stub-browser-bin"
    directory.mkdir()
    for name in BROWSER_NAMES:
        stub = directory / name
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
    return directory


def test_the_guard_skips_and_names_the_binaries_without_a_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", browser_free_path())

    with pytest.raises(SkipTest) as denial:
        installed_browser_or_skip()

    reason = str(denial.value)
    for name in BROWSER_NAMES:
        assert name in reason


def test_a_stub_browser_makes_a_planted_defect_fail_rather_than_skip(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "test_planted_browser_defect.py"
    probe.write_text(PLANTED_DEFECT_TEST, encoding="utf-8")
    stub_directory = stub_browser_directory(tmp_path)

    environment = dict(os.environ)
    environment["PATH"] = os.pathsep.join([str(stub_directory), browser_free_path()])
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", str(probe)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, output
    assert "1 failed" in output
    assert "1 skipped" not in output
