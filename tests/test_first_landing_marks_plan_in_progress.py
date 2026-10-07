"""A plan's first landing moves a draft or pending plan to in-progress."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, _store
from reckon.crew import promotion

PROJECT = "first-landing-fixture"
PLAN = "first-landing-target"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_plan(root: Path, *, status: str) -> Path:
    path = root / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "First landing target",
        "status": status,
        "version": 0,
        "comments": {},
    }
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def real_stores_are_not_fixture_targets() -> None:
    repository = Path(__file__).resolve().parents[1]
    real_plan = repository / "docs" / "plans" / f"{PLAN}.html"
    real_state = repository / "docs" / "state" / PROJECT
    assert not real_plan.exists()
    assert not real_state.exists()
    yield
    assert not real_plan.exists()
    assert not real_state.exists()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    _write_plan(root, status="draft")
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _record(repository: Path, run_id: str, narrative: str) -> dict:
    return promotion._record_landing_comment(
        project=PROJECT,
        plan=PLAN,
        section="§1",
        run_id=run_id,
        narrative=narrative,
        author="reckon-build",
        when="2026-10-07T01:20:00Z",
        root=repository,
    )


def test_draft_plan_becomes_in_progress_with_the_landing_comment(
    repository: Path,
) -> None:
    before, before_version = _store.read_plan(
        PROJECT, PLAN, repository, artifact_type="plan"
    )
    assert before["status"] == "draft"

    result = _record(repository, "r-20261007T012000000001-first-landing", "landed")

    assert result["recorded"] is True
    plan, version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    assert plan["status"] == "in-progress"
    assert (
        plan["comments"]["s1"][0]["id"] == "c-run-r-20261007T012000000001-first-landing"
    )
    # Both the comment and the status flip rode in one write: one version bump.
    assert version == before_version + 1


def test_pending_plan_becomes_in_progress_with_the_landing_comment(
    repository: Path,
) -> None:
    _write_plan(repository, status="pending")
    _git(repository, "add", "docs")
    _git(repository, "commit", "-q", "-m", "test: pending plan")

    _record(repository, "r-20261007T012000000002-first-landing", "landed")

    plan, _version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    assert plan["status"] == "in-progress"


@pytest.mark.parametrize("status", ["active", "shipped"])
def test_other_statuses_are_left_unchanged(repository: Path, status: str) -> None:
    _write_plan(repository, status=status)
    _git(repository, "add", "docs")
    _git(repository, "commit", "-q", "-m", f"test: {status} plan")

    _record(repository, f"r-20261007T012000000003-{status}", "landed")

    plan, _version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    assert plan["status"] == status
    assert plan["comments"]["s1"][0]["id"].endswith(status)

