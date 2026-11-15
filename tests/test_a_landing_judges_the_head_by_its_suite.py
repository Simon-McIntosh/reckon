"""A landing reads the evidence it has, and measures nothing it was not asked to.

A suite is judged by the failures it added to its base, so a head whose only
failure is the id the base also fails measures zero added, and a head that adds
one beside a red base is refused naming that id. An after arm left behind by a
commit that touches nothing the suite reads keeps its standing, because the
suite the head runs is the suite the arm measured.

A dry run that finds an arm or a declared negative control it cannot read from
the run's own logs reports what the landing would measure and starts nothing,
so reading a dry run never submits a job. And a coordinator repeating a complete
command whose request is already queued drives that request rather than
answering "duplicate" and leaving the queue where it was.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import ledger
from reckon.cli import main as cli_main
from reckon.crew import promotion_checks, promotion_records, promotion_requests
from reckon.crew.promotion_records import _evaluate_suite_delta
from reckon.crew.runs import _write_json, pointer_path
from tests.test_passed_verdict_needs_exit_zero import (
    _complete_arguments,
    _write_pointer,
)

SUITE_TEMPLATE = (
    "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q {node_test_files}"
)
TEST_FILE = "tests/test_alpha.py"
BASE_FAILURE_NAME = "tests/test_alpha.py::test_alpha"
ADDED_FAILURE = "tests/test_alpha.py::test_added"


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _command() -> str:
    return SUITE_TEMPLATE.replace("{node_test_files}", TEST_FILE)


def _observation(
    revision: str, *, failure_ids: list[str] | None = None
) -> dict[str, object]:
    ids = list(failure_ids or [])
    return {
        "revision": revision,
        "command": _command(),
        "exit_status": 1 if ids else 0,
        "log_path": f"/durable/{revision}.log",
        "completed": True,
        "failure_count": len(ids),
        "failure_ids": ids,
    }


@pytest.fixture()
def landing_repository(tmp_path, monkeypatch) -> Path:
    """A fixture crew home and a repository carrying one plan."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "plans" / "plan-a.html").write_text(
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="plan-a">'
        "</head><body></body></html>",
        encoding="utf-8",
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(root, "add", "docs")
    _git(root, "commit", "-q", "-m", "seed repository")
    return root


@pytest.fixture(autouse=True)
def _live_pointers() -> None:
    """The refusal paths amend the run's live pointer, so it must exist."""
    for run_id in ("r-suite-head", "r-dry-run"):
        _write_json(
            pointer_path(run_id),
            {"run_id": run_id, "node": {"id": "node-a", "write_paths": [TEST_FILE]}},
        )


@pytest.fixture()
def tree(tmp_path: Path) -> tuple[Path, Path, str, str, str]:
    """A run's worktree whose head moved from a base through code to docs."""
    main = tmp_path / "main"
    (main / "tests").mkdir(parents=True)
    (main / TEST_FILE).write_text("def test_observation():\n    assert True\n")
    _git(main, "init", "-q", "-b", "main")
    _git(main, "config", "user.email", "worker@example.invalid")
    _git(main, "config", "user.name", "Worker")
    _git(main, "add", "tests")
    _git(main, "commit", "-q", "-m", "base")
    base = _git(main, "rev-parse", "HEAD")
    worktree = tmp_path / "run-worktree"
    _git(main, "worktree", "add", "-q", "--detach", str(worktree), base)
    (worktree / "reckon").mkdir(parents=True, exist_ok=True)
    (worktree / "reckon" / "alpha.py").write_text("VALUE = 1\n")
    _git(worktree, "add", "reckon")
    _git(worktree, "commit", "-q", "-m", "code")
    code_head = _git(worktree, "rev-parse", "HEAD")
    (worktree / "docs").mkdir(exist_ok=True)
    (worktree / "docs" / "note.html").write_text("<p>evidence</p>\n")
    _git(worktree, "add", "docs")
    _git(worktree, "commit", "-q", "-m", "docs")
    return worktree, main, base, code_head, _git(worktree, "rev-parse", "HEAD")


