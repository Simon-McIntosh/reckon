"""A drain's guarantees are measured from its records, or reported as unmeasured."""

import json
from datetime import UTC, datetime, timedelta

from reckon.crew import drain_measure


def _run(
    run_id,
    *,
    project="reckon",
    plan="some-plan",
    section="§2",
    launched="2026-11-20T10:00:00Z",
    completed="2026-11-20T12:00:00Z",
    backend="clive",
    promoted_by=None,
    fallback_reason=None,
):
    record = {
        "run_id": run_id,
        "project": project,
        "node_definition": {"plan": plan, "section": section},
        "worker_exit": {"launched_at": launched},
        "completed_at": completed,
        "backend": backend,
        "promoted_by": promoted_by,
    }
    if fallback_reason is not None:
        record["picker_selection"] = {
            "backend": backend,
            "fallback_reason": fallback_reason,
        }
    return record


def _pass(at, rows):
    return {
        "at": at,
        "pointers": [
            {"run_id": run_id, "verdict": verdict} for run_id, verdict in rows
        ],
    }


NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def test_a_launch_before_its_upstream_landed_is_the_one_seeded_violation():
    """The instrument counts the violation it is seeded with, and only that one."""
    runs = [
        # The seeded violation: §3 launches at 09:00, §2 it waits on lands 12:00.
        _run(
            "r-before",
            section="§3",
            launched="2026-11-20T09:00:00Z",
            completed="2026-11-20T10:00:00Z",
        ),
        _run(
            "r-upstream",
            section="§2",
            launched="2026-11-20T11:00:00Z",
            completed="2026-11-20T12:00:00Z",
        ),
        # Correct order: §5 waits on §4, and §4 landed first.
        _run(
            "r-upstream-2",
            section="§4",
            launched="2026-11-20T08:00:00Z",
            completed="2026-11-20T09:00:00Z",
        ),
        _run(
            "r-after",
            section="§5",
            launched="2026-11-20T09:30:00Z",
            completed="2026-11-20T11:00:00Z",
        ),
    ]
    depends_on = {
        ("reckon", "some-plan"): {
            "s3": ["some-plan#s2"],
            "s5": ["some-plan#s4"],
        }
    }
    result = drain_measure.measure_dag_order(runs, depends_on)

    assert result["status"] == "measured"
    assert result["launches_with_declared_waits"] == 2
    assert result["violations"] == 1
    assert result["runs_in_violation"] == 1
    assert result["rows"][0]["run_id"] == "r-before"
    assert result["rows"][0]["waits_on"] == "some-plan#s2"
    assert result["rows"][0]["seconds_early"] == 3 * 3600


def test_a_wait_on_a_section_that_never_landed_is_a_violation():
    runs = [_run("r-orphan", section="§7", launched="2026-11-20T09:00:00Z")]
    depends_on = {("reckon", "some-plan"): {"s7": ["some-plan#s6"]}}

    result = drain_measure.measure_dag_order(runs, depends_on)

    assert result["violations"] == 1
    assert result["rows"][0]["reason"] == "the waited-on section never landed"


def test_an_upstream_that_landed_before_the_window_is_not_a_violation():
    upstream = _run(
        "r-upstream",
        section="§2",
        launched="2026-10-01T08:00:00Z",
        completed="2026-10-01T09:00:00Z",
    )
    window = [
        _run(
            "r-dependent",
            section="§3",
            launched="2026-11-20T09:00:00Z",
            completed="2026-11-20T10:00:00Z",
        ),
    ]
    depends_on = {("reckon", "some-plan"): {"s3": ["some-plan#s2"]}}

    without_history = drain_measure.measure_dag_order(window, depends_on)
    with_history = drain_measure.measure_dag_order(
        window, depends_on, history=[*window, upstream]
    )

    assert without_history["violations"] == 1
    assert with_history["violations"] == 0
    assert with_history["launches_with_declared_waits"] == 1


def test_a_cross_project_wait_resolves_to_the_other_project():
    runs = [
        _run(
            "r-far",
            project="reckon",
            plan="other-plan",
            section="§2",
            launched="2026-11-20T09:00:00Z",
        ),
        _run(
            "r-upstream",
            project="imas-ambix",
            plan="their-plan",
            section="§4",
            launched="2026-11-20T10:00:00Z",
            completed="2026-11-20T11:00:00Z",
        ),
    ]
    depends_on = {
        ("reckon", "other-plan"): {
            "s2": ["imas-ambix:their-plan#s4"],
        }
    }

    result = drain_measure.measure_dag_order(runs, depends_on)

    assert result["violations"] == 1
    assert result["rows"][0]["seconds_early"] == 2 * 3600


def test_dag_order_counts_only_launches_that_declare_a_wait():
    """A control the instrument must show it saw: the read launches are counted."""
    runs = [_run("r-plain", section="§2")]
    empty = drain_measure.measure_dag_order(runs, {})
    declared = drain_measure.measure_dag_order(
        runs, {("reckon", "some-plan"): {"s2": []}}
    )
    assert empty["launches_with_declared_waits"] == 0
    assert declared["launches_with_declared_waits"] == 0
    assert empty["violations"] == declared["violations"] == 0


