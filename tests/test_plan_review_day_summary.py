"""The per-day review summary folds the committed records into a plan report.

The summary answers a look-back question — what a day of plan reviewing cost,
and what the per-section rule would have spared — from the committed records
rather than from any scratch tree. It is a fold over
:func:`reckon.crew.plan_review.list_plan_reviews`, so it reads a decline that
exists only in the committed archive, it resolves each review's authored bytes
from the repository object store or, failing that, the snapshot the review
composed for, and it lists a review it cannot measure rather than counting a
missing comparison as no change. The rubric split separates design reviews from
content reviews; the change behind each re-review is measured with the module's
own prose reader and edit-share rule, so the summary and the coverage gate
cannot drift.

Every fixture is synthesised: a temporary repository, a temporary config home,
and committed review records under the repository's own ``docs/state`` tree.
No real store is read or written.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import flight, mcp
from reckon.crew import plan_review

PROJECT = "day-summary"
SLUG = "demo"
OTHER_SLUG = "extra"
DEFAULT_THRESHOLD = 0.30

RUN_A = "r-20261005T100000000000-plan-review-of-demo"
RUN_B = "r-20261006T020000000000-plan-review-of-demo"
RUN_C = "r-20261006T030000000000-plan-review-of-demo"
RUN_D = "r-20261006T040000000000-plan-review-of-demo"
RUN_P1 = "r-20261005T110000000000-plan-review-of-extra"
RUN_P2 = "r-20261006T050000000000-plan-review-of-extra"
RUN_P3 = "r-20261006T060000000000-plan-review-of-extra"


def _review_config(threshold: float = DEFAULT_THRESHOLD) -> dict:
    return {"review": {"plan_change_threshold": threshold}}


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A synthesised checkout, config home and mounts entry, isolated from the real ones."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repo / "docs")}))
    config = _review_config()
    monkeypatch.setattr(flight, "resolve", lambda **kw: SimpleNamespace(config=config))
    return SimpleNamespace(home=home, repo=repo, config=config)


def _render(sections: dict[str, str]) -> str:
    body = "".join(
        f'<h2 id="{identity}">{identity}</h2><p>{text}</p>'
        for identity, text in sections.items()
    )
    return (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{SLUG}">'
        "</head><body>" + body + "</body></html>"
    )


def _blob(repo: Path, document: str) -> str:
    result = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        input=document.encode("utf-8"),
        cwd=repo,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode("ascii").strip()


def _reviews_root(repo: Path) -> Path:
    return repo / "docs" / "state" / PROJECT / "reviews" / "plan"


def _commit_record(repo: Path, slug: str, record: dict) -> None:
    directory = _reviews_root(repo) / slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{record['review_run_id']}.json").write_text(
        json.dumps(record), encoding="utf-8"
    )


def _record(
    slug: str,
    run_id: str,
    *,
    rubric: str = "content",
    blob: str | None = None,
    dispatched: str | None = None,
    findings: list | None = None,
    responses: dict | None = None,
) -> dict:
    record = {
        "project": PROJECT,
        "plan_slug": slug,
        "plan_version": 1,
        "review_run_id": run_id,
        "rubric": rubric,
        "findings": findings or [],
        "responses": responses or {},
        "status": "ready",
        "timestamp": "2026-10-06T00:00:00+00:00",
    }
    if blob is not None:
        record["reviewed_blob_sha"] = blob
    # A record with no stamp resolves its dispatch instant from the run id.
    if dispatched is not None:
        record["dispatched_at"] = dispatched
    return record


def _snapshot(home: Path, slug: str, run_id: str, document: str) -> None:
    directory = home / "crew" / "reports" / PROJECT / "plan-review" / slug / run_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plan.html").write_text(document, encoding="utf-8")


# The authored prose is chosen so a re-review's change is a known share of its
# section's own words: ``s1`` shares four of eight words with ``s1_edit`` (a
# half), and ``s2`` shares three of four with ``s2_edit`` (a quarter, below the
# configured threshold).
_S1 = "alpha beta gamma delta epsilon zeta eta theta"
_S1_EDIT = "alpha beta gamma delta iota kappa lambda mu"
_S2 = "nu xi omicron pi"
_S2_EDIT = "nu xi omicron rho"
_P1 = "one two three four five six seven eight"


@pytest.fixture()
def committed(project):
    """The committed records the summary folds.

    Plan ``demo`` carries a design review before the window, then three reviews
    inside it: a re-review with a new section, a re-review whose section changed
    by half, and a re-review changed below the threshold. Plan ``extra`` carries
    a re-review measured from its snapshot when the object is missing, and a
    review with neither bytes nor snapshot.
    """
    repo = project.repo
    home = project.home
    blob_a = _blob(repo, _render({"s1": _S1}))
    blob_b = _blob(repo, _render({"s1": _S1, "s2": _S2}))
    blob_c = _blob(repo, _render({"s1": _S1_EDIT, "s2": _S2}))
    blob_d = _blob(repo, _render({"s1": _S1_EDIT, "s2": _S2_EDIT}))
    blob_p1 = _blob(repo, _render({"s1": _P1}))

    _commit_record(
        repo,
        SLUG,
        _record(
            SLUG,
            RUN_A,
            rubric="design",
            blob=blob_a,
            findings=[{"id": "f1", "type": "duplicate_owner", "text": "One owner."}],
            responses={
                "f1": {
                    "action": "declined",
                    "reason": "the owner is deliberately shared",
                    "by": "author",
                    "when": "2026-10-05T10:05:00+00:00",
                }
            },
        ),
    )
    _commit_record(repo, SLUG, _record(SLUG, RUN_B, blob=blob_b))
    _commit_record(repo, SLUG, _record(SLUG, RUN_C, rubric="design", blob=blob_c))
    _commit_record(repo, SLUG, _record(SLUG, RUN_D, blob=blob_d))

    _commit_record(repo, OTHER_SLUG, _record(OTHER_SLUG, RUN_P1, blob=blob_p1))
    # The object is missing, so the re-review is measured from its snapshot.
    _commit_record(repo, OTHER_SLUG, _record(OTHER_SLUG, RUN_P2, blob="0" * 40))
    _snapshot(home, OTHER_SLUG, RUN_P2, _render({"s1": _P1}))
    # Neither the bytes nor a snapshot resolve, so this review is unmeasured.
    _commit_record(repo, OTHER_SLUG, _record(OTHER_SLUG, RUN_P3, blob="1" * 40))
    return project


def _plan(summary: dict, slug: str) -> dict:
    return next(entry for entry in summary["plans"] if entry["plan_slug"] == slug)


def _window(project, threshold: float) -> dict:
    project.config["review"]["plan_change_threshold"] = threshold
    return plan_review.review_day_summary(
        PROJECT, since="2026-10-06", until="2026-10-07"
    )


def test_reviews_in_the_window_are_counted_by_rubric(committed) -> None:
    summary = _window(committed, DEFAULT_THRESHOLD)
    demo = _plan(summary, SLUG)
    # The design review of 5 October is outside the window; the three reviews of
    # 6 October are inside it, two content reviews and one design review.
    assert demo["reviews_run"] == 3
    assert demo["by_rubric"] == {"design": 1, "content": 2}
    assert [review["review_run_id"] for review in demo["reviews"]] == [
        RUN_B,
        RUN_C,
        RUN_D,
    ]
    assert summary["threshold"] == pytest.approx(DEFAULT_THRESHOLD)


def test_a_review_before_the_window_is_still_the_predecessor(committed) -> None:
    demo = _plan(_window(committed, DEFAULT_THRESHOLD), SLUG)
    first = demo["reviews"][0]
    # RUN_A was dispatched on 5 October, before the window, yet it is the review
    # RUN_B is compared with: a re-review is measured against any earlier review
    # of the plan, at any time.
    assert first["re_review"] is True
    assert first["previous_run_id"] == RUN_A


def test_a_new_section_fires_and_the_change_share_measures_the_rest(committed) -> None:
    demo = _plan(_window(committed, DEFAULT_THRESHOLD), SLUG)
    with_new, changed, below = demo["reviews"]
    assert with_new["new_sections"] == ["s2"]
    assert with_new["fires"] is True
    assert with_new["measured"] is True
    # RUN_C changed four of s1's nine words and nothing else, so the largest
    # section change is that share and the rule fires on the change alone.
    assert changed["new_sections"] == []
    assert changed["max_section_change"] == pytest.approx(4 / 9, abs=0.01)
    assert changed["fires"] is True
    # RUN_D changed one of s2's five words and added nothing, under the threshold.
    assert below["new_sections"] == []
    assert below["max_section_change"] == pytest.approx(1 / 5, abs=0.01)
    assert below["fires"] is False


def test_the_rule_reads_the_threshold_from_the_config(committed) -> None:
    # The same change fires under the shipped threshold and not under a higher
    # one, so the boundary is the project's configured threshold, not a literal.
    fires_low = _plan(_window(committed, 0.30), SLUG)["reviews"][1]
    fires_high = _plan(_window(committed, 0.60), SLUG)["reviews"][1]
    assert fires_low["fires"] is True
    assert fires_high["fires"] is False
    assert fires_low["max_section_change"] == fires_high["max_section_change"]


def test_totals_count_re_reviews_new_sections_firings_and_unmeasured(
    committed,
) -> None:
    summary = _window(committed, DEFAULT_THRESHOLD)
    assert summary["totals"] == {
        "re_reviews": 5,
        "re_reviews_with_new_section": 1,
        "firings": 2,
        "unmeasured": 1,
    }
    demo = _plan(summary, SLUG)
    assert demo["totals"] == {
        "re_reviews": 3,
        "re_reviews_with_new_section": 1,
        "firings": 2,
        "unmeasured": 0,
    }


def test_a_missing_object_is_measured_from_the_snapshot(committed) -> None:
    other = _plan(_window(committed, DEFAULT_THRESHOLD), OTHER_SLUG)
    measured, _unmeasured = other["reviews"]
    # RUN_P2's reviewed object is missing, so its change is measured from the
    # snapshot the review composed for rather than read as no change.
    assert measured["review_run_id"] == RUN_P2
    assert measured["measured"] is True
    assert measured["max_section_change"] == pytest.approx(0.0)
    assert measured["fires"] is False


def test_a_review_with_neither_bytes_nor_snapshot_is_unmeasured(committed) -> None:
    other = _plan(_window(committed, DEFAULT_THRESHOLD), OTHER_SLUG)
    _measured, unmeasured = other["reviews"]
    # RUN_P3's object is missing and it left no snapshot, so it is listed as
    # unmeasured with no change share and no firing verdict — never counted as a
    # zero change that happens not to fire.
    assert unmeasured["review_run_id"] == RUN_P3
    assert unmeasured["measured"] is False
    assert unmeasured["fires"] is None
    assert unmeasured["max_section_change"] is None
    assert unmeasured["new_sections"] == []
    assert other["totals"]["unmeasured"] == 1


def test_a_named_plan_narrows_the_summary(committed) -> None:
    summary = plan_review.review_day_summary(
        PROJECT, since="2026-10-06", until="2026-10-07", plan=SLUG
    )
    assert [entry["plan_slug"] for entry in summary["plans"]] == [SLUG]
    assert summary["totals"]["re_reviews"] == 3


def test_the_view_summarises_the_day_when_since_is_passed(committed) -> None:
    payload = mcp._crew(
        project=PROJECT, view="plan-review", since="2026-10-06", until="2026-10-07"
    )
    assert set(payload) == {"day_summary"}
    summary = payload["day_summary"]
    assert summary["project"] == PROJECT
    assert summary["totals"]["re_reviews"] == 5
    # With ``since`` present ``plan`` is optional, and it narrows the fold.
    narrowed = mcp._crew(
        project=PROJECT,
        view="plan-review",
        since="2026-10-06",
        until="2026-10-07",
        plan=SLUG,
    )
    assert [entry["plan_slug"] for entry in narrowed["day_summary"]["plans"]] == [SLUG]


def test_the_flat_view_still_requires_a_plan(committed) -> None:
    # Without ``since`` the existing shape and the existing refusal are
    # unchanged, so no current caller is affected by the new branch.
    assert mcp._crew(project=PROJECT, view="plan-review")["error"] == "missing_plan"
    payload = mcp._crew(project=PROJECT, view="plan-review", since="not-a-clock")
    assert payload["error"] == "crew_error"


def test_the_decline_is_counted_from_the_committed_archive(committed) -> None:
    # No staging store exists under the config home: the declined finding lives
    # only in the committed tree, so counting it proves the fold reads the
    # archive rather than an in-memory or staging copy.
    staging = committed.home / "crew" / "reviews" / PROJECT
    assert not staging.exists()
    records = plan_review.list_plan_reviews(PROJECT)
    assert records
    assert all("/reviews/plan/" in record["review_path"] for record in records)
    recurrence = plan_review.declined_recurrence()
    assert recurrence["duplicate_owner"]["plan_count"] == 1
    assert recurrence["duplicate_owner"]["surfaced"] is False
