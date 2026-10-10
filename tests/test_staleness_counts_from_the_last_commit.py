"""A plan's staleness is measured from its last commit, not from its file.

The audit read a plan's mtime, which every server write refreshes, so a
freshly-served plan reported as new while its committed work sat unchanged.
This pins the last commit as the age source, and the band in which an active
plan with unfinished work is reported as ageing before it is stale.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from reckon.doccheck import LifecycleFinding, audit_lifecycle

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _plan_html(slug: str) -> str:
    """A minimal active plan with unfinished work and no authored dates."""

    return (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        f'<meta name="plan-slug" content="{slug}">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-impl" content="0.5">'
        '<meta name="docs-project" content="proj">'
        f"<title>{slug}</title></head>"
        '<body><main class="plan-doc"></main></body></html>'
    )


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_GIT_ENV, **(env or {})},
    )


def _commit(repo: Path, when: str) -> None:
    _git(repo, "add", "docs")
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        "commit plan",
        env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when},
    )


def _today() -> date:
    """Today in the local zone, the way the audit reads a date from a stamp."""

    return datetime.now(tz=UTC).astimezone().date()


def _day_of(ts: float) -> date:
    return datetime.fromtimestamp(ts, tz=UTC).astimezone().date()


def _age_of(committed: datetime) -> int:
    """Whole days from a commit's own date to today, never a literal."""

    return (_today() - committed.astimezone().date()).days


@pytest.fixture
def aged_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A temporary repository whose two plans were last committed at set ages.

    The plans are left with no authored dates and are touched on disk today,
    so the only sources of their age are the commit and the file mtime — the
    two the audit must tell apart.
    """

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config-home"))
    # The commit map persists a cache under the configuration home; point it at
    # in-process state so the run touches nothing outside the test's repository.
    from reckon import serve

    monkeypatch.setattr(serve, "_GIT_LAST_MODIFIED_CACHE", {})
    monkeypatch.setattr(serve, "_GIT_HEAD_MEMO", {})
    monkeypatch.setattr(serve, "_load_git_last_modified_cache", lambda key: None)
    monkeypatch.setattr(serve, "_store_git_last_modified_cache", lambda *a, **k: None)

    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    _git(repo, "init", "-q")

    now = datetime.now(UTC)
    aging_committed = now - timedelta(days=20)
    stale_committed = now - timedelta(days=40)

    (docs / "aging.html").write_text(_plan_html("aging"), encoding="utf-8")
    _commit(repo, aging_committed.isoformat())
    (docs / "stale.html").write_text(_plan_html("stale"), encoding="utf-8")
    _commit(repo, stale_committed.isoformat())

    # A server write refreshes mtimes without any author touching the plan.
    touched = now.timestamp()
    for name in ("aging.html", "stale.html"):
        os.utime(docs / name, (touched, touched))

    return {
        "docs": docs,
        "aging_committed": aging_committed,
        "stale_committed": stale_committed,
    }


def _lifecycle(docs: Path) -> dict[tuple[str, str], LifecycleFinding]:
    findings = audit_lifecycle(project="proj", docs_dir=docs)
    return {
        (item.slug, item.flag): item
        for item in findings
        if isinstance(item, LifecycleFinding)
    }


def test_plan_age_counts_from_the_last_commit_not_the_file(aged_repo: dict) -> None:
    found = _lifecycle(aged_repo["docs"])

    aging = found[("aging", "AGING")]
    assert aging.age_days == _age_of(aged_repo["aging_committed"])
    assert aging.age_source == "commit"
    assert aging.age_days == 20

    stale = found[("stale", "STALE")]
    assert stale.age_days == _age_of(aged_repo["stale_committed"])
    assert stale.age_source == "commit"
    assert stale.age_days == 40


def test_todays_file_mtime_does_not_hide_an_old_plan(aged_repo: dict) -> None:
    """The negative control: the file was touched today, the commit was not."""

    docs = aged_repo["docs"]
    assert (_today() - _day_of((docs / "aging.html").stat().st_mtime)).days == 0

    aging = _lifecycle(docs)[("aging", "AGING")]
    assert aging.age_days != 0
