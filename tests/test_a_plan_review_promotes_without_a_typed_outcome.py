"""A review's promotion composes its default outcome, without a typed one.

``crew complete --gate not-run`` with no ``--outcome`` takes the outcome from
the review the run delivered. Two reviews were refused that way, and neither
refusal was about the work.

A plan review reads a plan, keyed by version and fingerprint, so it declares no
head-keyed record path. ``_resolve_promotion_outcome`` judged every review by
``review_delivery_refusal``, whose conditions are a run review's — the head the
review read is the revision its record path spells — so a plan review with a
complete report was refused ``no head-keyed record path``, with a remedy that
does not apply to it: re-dispatching composes the same review again.

A run review reaches promotion through the admission pass, and a run whose live
pointer is gone has its record rebuilt from the run directory it left behind.
That rebuild carries neither the role nor the default node id a review is
recognised by, so ``_is_review_run`` read the run as no review at all and the
refusal was ``a non-passing gate requires --outcome`` while the run's own record
sat in the review store.

Both are exercised through the temporary home's own store and report trees.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon.crew import plan_review as plan_review_module
from reckon.crew import review as review_module
from reckon.crew.node import CrewError
from reckon.crew.promotion_checks import _resolve_promotion_outcome
from reckon.crew.runs import _write_json, pointer_path, run_dir
from tests.helpers.git import git as _git

PROJECT = "review-promotion-fixture"
PLAN = "review-promotion-target"
PLAN_NODE_ID = f"plan-review-of-{PLAN}"
COMPOSED_RUN_ID = f"r-20261021T200000000000-plan-review-of-{PLAN}"
CREW_RUN_ID = f"r-20261021T201000000000-plan-review-of-{PLAN}"
RUN_REVIEW_ID = "r-20261021T221543555992-review-of-a-reviewed-run-abcdef123456"
REVIEWED_RUN_ID = "r-20261021T204353308966-a-reviewed-run"
RUN_REVIEW_NODE_ID = f"review-of-a-reviewed-run-{'a' * 12}"
RUN_REVIEW_TOTAL = 109

# Two findings, one of which would change the plan. The grammar separates its
# fields with em dashes, so the lines are built from the separator the parser
# reads rather than from a lookalike.
_DASH = "—"
REPORT_TEXT = (
    "".join(f"RUBRIC {item}: passes\n" for item in review_module.PLAN_REVIEW_ITEMS)
    + f"FINDING wiring docs/plans/plan.html:3 {_DASH} the wiring is absent "
    f"{_DASH} WOULD_CHANGE_THE_PLAN: yes {_DASH} REASON: the section is unreachable\n"
    + f"FINDING naming docs/plans/plan.html:9 {_DASH} the label is vague "
    f"{_DASH} WOULD_CHANGE_THE_PLAN: no {_DASH} REASON: cosmetic\n"
)
# A report that is prose only: well formed, and holding no review.
PROSE_ONLY_TEXT = "This plan reads well and I have no findings to report.\n"


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    """A committed repository whose docs directory is the project's mount."""
    root = tmp_path / "repo"
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Review Promotion Target",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (root / "docs" / "state" / "proj-placeholder").mkdir(parents=True, exist_ok=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "chore: seed repository")
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}),
        encoding="utf-8",
    )
    return root


def _report_directory() -> Path:
    return plan_review_module.review_report_directory(PROJECT, PLAN, COMPOSED_RUN_ID)


def _deliver_plan_review(
    *, report_text: str = REPORT_TEXT, rubric: str = "content"
) -> Path:
    directory = _report_directory()
    directory.mkdir(parents=True, exist_ok=True)
    report_path = directory / "report.md"
    report_path.write_text(report_text, encoding="utf-8")
    _write_json(
        directory / review_module.REVIEW_DISPATCH_FILE_NAME,
        {"run_id": CREW_RUN_ID},
    )
    plan_review_module.write_review_sidecar(
        directory,
        project=PROJECT,
        plan_slug=PLAN,
        plan_version=1,
        reviewed_blob_sha="b" * 40,
        document=f"<html><body>plan {PLAN}</body></html>",
        rubric=rubric,
        report_path=report_path,
        name="plan-review.json",
    )
    return report_path


def _plan_review_record(*, report_path: Path | None = None) -> dict:
    directory = _report_directory()
    return {
        "run_id": CREW_RUN_ID,
        "project": PROJECT,
        "role": "review",
        "launch": "in-harness",
        "manifest_path": "",
        "node": {
            "id": PLAN_NODE_ID,
            "role": "review",
            "plan": PLAN,
            "write_paths": [
                str(report_path or (directory / "report.md")),
                str(directory / "plan-review.json"),
            ],
        },
    }


def _outcome(record: dict, run_id: str = CREW_RUN_ID) -> str:
    return _resolve_promotion_outcome(run_id, record, verdict="not-run")


def _refusal(record: dict, run_id: str = CREW_RUN_ID) -> str:
    with pytest.raises(CrewError) as raised:
        _outcome(record, run_id)
    return str(raised.value)


# ── A plan review's default outcome ─────────────────────────────────────────


