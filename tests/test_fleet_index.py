"""Fleet index rows computed live from a project's own repository."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from reckon import crew as crew_module
from reckon.fleet_index import compute_project_row

_NOW = datetime(2026, 3, 15, 12, 0, 0)  # noqa: DTZ001 — naive, matches GIT_AUTHOR_DATE below


@pytest.fixture(autouse=True)
def no_real_live_pointers(monkeypatch):
    """crew.list_live() must never touch this machine's real live-run state."""

    monkeypatch.setattr(crew_module, "list_live", lambda **kwargs: [])


def _plan_html(slug: str, status: str, *, depends_on: str = "") -> str:
    dependency = (
        f'<meta name="plan-depends-on" content="{depends_on}">' if depends_on else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{slug}">
<meta name="plan-status" content="{status}">
{dependency}
<title>{slug}</title></head><body><main class="plan-doc"></main></body></html>
"""


def _git(repo_dir: Path, *args: str, env: dict | None = None) -> None:
    subprocess.run(
        ["git", *args], cwd=repo_dir, check=True, capture_output=True, env=env
    )


def _commit_all(repo_dir: Path, message: str, when: datetime) -> None:
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S")
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_DATE": stamp,
        "GIT_AUTHOR_NAME": "tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
    }
    _git(repo_dir, "add", "-A", env=env)
    _git(repo_dir, "commit", "-q", "--allow-empty", "-m", message, env=env)


@pytest.fixture
def repo(tmp_path):
    repo_dir = tmp_path / "repository"
    docs_dir = repo_dir / "docs"
    (docs_dir / "plans").mkdir(parents=True)
    _git(repo_dir, "init", "-q")
    return repo_dir, docs_dir


def test_status_counts_use_the_spa_effective_status_rule(repo):
    repo_dir, docs_dir = repo
    (docs_dir / "plans" / "shipped.html").write_text(_plan_html("shipped", "shipped"))
    (docs_dir / "plans" / "active.html").write_text(_plan_html("active", "active"))
    (docs_dir / "plans" / "no-status.html").write_text(_plan_html("no-status", ""))
    (docs_dir / "plans" / "blocked-dep.html").write_text(
        _plan_html("blocked-dep", "active", depends_on="sample:missing")
    )
    (docs_dir / "plans" / "archive").mkdir()
    (docs_dir / "plans" / "archive" / "old.html").write_text(
        _plan_html("old", "shipped")
    )
    from reckon._plan_html import write_state

    archived_path = docs_dir / "plans" / "archive" / "old.html"
    archived_path.write_text(write_state(archived_path.read_text(), {"archived": "1"}))
    _commit_all(repo_dir, "seed", _NOW)

    row = compute_project_row(docs_dir, "sample", now=_NOW)

    # The archived plan is excluded from every count.
    assert row["plans_count"] == 4
    assert row["active"] == 1
    assert row["blocked"] == 1
    assert row["pending"] == 1
    assert row["shipped"] == 1
    assert (
        row["active"] + row["blocked"] + row["pending"] + row["shipped"]
        == (row["plans_count"])
    )


def test_live_counts_this_projects_run_pointers_only(repo, monkeypatch):
    repo_dir, docs_dir = repo
    (docs_dir / "plans" / "solo.html").write_text(_plan_html("solo", "active"))
    _commit_all(repo_dir, "seed", _NOW)

    calls = []

    def fake_list_live(**kwargs):
        calls.append(kwargs)
        return [{"project": "sample"}, {"project": "sample"}]

    monkeypatch.setattr(crew_module, "list_live", fake_list_live)

    row = compute_project_row(docs_dir, "sample", now=_NOW)

    assert row["live"] == 2
    assert {"project": "sample"} in calls


def test_last_edited_is_the_latest_edited_stamp_across_artifacts(repo):
    repo_dir, docs_dir = repo
    older = docs_dir / "plans" / "older.html"
    newer = docs_dir / "plans" / "newer.html"
    older.write_text(_plan_html("older", "active"))
    newer.write_text(_plan_html("newer", "active"))
    _commit_all(repo_dir, "seed", _NOW - timedelta(days=5))

    # Touch one file after the commit so its edited stamp is its own mtime,
    # strictly after the committed stamp every other row carries.
    future = _NOW + timedelta(days=1)
    os.utime(newer, (future.timestamp(), future.timestamp()))

    row = compute_project_row(docs_dir, "sample", now=_NOW)

    assert row["last_edited"] == row["last_modified"]
    edited_dt = datetime.fromisoformat(row["last_edited"])
    assert edited_dt >= future - timedelta(seconds=2)


def test_active_sprint_from_distributed_state_or_null(repo):
    repo_dir, docs_dir = repo
    (docs_dir / "plans" / "solo.html").write_text(_plan_html("solo", "active"))
    state_dir = docs_dir / "state" / "sample"
    state_dir.mkdir(parents=True)
    (state_dir / "index.json").write_text(
        json.dumps(
            {
                "updated": "2026-03-01T00:00:00",
                "project": "sample",
                "doc": "index",
                "data": {
                    "active_sprint_id": "current",
                    "projects": [],
                    "sprints": [
                        {
                            "id": "earlier",
                            "theme": "Earlier work",
                            "status": "done",
                            "items": [],
                        },
                        {
                            "id": "current",
                            "theme": "Present work",
                            "status": "active",
                            "items": [],
                        },
                    ],
                },
            }
        )
    )
    _commit_all(repo_dir, "seed", _NOW)

    row = compute_project_row(
        docs_dir, "sample", state_root=docs_dir / "state", now=_NOW
    )
    assert row["active_sprint"] == {"id": "current", "theme": "Present work"}


def test_active_sprint_is_null_without_a_referenced_sprint(repo):
    repo_dir, docs_dir = repo
    (docs_dir / "plans" / "solo.html").write_text(_plan_html("solo", "active"))
    _commit_all(repo_dir, "seed", _NOW)

    row = compute_project_row(docs_dir, "sample", now=_NOW)
    assert row["active_sprint"] is None


def test_activity30_counts_commits_touching_named_subpaths_by_day(repo):
    repo_dir, docs_dir = repo
    (docs_dir / "plans" / "solo.html").write_text(_plan_html("solo", "active"))
    _commit_all(repo_dir, "seed", _NOW - timedelta(days=25))

    (docs_dir / "plans" / "second.html").write_text(_plan_html("second", "active"))
    _commit_all(repo_dir, "second", _NOW - timedelta(days=25))

    (docs_dir / "research").mkdir()
    (docs_dir / "research" / "note.html").write_text("<html></html>")
    _commit_all(repo_dir, "research note", _NOW - timedelta(days=2))

    # A commit outside the 30-day window must not appear in the series.
    (docs_dir / "plans" / "ancient.html").write_text(_plan_html("ancient", "active"))
    _commit_all(repo_dir, "ancient", _NOW - timedelta(days=45))

    row = compute_project_row(docs_dir, "sample", now=_NOW)

    assert len(row["activity30"]) == 30
    assert row["activity30"][-1] == 0  # today itself has no commit
    assert row["activity30"][-3] == 1  # research note, two days ago
    assert row["activity30"][4] == 2  # both plan commits, 25 days ago
    assert sum(row["activity30"]) == 3


def test_activity30_is_empty_not_thirty_zeros_when_silent(repo):
    repo_dir, docs_dir = repo
    (docs_dir / "plans" / "solo.html").write_text(_plan_html("solo", "active"))
    _commit_all(repo_dir, "seed", _NOW - timedelta(days=90))

    row = compute_project_row(docs_dir, "sample", now=_NOW)

    assert row["activity30"] == []


def test_reference_instant_is_a_parameter_not_the_wall_clock(repo):
    repo_dir, docs_dir = repo
    (docs_dir / "plans" / "solo.html").write_text(_plan_html("solo", "active"))
    _commit_all(repo_dir, "seed", _NOW)

    fixed_past = _NOW - timedelta(days=400)
    row = compute_project_row(docs_dir, "sample", now=fixed_past)

    # The commit long postdates this artificially early reference instant,
    # so it must fall outside the computed window regardless of wall time.
    assert row["activity30"] == []
