"""A coordinator's landing is measured over the population the lander measures.

A landing the pass makes by itself compares the base and head of the tests that
import what the delivery changed and refuses a head that adds a failure.
``crew complete --land`` did not: it read the run's own gate evidence, which
covers the node's own tests and nothing that imports what they changed, so a
head that reddened an importer test merged with nothing reported and the failure
surfaces hours later from an unrelated merge.

The three cases below drive the coordinator's own command against a temporary
repository whose importer test only the population reads: a head that reddens an
importer test the gate never ran is refused naming that id and nothing is
merged; a head that adds no failure lands; and a failure the base already
carries is not charged to the landing. The measurement each landing takes is
read from the run's own directory, beside the gate evidence the coordinator
presented.

The declared mutation makes the coordinator path skip the population measure
again, which is the defect the first case names: the landing is then accepted
with the importer test red, so that case fails.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from reckon.crew import landing_measure
from reckon.crew.runs import _write_json, pointer_path, run_dir
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import (
    PLAN,
    PROJECT,
    _complete_land,
    _write_origin_and_checkout,
)

RUN_ID = "r-20261122T080000000000-a-coordinator-landing-is-measured"
ARMED = "python -m pytest -q tests/test_node.py"
FAILURE = "tests/test_consumer.py::test_integration"
MARKER = "delivered"


def _suite_arm(command: str, revision: str) -> dict[str, object]:
    return {
        "revision": revision,
        "command": command,
        "exit_status": 0,
        "log_path": f"/durable/{revision}.log",
        "completed": True,
        "failure_count": 0,
        "failure_ids": [],
    }


def _manifest(tmp_path: Path, base: str, head: str) -> Path:
    path = tmp_path / "manifest.md"
    path.write_text(
        "\n".join(
            [
                "node: node-a",
                "status: complete",
                f"commits: {head}",
                f"baseline_suite: {json.dumps(_suite_arm(ARMED, base))}",
                f"after_suite: {json.dumps(_suite_arm(ARMED, head))}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _install_runner(
    monkeypatch: pytest.MonkeyPatch, *, main_fails: bool, clean: bool
) -> list[list[str]]:
    """Drive the measurement's arms without executing a real suite.

    The importer test is the only one that reads the delivered module, so it is
    red at the head and, when the case says the base carries it, red at the base
    too. The suite arm reads the node's own test and always passes.
    """
    real_run = subprocess.run
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        if Path(str(argv[0])).name == "git":
            return real_run(argv, **kwargs)
        tree = Path(kwargs["cwd"])
        consumer = (tree / "tests" / "test_consumer.py").is_file()
        delivered = MARKER in (tree / "reckon" / "example.py").read_text(
            encoding="utf-8"
        )
        population = "tests/test_consumer.py" in argv or FAILURE in argv
        fails = consumer and population and not clean
        fails = fails and (main_fails or delivered)
        calls.append(list(argv))
        output = (
            f"FAILED {FAILURE} - AssertionError\n1 failed\n" if fails else "1 pass\n"
        )
        return subprocess.CompletedProcess(argv, int(fails), output, "")

    monkeypatch.setattr(landing_measure, "_RUN", run)
    return calls


def _landing_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    main_fails: bool = False,
    clean: bool = False,
) -> dict[str, object]:
    """A live run whose change reddens a test the coordinator's gate never ran."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    (repository / "reckon").mkdir(exist_ok=True)
    (repository / "reckon" / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "tests").mkdir(exist_ok=True)
    (repository / "tests" / "test_node.py").write_text(
        "def test_node():\n    assert True\n", encoding="utf-8"
    )
    (repository / "tests" / "test_consumer.py").write_text(
        "import reckon.example\n\ndef test_integration():\n    assert True\n",
        encoding="utf-8",
    )
    _git(repository, "add", "reckon", "tests")
    _git(repository, "commit", "-q", "-m", "test: seed the module and its callers")
    base = _git(repository, "rev-parse", "HEAD")
    _git(repository, "push", "-q", "origin", "main")
    worktree = tmp_path / "worktree"
    _git(repository, "worktree", "add", "-q", "--detach", str(worktree), base)
    (worktree / "reckon" / "example.py").write_text(
        f"VALUE = 2  # {MARKER}\n", encoding="utf-8"
    )
    _git(worktree, "add", "reckon/example.py")
    _git(worktree, "commit", "-q", "-m", "feat: change the module's value")
    head = _git(worktree, "rev-parse", "HEAD")
    gate_log = tmp_path / "gate.log"
    gate_log.write_text("# command: true\nEXIT=0\n", encoding="utf-8")
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "base_sha": base,
            "suite_command": ARMED,
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-11-22T08:00:00Z",
            "manifest_path": str(_manifest(tmp_path, base, head)),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "section-one",
                "time_budget": "20m",
                "write_paths": ["reckon/example.py"],
            },
        },
    )
    _install_runner(monkeypatch, main_fails=main_fails, clean=clean)
    return {
        "origin": origin,
        "repository": repository,
        "worktree": worktree,
        "run_id": RUN_ID,
        "base": base,
        "head": head,
        "gate_log": gate_log,
    }