def _record(
    tmp_path: Path,
    worktree: Path,
    repo: Path,
    base: str,
    *,
    baseline: dict[str, object],
    after: dict[str, object] | None = None,
    commits: list[str] | None = None,
) -> dict[str, object]:
    manifest = tmp_path / "manifest.md"
    lines = [
        "node: node-a",
        "status: complete",
        f"baseline_suite: {json.dumps(baseline)}",
        f"after_suite: {json.dumps(after or baseline)}",
    ]
    if commits is not None:
        lines.append("commits: " + ",".join(commits))
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "run_id": "r-suite-head",
        "suite_command": SUITE_TEMPLATE,
        "worktree": str(worktree),
        "repo": str(repo),
        "base_sha": base,
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {"write_paths": [TEST_FILE]},
    }


def test_a_head_that_adds_no_failure_measures_zero_against_a_red_base(
    tmp_path: Path, tree: tuple[Path, Path, str, str, str]
) -> None:
    """The plan's negative control: the paired base-and-head failure is red twice."""
    worktree, repo, base, code_head, _doc_head = tree
    record = _record(
        tmp_path,
        worktree,
        repo,
        base,
        baseline=_observation(base, failure_ids=[BASE_FAILURE_NAME]),
        after=_observation(code_head, failure_ids=[BASE_FAILURE_NAME]),
    )

    delta = _evaluate_suite_delta("r-suite-head", record, waiver_reason="")

    assert delta["status"] == "clean"
    assert delta["added_failure_ids"] == []


def test_a_head_that_adds_a_failure_beside_a_red_base_is_refused(
    tmp_path: Path, tree: tuple[Path, Path, str, str, str]
) -> None:
    """The other half of the pair: the same base, an id the head alone fails."""
    worktree, repo, base, code_head, _doc_head = tree
    record = _record(
        tmp_path,
        worktree,
        repo,
        base,
        baseline=_observation(base, failure_ids=[BASE_FAILURE_NAME]),
        after=_observation(code_head, failure_ids=[BASE_FAILURE_NAME, ADDED_FAILURE]),
    )

    with pytest.raises(ledger.SuiteDeltaError) as refused:
        _evaluate_suite_delta("r-suite-head", record, waiver_reason="")

    assert ADDED_FAILURE in refused.value.added_failure_ids


def test_a_documentation_commit_after_the_arm_that_suite_reads_keeps_it(
    tmp_path: Path, tree: tuple[Path, Path, str, str, str]
) -> None:
    """The head moved through documentation: the arm still measures its suite."""
    worktree, repo, base, code_head, doc_head = tree
    record = _record(
        tmp_path,
        worktree,
        repo,
        base,
        baseline=_observation(base),
        after=_observation(code_head),
        commits=[doc_head],
    )

    delta = _evaluate_suite_delta("r-suite-head", record, waiver_reason="")

    assert delta["status"] == "clean"


def test_a_code_commit_after_the_arm_still_refuses_it(
    tmp_path: Path, tree: tuple[Path, Path, str, str, str]
) -> None:
    """The same shape with the doc commit replaced by a code commit refuses."""
    worktree, repo, base, code_head, _doc_head = tree
    record = _record(
        tmp_path,
        worktree,
        repo,
        base,
        baseline=_observation(base),
        after=_observation(base),
        commits=[code_head],
    )

    with pytest.raises(ledger.SuiteDeltaError) as refused:
        _evaluate_suite_delta("r-suite-head", record, waiver_reason="")

    assert "after_suite.revision_matches_head" in refused.value.missing_fields


