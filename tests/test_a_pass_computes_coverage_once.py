"""An admission pass reads each plan's review evidence once.

An admission pass judges the review gate for every queued request it considers,
and every one of those judgments used to re-read the same plan: its review
coverage, its section digests, its parsed state and its document contract were
taken per request. These cases pin the pass reading each of them once per plan
while every request keeps the verdict it reached on the base revision, and pin
wait resolution to its own stage of the pass's clock rather than to the launch
figure.
"""

# ruff: noqa: F811 - the fixtures are imported and also named as test parameters.

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from reckon import _plan_html
from reckon.crew import (
    plan_review,
    queue_admission,
    recovery,
    review,
    routing,
    runs,
)
from reckon.crew.node import CrewError, PlanReviewMissingError, TaskNode
from tests.test_a_queued_request_is_checked_once import (  # noqa: F401 - fixtures
    two_section_plan,
)
from tests.test_review_plan_command import project as project  # noqa: PLC0414

ENFORCING = {"plan_review_gate": "enforce"}
PLAN = "fixture"
PROJECT = "sample"


def _config_for(_pointer) -> dict:
    return ENFORCING


def _node(section: str, *, node_id: str | None = None, role: str = "implement"):
    return TaskNode(
        id=node_id or f"build-{section}",
        goal=f"Build {section}",
        plan=PLAN,
        section=section,
        role=role,
    )


def _request(
    repo,
    *,
    run_id: str,
    section: str = "s2",
    node_id: str | None = None,
    role: str = "implement",
) -> dict:
    """A queued request record carrying the contract it was queued against."""
    node = _node(section, node_id=node_id, role=role)
    joined = datetime.now(UTC).isoformat()
    return {
        "run_id": run_id,
        "project": PROJECT,
        "plan": PLAN,
        "section": section,
        "repo": str(repo),
        "session": "coordinator",
        "node": node.as_dict(),
        "phase": queue_admission.QUEUED_PHASE,
        "queued_at": joined,
        "created_at": joined,
        "backend": "worker",
        "local": True,
        "dispatch_options": {},
        "review_contract": queue_admission.review_contract(node, repo_root=repo),
    }


def _owed(pointer) -> str | None:
    return queue_admission._plan_review_owed(pointer, config_for=_config_for)


def _fresh_dispatch(
    repo, section: str = "s2", *, role: str = "implement"
) -> str | None:
    """The dispatch gate's own verdict for a fresh dispatch of the section."""
    try:
        routing.require_plan_reviewed(
            node=_node(section, role=role),
            project=PROJECT,
            repo=repo,
            authority={"plan": {"docs": str(repo / "docs"), "source": "repository"}},
            enforce=True,
        )
    except PlanReviewMissingError as exc:
        return str(exc)
    return None


#: What each read the pass is allowed to take once costs. Patching the four
#: readers this judgment is built from is what makes "once per plan" a
#: measurement rather than a claim about the shape of the code.
class _Counts:
    def __init__(self) -> None:
        self.coverage = 0
        self.digests = 0
        self.state = 0
        self.gate = 0


def _count_reads(monkeypatch) -> _Counts:
    counts = _Counts()
    real_coverage = plan_review.review_coverage
    real_digests = plan_review._section_digests
    real_state = _plan_html.read_state_file
    real_gate = routing.require_plan_reviewed
    # A coverage read digests the plan internally to fingerprint it, so those
    # calls are the cost of the coverage read the pass takes once and not a
    # second read of the plan. Only the pass's own digest reads are counted.
    inside = {"coverage": False}

    def coverage(*args, **kwargs):
        counts.coverage += 1
        inside["coverage"] = True
        try:
            return real_coverage(*args, **kwargs)
        finally:
            inside["coverage"] = False

    def digests(*args, **kwargs):
        if not inside["coverage"]:
            counts.digests += 1
        return real_digests(*args, **kwargs)

    def state(*args, **kwargs):
        counts.state += 1
        return real_state(*args, **kwargs)

    def gate(*args, **kwargs):
        counts.gate += 1
        return real_gate(*args, **kwargs)

    monkeypatch.setattr(plan_review, "review_coverage", coverage)
    monkeypatch.setattr(plan_review, "_section_digests", digests)
    monkeypatch.setattr(_plan_html, "read_state_file", state)
    monkeypatch.setattr(routing, "require_plan_reviewed", gate)
    return counts


