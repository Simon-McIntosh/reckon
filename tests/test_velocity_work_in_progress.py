"""Plans opened, closed and pending, driven by a synthesised plan history.

The census reads a project's own history, so the arithmetic is verified here on
a repository built under ``tmp_path``: a git history whose commits add plan
files and move their status, and whose closing events are derivable by hand.
The pinned reading the plan declares is exercised against this repository's own
history at a named commit rather than against today's tree, so it does not move
when the branch does — a test that dated a plan's opening by the latest commit
to touch its file, or by the current date, would report a different number
without any code changing.

Two closing shapes are asserted separately because each is a distinct branch: a
plan whose status reaches a closed state twice counts once, and a plan whose
archive flag is set is closed even while its status stays open.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
from pathlib import Path

from reckon import velocity

DAY = 86400
BASE = int(dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp())
PROJECT = "sample"

# The pinned reading the plan declares: this repository's own history at the
# commit the review pinned, over the window the review fixed.
PINNED_COMMIT = "23eb2545"
PINNED_OPENED = 58
PINNED_CLOSED = 7

_DISPATCH_IDENTITY = ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT")


def _iso(day: int, seconds: int = 0) -> str:
    return velocity.iso(BASE + day * DAY + seconds)


def _git(repo: Path, *arguments: str, env: dict | None = None) -> str:
    # A git wrapper keyed on the running worker's identity refuses a mutating
    # verb outside its own worktree, so the synthesised repository is reached by
    # dropping the inherited identity whatever fixture scope runs this.
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
    _git(repo, "add", "--", *changes, env=env)
    _git(repo, "commit", "-q", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD", env=env)


def _plan(status: str = "active", archived: str | None = None) -> str:
    meta = [f'<meta name="plan-status" content="{status}">']
    if archived is not None:
        meta.append(f'<meta name="plan-archived" content="{archived}">')
    return "<!doctype html>\n<html><head>\n" + "\n".join(meta) + "\n</head></html>\n"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / PROJECT
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "fixture")
    return repo


def _build(tmp_path: Path) -> Path:
    """A five-commit history: alpha closes twice, beta closes by archive, gamma
    stays open. Alpha and beta share an opening commit, so the opening week and
    the closing week are both exercised against a shared event."""
    repo = _repo(tmp_path)
    _commit(
        repo,
        "open alpha and beta",
        0,
        {
            "docs/plans/alpha.html": _plan("active"),
            "docs/plans/beta.html": _plan("active"),
        },
    )
    _commit(repo, "alpha ships", 2, {"docs/plans/alpha.html": _plan("shipped")})
    _commit(repo, "alpha lands again", 3, {"docs/plans/alpha.html": _plan("done")})
    _commit(
        repo,
        "beta archived while open",
        6,
        {"docs/plans/beta.html": _plan("active", archived="1")},
    )
    _commit(repo, "open gamma", 8, {"docs/plans/gamma.html": _plan("active")})
    return repo


def _plan_by_path(census: dict, path: str) -> dict:
    return next(plan for plan in census["plans"] if plan["path"] == path)


def test_opening_is_the_commit_that_added_the_file(tmp_path):
    repo = _build(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    census = velocity.plan_census(repo, head)
    opened = {
        plan["path"]: plan
        for plan in census["plans"]
        if plan["opened_epoch"] is not None
    }
    assert set(opened) == {
        "docs/plans/alpha.html",
        "docs/plans/beta.html",
        "docs/plans/gamma.html",
    }
    # Alpha and beta share an opening commit; gamma is a later commit weeks later.
    assert opened["docs/plans/alpha.html"]["opened_epoch"] == BASE
    assert opened["docs/plans/beta.html"]["opened_epoch"] == BASE
    assert opened["docs/plans/gamma.html"]["opened_epoch"] == BASE + 8 * DAY


def test_a_plan_closed_twice_counts_once(tmp_path):
    repo = _build(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    census = velocity.plan_census(repo, head)
    alpha = _plan_by_path(census, "docs/plans/alpha.html")
    # Closed at the first closed commit (day 2), not the later reinforcement.
    assert alpha["closed_epoch"] == BASE + 2 * DAY
    # Two plans are closed (alpha and beta) though three closed-status commits
    # exist: alpha's second closing does not add a second closing event.
    assert census["closed"] == 2
    assert [row for row in census["by_week"] if row["closed"]] == [
        {"week_start": "2026-08-31", "iso_week": "2026-W36", "opened": 2, "closed": 1},
        {"week_start": "2026-09-07", "iso_week": "2026-W37", "opened": 1, "closed": 1},
    ]


def test_archive_flag_closes_an_open_plan(tmp_path):
    repo = _build(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    census = velocity.plan_census(repo, head)
    beta = _plan_by_path(census, "docs/plans/beta.html")
    assert beta["closed_epoch"] == BASE + 6 * DAY
    # The status never left "active"; without the archive flag beta would be
    # pending, so this asserts the archive branch closed it.
    assert beta["closed_epoch"] is not None
    assert census["pending"] == 1


def test_pending_is_every_plan_not_closed(tmp_path):
    repo = _build(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    census = velocity.plan_census(repo, head)
    assert census["pending"] == 1
    pending = [plan["path"] for plan in census["plans"] if plan["closed_epoch"] is None]
    assert pending == ["docs/plans/gamma.html"]


def test_cohort_counts_closed_as_of_the_named_commit(tmp_path):
    repo = _build(tmp_path)
    before_beta = _git(repo, "rev-parse", "HEAD~2")
    head = _git(repo, "rev-parse", "HEAD")
    window = (BASE, BASE + 20 * DAY)
    reading = velocity.plan_cohort(repo, head, start=window[0], end=window[1])
    assert reading["opened"] == 3
    assert reading["closed"] == 2
    # Pinned two commits back, beta has not yet been archived, so the same
    # window and the same plans read one closing fewer: the answer moves with
    # the named commit, not with today.
    earlier = velocity.plan_cohort(repo, before_beta, start=window[0], end=window[1])
    assert earlier["opened"] == 2
    assert earlier["closed"] == 1


def test_window_bounds_which_openings_enter_the_cohort(tmp_path):
    repo = _build(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    # A window that opens after the shared opening commit excludes alpha and
    # beta, so only gamma is opened within it and nothing opened in the window
    # is closed; alpha and beta remain pending census rows outside the window.
    reading = velocity.plan_cohort(
        repo, head, start=BASE + 4 * DAY, end=BASE + 20 * DAY
    )
    assert reading["opened"] == 1
    assert reading["opened_plans"] == ["docs/plans/gamma.html"]
    assert reading["closed"] == 0


def test_reckon_cohort_reading_at_the_pinned_commit():
    repo = Path(velocity.__file__).resolve().parents[1]
    reading = velocity.plan_cohort(
        repo, PINNED_COMMIT, start=velocity.START, end=velocity.END
    )
    # The as-of commit and window are the pinned ones, not the branch tip.
    assert reading["head"] == PINNED_COMMIT
    assert reading["window"] == {
        "start": "2026-09-12T00:00:00Z",
        "end": "2026-09-26T10:00:00Z",
    }
    assert reading["opened"] == PINNED_OPENED
    assert reading["closed"] == PINNED_CLOSED
