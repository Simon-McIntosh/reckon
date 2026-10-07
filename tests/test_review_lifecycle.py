"""A review's lifecycle is derived from the record and its subject, never stored.

Four states, first match wins. The cases below hold each one to the fixture that
produces it, for a plan review and for a run review, and hold the order between
them: a landed review whose finding is still unanswered reads landed, because
the work it describes has collapsed and its obligation went with it. A plan
shipping moves its reviews to landed while every record keeps its own bytes —
the state is a derivation, not a field a write would have to keep true. A run
review that carries only follow-on findings is answered rather than open. The
loader pairs each stored record's path with the state derived for it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import plan_review
from reckon.crew import review as review_module
from reckon.crew import review_lifecycle as module

PROJECT = "lifecycle-fixture"
PLAN_SLUG = "demo"
REVIEWED_RUN = "r-20261006T120000000000-reviewed-run"
RUN_REVIEW = "r-20261006T130000000000-run-review"


def _plan_record(
    *,
    version: int = 1,
    dispatched: str = "2026-10-01T00:00:00+00:00",
    rubric: str = "plan_review",
    findings: list | None = None,
    responses: dict | None = None,
    section_digests: dict | None = None,
) -> dict:
    return {
        "project": PROJECT,
        "plan_slug": PLAN_SLUG,
        "plan_version": version,
        "rubric": rubric,
        "dispatched_at": dispatched,
        "findings": findings or [],
        "responses": responses or {},
        "section_digests": section_digests
        if section_digests is not None
        else {"s1": "digest-s1", "_document": "digest-doc"},
    }


def _run_record(
    *,
    dispatched: str = "2026-10-01T23:00:00+00:00",
    findings: list | None = None,
    responses: dict | None = None,
    response_events: list | None = None,
) -> dict:
    record = {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": RUN_REVIEW,
        "dispatched_at": dispatched,
        "findings": findings or [],
        "responses": responses or {},
    }
    if response_events is not None:
        record["response_events"] = response_events
    return record


def _plan_state(*, status: str = "active", declarations: dict | None = None) -> dict:
    state = {"status": status}
    if declarations is not None:
        state["section_declarations"] = declarations
    return state


# ── Plan reviews: the four states ───────────────────────────────────────────


def test_a_plan_review_lands_when_its_plan_has_shipped() -> None:
    for status in ("shipped", "done", "archived"):
        assert module.lifecycle(
            _plan_record(), plan_state=_plan_state(status=status)
        ) == (module.LANDED)


def test_a_plan_review_lands_when_every_reviewed_section_is_done() -> None:
    record = _plan_record(section_digests={"s1": "d1", "s2": "d2", "_document": "d"})
    state = _plan_state(status="active", declarations={"s1": "done", "s2": "done"})
    assert module.lifecycle(record, plan_state=state) == module.LANDED


def test_a_plan_review_is_not_landed_while_one_reviewed_section_is_open() -> None:
    record = _plan_record(section_digests={"s1": "d1", "s2": "d2", "_document": "d"})
    state = _plan_state(
        status="active", declarations={"s1": "done", "s2": "implementable"}
    )
    assert module.lifecycle(record, plan_state=state) == module.ANSWERED


def test_a_plan_review_is_superseded_by_a_later_review_of_the_same_rubric() -> None:
    record = _plan_record(version=1, dispatched="2026-10-01T00:00:00+00:00")
    later = _plan_record(version=2, dispatched="2026-10-02T00:00:00+00:00")
    assert (
        module.lifecycle(record, plan_state=_plan_state(), later_records=(later,))
        == module.SUPERSEDED
    )


def test_a_design_review_does_not_supersede_a_content_review() -> None:
    content = _plan_record(rubric="plan_review", version=1)
    design = _plan_record(rubric="plan_design_review", version=2)
    assert (
        module.lifecycle(content, plan_state=_plan_state(), later_records=(design,))
        == module.ANSWERED
    )


def test_a_plan_review_is_open_while_a_finding_is_unanswered() -> None:
    record = _plan_record(findings=[{"id": "f1", "type": "wiring"}])
    assert module.lifecycle(record, plan_state=_plan_state()) == module.OPEN


def test_a_plan_review_is_answered_once_its_finding_is_answered() -> None:
    record = _plan_record(
        findings=[{"id": "f1", "type": "wiring"}],
        responses={"f1": {"action": "acted"}},
    )
    assert module.lifecycle(record, plan_state=_plan_state()) == module.ANSWERED


# ── Run reviews: the four states ────────────────────────────────────────────


def test_a_run_review_lands_when_its_run_is_promoted() -> None:
    record = _run_record(findings=[{"id": "f1", "severity": "blocking"}])
    assert module.lifecycle(record, run_promoted=True) == module.LANDED


def test_a_run_review_is_superseded_by_a_later_review_of_the_same_run() -> None:
    record = _run_record(dispatched="2026-10-01T01:00:00+00:00")
    later = _run_record(dispatched="2026-10-01T02:00:00+00:00")
    assert module.lifecycle(record, later_records=(later,)) == module.SUPERSEDED


def test_a_run_review_is_open_while_a_blocking_finding_is_unanswered() -> None:
    record = _run_record(findings=[{"id": "f1", "severity": "blocking"}])
    assert module.lifecycle(record) == module.OPEN


def test_a_run_finding_with_no_declared_severity_blocks() -> None:
    record = _run_record(findings=[{"id": "f1"}])
    assert module.lifecycle(record) == module.OPEN


def test_a_follow_on_only_run_review_is_answered_not_open() -> None:
    record = _run_record(findings=[{"id": "f1", "severity": "follow-on"}])
    assert module.lifecycle(record) == module.ANSWERED


def test_a_blocking_finding_answered_through_response_events_is_answered() -> None:
    record = _run_record(
        findings=[{"id": "f1", "severity": "blocking"}],
        response_events=[{"finding": "f1", "action": "acted"}],
    )
    assert module.lifecycle(record) == module.ANSWERED


# ── Precedence: landed over superseded over open ────────────────────────────


def test_landed_takes_precedence_over_superseded_and_open_for_a_plan_review() -> None:
    # The plan has shipped, the review's finding is unanswered and a later
    # review of the same rubric exists: the collapsed work wins, so the review
    # has landed. Checking open before landed would read this open.
    record = _plan_record(findings=[{"id": "f1", "type": "wiring"}])
    later = _plan_record(version=2, dispatched="2026-10-02T00:00:00+00:00")
    assert (
        module.lifecycle(
            record,
            plan_state=_plan_state(status="shipped"),
            later_records=(later,),
        )
        == module.LANDED
    )


def test_landed_takes_precedence_over_open_for_a_run_review() -> None:
    record = _run_record(findings=[{"id": "f1", "severity": "blocking"}])
    assert module.lifecycle(record, run_promoted=True) == module.LANDED


def test_the_hot_set_is_the_two_unsettled_states() -> None:
    assert {"open", "answered"} == module.HOT_STATES


# ── The loader ──────────────────────────────────────────────────────────────

_TEST_STATUS = "active"

_PLAN_HTML = """<html><head>
<meta name="plan-slug" content="{slug}">
<meta name="plan-status" content="{status}">
<meta name="plan-section-declarations" content='{{"s1": "implementable"}}'>
</head><body></body></html>
"""


@pytest.fixture()
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised config home, store root and project checkout."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    return root


def _write_plan(root: Path, *, status: str) -> Path:
    plan_path = root / "docs" / "plans" / f"{PLAN_SLUG}.html"
    plan_path.write_text(
        _PLAN_HTML.format(slug=PLAN_SLUG, status=status), encoding="utf-8"
    )
    return plan_path


def _promote_run(root: Path, run_id: str) -> None:
    path = root / "docs" / "state" / PROJECT / "runs" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"run_id": run_id, "project": PROJECT}), encoding="utf-8"
    )


def _fingerprint(path: Path) -> tuple[int, bytes]:
    stat = path.stat()
    return stat.st_mtime_ns, path.read_bytes()


def test_the_loader_pairs_each_records_path_with_its_state(
    checkout: Path, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    plan_path = plan_review.store_plan_review(
        _plan_record(findings=[{"id": "f1", "type": "wiring"}]), base_dir=store
    )
    run_path = review_module.store_review(
        _run_record(findings=[{"id": "f2", "severity": "follow-on"}]), base_dir=store
    )
    _write_plan(checkout, status="active")

    states = module.review_lifecycles(PROJECT, base_dir=store, root=checkout)

    assert states[str(plan_path)] == module.OPEN
    assert states[str(run_path)] == module.ANSWERED


def test_a_plan_shipping_moves_its_reviews_to_landed_without_rewriting_records(
    checkout: Path, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    path = plan_review.store_plan_review(
        _plan_record(findings=[{"id": "f1", "type": "wiring"}]), base_dir=store
    )
    _write_plan(checkout, status="active")
    before = _fingerprint(path)
    assert (
        module.review_lifecycles(PROJECT, base_dir=store, root=checkout)[str(path)]
        == module.OPEN
    )

    _write_plan(checkout, status="shipped")
    after = _fingerprint(path)
    states = module.review_lifecycles(PROJECT, base_dir=store, root=checkout)

    assert states[str(path)] == module.LANDED
    assert after == before


def test_the_loader_reads_a_promoted_run_from_the_ledger(
    checkout: Path, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    path = review_module.store_review(
        _run_record(findings=[{"id": "f1", "severity": "blocking"}]), base_dir=store
    )
    _write_plan(checkout, status="active")
    assert (
        module.review_lifecycles(PROJECT, base_dir=store, root=checkout)[str(path)]
        == module.OPEN
    )

    _promote_run(checkout, REVIEWED_RUN)
    assert (
        module.review_lifecycles(PROJECT, base_dir=store, root=checkout)[str(path)]
        == module.LANDED
    )
