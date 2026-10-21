"""A temporary repository and the driver a landing test runs ``crew complete`` against.

Several landing tests need the same three things: a bare origin and a checkout
kept apart from the suite's own tree, a run whose manifest and gate log are
already written, and a ``crew complete --land`` invocation that lands the run's
head. They live here rather than in one test module so a second test builds the
same fixture without copying it.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from reckon import _plan_html
from reckon import cli as cli_module
from tests.helpers.git import git as _git

PROJECT = "proj"
PLAN = "plan-a"


def _write_origin_and_checkout(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "commit.gpgsign", "false")
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    plan = repository / "docs" / "plans" / f"{PLAN}.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": PLAN,
                "title": "One call lands a run",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repository, "add", "docs", "seed.txt")
    _git(repository, "commit", "-q", "-m", "test: seed repository")
    _git(repository, "remote", "add", "origin", str(origin))
    _git(repository, "push", "-q", "-u", "origin", "main")
    return origin, repository


def _complete_land(run: dict, *extra: str):
    return CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "complete",
            "--run",
            run["run_id"],
            "--gate",
            "passed",
            "--commit",
            run["head"],
            "--outcome",
            "one call promoted, merged, pushed and released the run",
            "--checkout-path",
            str(run["repository"]),
            "--no-impl-change",
            "the fixture lands one file and moves no plan",
            "--gate-command",
            "true",
            "--gate-exit-status",
            "0",
            "--gate-log-path",
            str(run["gate_log"]),
            "--land",
            *extra,
        ],
    )