def test_fair_share_reads_the_weights_both_projects_were_launched_under():
    runs = [_run(f"r-a{n}", project="reckon") for n in range(6)] + [
        _run(f"r-b{n}", project="imas-ambix") for n in range(2)
    ]
    result = drain_measure.measure_fair_share(
        runs,
        weights={"reckon": 3.0, "imas-ambix": 1.0},
        ready_by_project={"reckon": 5, "imas-ambix": 2},
    )

    assert result["status"] == "measured"
    assert result["within_tolerance"] is True
    assert result["projects"]["reckon"]["expected_share"] == 0.75
    assert result["projects"]["imas-ambix"]["expected_share"] == 0.25
    assert result["projects"]["reckon"]["deviation"] == 0.0


def test_fair_share_is_unmeasured_while_one_project_alone_has_work():
    runs = [_run(f"r-a{n}", project="reckon") for n in range(4)]
    result = drain_measure.measure_fair_share(
        runs,
        weights={"reckon": 3.0, "imas-ambix": 1.0},
        ready_by_project={"reckon": 5, "imas-ambix": 0},
    )
    assert result["status"] == "unmeasured"
    assert result["launches"] == {"reckon": 4}


def test_fair_share_flags_a_split_outside_the_tolerance():
    runs = [_run(f"r-a{n}", project="reckon") for n in range(1)] + [
        _run(f"r-b{n}", project="imas-ambix") for n in range(3)
    ]
    result = drain_measure.measure_fair_share(
        runs,
        weights={"reckon": 3.0, "imas-ambix": 1.0},
        ready_by_project={"reckon": 5, "imas-ambix": 3},
    )
    assert result["status"] == "measured"
    assert result["within_tolerance"] is False


def test_a_verdict_silence_longer_than_the_limit_is_reported():
    passes = [
        _pass("2026-11-22T09:00:00Z", [("r-waiting", "queued")]),
        _pass("2026-11-22T09:30:00Z", [("r-waiting", "queued")]),
        _pass("2026-11-22T11:00:00Z", [("r-waiting", "queued")]),
    ]
    result = drain_measure.measure_verdict_gap(
        passes, window_start=NOW - timedelta(days=1), now=NOW
    )
    assert result["violations"] == 1
    assert result["rows"][0]["silent_seconds"] == 5400.0


def test_a_covered_window_with_frequent_verdicts_has_no_violation():
    """The positive control: the instrument sees a healthy wait and says so."""
    passes = [
        _pass(f"2026-11-22T{h:02d}:00:00Z", [("r-waiting", "queued")])
        for h in range(8, 12)
    ]
    result = drain_measure.measure_verdict_gap(
        passes,
        window_start=datetime(2026, 11, 22, 8, 0, tzinfo=UTC),
        now=NOW,
    )
    assert result["status"] == "measured"
    assert result["waiting_requests"] == 1
    assert result["violations"] == 0
    assert result["widest_gap_seconds"] == 3600.0


def test_a_log_that_opens_inside_the_window_reports_itself_unmeasured():
    passes = [_pass("2026-11-22T11:30:00Z", [("r-waiting", "queued")])]
    result = drain_measure.measure_verdict_gap(
        passes, window_start=NOW - timedelta(days=7), now=NOW
    )
    assert result["status"] == "unmeasured"
    assert "opens" in result["reason"]


def test_landed_without_a_turn_counts_only_self_landed_runs():
    runs = [
        _run("r-1", promoted_by="review-self"),
        _run("r-2", promoted_by="review-acceptance"),
        _run("r-3", promoted_by=None),
    ]
    result = drain_measure.measure_landed_without_a_turn(runs)
    assert result["landed_without_a_coordinator_turn"] == 2
    assert result["share"] == 0.6667


def test_live_interventions_are_counted_where_they_are_kept():
    pointers = [
        {
            "run_id": "r-1",
            "priority_override": 5,
            "queue_hold": {"reason": "x"},
            "steering_sha256": "abc",
        },
        {"run_id": "r-2", "priority_override": None, "queue_hold": None},
    ]
    result = drain_measure.measure_live_interventions(pointers)
    assert result["requests"] == 2
    assert result["with_a_priority_override"] == 1
    assert result["held"] == 1
    assert result["edited"] == 1


def test_unloaded_sections_are_counted_per_project():
    result = drain_measure.measure_unloaded({"reckon": ["s2", "s3"], "imas-ambix": []})
    assert result["plans"] == 1
    assert result["sections"] == 2
    assert result["unloaded"] == {"reckon": ["s2", "s3"]}


def test_metered_routing_counts_fallbacks_and_their_reasons():
    runs = [
        _run("r-codex", backend="codex", fallback_reason="no local slot"),
        _run("r-local", backend="omp-local"),
    ]
    result = drain_measure.measure_metered_routing(runs)
    assert result["recorded_fallbacks"] == 1
    assert result["fallbacks"][0]["reason"] == "no local slot"
    assert result["launches_by_backend"] == {"codex": 1, "omp-local": 1}


def test_the_report_names_the_record_each_unmeasured_guarantee_needs():
    report = drain_measure.measure_drain(runs=[], now=NOW)
    for key in ("coordinator_turns", "held_launch", "bumped_priority", "edited_digest"):
        assert report[key]["status"] == "unmeasured"
        assert report[key]["reason"]
    assert report["runs_in_window"] == 0
    assert json.dumps(report)  # the report is plain data


def test_the_window_keeps_only_the_runs_it_covers():
    runs = [
        _run("r-inside", launched="2026-11-20T10:00:00Z"),
        _run("r-old", launched="2026-10-01T10:00:00Z"),
    ]
    report = drain_measure.measure_drain(runs=runs, now=NOW, window_days=7)
    assert report["runs_in_window"] == 1