def _refuse_launches(monkeypatch) -> None:
    """Let a pass reach every request's review judgment without launching.

    Every request in these cases is decided before its launch, so a launch
    refused here proves the pass got that far rather than failing early.
    """

    def refuse(_pointer, **_kwargs):
        raise CrewError("the launch step is stubbed out for this measure")

    monkeypatch.setattr(queue_admission, "_launch_queued", refuse)


def _queue(*requests: dict) -> None:
    for request in requests:
        runs.queue_dispatch(request)


def _allow_flight(monkeypatch) -> None:
    """Resolve flight configuration whatever the caller's argument style is.

    The project fixture's own stand-in accepts keyword arguments only, while the
    queue-depth read asks positionally, so this restates it for the shapes this
    module's paths use.
    """
    from types import SimpleNamespace

    from reckon import flight

    monkeypatch.setattr(
        flight,
        "resolve",
        lambda *_args, **_kwargs: SimpleNamespace(config=dict(ENFORCING)),
    )


def test_a_pass_reads_one_plans_evidence_once_for_three_requests(
    two_section_plan, monkeypatch
):
    """Three requests on one plan cost one coverage, digests and state read."""
    _, repo, path = two_section_plan
    _allow_flight(monkeypatch)
    plan_review.store_plan_review(_content_record(path, "review-content"))
    _queue(
        _request(repo, run_id="r-one", section="s2", node_id="build-s2-a"),
        _request(repo, run_id="r-two", section="s2", node_id="build-s2-b"),
        _request(repo, run_id="r-three", section="s3"),
    )
    counts = _count_reads(monkeypatch)
    _refuse_launches(monkeypatch)

    queue_admission.admit_once(config_for=_config_for)

    assert counts.coverage == 1, "the plan's coverage is read once for the pass"
    assert counts.digests == 1, "the plan's section digests are read once"
    assert counts.state == 1, "the plan's parsed state is read once"


def test_a_pass_asks_the_dispatch_gate_once_per_section_and_exemption(
    two_section_plan, monkeypatch
):
    """Three uncovered requests cost two gate reads, not three."""
    _, repo, _path = two_section_plan
    _allow_flight(monkeypatch)
    _queue(
        _request(repo, run_id="r-one", section="s2", node_id="build-s2-a"),
        _request(repo, run_id="r-two", section="s2", node_id="build-s2-b"),
        _request(
            repo,
            run_id="r-review",
            section="s2",
            node_id="review-of-fixture",
            role="review",
        ),
    )
    counts = _count_reads(monkeypatch)
    _refuse_launches(monkeypatch)

    result = queue_admission.admit_once(config_for=_config_for)

    assert counts.gate == 2, (
        "one gate read for the implement requests' shared section and exemption "
        "answer, one for the review request's"
    )
    held = result.get("hold_detail") or {}
    direct = _fresh_dispatch(repo, "s2")
    assert direct is not None, "an unreviewed plan holds a fresh dispatch"
    for run_id in ("r-one", "r-two"):
        assert held.get(run_id) == direct, (
            f"{run_id} holds exactly as a fresh dispatch of the section does"
        )
    assert "r-review" not in held, "a review request is exempt from the gate"
    assert _fresh_dispatch(repo, "s2", role="review") is None


def test_a_review_stored_during_the_pass_makes_the_next_decision_recompute(
    two_section_plan, monkeypatch
):
    """A record added after the plan was read changes the key it is held under."""
    _, repo, path = two_section_plan
    pointer = _request(repo, run_id="r-one")
    counts = _count_reads(monkeypatch)

    with queue_admission.plan_review_pass():
        assert _owed(pointer) is not None, "the plan carries no stored review"
        read_once = counts.coverage
        assert read_once == 1

        plan_review.store_plan_review(_content_record(path, "review-content"))

        assert _owed(pointer) is None, "the stored review covers the request"
        assert counts.coverage == read_once + 1, (
            "a review stored after the record was built changes its key"
        )


