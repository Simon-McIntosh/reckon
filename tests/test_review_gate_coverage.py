"""The promotion review gate covers every run that writes the repository.

A run is gated on having produced work a reviewer must read — a path inside its
own repository changed — rather than on the role name that carried it, and a
manifest status the reader does not recognise is refused by name rather than
left to declassify the run.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, crew, ledger
from reckon.cli import main as cli_main
from reckon.crew import review as review_module
from reckon.crew.reports import MANIFEST_STATUSES
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


def _write_resource(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": PLAN,
                "title": "Plan A",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_resource(root / "docs" / "plans" / f"{PLAN}.html")
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


def _seed_candidate(repository: Path) -> tuple[str, str]:
    """Commit a candidate file twice; return (base, delivered).

    The base is the revision the run started from and the delivered revision is
    the one its manifest cites, so a cited commit genuinely changes an
    in-repository path rather than merely claiming to.
    """
    candidate = repository / "candidate.txt"
    candidate.write_text("seed\n", encoding="utf-8")
    _git(repository, "add", "candidate.txt")
    _git(repository, "commit", "-q", "-m", "test: seed candidate")
    base = _git(repository, "rev-parse", "HEAD")
    candidate.write_text("seed\ndelivered\n", encoding="utf-8")
    _git(repository, "add", "candidate.txt")
    _git(repository, "commit", "-q", "-m", "test: record candidate")
    return base, _git(repository, "rev-parse", "HEAD")


def _write_complete_pointer(
    repository: Path,
    tmp_path: Path,
    run_id: str,
    *,
    role: str = "implement",
    changed_paths: str = "[]",
    commits: str = "",
    status: str = "complete",
    base: str = "",
) -> None:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    commit_line = f"commits: {commits}\n" if commits else ""
    manifest.write_text(
        "node: node-a\n"
        f"status: {status}\n"
        f"{commit_line}"
        f"changed_paths: {changed_paths}\n"
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
            "base_sha": base,
            "launch": "in-harness",
            "role": role,
            "backend": "native",
            "created_at": "2026-09-21T06:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "candidate-change",
                "time_budget": "25m",
                "write_paths": ["candidate.txt"],
            },
        },
    )


def _store_complete_review(run_id: str) -> None:
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    review = review_module.parse_review(emitted)
    review.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
        }
    )
    review_module.store_review(review)


def _row(repository: Path, run_id: str) -> dict:
    return next(
        row
        for row in ledger.load(PROJECT, repository)[0]["runs"]
        if row["run_id"] == run_id
    )


def test_passing_implement_run_without_review_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260921T060000000000-unreviewed"
    _write_complete_pointer(repository, tmp_path, run_id)

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", root=repository)

    message = str(refusal.value)
    assert run_id in message
    assert "reckon crew dispatch" in message
    assert "--role review" in message
    assert pointer_path(run_id).exists()


def test_reasoned_waiver_promotes_and_records_reason(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260921T060100000000-unreviewed"
    reason = "the review lane is unavailable and this repair is urgent"
    _write_complete_pointer(repository, tmp_path, run_id)

    crew.complete(
        run_id,
        gate="passed",
        review_waiver=reason,
        root=repository,
    )

    assert _row(repository, run_id)["review_waiver"]["reason"] == reason


def test_reviewed_run_promotes_without_waiver(repository: Path, tmp_path: Path) -> None:
    run_id = "r-20260921T060200000000-reviewed"
    _write_complete_pointer(repository, tmp_path, run_id)
    _store_complete_review(run_id)

    crew.complete(run_id, gate="passed", root=repository)

    row = _row(repository, run_id)
    assert row["review"]["status"] == "parsed"
    assert "review_waiver" not in row


def test_review_role_is_not_gated_on_its_own_review(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260921T060300000000-review"
    _write_complete_pointer(repository, tmp_path, run_id, role="review")

    crew.complete(run_id, gate="passed", root=repository)

    assert _row(repository, run_id)["role"] == "review"


def test_waiver_is_refused_when_review_is_already_stored(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260921T060400000000-reviewed"
    reason = "there is nothing left to review"
    _write_complete_pointer(repository, tmp_path, run_id)
    _store_complete_review(run_id)

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(
            run_id,
            gate="passed",
            review_waiver=reason,
            root=repository,
        )

    message = str(refusal.value)
    assert "no unreviewed promotion" in message
    assert reason in message


def test_complete_help_names_the_review_waiver() -> None:
    result = CliRunner().invoke(cli_main, ["crew", "complete", "--help"])

    assert result.exit_code == 0
    assert "--waive-unreviewed-promotion" in result.output


def _refusal(run_id: str, repository: Path, **kwargs: object) -> str:
    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", root=repository, **kwargs)
    return str(refusal.value)


@pytest.mark.parametrize("role", ["test", "documentation", "investigate"])
def test_a_role_writing_a_tracked_file_is_refused_without_a_review(
    role: str, repository: Path, tmp_path: Path
) -> None:
    base, commit = _seed_candidate(repository)
    run_id = f"r-20260921T070000000000-{role}-writes"
    _write_complete_pointer(
        repository,
        tmp_path,
        run_id,
        role=role,
        changed_paths="candidate.txt",
        commits=commit,
        base=base,
    )

    message = _refusal(run_id, repository, commits=[commit])

    assert run_id in message
    assert "no complete independent review is stored" in message
    assert "--waive-unreviewed-promotion" in message
    assert pointer_path(run_id).exists()


@pytest.mark.parametrize("role", ["test", "documentation", "investigate"])
def test_a_role_changing_no_tracked_path_is_not_refused(
    role: str, repository: Path, tmp_path: Path
) -> None:
    run_id = f"r-20260921T070100000000-{role}-report"
    _write_complete_pointer(repository, tmp_path, run_id, role=role)

    crew.complete(run_id, gate="passed", root=repository)

    assert _row(repository, run_id)["run_id"] == run_id


def test_a_run_without_a_recorded_role_but_a_changed_path_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """The gate follows the writing even when no role name was recorded."""
    base, commit = _seed_candidate(repository)
    run_id = "r-20260921T070200000000-role-less-writes"
    _write_complete_pointer(
        repository,
        tmp_path,
        run_id,
        role="",
        changed_paths="candidate.txt",
        commits=commit,
        base=base,
    )

    message = _refusal(run_id, repository, commits=[commit])

    assert "no complete independent review is stored" in message


def test_a_test_role_run_with_a_stored_review_promotes(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260921T070300000000-test-reviewed"
    _write_complete_pointer(repository, tmp_path, run_id, role="test")
    _store_complete_review(run_id)

    crew.complete(run_id, gate="passed", root=repository)

    assert _row(repository, run_id)["review"]["status"] == "parsed"


def test_a_review_role_run_stays_exempt_when_it_changed_a_tracked_file(
    repository: Path, tmp_path: Path
) -> None:
    """The exemption belongs to the role that produces reviews, not to the
    absence of a change: the review a review run wrote for another run is its
    deliverable, so the run itself is not gated on having one."""
    base, commit = _seed_candidate(repository)
    run_id = "r-20260921T070400000000-review-writes"
    _write_complete_pointer(
        repository,
        tmp_path,
        run_id,
        role="review",
        changed_paths="candidate.txt",
        commits=commit,
        base=base,
    )

    crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    row = _row(repository, run_id)
    assert row["role"] == "review"
    assert "review_waiver" not in row


def test_an_implement_run_changing_nothing_is_still_refused(
    repository: Path, tmp_path: Path
) -> None:
    """Existing implement behaviour is unchanged: the role is gated whether or
    not its manifest names a path, so silence is not an escape."""
    run_id = "r-20260921T070500000000-implement-silent"
    _write_complete_pointer(repository, tmp_path, run_id, role="implement")

    message = _refusal(run_id, repository)

    assert "no complete independent review is stored" in message


def test_an_implement_run_with_a_stored_review_promotes(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260921T070600000000-implement-reviewed"
    _write_complete_pointer(repository, tmp_path, run_id, role="implement")
    _store_complete_review(run_id)

    crew.complete(run_id, gate="passed", root=repository)

    assert _row(repository, run_id)["review"]["status"] == "parsed"


def test_a_reasoned_waiver_permits_a_documentation_change(
    repository: Path, tmp_path: Path
) -> None:
    base, commit = _seed_candidate(repository)
    run_id = "r-20260921T070700000000-documentation-waived"
    reason = "the review lane is unavailable and this doc repair is urgent"
    _write_complete_pointer(
        repository,
        tmp_path,
        run_id,
        role="documentation",
        changed_paths="candidate.txt",
        commits=commit,
        base=base,
    )

    crew.complete(
        run_id,
        gate="passed",
        commits=[commit],
        review_waiver=reason,
        root=repository,
    )

    assert _row(repository, run_id)["review_waiver"]["reason"] == reason


@pytest.mark.parametrize(
    "status",
    ["awaiting-orchestrator-review", "implemented-not-closed", "done"],
)
def test_an_unrecognised_manifest_status_is_refused_by_name(
    status: str, repository: Path, tmp_path: Path
) -> None:
    run_id = f"r-20260921T070800000000-status-{status}"
    _write_complete_pointer(repository, tmp_path, run_id, status=status)

    message = _refusal(run_id, repository)

    assert status in message
    assert pointer_path(run_id).exists()


def test_the_status_refusal_names_the_accepted_vocabulary(
    repository: Path, tmp_path: Path
) -> None:
    """The refusal states what the reader does accept, so the writer's next
    turn is a correction rather than a guess, and ``complete`` is named among
    them for the reason the gate exists."""
    run_id = "r-20260921T070900000000-status-vocabulary"
    _write_complete_pointer(repository, tmp_path, run_id, status="shipped")

    message = _refusal(run_id, repository)

    assert "shipped" in message
    for accepted in sorted(MANIFEST_STATUSES):
        assert accepted in message