def test_a_plan_review_composes_its_outcome_from_its_own_record(
    repository: Path,
) -> None:
    """The default names the finding count and how many would change the plan."""
    _deliver_plan_review()

    outcome = _outcome(_plan_review_record())

    assert "2 finding(s)" in outcome, outcome
    assert "1 of which would change the plan" in outcome, outcome


def test_a_plan_review_with_no_review_lines_is_refused_naming_the_report(
    repository: Path,
) -> None:
    """A report with no RUBRIC or FINDING line holds no review, however tidy."""
    report_path = _deliver_plan_review(report_text=PROSE_ONLY_TEXT)

    refusal = _refusal(_plan_review_record())

    assert str(report_path) in refusal, refusal
    assert "carries no RUBRIC or FINDING line" in refusal, refusal


def test_a_plan_review_the_store_cannot_parse_is_refused_naming_the_reason(
    repository: Path,
) -> None:
    """A report that carries lines the store still refuses is refused why."""
    report_path = _deliver_plan_review(rubric="not-a-rubric")

    refusal = _refusal(_plan_review_record(report_path=report_path))

    assert "the review store refused" in refusal, refusal
    assert "unknown review rubric" in refusal, refusal


def test_a_plan_review_promotes_end_to_end_without_a_typed_outcome(
    repository: Path,
) -> None:
    """``crew complete --gate not-run`` with no ``--outcome`` lands the review."""
    from reckon import crew

    _deliver_plan_review()
    record = _plan_review_record()
    (Path(run_dir(CREW_RUN_ID))).mkdir(parents=True, exist_ok=True)
    (Path(run_dir(CREW_RUN_ID)) / "exit.json").write_text(
        json.dumps({"run_id": CREW_RUN_ID, "exit_code": 0}), encoding="utf-8"
    )
    _write_json(pointer_path(CREW_RUN_ID), record)

    result = crew.complete(
        CREW_RUN_ID, gate="not-run", root=repository, promoted_by="review-self"
    )

    assert result["plan_comment"]["recorded"] is True, result["plan_comment"]
    landed = (repository / "docs" / "plans" / f"{PLAN}.html").read_text(
        encoding="utf-8"
    )
    assert "2 finding(s)" in landed, landed
    assert "1 of which would change the plan" in landed, landed


# ── A queue-launched run review ─────────────────────────────────────────────


def _stage_store_record(run_id: str) -> Path:
    """A stored run review keyed by the review run that produced it."""
    directory = review_module.review_store_root() / PROJECT
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{REVIEWED_RUN_ID}.at-{'c' * 12}.json"
    _write_json(
        path,
        {
            "project": PROJECT,
            "status": "parsed",
            "review_run_id": run_id,
            "reviewed_run_id": REVIEWED_RUN_ID,
            "reviewed_base_sha": "d" * 40,
            "reviewed_head_sha": "c" * 40,
            "total": RUN_REVIEW_TOTAL,
            "findings": [{"id": "wiring-1", "type": "wiring"}],
            "timestamp": "2026-10-21T22:20:00Z",
        },
    )
    return path


def test_a_queue_launched_run_review_composes_its_outcome_from_the_store(
    repository: Path,
) -> None:
    """A rebuilt record with no role or node id still reaches its stored review."""
    _stage_store_record(RUN_REVIEW_ID)
    # The shape a run directory left without a manifest rebuilds to: the role and
    # the node id its review is recognised by are both gone.
    rebuilt = {
        "run_id": RUN_REVIEW_ID,
        "project": PROJECT,
        "repo": str(repository),
        "role": "",
        "node": {},
        "rebuilt_from_run_directory": True,
    }

    outcome = _outcome(rebuilt, RUN_REVIEW_ID)

    assert f"review scored {RUN_REVIEW_TOTAL}" in outcome, outcome
    assert "1 finding(s)" in outcome, outcome


def test_a_run_that_stored_no_review_is_still_refused(
    repository: Path,
) -> None:
    """The store lookup proves identity; a run holding no record still refuses."""
    rebuilt = {
        "run_id": RUN_REVIEW_ID,
        "project": PROJECT,
        "repo": str(repository),
        "role": "",
        "node": {},
        "rebuilt_from_run_directory": True,
    }

    refusal = _refusal(rebuilt, RUN_REVIEW_ID)

    assert "a non-passing gate requires --outcome" in refusal, refusal


# ── A run review declares no head-keyed record path ─────────────────────────


def test_a_run_review_declaring_no_head_keyed_path_is_still_refused(
    repository: Path,
) -> None:
    """The run-review conditions are unchanged: a headless declaration refuses."""
    directory = review_module.run_review_record_directory(
        Path(run_dir(REVIEWED_RUN_ID))
    )
    directory.mkdir(parents=True, exist_ok=True)
    legacy = directory / f"{REVIEWED_RUN_ID}.json"
    _write_json(legacy, {"status": "parsed"})
    record = {
        "run_id": RUN_REVIEW_ID,
        "project": PROJECT,
        "role": "review",
        "node": {
            "id": RUN_REVIEW_NODE_ID,
            "role": "review",
            "write_paths": [str(legacy)],
        },
    }

    refusal = _refusal(record, RUN_REVIEW_ID)

    assert "no head-keyed record path" in refusal, refusal
