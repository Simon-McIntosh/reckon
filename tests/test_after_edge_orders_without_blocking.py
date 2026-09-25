"""An after edge orders the ready set and never blocks a plan.

An after edge expresses *start after X lands if you can, do not wait* — the
relation a coordinator reached for and could not find, wiring ``depends_on``
between plans whose work was already dispatchable, so the roadmap reported
both blocked until the wire was undone. These cases pin the three properties
that separate it from a hard prerequisite: the ready set keeps the plan, the
order and the annotation name the target it is sequenced behind, and a
``depends_on`` to the same target still blocks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon._plan_html import parse_meta, read_state, write_state
from reckon._schema import LINK_LIST_FIELDS, PlanState
from reckon.roadmap import build_roadmap


def _plan(slug: str, **overrides) -> dict:
    plan = {
        "slug": slug,
        "title": slug.title(),
        "type": "plan",
        "status": "active",
        "impl": 0.0,
        "depends_on": [],
        "sprint": None,
        "effort": "M",
        "roi": "high",
        "blocking": [],
        "gates": [{"id": "evidence", "verdict": "passed"}],
        "followups": [{"id": "next", "status": "open"}],
    }
    plan.update(overrides)
    return plan


def _roadmap(*plans: dict) -> dict:
    return build_roadmap("sample", list(plans), [])


def _ready(report: dict) -> list[str]:
    return [row["slug"] for row in report["ready_now"]]


def _row(report: dict, slug: str) -> dict:
    return next(row for row in report["ready_now"] if row["slug"] == slug)


def test_the_after_edge_keeps_its_plan_ready_rather_than_blocking_it() -> None:
    report = _roadmap(
        _plan("aa-consumer", after=["mm-unshipped"]),
        _plan("zz-ready"),
        _plan("mm-unshipped"),
    )
    ready = _ready(report)

    # Both stay in the ready set: the after target has not shipped and the
    # carrier is still work a coordinator may start.
    assert "aa-consumer" in ready
    assert "zz-ready" in ready
    row = _row(report, "aa-consumer")
    assert row["ready"] is True
    assert row["readiness"] == "ready"
    assert row["dependency_ready"] is True
    assert row["gate_blockers"] == []
    assert "aa-consumer" not in [blocked["slug"] for blocked in report["blocked"]]


def test_the_after_edge_orders_its_plan_behind_the_rest_of_the_ready_set() -> None:
    report = _roadmap(
        _plan("aa-consumer", after=["mm-unshipped"]),
        _plan("zz-ready"),
        _plan("mm-unshipped"),
    )
    ready = _ready(report)
    # The carrier sorts last only if the soft edge, not the slug, decides:
    # alphabetically it would come first.
    assert ready.index("zz-ready") < ready.index("aa-consumer")


def test_the_ready_row_names_the_targets_it_is_sequenced_behind() -> None:
    report = _roadmap(
        _plan("aa-consumer", after=["mm-unshipped"]),
        _plan("mm-unshipped"),
    )
    row = _row(report, "aa-consumer")
    assert row["came_after"] == ["mm-unshipped"]
    assert [after_row["slug"] for after_row in row["after"]] == ["mm-unshipped"]
    assert row["after"][0]["satisfied"] is False
    assert row["after_hold"] is True
    reasons = {item["slug"]: item["reason"] for item in report["immediate_roadmap"]}
    assert "mm-unshipped" in reasons["aa-consumer"]


def test_depends_on_to_the_same_target_is_still_blocked() -> None:
    report = _roadmap(
        _plan("aa-consumer", after=["mm-unshipped"]),
        _plan("hh-hard", depends_on=["mm-unshipped"]),
        _plan("mm-unshipped"),
    )
    assert "aa-consumer" in _ready(report)
    assert "hh-hard" not in _ready(report)
    assert "hh-hard" in [blocked["slug"] for blocked in report["blocked"]]


def test_a_landed_after_target_neither_ranks_nor_hints() -> None:
    report = _roadmap(
        _plan("aa-consumer", after=["mm-landed"]),
        _plan("mm-landed", status="done"),
    )
    row = _row(report, "aa-consumer")
    assert row["after_hold"] is False
    assert row["came_after"] == []
    assert row["after"][0]["satisfied"] is True


def test_an_unresolvable_after_edge_warns_and_holds_nothing() -> None:
    report = _roadmap(_plan("aa-consumer", after=["ghost"]))
    row = _row(report, "aa-consumer")
    assert row["after_hold"] is False
    assert row["came_after"] == []
    findings = [
        finding
        for finding in report["wiring_findings"]
        if finding["code"] == "unresolved-after-edge"
    ]
    assert [finding["severity"] for finding in findings] == ["warn"]


def test_the_after_meta_round_trips_through_the_plan_html(tmp_path: Path) -> None:
    document = (
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="consumer">'
        '<meta name="plan-after" content="nova:spine#s2,plain-slug">'
        "<title>Consumer</title></head><body></body></html>"
    )
    state = read_state(document)
    assert state["after"] == ["nova:spine#s2", "plain-slug"]
    assert 'name="plan-after"' in write_state(document, state)

    path = tmp_path / "consumer.html"
    path.write_text(document, encoding="utf-8")
    assert parse_meta(path)["after"] == ["nova:spine#s2", "plain-slug"]


def test_after_carries_the_plan_ref_grammar_at_the_write_boundary() -> None:
    assert "after" in LINK_LIST_FIELDS
    state = PlanState.model_validate(
        {
            "project": "sample",
            "slug": "consumer",
            "title": "Consumer",
            "after": ["nova:spine#s2", "plain-slug"],
        }
    )
    state.validate_for_write()
    assert state.canonical_dump()["after"] == ["nova:spine#s2", "plain-slug"]

    malformed = PlanState.model_validate(
        {
            "project": "sample",
            "slug": "consumer",
            "title": "Consumer",
            "after": ["nova:spine:s2"],
        }
    )
    with pytest.raises(ValueError, match="after"):
        malformed.validate_for_write()
