"""Promotion waits on the project's declared suite for its lighter tiers.

A per-node gate runs only the tests a node's brief names, so a project whose
default command stopped at collection can keep promoting unseen. The project's
own suite is the check that sees the whole tree, and this node makes promotion
consult it: while the latest recorded run failed to collect or overran its
budget, a promotion at tier ``none`` or ``light`` is refused with the recorded
reason and both ways out; a ``full`` review, which reads the run for itself, is
never held; and a later passing run or a recorded waiver lifts the hold.

The suite is synthesised under ``tmp_path``: the declared command is the
interpreter itself, exiting 5 (pytest's "no tests collected") for the failing
arm and 0 for the passing one, so the check runs nothing and touches nothing
outside the temporary tree.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, crew, ledger, review_tiers
from reckon.cli import main
from reckon.crew import review as review_module
from reckon.crew import standing_suite
from reckon.crew.runs import _write_json, pointer_path
from reckon.flight import SuiteDeclaration

PROJECT = "proj"
PLAN = "plan-a"

# pytest's exit status for "no tests were collected": positive evidence that
# collection itself broke, with no test reaching the runner.
_COLLECTION_FAILURE = (sys.executable, "-c", "raise SystemExit(5)")
# A run that collected and passed.
_PASSING = (sys.executable, "-c", "raise SystemExit(0)")

_SMALL_SOURCE = "def answer():\n    return 42\n"


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write_plan(root: Path) -> None:
    plan = root / "docs" / "plans" / f"{PLAN}.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    state: dict = {
        "type": "plan",
        "slug": PLAN,
        "title": "Plan A",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    plan.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


def _write_flight(repository: Path, command: tuple[str, ...]) -> None:
    """Write the synthesised project's suite declaration."""
    path = repository / "docs" / "state" / PROJECT / "flight.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = ", ".join(json.dumps(part) for part in command)
    path.write_text(
        f"review:\n  suite:\n    command: [{rendered}]\n    budget: 10s\n",
        encoding="utf-8",
    )


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_plan(root)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    _write_flight(root, _COLLECTION_FAILURE)
    return root


def _record_suite_run(repository: Path, command: tuple[str, ...], *, name: str) -> Path:
    """Run ``command`` as the project's suite and record the result."""
    log = repository / "docs" / "state" / PROJECT / "suite-runs" / f"{name}.log"
    record = standing_suite.run(
        repository, SuiteDeclaration(command=command, budget="10s"), log
    )
    return standing_suite.record(repository, PROJECT, record)


def _commit_change(repository: Path, relative_path: str, content: str) -> str:
    path = repository / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repository, "add", relative_path)
    _git(repository, "commit", "-q", "-m", "feature: change a path")
    return _git(repository, "rev-parse", "HEAD")


def _write_pointer(
    repository: Path,
    tmp_path: Path,
    run_id: str,
    *,
    commit: str,
    changed_paths: tuple[str, ...],
    role: str = "implement",
    spec_level: str = "exact",
) -> None:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\n"
        "status: complete\n"
        f"commits: {commit}\n"
        f"changed_paths: {', '.join(changed_paths)}\n"
        "tests: focused check passed\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "launch": "in-harness",
            "role": role,
            "backend": "native",
            "created_at": "2026-09-29T06:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "node",
                "time_budget": "25m",
                "spec_level": spec_level,
                "write_paths": list(changed_paths),
            },
        },
    )


def _store_review(run_id: str, *, head: str) -> None:
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    review = review_module.parse_review(emitted)
    review.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"preview-of-{run_id}",
            review_module.REVIEWED_HEAD_KEY: head,
        }
    )
    review_module.store_review(review)


def _row(repository: Path, run_id: str) -> dict:
    return next(
        row
        for row in ledger.load(PROJECT, repository)[0]["runs"]
        if row["run_id"] == run_id
    )


