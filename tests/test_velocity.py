"""The velocity view measures a synthesised fleet by hand-computable fixtures.

The census this module ports reads five live repositories and a committed run
ledger, so parity against the review's pinned window is a recorded measurement
rather than a test: a test must not read or write state outside the repository
under test. What this module verifies here is the arithmetic on a repository it
builds itself under ``tmp_path`` — a git history, the run ledger committed at
its head, and the promote commits that date each promotion.

Every expectation is derived from the fixture the code sees rather than echoed
from the code's own output. The seven-day share is exercised with a deliberate
censoring case: an addition landed less than seven days before the window
closed is right censored and must be excluded from the share's denominator, and
the same addition is later deleted. If the censoring cut is dropped the share
moves from 2/9 to 12/19, so the censoring test is load-bearing rather than
decorative.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import velocity

DAY = 86400
BASE = int(dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp())
WINDOW_START = velocity.iso(BASE + 1 * DAY)
WINDOW_END = velocity.iso(BASE + 20 * DAY)
PROJECT = "sample"
BRANCH = "main"


def _iso(day: int, seconds: int = 0) -> str:
    return velocity.iso(BASE + day * DAY + seconds)


_DISPATCH_IDENTITY = ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT")


def _git(repo: Path, *arguments: str, env: dict | None = None) -> str:
    # The worker that runs this suite exports the identity of its own run, and a
    # git wrapper keyed on that identity refuses a mutating verb outside the
    # worker's worktree. Point the subprocess at the synthesised repository by
    # dropping the inherited identity, whatever fixture scope happens to run.
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


def _build_repository(root: Path) -> Path:
    repo = root / PROJECT
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", BRANCH)

    ledger = {
        "data": {
            "runs": [
                {
                    "run_id": "r-impl",
                    "node": "impl-node",
                    "plan": "p",
                    "role": "implement",
                    "gate": "passed",
                    "backend": "claude",
                    "dispatched_at": _iso(6),
                    "completed_at": _iso(6, 100),
                    "worker_seconds": 100,
                    "lineage": {},
                    "attempt": 1,
                },
                {
                    "run_id": "r-review",
                    "node": "review-node",
                    "plan": "p",
                    "role": "review",
                    "gate": "passed",
                    "backend": "codex",
                    "dispatched_at": _iso(8),
                    "completed_at": _iso(8, 300),
                    "worker_seconds": 300,
                    "lineage": {},
                    "attempt": 1,
                },
            ]
        }
    }
    # The ledger is committed before the window opens so no window commit lands
    # a docs/state line that would otherwise be confused with the classification
    # fixture below.
    _commit(
        repo,
        "chore: seed the ledger",
        0,
        {"docs/state/sample/crew.json": json.dumps(ledger, indent=2)},
    )
    _commit(repo, "feat: add source", 1, {"src/a.py": "l1\nl2\nl3\nl4\nl5\n"})
    _commit(repo, "test: add tests", 2, {"tests/test_a.py": "t1\nt2\nt3\nt4\n"})
    _commit(
        repo,
        "docs(plan): add a plan",
        3,
        {"docs/plans/p.html": "<p>a</p>\n<p>b</p>\n<p>c</p>\n"},
    )
    _commit(repo, "docs: add a figure", 4, {"docs/figures/f.png": "p1\np2\n"})
    _commit(
        repo,
        "docs: add state",
        5,
        {"docs/state/sample/notes.json": '{\n  "a": 1\n}\n'},
    )
    _commit(repo, "docs: add prose", 6, {"docs/notes.md": "n1\nn2\n"})
    _commit(repo, "promote(r-impl)", 7, {})
    _commit(repo, "fix: drop two lines", 8, {"src/a.py": "l1\nl2\nl3\n"})
    _commit(repo, "promote(r-review)", 9, {})
    # Right censored: born less than seven days before the window closes.
    _commit(
        repo,
        "feat: add late source",
        18,
        {"src/late.py": "".join(f"x{i}\n" for i in range(10))},
    )
    _commit(repo, "fix: delete late source", 19, {"src/late.py": None})
    return repo


@pytest.fixture()
def totals(tmp_path: Path) -> dict:
    root = tmp_path / "code"
    _build_repository(root)
    snapshot = velocity.capture(
        {PROJECT: BRANCH},
        start=WINDOW_START,
        end=WINDOW_END,
        code_root=root,
        run_store_db=None,
    )
    return velocity.measure(
        snapshot,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        projects={PROJECT: BRANCH},
    )["total"]


def test_classifier_is_imported_not_copied():
    assert velocity.path_class.__module__ == "reckon.path_classes"
    assert velocity.file_class.__module__ == "reckon.path_classes"


def test_promotions_split_implementation_against_review(totals: dict):
    promoted = totals["promoted_nodes"]
    assert promoted["denominator"] == 2
    assert promoted["implement_class"] == 1
    assert promoted["review_investigate"] == 1


def test_lines_land_in_their_six_classes(totals: dict):
    lines = totals["lines"]
    assert set(lines) == set(velocity.CLASSES)
    assert {key: lines[key]["added"] for key in velocity.CLASSES} == {
        "source": 15,
        "tests": 4,
        "plan_evidence_research_html": 3,
        "figures": 2,
        "docs_state": 3,
        "other": 2,
    }


def test_seven_day_deletion_share_censors_additions_without_full_followup(
    totals: dict,
):
    share = totals["product_deleted_within_seven_days"]
    # A mature commit adds five source lines and a later commit deletes two of
    # them within seven days. A second addition of ten lines is deleted within
    # a day of birth but has no full seven days of follow-up before the window
    # closed, so it is right censored: it enters neither the numerator nor the
    # denominator. Dropping that cut moves the share to 12/19.
    assert totals["mature_product_additions"] == 9
    assert totals["right_censored_product_additions"] == 10
    assert share["numerator"] == 2
    assert share["denominator"] == 9
    assert share["value"] == pytest.approx(2 / 9)


def test_dispatch_to_completion_reports_median_and_p75(totals: dict):
    distribution = totals["dispatch_to_completion_seconds"]
    # Two runs completed after 100 s and 300 s. The linearly interpolated p75
    # sits three quarters of the way between them.
    assert distribution["denominator"] == 2
    assert distribution["population"] == 2
    assert distribution["median"] == pytest.approx(200.0)
    assert distribution["p75"] == pytest.approx(250.0)
