"""The browser population is a marked suite row the standing suite excludes.

The browser tests run a headless shell, take far longer than the rest of the
suite, and must not be folded into the standing suite's time bound. They are
marked ``browser`` at collection — every test in a module that imports
``tests/spa_browser_harness.py`` — and the standing-suite command deselects the
marker, so the population can run as its own row with its own budget.

Three facts are checked here, none inferred from another:

* Every collected test in a module that imports the harness carries the marker,
  and no test outside those modules does. The expected set is built by scanning
  each test module's source for an import of the harness, an instrument
  independent of the collection hook that applies the marker.
* The command ``docs/state/reckon/flight.yaml`` declares for the standing suite
  collects no marked test. The command is read from the declaration and run, so
  removing its marker exclusion makes a marked test appear and this fails — the
  negative control.
* A fixed pytest log recorded under the ``browser`` population name writes a row
  that carries that name and the counts the log reports.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
FLIGHT = ROOT / "docs" / "state" / "reckon" / "flight.yaml"

BROWSER_MARKER = "browser"

# A test module imports the harness directly, binding either the module or a name
# defined in it; both spellings are captured. Transitive imports resolve through
# a local helper only where the module also imports the harness itself, so this
# matches the marker hook's identity predicate on this tree.
_HARNESS_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+(?:tests\.)?spa_browser_harness\s+import"
    r"|import\s+(?:tests\.)?spa_browser_harness\b)",
    re.MULTILINE,
)

# A collected node id from ``--collect-only -q``, one per line.
_NODE_ID_RE = re.compile(r"^\S+\.py::\S+")


def _modules_importing_the_harness() -> set[str]:
    """Test filenames whose source imports the browser harness."""
    found: set[str] = set()
    for path in sorted(TESTS.glob("*.py")):
        if _HARNESS_IMPORT_RE.search(path.read_text(encoding="utf-8")):
            found.add(path.name)
    return found


def _collect(extra: list[str]) -> list[str]:
    """The node ids ``pytest --collect-only -q`` reports over ``tests/``.

    The run is a subprocess with the interpreter running this test, so it
    collects against the same tree and the same conftest hook. The exit status is
    not asserted here: a collection that reports ids has done its job, and the
    callers assert on the ids themselves.
    """
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "--collect-only",
        "-q",
        *extra,
        "tests/",
    ]
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, check=False
    )
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if _NODE_ID_RE.match(line.strip())
    ]


def _file_of(node_id: str) -> str:
    return Path(node_id.split("::", 1)[0]).name


def test_every_harness_test_is_marked_and_no_other_is():
    all_ids = _collect([])
    assert all_ids, "collecting tests/ reported no tests at all"

    harness_modules = _modules_importing_the_harness()
    assert harness_modules, "no test module imports the browser harness"

    expected = {node for node in all_ids if _file_of(node) in harness_modules}
    assert expected, "the browser population is empty"

    marked = set(_collect(["-m", BROWSER_MARKER]))
    assert marked, "no test carries the browser marker"
    assert marked == expected, (
        "the browser marker and the harness-importing modules disagree: "
        f"marked but not expected {sorted(marked - expected)}; "
        f"expected but not marked {sorted(expected - marked)}"
    )


def _standing_suite_command() -> list[str]:
    import yaml

    data = yaml.safe_load(FLIGHT.read_text(encoding="utf-8"))
    return list(data["review"]["suite"]["command"])


def test_the_standing_suite_command_collects_no_marked_test():
    command = _standing_suite_command()
    assert "-m" in command, (
        "the standing-suite command carries no marker selection, so it cannot "
        "exclude the browser population"
    )

    marked = set(_collect(["-m", BROWSER_MARKER]))
    assert marked, "no test carries the browser marker"

    # Run the declared command's own selection over tests/, dropping the
    # interpreter so this process's is used and appending a collection-only flag.
    run_command = [sys.executable, *command[1:], "--collect-only"]
    result = subprocess.run(
        run_command, cwd=ROOT, capture_output=True, text=True, check=False
    )
    collected = {
        line.strip()
        for line in result.stdout.splitlines()
        if _NODE_ID_RE.match(line.strip())
    }
    assert collected, (
        "the standing-suite command collected nothing; its exclusion or target "
        f"is wrong\nstderr:\n{result.stderr[-2000:]}"
    )

    leaked = sorted(collected & marked)
    assert not leaked, (
        f"the standing-suite command collects browser-marked tests: {leaked[:10]}"
    )


def test_a_fixed_log_records_a_row_under_the_browser_population_name(tmp_path):
    from reckon.crew import standing_suite
    from reckon.flight import SuiteDeclaration

    fixed_log = tmp_path / "browser-population.log"
    fixed_log.write_text(
        "FAILED tests/test_spa_x.py::test_a_rendered_fact\n"
        "3 passed, 1 failed, 2 skipped in 12.34s\n",
        encoding="utf-8",
    )
    declaration = SuiteDeclaration(command=("pytest", "tests/"), budget="20m")
    project_root = tmp_path / "checkout"

    row_path = standing_suite.record_from_log(
        project_root,
        "reckon",
        fixed_log,
        population=BROWSER_MARKER,
        declaration=declaration,
        revision="deadbeefcafe",
    )

    row = json.loads(Path(row_path).read_text(encoding="utf-8"))
    assert row["population"] == BROWSER_MARKER
    assert row["passed"] == 3
    assert row["failed"] == 1
    assert row["skipped"] == 2
    assert row["failure_ids"] == ["tests/test_spa_x.py::test_a_rendered_fact"]
    assert row["budget_seconds"] == declaration.budget_seconds()

    # The row sits under the project's suite-runs directory, not somewhere else.
    assert Path(row_path).parent == standing_suite.suite_runs_dir(
        project_root, "reckon"
    )