def test_a_collection_failure_holds_a_tier_none_promotion(
    repository: Path, tmp_path: Path
) -> None:
    _record_suite_run(repository, _COLLECTION_FAILURE, name="failing")
    run_id = "r-20260929T060000000000-tests-only"
    commit = _commit_change(
        repository, "tests/test_thing.py", "def test_it():\n    pass\n"
    )
    _write_pointer(
        repository,
        tmp_path,
        run_id,
        commit=commit,
        changed_paths=("tests/test_thing.py",),
    )

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    message = str(refusal.value)
    assert "standing suite holds this promotion" in message
    assert "failed to collect" in message
    assert "reckon crew suite run" in message
    assert "reckon crew suite waive" in message


def test_a_later_passing_run_lifts_the_hold(repository: Path, tmp_path: Path) -> None:
    _record_suite_run(repository, _COLLECTION_FAILURE, name="failing")
    run_id = "r-20260929T060100000000-tests-only"
    commit = _commit_change(
        repository, "tests/test_thing.py", "def test_it():\n    pass\n"
    )
    _write_pointer(
        repository,
        tmp_path,
        run_id,
        commit=commit,
        changed_paths=("tests/test_thing.py",),
    )
    with pytest.raises(crew.CrewError):
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    _record_suite_run(repository, _PASSING, name="passing")

    crew.complete(run_id, gate="passed", commits=[commit], root=repository)
    assert _row(repository, run_id)["review_tier"] == review_tiers.NONE


def test_a_recorded_waiver_lifts_the_hold(repository: Path, tmp_path: Path) -> None:
    _record_suite_run(repository, _COLLECTION_FAILURE, name="failing")
    run_id = "r-20260929T060200000000-tests-only"
    commit = _commit_change(
        repository, "tests/test_thing.py", "def test_it():\n    pass\n"
    )
    _write_pointer(
        repository,
        tmp_path,
        run_id,
        commit=commit,
        changed_paths=("tests/test_thing.py",),
    )
    with pytest.raises(crew.CrewError):
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    standing_suite.record_waiver(repository, PROJECT, who="lead", why="known red")

    crew.complete(run_id, gate="passed", commits=[commit], root=repository)
    assert _row(repository, run_id)["review_tier"] == review_tiers.NONE


def test_a_full_tier_promotion_is_not_held(repository: Path, tmp_path: Path) -> None:
    """A run that resolves the fuller tier promotes despite a failing suite.

    The suite holds the lighter tiers only: a full review reads the run for
    itself, so it is not made to wait on the project's suite.
    """
    _record_suite_run(repository, _COLLECTION_FAILURE, name="failing")
    run_id = "r-20260929T060300000000-open-spec"
    commit = _commit_change(repository, "reckon/thing.py", _SMALL_SOURCE)
    _write_pointer(
        repository,
        tmp_path,
        run_id,
        commit=commit,
        changed_paths=("reckon/thing.py",),
        spec_level="open",
    )

    # An open spec level forces the full tier on a small diff, so the run owes
    # a review; with that review stored the full-tier promotion is not held.
    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)
    assert "standing suite" not in str(refusal.value)

    _store_review(run_id, head=commit)
    crew.complete(run_id, gate="passed", commits=[commit], root=repository)
    assert _row(repository, run_id)["review_tier"] == review_tiers.FULL


def test_the_suite_commands_run_and_waive(repository: Path) -> None:
    """The two coordinator commands run the suite and record the waiver."""
    runner = CliRunner()

    run_result = runner.invoke(main, ["crew", "suite", "run", "--project", PROJECT])
    assert run_result.exit_code == 0, run_result.output
    payload = json.loads(run_result.output)
    assert payload["exit_status"] == 5
    assert payload["collection_failed"] is True
    assert payload["collected"] == 0

    waive_result = runner.invoke(
        main,
        ["crew", "suite", "waive", "--project", PROJECT, "--reason", "known red"],
    )
    assert waive_result.exit_code == 0, waive_result.output
    assert json.loads(waive_result.output)["ok"] is True
