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
* ``reckon crew suite run`` over a project declaring a standing suite and a
  browser population writes one row for each, and the browser row's failure does
  not hold a tier ``none`` promotion while the standing suite's does. The
  population is fed through the flight resolver's own parser, so a second budget
  shape cannot drift from the standing suite's.
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


def _python(code: str) -> list[str]:
    """A declared command that runs ``code`` under this test's interpreter."""
    return [sys.executable, "-c", code]


# A command that reports a passing run, and one that reports a collection
# failure — the only failure a standing-suite hold is raised on.
_PASSING = _python("print('3 passed in 0.01s')")
_COLLECTION_FAILURE = _python(
    "import sys; print('ERROR collecting tests/test_x.py'); sys.exit(2)"
)


def _declare_project(root: Path, project: str, standing: list[str], browser: list[str]):
    """Write a project flight layer declaring a standing suite and a browser row."""
    import yaml

    state = root / "docs" / "state" / project
    state.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 1,
        "review": {
            "suite": {"command": standing, "budget": "20m"},
            "populations": [
                {"name": BROWSER_MARKER, "command": browser, "budget": "20m"}
            ],
        },
    }
    (state / "flight.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _git_checkout(root: Path) -> None:
    """A minimal git repository, so the suite record can read a revision."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "README").write_text("x", encoding="utf-8")
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.invalid"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "README"],
        ["git", "commit", "-qm", "init"],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)


def _run_declared_suite(monkeypatch, tmp_path, project, root):
    """Invoke ``reckon crew suite run`` against a temporary project and read the rows."""
    from click.testing import CliRunner

    from reckon import cli as cli_module

    # The host layer is pointed at an empty temporary file, so the run reads no
    # state outside the checkout under test.
    host = tmp_path / "host-flight.yaml"
    host.write_text("version: 1\n", encoding="utf-8")
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(host))

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "suite",
            "run",
            "--project",
            project,
            "--checkout-path",
            str(root),
        ],
    )
    # The exit verdict answers the standing suite, so a scenario whose standing
    # suite fails to collect exits 1 while still emitting its JSON.
    return json.loads(result.output), root


def _rows_by_population(root: Path, project: str) -> dict:
    from reckon.crew import standing_suite

    rows = {}
    for path in standing_suite.suite_runs_dir(root, project).glob("*.json"):
        row = json.loads(path.read_text(encoding="utf-8"))
        if row.get("kind") == "run":
            rows[row.get("population")] = row
    return rows


def test_crew_suite_run_records_each_population_and_the_hold_reads_only_the_standing_row(
    tmp_path, monkeypatch
):
    from reckon.crew import standing_suite

    project = "tempsuite"

    # The browser population fails to collect, the standing suite passes: two
    # rows are written, and the browser row's failure does not hold a promotion.
    root = tmp_path / "checkout-browser-fails"
    _declare_project(root, project, _PASSING, _COLLECTION_FAILURE)
    _git_checkout(root)
    payload, root = _run_declared_suite(monkeypatch, tmp_path, project, root)

    assert len(payload["populations"]) == 1
    assert payload["populations"][0]["population"] == BROWSER_MARKER
    rows = _rows_by_population(root, project)
    assert set(rows) == {None, BROWSER_MARKER}
    assert rows[BROWSER_MARKER]["collection_failed"] is True
    assert rows[None]["collection_failed"] is False
    assert standing_suite.hold_reason(root, "none", project) is None

    # The standing suite fails to collect, the population passes: the hold fires
    # on the standing row and, again, not on the population's.
    failing_root = tmp_path / "checkout-standing-fails"
    _declare_project(failing_root, project, _COLLECTION_FAILURE, _PASSING)
    _git_checkout(failing_root)
    payload, failing_root = _run_declared_suite(
        monkeypatch, tmp_path, project, failing_root
    )

    assert len(payload["populations"]) == 1
    rows = _rows_by_population(failing_root, project)
    assert set(rows) == {None, BROWSER_MARKER}
    assert rows[None]["collection_failed"] is True
    assert rows[BROWSER_MARKER]["collection_failed"] is False
    assert standing_suite.hold_reason(failing_root, "none", project) is not None
