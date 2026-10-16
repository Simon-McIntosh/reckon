"""A docs root outside any repository lets git look once, not once per plan.

The lifecycle audit derives each plan's age from the last commit touching a
path. For a docs root with no repository that lookup spawns ``git rev-parse
HEAD``, which fails and yields no commit, and nothing used to remember the
failure, so the read paid one failed process per plan. The HEAD lookup now
records a root git reports as outside any repository, keyed by the root's path
and directory stamp, so later age and HEAD lookups return no commit without
spawning again.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

from reckon import doccheck, serve
from reckon.doccheck import derived_plan_age
from reckon.serve import _git_head

PLANS = 12
COMMIT_TS = datetime(2026, 1, 5, 12, 0, 0, tzinfo=UTC).timestamp()
TODAY = date(2026, 6, 1)


def _plan_html(slug: str, modified: str) -> str:
    return (
        "<!doctype html><html><head>\n"
        '<meta name="docs-project" content="probe">\n'
        '<meta name="reckon-type" content="plan">\n'
        f'<meta name="plan-slug" content="{slug}">\n'
        f'<meta name="plan-title" content="Plan {slug}">\n'
        '<meta name="plan-summary" content="a short summary">\n'
        '<meta name="plan-status" content="active">\n'
        f'<meta name="plan-modified" content="{modified}">\n'
        "</head><body></body></html>\n"
    )


def _build_tree(root: Path, modified: str = "2026-01-05") -> Path:
    """Write a docs root holding ``PLANS`` plans and return its docs directory."""

    docs = root / "docs"
    (docs / "plans").mkdir(parents=True)
    for index in range(PLANS):
        plan = docs / "plans" / f"p{index:02d}.html"
        plan.write_text(_plan_html(f"p{index:02d}", modified), encoding="utf-8")
        os.utime(plan, (COMMIT_TS, COMMIT_TS))
    return docs


def _git(cwd: Path, *args: str, env: dict | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, env=env)


def _run_audit(docs: Path) -> list:
    return doccheck.audit_lifecycle(project="probe", docs_dir=docs, now_ts=COMMIT_TS)


@contextlib.contextmanager
def _counting_git_spawns(monkeypatch):
    """Count every git process the read starts, whatever its call site.

    ``reckon.serve._run_git`` is the one place that spawns git for a plan's age
    or HEAD, so a counter wrapped around it counts one per subprocess and needs
    no shim on ``PATH``.
    """

    calls = {"count": 0}
    real = serve._run_git

    def wrapper(args, repo_dir, *, operation):
        calls["count"] += 1
        return real(args, repo_dir, operation=operation)

    monkeypatch.setattr(serve, "_run_git", wrapper)
    yield lambda: calls["count"]


def test_no_repository_root_asks_git_at_most_once(tmp_path, monkeypatch):
    """A no-repo docs root starts at most one git process for the whole audit."""

    monkeypatch.setattr("reckon.serve._GIT_OUTSIDE_MEMO", {})
    docs = _build_tree(tmp_path / "loose")
    assert not (tmp_path / "loose" / ".git").exists()

    with _counting_git_spawns(monkeypatch) as spawns:
        _run_audit(docs)
        count = spawns()

    assert count <= 1, f"the no-repository audit started {count} git processes"


def test_no_repository_root_answers_no_commit_without_spawning(tmp_path, monkeypatch):
    """A remembered outside root answers HEAD with no commit and no process."""

    monkeypatch.setattr("reckon.serve._GIT_OUTSIDE_MEMO", {})
    docs = _build_tree(tmp_path / "loose")
    repo_dir = docs.parent

    assert _git_head(repo_dir) is None

    with _counting_git_spawns(monkeypatch) as spawns:
        assert _git_head(repo_dir) is None
        assert _git_head(repo_dir) is None
        assert spawns() == 0, "a remembered outside root must not spawn git again"


def test_repository_root_ages_are_commit_derived(tmp_path, monkeypatch):
    """A docs root inside a repository still reads commit-derived ages."""

    monkeypatch.setattr("reckon.serve._GIT_OUTSIDE_MEMO", {})
    repo = tmp_path / "checkout"
    docs = _build_tree(repo)

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "probe@example.invalid")
    _git(repo, "config", "user.name", "Probe")
    _git(repo, "add", "docs")
    stamp = f"{int(COMMIT_TS)} +0000"
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        "add docs",
        env={**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
    )

    docs = docs.resolve()
    plan = docs / "plans" / "p00.html"
    age_days, source = derived_plan_age(
        "2026-01-05",
        fallback_path=plan,
        repo_dir=docs.parent,
        docs_dir=docs,
        today=TODAY,
    )
    committed_day = datetime.fromtimestamp(COMMIT_TS, tz=UTC).date()
    assert source == "commit"
    assert age_days == (TODAY - committed_day).days