def test_each_requests_verdict_matches_the_reading_it_replaces(two_section_plan):
    """Covered, uncovered and answered-finding verdicts survive the pass cache."""
    _, repo, path = two_section_plan
    pointer = _request(repo, run_id="r-one")
    pointer_two = _request(repo, run_id="r-two", node_id="build-s2-b")

    # A plan whose covering review is stored.
    record = _content_record(path, "review-content")
    plan_review.store_plan_review(record)
    with queue_admission.plan_review_pass():
        covered = _owed(pointer)
    assert covered is None
    assert covered == _owed(pointer), "the reading outside a pass agrees"
    assert _fresh_dispatch(repo, "s2") is None

    # The covering review removed again: the request owes the gate's hold.
    Path(record["review_path"]).unlink()  # the store path the record was written to
    with queue_admission.plan_review_pass():
        removed = _owed(pointer)
        removed_two = _owed(pointer_two)
    assert removed is not None
    assert removed == removed_two == _owed(pointer)
    assert removed == _fresh_dispatch(repo, "s2")

    # A review of the same text that leaves one finding unanswered: admission's
    # narrower question is unchanged, and the dispatch gate refuses the build.
    _store_finding(path, "review-content-two", unit="s2")
    with queue_admission.plan_review_pass():
        answered = _owed(pointer)
    assert answered == _owed(pointer)
    assert answered is None, "an unanswered finding is the gate's hold, not this one"
    refused = _fresh_dispatch(repo, "s2")
    assert refused is not None
    assert "unanswered" in refused


def test_the_pass_times_wait_resolution_outside_launch_seconds(
    two_section_plan, monkeypatch
):
    """A half-second wait stage is waits_seconds, not launch_seconds."""
    _, _repo, _path = two_section_plan

    def slow_advance(_live):
        time.sleep(0.5)
        return {"promoted": [], "blocked": [], "obligations": []}

    monkeypatch.setattr(queue_admission, "advance_waiting_requests", slow_advance)
    monkeypatch.setitem(queue_admission._RELEASE_TICK_LAST, "at", 0.0)

    def refuse_launch(*_args, **_kwargs):
        raise AssertionError("a pass with no queued request launches nothing")

    result = queue_admission.admit_until_stopped(
        once=True,
        config_for=_config_for,
        launcher=refuse_launch,
        lander=lambda: {"landed": [], "skipped": [], "obligations": []},
    )

    assert result["waits_seconds"] >= 0.5, "the wait stage is measured on its own"
    assert result["launch_seconds"] < 0.1, (
        "wait resolution is no longer charged to the launch stage"
    )


# --- stored reviews -------------------------------------------------------


def _report(items) -> str:
    return "".join(f"RUBRIC {item}: pass\n" for item in items)


def _content_record(path: Path, run_id: str, *, findings=None) -> dict:
    """Store one content review of the plan as it stands now."""
    subject = recovery.plan_review_subject(PROJECT, PLAN, "coordinator")
    fields = recovery._review_dispatch_fields(subject, rubric="content")
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    Path(sidecar["report_path"]).write_text(_report(review.PLAN_REVIEW_ITEMS))
    record = {
        **sidecar,
        "findings": list(findings or []),
        "responses": {},
        "review_run_id": run_id,
    }
    stored = plan_review.store_plan_review(record)
    return {**record, "review_path": str(stored)}


def _store_finding(path: Path, run_id: str, *, unit: str) -> dict:
    """Store a review of the plan that leaves one finding unanswered on a unit."""
    line = next(
        index
        for index, text in enumerate(path.read_text().splitlines(), start=1)
        if text.strip() and unit in text
    )
    return _content_record(
        path,
        run_id,
        findings=[
            {
                "id": f"{unit}-finding",
                "type": "reasoning",
                "anchor": f"{PLAN}:{line}",
                "text": "The section names a mechanism the code cannot observe.",
            }
        ],
    )
