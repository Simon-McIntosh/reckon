"""Gate the local-lane session-carryover census against its own counts.

The census file holds three things this gate reads: the commit that keyed
session reuse to a run's own task and the instant it landed, the resume counts
measured over the local dispatches either side of it, and a positive control
whose inheritance the same instrument is known to read. The file states its
own verdict, so the after window's cross-task figure is asserted against the
figure the file records rather than against a target.

The control is the assertion the declared negative control reddens: with every
dispatch classified fresh, no before-window cross-task resume exists to name,
and this file's control test fails where the class counts still reconcile.
"""

import json
from pathlib import Path

CENSUS = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "research"
    / "data"
    / "local-session-carryover.json"
)

# The census is a research artifact, not a transcript dump: the per-run rows it
# keeps are bounded, and the whole file must stay small enough to review.
MAX_CENSUS_BYTES = 300_000
# The scan is a bounded pass over the run directories, not an offline job.
MAX_SCAN_SECONDS = 1200
# The per-window cap on stored fresh rows, from the census's own contract.
MAX_FRESH_ROWS = 50


def census():
    with CENSUS.open() as handle:
        return json.load(handle)


def test_positive_control_is_a_before_window_cross_task_resume():
    data = census()
    control = data["positive_control"]
    assert control, "the census records no positive control"
    assert control["source"] == "before_window_known_cross_task_resume", control
    assert control["class"] == "cross_task", control
    assert control["inherited_tokens"] > 0, control
    assert control["prior_run"] and control["prior_run"] != control["run_id"], control
    assert control["session_id"] == control["recorded_session_id"], control
    assert control["ts"] <= data["boundary_utc"], control
    corroboration = control["corroboration"]
    assert corroboration["prior_census_names_it_resumed"] is True, corroboration
    assert corroboration["prior_census_prior_run_matches"] is True, corroboration


def test_every_class_count_reconciles_with_the_window_dispatches():
    data = census()
    for name in ("before_window", "after_window"):
        window = data[name]
        classes = window["class_counts"]
        assert sum(classes.values()) == window["dispatches"], (name, classes)
        assert window["fresh"] == classes.get("fresh", 0), (name, classes)
        assert window["cross_task_resumes"] == classes.get("cross_task", 0), (
            name,
            classes,
        )
        assert window["same_task_resumes"] == classes.get("same_task", 0) + classes.get(
            "same_run_id", 0
        ), (name, classes)
        assert window["prior_unknown_resumes"] == classes.get(
            "resumed_prior_unknown", 0
        ), (name, classes)
        assert window["runs_with_inherited_tokens"] <= (
            window["dispatches"] - window["fresh"]
        ), (name, window)


def test_stored_rows_cover_every_resume_and_only_sample_fresh_runs():
    data = census()
    for name in ("before_window", "after_window"):
        window = data[name]
        rows = window["rows"]
        resumes = [row for row in rows if row["class"] != "fresh"]
        fresh = [row for row in rows if row["class"] == "fresh"]
        assert len(resumes) == window["dispatches"] - window["fresh"], name
        assert len(fresh) <= MAX_FRESH_ROWS, (name, len(fresh))
        assert len(fresh) <= window["fresh"], (name, len(fresh))
        for row in rows:
            assert row["class"] in window["class_counts"], (name, row)


def test_after_window_cross_task_figure_matches_the_verdict_it_states():
    data = census()
    window = data["after_window"]
    verdict = data["cross_task_resumes_verdict"]
    assert data["boundary_commit"].startswith("adbb6067")
    assert data["boundary_utc"] == "2026-09-23T10:40:20Z"
    if verdict == "zero":
        assert window["cross_task_resumes"] == 0, data["statement"]
    else:
        assert verdict == "reported-non-zero", verdict
        assert window["cross_task_resumes"] > 0, data["statement"]
    assert window["dispatches"] >= 50, data["statement"]
    assert "cross-task resumes" in data["statement"]


def test_declared_resumes_reconcile_with_their_targets():
    data = census()
    for name in ("before", "after"):
        declared = data["declared_resumes"][name]
        assert (
            declared["declared_resumes"]
            == declared["declared_same_task"]
            + declared["declared_cross_task"]
            + declared["declared_unresolved"]
        ), (name, declared)
        assert declared["declared_own_run"] <= declared["declared_same_task"], (
            name,
            declared,
        )
        examples = declared["unresolved_or_cross_task_examples"]
        assert all(
            row["declared_target"] in ("cross_task", "unresolved") for row in examples
        ), (name, examples)
        assert len(examples) == min(
            12, declared["declared_cross_task"] + declared["declared_unresolved"]
        ), (name, declared)


def test_census_stays_reviewable_and_the_scan_stays_bounded():
    data = census()
    assert CENSUS.stat().st_size <= MAX_CENSUS_BYTES, CENSUS.stat().st_size
    assert data["wall_seconds"] < MAX_SCAN_SECONDS, data["wall_seconds"]
    assert data["scan"]["prefix_bound_hits"] == 0, data["scan"]["prefix_bound_hits"]
    assert data["scan"]["local_runs"] >= data["before_window"]["dispatches"]
    assert data["scan"]["first_record_types"].get("claude", 0) > 0
    assert data["scan"]["first_record_types"].get("codex", 0) > 0
