"""Promotion makes an absent independent review a reasoned refusal."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, crew, ledger
from reckon.cli import main as cli_main
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


def _write_complete_pointer(
    repository: Path,
    tmp_path: Path,
    run_id: str,
    *,
    role: str = "implement",
) -> None:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\n"
        "status: complete\n"
        "commits: []\n"
        "changed_paths: []\n"
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
            "created_at": "2026-09-21T06:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "s2",
                "time_budget": "25m",
                "write_paths": [],
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
