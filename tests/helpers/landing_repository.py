"""A temporary repository and the driver a landing test runs ``crew complete`` against.

Several landing tests need the same three things: a bare origin and a checkout
kept apart from the suite's own tree, a run whose manifest and gate log are
already written, and a ``crew complete --land`` invocation that lands the run's
head. They live here rather than in one test module so a second test builds the
same fixture without copying it.

The same rule covers the checkout a departure test promotes into and the ledger
row a landed promotion leaves behind: a run's departure word reads the row's
head against that checkout's primary branch, so every test that words a
departure needs both, and two copies of the git setup would have to be changed
twice.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from reckon import _plan_html, ledger
from reckon import cli as cli_module
from tests.helpers.git import git as _git

PROJECT = "proj"
PLAN = "plan-a"


def landed_checkout(
    home: Path, tmp_path: Path, *, project: str = PROJECT
) -> tuple[Path, str]:
    """A checkout whose project state resolves to the temporary home's own.

    A promoted run's ledger row names the checkout it lives in, and the row is
    where a departure reader takes the row's delivered head from. The config
    home's state directory is the store the fixture writes, so the checkout
    carries it as the symlink a mounted project carries in the real system: one
    row, reached by the discovery path and by the checkout's own path alike. The
    returned head is the commit the branch carries, so a row naming it is a
    landing that has already reached primary.
    """
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    project_state = home / "state" / project
    project_state.mkdir(parents=True)
    (project_state / "index.json").write_text("{}\n", encoding="utf-8")
    (repo / "docs" / "state").symlink_to(home / "state")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "chore: seed the checkout")
    return repo, _git(repo, "rev-parse", "HEAD")


def record_promoted_row(
    run_id: str, repo: Path, head: str, *, project: str = PROJECT
) -> None:
    """Write the per-run ledger file a landed promotion leaves behind.

    The row names the checkout the run was promoted into and the head its
    landing delivered. That head is on the checkout's primary branch, which is
    what a departure reader asks before it words the run promoted: a row whose
    head no branch carries is a landing still owed, and reads delivered instead.
    """
    path = ledger.run_path(project, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"run_id": run_id, "repo": str(repo), "commits": [head]})
    )


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
            "--wait",
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
