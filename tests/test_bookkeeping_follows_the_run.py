"""Bookkeeping a run's own record carries is not demanded again of the operator.

Three demands on a coordinator's promotion are re-read from the run's own
record rather than restated by hand: a corrective attempt inherits the plan
movement its predecessor produced, a review run's summary is the review it
stored, and a checkout that has moved past the merged revision is measured
when nothing the run changed differs. Each test here makes the bookkeeping the
run already carries the only thing standing between it and a refusal, and
reads the promotion that results.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, _store, crew
from reckon.crew import promotion
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write_plan_file(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc">'
        '<h2 id="s2">&sect;2 &mdash; Section two</h2>'
        "</main></body></html>\n"
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_plan_file(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
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
    return root


def _set_plan(repository: Path, **fields) -> None:
    state, version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    state.update(fields)
    _store.write_plan(PROJECT, PLAN, state, version, repository, artifact_type="plan")


def _plan_state(repository: Path) -> dict:
    state, _version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    return state


def _write_pointer(
    repository: Path,
    run_id: str,
    *,
    role: str = "implement",
    plan_impl_at_dispatch: float | None = None,
    attempt_kind: str | None = None,
    node_id: str = "node-a",
    node_section: str = "s2",
    write_paths: list[str] | None = None,
    plan: str = PLAN,
) -> None:
    record: dict = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(repository),
        "launch": "in-harness",
        "role": role,
        "backend": "native",
        "created_at": "2026-09-18T08:00:00Z",
        "node": {
            "id": node_id,
            "plan": plan,
            "section": node_section,
            "time_budget": "25m",
            "write_paths": list(write_paths or ()),
        },
    }
    if plan_impl_at_dispatch is not None:
        record["plan_impl_at_dispatch"] = plan_impl_at_dispatch
    if attempt_kind is not None:
        record["attempt_kind"] = attempt_kind
    _write_json(pointer_path(run_id), record)


def _ledger_row(repository: Path, run_id: str) -> dict:
    rows = crew.ledger.runs(PROJECT, root=repository)
    return next(row for row in rows if row.get("run_id") == run_id)


# (1) a corrective attempt inherits the movement it corrects


@pytest.mark.parametrize("attempt_kind", ["resume", "redispatch"])
def test_a_corrective_attempt_promotes_exempt_and_needs_no_impl_flag(
    repository: Path, attempt_kind: str
) -> None:
    """The plan did not move under this attempt because it moved under the one
    the attempt continues; the retry lands without an operator flag.

    Both arms are exercised: the same record without the attempt kind is
    refused, so the exemption is the attempt kind and not the fixture.
    """
    _set_plan(repository, impl=0.5)
    moving_run = f"r-20260918T083000000000-{attempt_kind}-moves"
    _write_pointer(repository, moving_run, plan_impl_at_dispatch=0.5)
    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(
            moving_run, root=repository, gate="passed", outcome="the work landed"
        )
    assert "--no-impl-change" in str(refusal.value)

    run_id = f"r-20260918T083100000000-{attempt_kind}-corrective"
    _write_pointer(
        repository,
        run_id,
        plan_impl_at_dispatch=0.5,
        attempt_kind=attempt_kind,
    )

    promoted = crew.complete(
        run_id,
        root=repository,
        gate="passed",
        outcome="the retry corrected the earlier attempt",
    )

    assert promoted["impl_move"]["verdict"] == "exempt"
    assert promoted["impl_move"]["reason"] == f"corrective-run:{attempt_kind}"
    row = _ledger_row(repository, run_id)
    assert row["impl_move"]["reason"] == f"corrective-run:{attempt_kind}"


# (2) a review run's outcome is the review it stored


def _write_stored_review(reviewed_run_id: str, review_run_id: str) -> Path:
    path = review_module.review_path(PROJECT, reviewed_run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "reviewed_run_id": reviewed_run_id,
                "review_run_id": review_run_id,
                "status": "parsed",
                "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 3),
                "total": 17,
                "findings": [
                    {"summary": "the first finding"},
                    {"summary": "the second finding"},
                    {"summary": "the third finding"},
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_a_review_runs_outcome_defaults_to_its_stored_review(
    repository: Path,
) -> None:
    """A non-passing review run lands on the summary its review already holds.

    A run with no stored review to read is still refused for the same gate, so
    the default is the review store and not an outcome field that quietly
    emptied.
    """
    _set_plan(repository, impl=0.5)
    reviewed = "r-20260918T084000000000-reviewed"
    review_run = "r-20260918T084100000000-reviewer"
    stored_review = _write_stored_review(reviewed, review_run)
    _write_pointer(
        repository,
        review_run,
        role="review",
        node_id=f"review-of-{reviewed}",
        write_paths=[str(stored_review)],
    )

    promoted = crew.complete(
        review_run,
        root=repository,
        gate="failed",
        failure_classification="work-rejected",
    )

    assert promoted["impl_move"]["verdict"] == "exempt"
    comments = _plan_state(repository).get("comments", {}).get("s2", [])
    landing = next(item for item in comments if item.get("id") == f"c-run-{review_run}")
    assert "review scored 17, 3 finding(s)" in landing["body"]

    unstored = "r-20260918T084300000000-reviewed-but-unstored"
    review_with_no_review = "r-20260918T084400000000-review-scoring-nothing"
    _write_pointer(
        repository,
        review_with_no_review,
        role="review",
        node_id=f"review-of-{unstored}",
    )
    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(
            review_with_no_review,
            root=repository,
            gate="failed",
            failure_classification="work-rejected",
        )
    assert "review run whose stored review cannot be read" in str(refusal.value)

    plain_run = "r-20260918T084500000000-not-a-review"
    _write_pointer(repository, plain_run)
    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(
            plain_run,
            root=repository,
            gate="failed",
            failure_classification="work-rejected",
        )
    assert "a non-passing gate requires --outcome" in str(refusal.value)


# (3) a checkout past the merged revision is measured when nothing differs


def _commit_file(repository: Path, relative: str, text: str, subject: str) -> str:
    target = repository / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(repository, "add", relative)
    _git(repository, "commit", "-q", "-m", subject)
    return _git(repository, "rev-parse", "HEAD")


def _promote_run(repository: Path, run_id: str, *, commit: str) -> None:
    crew.ledger.append_run(
        PROJECT,
        {
            "run_id": run_id,
            "gate": "passed",
            "commits": [commit],
            "gate_check": {
                "command": "sh gate.sh",
                "exit_status": 0,
                "log_digest": "x",
            },
        },
        root=repository,
    )


def _verify_gate(repository: Path, run_id: str, revision: str):
    from click.testing import CliRunner

    from reckon.cli import main as cli_main

    return CliRunner().invoke(
        cli_main,
        [
            "crew",
            "verify-gate",
            "--project",
            PROJECT,
            "--run",
            run_id,
            "--checkout-path",
            str(repository),
            "--revision",
            revision,
        ],
    )


def _seed_two_ledger_commits(repository: Path, *, touches_run_path: bool) -> str:
    """Land two bookkeeping commits past the merge.

    The second touches the run's own changed path only when the caller asks it
    to, so the accepted and refused arms differ in exactly that fact.
    """
    _commit_file(
        repository,
        "docs/state/proj/ledger-notes.txt",
        "{}\n",
        "chore: first bookkeeping commit past the merge",
    )
    later = "pkg/target.py" if touches_run_path else "docs/notes.md"
    return _commit_file(
        repository,
        later,
        "written by the second bookkeeping commit\n",
        "chore: second bookkeeping commit past the merge",
    )


def _merged_run(repository: Path) -> str:
    _set_plan(repository, impl=0.5)
    (repository / "gate.sh").write_text(
        "#!/bin/sh\necho stored > stored.marker\n", encoding="utf-8"
    )
    return _commit_file(
        repository, "pkg/target.py", "the run's own change\n", "feat: the run's change"
    )


def test_a_checkout_past_the_merge_is_measured_when_no_changed_path_differs(
    repository: Path,
) -> None:
    merged = _merged_run(repository)
    head = _seed_two_ledger_commits(repository, touches_run_path=False)
    assert head != merged
    run_id = "r-20260918T085000000000-verify-past-merge"
    _promote_run(repository, run_id, commit=merged)

    result = _verify_gate(repository, run_id, merged)

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)["report"]
    assert report["checkout_descends_from_integrated_revision"] is True
    assert report["changed_paths_differing"] == []
    assert report["ran"] is True
    assert report["integrated_verdict"] == "passed"


def test_a_checkout_past_the_merge_is_refused_when_a_changed_path_differs(
    repository: Path,
) -> None:
    merged = _merged_run(repository)
    _seed_two_ledger_commits(repository, touches_run_path=True)
    run_id = "r-20260918T085100000000-verify-differs"
    _promote_run(repository, run_id, commit=merged)

    result = _verify_gate(repository, run_id, merged)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    report = payload["report"]
    assert report["ran"] is False
    assert report["changed_paths_differing"] == ["pkg/target.py"]
    assert "wrong tree" in (report["reason"] or "")
    assert "pkg/target.py" in report["reason"]
    assert not (repository / "stored.marker").exists()


def test_a_checkout_past_the_merge_with_unknown_run_paths_is_refused(
    repository: Path,
) -> None:
    """A run whose changed paths cannot be read is refused past the merge.

    The ledger row cites a revision the checkout does not carry, so nothing
    establishes which paths the run changed. An unknown scope is not an empty
    one: the comparison is reported as null rather than as an empty list, and
    the gate never runs.
    """
    merged = _merged_run(repository)
    _seed_two_ledger_commits(repository, touches_run_path=False)
    run_id = "r-20260918T085200000000-verify-unknown-paths"
    _promote_run(repository, run_id, commit="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")

    result = _verify_gate(repository, run_id, merged)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    report = payload["report"]
    assert report["ran"] is False
    assert report["changed_paths_differing"] is None
    assert "wrong tree" in (report["reason"] or "")
    assert not (repository / "stored.marker").exists()


def test_a_path_comparison_that_cannot_be_taken_is_refused(
    repository: Path,
) -> None:
    """A failed path comparison refuses rather than measuring the tree.

    A comparison that cannot be taken establishes nothing about the extra
    commits, so it must not read as an empty difference and let the gate run.
    The path here is one git cannot use as a pathspec, which is the shape a
    hand-edited ledger row carries.
    """
    merged = _merged_run(repository)
    _seed_two_ledger_commits(repository, touches_run_path=False)

    report = promotion.rerun_gate_at_integrated_revision(
        repository=repository,
        gate_check={"command": "sh gate.sh", "exit_status": 0, "log_digest": "x"},
        base_verdict="passed",
        integrated_revision=merged,
        changed_paths=[":(bogus)"],
    )

    assert report["ran"] is False
    assert report["integrated_verdict"] == "not-run"
    assert report["changed_paths_differing"] is None
    assert "wrong tree" in (report["reason"] or "")
    assert not (repository / "stored.marker").exists()
