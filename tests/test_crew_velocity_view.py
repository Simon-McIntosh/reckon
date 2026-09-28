"""The velocity view is served through the crew surface over caller-named checkouts.

The view measures git histories and a committed run ledger, so the test
synthesises two repositories under ``tmp_path`` and points the view at them
through the MCP function — one project named directly, or ``"*"`` for every
mounted checkout. Nothing outside ``tmp_path`` is read: the window's promotions
all carry a committed ledger record, so the fallback run store is never opened,
and the view's transcript root defaults to unset.

Every expectation is derived from the fixture the code sees. The window end is
load-bearing: a commit dated after ``until`` sits past the derived head and must
be excluded, and the declaration's negative control makes the view ignore
``until`` and measure to the current time, at which point that commit is in the
past and the exclusion assertion fails.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import mcp, velocity

DAY = 86400
BASE = int(dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp())
WINDOW_START = velocity.iso(BASE + 1 * DAY)
WINDOW_END = velocity.iso(BASE + 20 * DAY)
LATE_END = velocity.iso(BASE + 30 * DAY)
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


def _commit(repo: Path, message: str, day: int, changes: dict[str, str | None]) -> str:
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
    # Dated after the window closes; the view derives its head before ``until``,
    # so this commit is unreachable when the window ends at day 20.
    _commit(repo, "feat: late source", 25, {"src/late.py": "".join(
        f"x{i}\n" for i in range(7)
    )})
    return repo


def _build_beta(root: Path) -> Path:
    repo = root / "beta"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", BRANCH)
    _seed(repo, "beta", [_ledger_row("r-impl2", "impl-node-2", "implement", "clive", 4)])
    _commit(repo, "feat: beta source", 1, {"src/b.py": "m1\nm2\nm3\nm4\n"})
    _commit(repo, "promote(r-impl2)", 5, {})
    return repo


@pytest.fixture()
def fleet(tmp_path: Path, isolated_reckon_home: Path) -> dict[str, Path]:
    root = tmp_path / "code"
    alpha, beta = _build_alpha(root), _build_beta(root)
    # The mounted set is what ``project="*"`` resolves through.
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


def test_promotions_split_and_review_share_are_hand_computable(fleet):
    result = mcp._crew("*", view="velocity", since=WINDOW_START, until=WINDOW_END)

    assert result["ok"] is True
    raised = result["total"]["promoted_nodes"]
    # Two implementation-class promotions (alpha, beta) and one review, so the
    # review share is exactly one third.
    assert raised["denominator"] == 3
    assert raised["implement_class"] == 2
    assert raised["review_investigate"] == 1
    assert raised["review_share"]["value"] == pytest.approx(1 / 3)


def test_a_project_lane_day_cell_carries_its_counts(fleet):
    result = mcp._crew("*", view="velocity", since=WINDOW_START, until=WINDOW_END)

    cells = {
        (cell["project"], cell["day"], cell["lane"]): cell["metrics"]
        for cell in result["by_project_day_lane"]
    }
    promotion_day = velocity.iso(BASE + 5 * DAY)[:10]
    cell = cells[("alpha", promotion_day, "claude")]
    assert cell["promoted_nodes"]["denominator"] == 1
    assert cell["promoted_nodes"]["implement_class"] == 1
    assert cell["promoted_nodes"]["review_investigate"] == 0
    assert cell["promoted_nodes"]["review_share"]["value"] == 0.0
    # And the review lane lands in its own project-lane-day cell.
    review_day = velocity.iso(BASE + 6 * DAY)[:10]
    review_cell = cells[("alpha", review_day, "codex")]
    assert review_cell["promoted_nodes"]["review_investigate"] == 1


def test_the_view_refuses_a_missing_since_by_name(fleet):
    result = mcp._crew("alpha", view="velocity", checkout_path=str(fleet["alpha"]))

    assert result["ok"] is False
    assert result["error"] == "crew_error"
    assert "since" in result["detail"]

    unparseable = mcp._crew(
        "alpha",
        view="velocity",
        since="not-a-clock",
        checkout_path=str(fleet["alpha"]),
    )
    assert unparseable["ok"] is False
    assert "since" in unparseable["detail"]


def test_a_commit_dated_after_until_is_excluded(fleet):
    windowed = mcp._crew(
        "*", view="velocity", since=WINDOW_START, until=WINDOW_END
    )
    extended = mcp._crew("*", view="velocity", since=WINDOW_START, until=LATE_END)

    def source_added(payload: dict) -> int:
        return payload["total"]["lines"]["source"]["added"]

    # alpha contributes 5 source lines before the window closes; the 7 late
    # lines sit past ``until`` and are excluded until the window is widened.
    assert source_added(windowed) == 9
    assert source_added(extended) == 16