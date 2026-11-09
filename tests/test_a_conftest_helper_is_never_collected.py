"""The conftest's shared-home helper is not collected as a test.

``tests/conftest.py`` holds a genuine test, ``test_shared_home_fixture_never_targets_the_real_store``,
that guards the operator's real store, and it also defines a helper other test
modules import. A helper whose name starts ``test_`` is collected by pytest,
which then tries to satisfy its signature as if the arguments were fixtures and
errors at setup. The defect is the helper's name: renaming it keeps the genuine
test collected and takes the helper out of collection.

Each fact is checked against a real pytest run in a subprocess, not inferred
from the source: the collected node ids, and a plain run of the conftest with no
collection ERROR.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFTEST = ROOT / "tests" / "conftest.py"

GENUINE_TEST = "test_shared_home_fixture_never_targets_the_real_store"
HELPER = "make_temp_config_home"


def _pytest(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _collected_node_ids(stdout: str) -> list[str]:
    return [
        line.strip()
        for line in stdout.splitlines()
        if line.strip().startswith("tests/") and "::" in line
    ]


def test_collect_only_keeps_the_genuine_test_and_drops_the_helper() -> None:
    result = _pytest("--collect-only", str(CONFTEST))

    collected = _collected_node_ids(result.stdout)
    assert any(node_id.endswith(f"::{GENUINE_TEST}") for node_id in collected), (
        f"the genuine guard test is not collected; collected={collected!r}, "
        f"stdout={result.stdout!r}"
    )
    assert not any(node_id.endswith(f"::{HELPER}") for node_id in collected), (
        f"the helper is still collected as a test; collected={collected!r}"
    )
    assert "test_temp_config_home" not in result.stdout, (
        f"the old helper name still appears in collection output; "
        f"stdout={result.stdout!r}"
    )
    assert result.returncode == 0, (
        f"collection exited {result.returncode}; stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


def test_running_the_conftest_reports_no_error() -> None:
    result = _pytest(str(CONFTEST))

    assert "ERROR" not in result.stdout, (
        f"running the conftest reported a collection/setup ERROR; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.returncode == 0, (
        f"running the conftest exited {result.returncode}; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