def _land(run: dict[str, object]):
    """The coordinator's own command, its lease on review waived for the case."""
    return _complete_land(
        run,
        "--waive-unreviewed-promotion",
        "the case measures the landing population, not the review",
    )


def _outcome(result) -> dict[str, object]:
    """The full receipt an invocation retained, read from the record it cites."""
    return json.loads(Path(json.loads(result.output)["record_path"]).read_text())


def _record() -> dict[str, object]:
    path = run_dir(RUN_ID) / "landing-measure" / "measurement.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _gate_record() -> dict[str, object]:
    path = run_dir(RUN_ID) / "landing-measure" / "coordinator-gate.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_head_that_reddens_an_importer_test_is_refused_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate cited one test; the population finds the failure it missed."""
    run = _landing_run(tmp_path, monkeypatch)

    result = _land(run)

    assert result.exit_code == 1, result.output
    payload = _outcome(result)
    assert payload["error"] == "landing-measure-refused"
    assert payload["added_failure_ids"] == [FAILURE]
    assert FAILURE in payload["detail"]
    # Nothing was merged: the branch is where the run branched from.
    assert _git(run["repository"], "rev-parse", "main") == run["base"]
    assert run["worktree"].is_dir()
    # The population found the failure, and the coordinator's own gate evidence
    # is stored beside it rather than standing in for it.
    record = _record()
    assert record["real"] == [FAILURE]
    assert record["population"]["head"]["failure_ids"] == [FAILURE]
    assert _gate_record()["command"] == "true"


def test_a_head_that_adds_no_failure_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A measured head that adds nothing is published by the same call."""
    run = _landing_run(tmp_path, monkeypatch, clean=True)

    result = _land(run)

    assert result.exit_code == 0, result.output
    landed = _git(run["repository"], "rev-parse", "main")
    assert (
        _git(run["repository"], "show", f"{landed}:reckon/example.py").find(MARKER) >= 0
    )
    assert _record()["real"] == []


def test_a_landing_the_population_cannot_measure_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A measurement that could not be taken is not one that added nothing."""
    run = _landing_run(tmp_path, monkeypatch)
    shutil.rmtree(run["worktree"])

    result = _land(run)

    assert result.exit_code == 1, result.output
    payload = _outcome(result)
    assert payload["error"] == "landing-measure-refused"
    assert payload["added_failure_ids"] == []
    assert "unreadable" in payload["detail"]
    assert payload["unreadable"]
    # Nothing was merged while the population went unmeasured.
    assert _git(run["repository"], "rev-parse", "main") == run["base"]


def test_a_failure_the_base_carries_is_not_charged_to_the_population(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same id is red at both revisions, so the head adds none of it."""
    run = _landing_run(tmp_path, monkeypatch, main_fails=True)

    result = _land(run)

    assert result.exit_code == 0, result.output
    assert _git(run["repository"], "rev-parse", "main") != run["base"]
    record = _record()
    assert record["population"]["base"]["failure_ids"] == [FAILURE]
    assert record["population"]["head"]["failure_ids"] == [FAILURE]
    assert record["real"] == []
