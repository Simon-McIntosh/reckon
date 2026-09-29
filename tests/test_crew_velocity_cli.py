"""The velocity view is served on the command line through the same composition.

``crew velocity`` in ``reckon/cli.py`` and ``crew(view="velocity")`` in
``reckon/mcp.py`` both call ``reckon.velocity.view``, so for the same window
and fields they must answer the same payload. This test drives the CLI with a
synthesised fleet under ``tmp_path`` and asserts the two surfaces agree — over
one default call and one project-lane-day cells page — and that a missing
``--since`` is refused by name.

The repositories are synthesised under ``tmp_path`` exactly as
``tests/test_crew_velocity_view.py`` synthesises them, and nothing outside
``tmp_path`` is read: every promotion in the window carries a committed ledger
record, so the fallback run store is never opened, and the view's transcript
root defaults to unset.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import mcp, velocity
from reckon.cli import main

DAY = 86400
BASE = int(dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp())
WINDOW_START = velocity.iso(BASE + 1 * DAY)
WINDOW_END = velocity.iso(BASE + 20 * DAY)
BRANCH = "main"

_DISPATCH_IDENTITY = ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT")


def _iso(day: int, seconds: int = 0) -> str:
    return velocity.iso(BASE + day * DAY + seconds)


def _git(repo: Path, *arguments: str, env: dict | None = None) -> str:
    # A git wrapper keyed on the running worker's identity refuses a mutating
    # verb outside its own worktree; drop that identity so the synthesised
    # repositories are the only ones in play.
    base = {**os.environ, **(env or {})}
    for name in _DISPATCH_IDENTITY:
        base.pop(name, None)
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=base,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str, day: int, changes: dict[str, ...]) -> str:
    when = _iso(day)
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    for path, content in changes.items():
        target = repo / path
        if content is None:
            _git(repo, "rm", "-q", path, env=env)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            _git(repo, "add", path, env=env)
    _git(repo, "commit", "-q", "--allow-empty", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD", env=env)


def _ledger_row(run_id: str, node: str, role: str, backend: str, day: int) -> dict:
    return {
        "run_id": run_id,
        "node": node,
        "plan": "p",
        "role": role,
        "gate": "passed",
        "backend": backend,
        "dispatched_at": _iso(day),
        "completed_at": _iso(day, 100),
        "worker_seconds": 100,
        "lineage": {},
        "attempt": 1,
    }


def _seed(repo: Path, project: str, runs: list[dict]) -> None:
    _commit(
        repo,
        "chore: seed the ledger",
        0,
        {
            f"docs/state/{project}/crew.json": json.dumps(
                {"data": {"runs": runs}}, indent=2
            )
        },
    )


def _build_alpha(root: Path) -> Path:
    repo = root / "alpha"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", BRANCH)
    _seed(
        repo,
        "alpha",
        [
            _ledger_row("r-impl", "impl-node", "implement", "claude", 4),
            _ledger_row("r-review", "review-node", "review", "codex", 4),
        ],
    )
    _commit(repo, "feat: add source", 1, {"src/a.py": "l1\nl2\nl3\nl4\nl5\n"})
    _commit(repo, "test: add tests", 2, {"tests/test_a.py": "t1\nt2\nt3\n"})
    _commit(
        repo,
        "docs(plan): add a plan",
        3,
        {"docs/plans/p.html": "<p>a</p>\n<p>b</p>\n<p>c</p>\n"},
    )
    _commit(repo, "promote(r-impl)", 5, {})
    _commit(repo, "promote(r-review)", 6, {})
    return repo


def _build_beta(root: Path) -> Path:
    repo = root / "beta"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", BRANCH)
    _seed(
        repo, "beta", [_ledger_row("r-impl2", "impl-node-2", "implement", "clive", 4)]
    )
    _commit(repo, "feat: beta source", 1, {"src/b.py": "m1\nm2\nm3\nm4\n"})
    _commit(repo, "promote(r-impl2)", 5, {})
    return repo


@pytest.fixture()
def fleet(tmp_path: Path, isolated_reckon_home: Path) -> dict[str, Path]:
    root = tmp_path / "code"
    alpha, beta = _build_alpha(root), _build_beta(root)
    # The mounted set is what ``--project *`` resolves through.
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps(
            {
                "alpha": str(alpha / "docs"),
                "beta": str(beta / "docs"),
            }
        ),
        encoding="utf-8",
    )
    return {"alpha": alpha, "beta": beta}


def _cli(*arguments: str) -> dict:
    result = CliRunner().invoke(main, ["crew", "velocity", *arguments])
    assert result.exit_code == 0, result.output + (result.stderr or "")
    # The JSON is the whole of stdout: the census's progress goes to stderr, so
    # a caller parses the answer without filtering the library's narration out.
    return json.loads(result.stdout)


def test_the_default_call_agrees_with_the_read_view(fleet):
    served = mcp._crew("*", view="velocity", since=WINDOW_START, until=WINDOW_END)
    emitted = _cli("--project", "*", "--since", WINDOW_START, "--until", WINDOW_END)

    assert emitted == served
    # And the default really is the aggregate tables, not an empty payload.
    assert emitted["by_project_day_lane_count"] > 0
    assert "pagination" not in emitted


def test_a_cells_page_agrees_with_the_read_view(fleet):
    # The first page, then a page reached by cursor: the cursor page is the one
    # that shows the two surfaces agree about where the caller asked to be.
    first = mcp._crew(
        "*",
        view="velocity",
        since=WINDOW_START,
        until=WINDOW_END,
        fields=["by_project_day_lane"],
        limit=1,
    )
    emitted_first = _cli(
        "--project",
        "*",
        "--since",
        WINDOW_START,
        "--until",
        WINDOW_END,
        "--fields",
        "by_project_day_lane",
        "--limit",
        "1",
    )
    assert emitted_first == first
    assert len(emitted_first["by_project_day_lane"]) == 1

    cursor = first["pagination"]["next_cursor"]
    assert cursor
    second = mcp._crew(
        "*",
        view="velocity",
        since=WINDOW_START,
        until=WINDOW_END,
        fields=["by_project_day_lane"],
        limit=1,
        cursor=cursor,
    )
    emitted_second = _cli(
        "--project",
        "*",
        "--since",
        WINDOW_START,
        "--until",
        WINDOW_END,
        "--fields",
        "by_project_day_lane",
        "--limit",
        "1",
        "--cursor",
        cursor,
    )
    assert emitted_second == second
    assert emitted_second["by_project_day_lane"] != first["by_project_day_lane"]


def test_the_cli_refuses_a_missing_since_by_name(fleet):
    result = CliRunner().invoke(main, ["crew", "velocity", "--project", "alpha"])

    assert result.exit_code != 0
    assert "since" in (result.output + (result.stderr or ""))