def test_a_dry_run_names_the_arms_it_would_measure_and_measures_none(
    tmp_path: Path,
    tree: tuple[Path, Path, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dry run reports the arms a landing would take, and runs none of them."""
    worktree, repo, base, _code_head, _doc_head = tree
    manifest = tmp_path / "manifest.md"
    manifest.write_text("node: node-a\nstatus: complete\n", encoding="utf-8")
    record = {
        "run_id": "r-dry-run",
        "suite_command": SUITE_TEMPLATE,
        "worktree": str(worktree),
        "repo": str(repo),
        "base_sha": base,
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {"write_paths": [TEST_FILE]},
    }

    def _must_not_measure(*_args, **_kwargs):
        raise AssertionError("a dry run measured the suite")

    monkeypatch.setattr(promotion_records, "_measure_missing_arms", _must_not_measure)

    delta = _evaluate_suite_delta("r-dry-run", record, waiver_reason="", dry_run=True)

    assert delta["status"] == "would-measure"
    assert "baseline_suite" in " ".join(delta["missing_fields"])
    assert delta["added_failure_ids"] == []


def test_a_dry_run_names_the_control_it_would_measure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A control the run kept no log for is what a landing would measure."""
    record = {
        "run_id": "r-dry-control",
        "node": {
            "id": "node-a",
            "write_paths": [TEST_FILE],
            "negative_control": "remove the guard in a scratch copy",
        },
    }

    def _must_not_measure(*_args, **_kwargs):
        raise AssertionError("a dry run ran the declared mutation")

    monkeypatch.setattr(promotion_checks, "_measured_control", _must_not_measure)

    check = promotion_checks._require_declared_negative_control(
        "r-dry-control",
        record,
        gate="passed",
        manifest={"negative_control_log": ""},
        manifest_path="/nonexistent/manifest.md",
        measure=False,
    )

    assert check["verdict"] == "would-measure"


def _queued_arguments(run_id: str, repository: Path, log: Path) -> list[str]:
    return [
        item
        for item in _complete_arguments(run_id, repository, log)
        if item != "--wait"
    ]


def test_the_request_a_complete_queues_runs_when_the_drain_takes_it(
    landing_repository: Path, tmp_path: Path
) -> None:
    """A queued request carries only names the command takes, so it can run.

    The drain forwards every stored option as a keyword to the command it
    re-runs, so a name the command does not declare makes the queued promotion
    fail before it reaches any guard. Queuing and then draining is the only way
    to see that: the queueing call returns a receipt either way.
    """
    run_id = "r-20261114T200000000000-queued"
    _write_pointer(run_id, landing_repository)
    log = tmp_path / "gate.log"
    log.write_text("12 passed in 0.4s\nEXIT=0\n", encoding="utf-8")

    first = CliRunner().invoke(
        cli_main, _queued_arguments(run_id, landing_repository, log)
    )
    assert first.exit_code == 0, first.output

    request = promotion_requests._read(promotion_requests._request_path(run_id))
    assert request["phase"] == promotion_requests.REQUESTED
    result = promotion_requests._execute(request)

    assert result["ok"] is True
    row = Path(result["ledger_path"])
    assert row.is_file()
    assert json.loads(row.read_text(encoding="utf-8"))["run_id"] == run_id


def test_a_repeat_carries_the_queued_promotion(
    landing_repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20261114T200000000000-repeated"
    _write_pointer(run_id, landing_repository)
    log = tmp_path / "gate.log"
    log.write_text("12 passed in 0.4s\nEXIT=0\n", encoding="utf-8")
    arguments = _queued_arguments(run_id, landing_repository, log)

    first = CliRunner().invoke(cli_main, arguments)
    assert first.exit_code == 0, first.output

    second = CliRunner().invoke(cli_main, arguments)

    assert second.exit_code == 0, second.output
    outcome = json.loads(second.output)
    assert outcome.get("driven") is True
    # The repeat carried the request out rather than only reporting it: the
    # row it wrote is on disk and nothing is left queued for a drain to find.
    assert Path(outcome["ledger_path"]).is_file()
    settled = promotion_requests._read(promotion_requests._request_path(run_id))
    assert settled["phase"] == promotion_requests.SETTLED
