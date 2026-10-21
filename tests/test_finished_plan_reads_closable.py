"""A plan whose declarations admit no work reads closable, not ready.

``plan_executable_remainder`` is the one reader of "is any declared section
still implementable", so the readiness ladder consumes its known zero rather
than testing the declarations a second time. Six plans across five tests
separate the cases.

``finished`` declares every section done: it reads ``closable``, leaves
``ready_now`` and the critical path, and its ``closure_blockers`` ends with the
``close`` op. ``working`` keeps one implementable section and still reads
``ready``. ``legacy`` declares nothing at all and ``legacy-empty`` persists an
empty declaration mapping, which reaches the ladder as the same thing; both keep
whatever readiness they would otherwise have. ``gated`` declares every section
done beside an open execution gate and no open followup: it reads ``closable``,
and the gate appears in ``closure_blockers`` with the verdict ops that clear it.
``deferred-and-followup`` declares one section done and one deferred beside an
open followup, and its ``closure_blockers`` names each obstacle with the ops
that clear it.

With the closable branch of the readiness ladder disabled, ``finished``,
``gated`` and ``deferred-and-followup`` lose their closable reading: three of
the five tests fail, and the two covering the unchanged readinesses pass.
"""

from __future__ import annotations

from reckon.roadmap import build_roadmap
from tests.roadmap_fixtures import plan_row

PROJECT = "closable-sample"


def _open_gate(gate_id: str, *, section: str = "s1") -> dict:
    return {
        "id": gate_id,
        "section": section,
        "status": "open",
        "measure": "a check that has not passed",
        "verdict": "",
    }


def _plans() -> list[dict]:
    return [
        # Every declared section is resolved, so nothing is implementable. Its
        # effort is uncalibrated and unstarted, which is what puts it at the
        # front of the base critical path before the closable branch exists.
        plan_row(
            "finished",
            impl=0.0,
            effort="L",
            effort_hours=40.0,
            section_declarations={"s1": "done", "s2": "done"},
        ),
        # One section still admits work, so the plan is ordinary ready work.
        plan_row(
            "working",
            effort="S",
            effort_hours=2.0,
            section_declarations={"s1": "done", "s2": "implementable"},
        ),
        # No persisted declarations is the legacy population: an unknown
        # remainder keeps whatever readiness the ladder would have given it.
        plan_row("legacy", effort="S", effort_hours=1.0),
        # A discovery row reports a plan that persisted no classification as an
        # empty mapping, so an empty map is the same legacy population and not a
        # plan whose every section happens to be resolved.
        plan_row("legacy-empty", effort="S", effort_hours=1.0, section_declarations={}),
        # Nothing implementable is left, but an execution gate is still open.
        plan_row(
            "gated",
            effort="S",
            effort_hours=1.0,
            section_declarations={"s1": "done"},
            gates=[_open_gate("g-s1")],
            followups=[],
        ),
        # Nothing implementable is left, but a deferred section and the default
        # open followup still stand between the plan and its close.
        plan_row(
            "deferred-and-followup",
            effort="S",
            effort_hours=1.0,
            section_declarations={"s1": "done", "s2": "deferred"},
        ),
    ]


def _roadmap() -> dict:
    sprints = [
        {
            "id": "current",
            "status": "active",
            "items": [plan["slug"] for plan in _plans()],
        }
    ]
    return build_roadmap(PROJECT, _plans(), sprints)


def _rows() -> dict[str, dict]:
    return {row["slug"]: row for row in _roadmap()["pending_work"]}


def _last_op(row: dict) -> list[str]:
    return row["closure_blockers"][-1]["ops"]


def test_a_finished_plan_reads_closable_and_leaves_the_ready_set() -> None:
    report = _roadmap()
    row = _rows()["finished"]

    assert row["readiness"] == "closable"
    assert row["ready"] is False
    assert "finished" not in [item["slug"] for item in report["ready_now"]]
    assert "finished" not in report["critical_path"]["plans"]
    # The list ends with the op that closes the plan once nothing stands in the
    # way, and the control plan with work left is still on the critical path.
    assert _last_op(row) == ["close"]
    assert "working" in report["critical_path"]["plans"]


def test_a_plan_with_implementable_work_still_reads_ready() -> None:
    report = _roadmap()
    row = _rows()["working"]

    assert row["readiness"] == "ready"
    assert row["ready"] is True
    assert "working" in [item["slug"] for item in report["ready_now"]]


def test_a_plan_without_declarations_keeps_its_readiness() -> None:
    rows = _rows()

    for slug in ("legacy", "legacy-empty"):
        row = rows[slug]
        assert row["readiness"] == "ready", slug
        assert row["ready"] is True, slug
        assert row["closure_blockers"] == [], slug


def test_a_finished_plan_with_an_open_gate_reads_closable_naming_the_gate() -> None:
    row = _rows()["gated"]

    assert row["readiness"] == "closable"
    assert row["ready"] is False
    assert [gate["id"] for gate in row["closure_blockers"][:-1]] == ["g-s1"]
    assert row["closure_blockers"][0]["ops"] == ["pass", "fail"]
    assert _last_op(row) == ["close"]


def test_a_closable_plan_names_every_obstacle_with_the_ops_that_clear_it() -> None:
    row = _rows()["deferred-and-followup"]

    assert row["readiness"] == "closable"
    # A deferred section and an open followup are obstacles of different kinds,
    # each carrying only the ops that clear it, ahead of the close op that ends
    # the list. Dropping either entry, or reporting the wrong op for it, leaves
    # the plan's closure list describing less than it must.
    assert [
        (entry["kind"], entry.get("id", ""), entry["ops"])
        for entry in row["closure_blockers"]
    ] == [
        ("section", "s2", ["descope"]),
        ("followup", "next", ["resolve"]),
        ("op", "", ["close"]),
    ]
