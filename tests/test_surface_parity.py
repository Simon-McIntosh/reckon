"""The derived-value parity gate: rendered equals returned, field for field.

For the one fixture project in ``spa_parity_harness`` these tests compare the
value the SPA renders against the value the Python reader returns — never
against a literal on either side:

- each sprint's derived state and the drift flag rendered beside it;
- the endpoint set, with every closure's membership and completion counts,
  through both graph adapters (the payload reformatter and the raw-inventory
  closure recomputation);
- each project's rollup counts and activity-series length;
- the schedule's lane count and the chain far end.

The comparison is N value pairs (8 sprint, 10 per endpoint across both
adapters plus the two endpoint-set memberships, 2 schedule, 6 rollup). Every
mismatch message names the two sources it compares.

The companion case is what makes this a check rather than a mirror: patch
exactly one value on one side of any pair and the comparison must report a
mismatch.
"""

from __future__ import annotations

import copy
import json
from html import escape
from pathlib import Path

import pytest

from tests import spa_parity_harness as harness

_REFERENCE = harness.REFERENCE


def _case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_rollup: bool = True):
    inventory, sprints, runs = harness.fixture_inventory()
    rollup_dir = harness._rollup_repo(tmp_path) if with_rollup else None
    returned = harness.returned_read(
        inventory=inventory,
        sprints=sprints,
        runs=runs,
        rollup_dir=rollup_dir,
        monkeypatch=monkeypatch,
    )
    rendered = harness.rendered_read(returned=returned, inventory=inventory)
    pairs = harness.build_pairs(rendered, returned)
    return rendered, returned, pairs


def _messages(pairs, dimension):
    return [
        pair.message
        for pair in harness.mismatches(pairs)
        if pair.dimension == dimension
    ]


def test_sprint_derived_state_and_drift_flag_equal_the_roadmap_return(
    tmp_path, monkeypatch
) -> None:
    _rendered, _returned, pairs = _case(tmp_path, monkeypatch, with_rollup=False)
    messages = _messages(pairs, "sprint")
    assert messages == [], "\n".join(messages)


def test_endpoint_closures_and_membership_counts_equal_the_roadmap_return(
    tmp_path, monkeypatch
) -> None:
    _rendered, _returned, pairs = _case(tmp_path, monkeypatch, with_rollup=False)
    messages = _messages(pairs, "endpoint")
    assert messages == [], "\n".join(messages)


def test_rollup_counts_and_activity_series_length_equal_the_fleet_return(
    tmp_path, monkeypatch
) -> None:
    _rendered, _returned, pairs = _case(tmp_path, monkeypatch, with_rollup=True)
    messages = _messages(pairs, "rollup")
    assert messages == [], "\n".join(messages)


def test_schedule_lane_count_and_chain_far_end_equal_the_schedule_return(
    tmp_path, monkeypatch
) -> None:
    _rendered, _returned, pairs = _case(tmp_path, monkeypatch, with_rollup=True)
    messages = _messages(pairs, "schedule")
    assert messages == [], "\n".join(messages)


def test_read_plan_resolves_a_section_wait_through_the_target_plan(tmp_path):
    """The production path resolves readiness from the target, not its status.

    The dependency rows here are the ones ``read_plan`` builds, resolved from
    two authored plans: a terminal plan that declares no ``s9`` and an open
    whose ``s1`` is declared done. The first is a ref to a section that does
    not exist, which the roadmap holds whatever the plan's status says; the
    second is satisfied. A reader that tests the row's status clears both, so
    the two rows are the disagreement and its control.
    """

    import reckon.mcp as mcp_module
    from reckon import mcp_views
    from reckon._plan_html import write_state

    project = "sample"
    root = tmp_path / project
    docs = root / "docs"
    docs.mkdir(parents=True)

    def plan(slug: str, *, status: str, declarations=None, section_depends_on=None):
        state = {
            "slug": slug,
            "title": slug,
            "summary": "A plan whose sections can wait on their own.",
            "version": 3,
            "type": "plan",
            "status": status,
            "impl": 0.4,
        }
        if declarations:
            state["section_declarations"] = dict(declarations)
        bare = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="docs-project" content="{project}">'
            f"<title>{slug}</title></head>"
            '<body><main class="plan-doc"></main></body></html>'
        )
        html = write_state(bare, state)
        if section_depends_on:
            meta = (
                '<meta name="plan-section-depends-on" content="'
                + escape(json.dumps(section_depends_on), quote=True)
                + '">'
            )
            html = html.replace("<title>", meta + "<title>", 1)
        (docs / f"{slug}.html").write_text(html, encoding="utf-8")
        return html

    plan(
        "target",
        status="shipped",
        declarations={"s1": "done"},
    )
    owner_html = plan(
        "owner",
        status="active",
        declarations={"s5": "implementable", "s6": "implementable"},
        section_depends_on={"s5": ["target#s9"], "s6": ["target#s1"]},
    )

    rows = mcp_module._section_dependency_rows(owner_html, project, str(root))
    assert sorted(row["ref"] for row in rows) == ["target#s1", "target#s9"]
    published = {
        row["section"]: row["ready"]
        for row in mcp_views._section_blocking(rows, checkout=str(root))
    }

    assert published == {"s5": False}, (
        "a wait on a section the target never declares must hold, and a wait "
        f"on a declared-done section must not; published {published!r} from "
        f"rows {[dict(row) for row in rows]!r}"
    )


def test_section_readiness_is_the_roadmap_verdict_not_the_row_status():
    """read_plan's section readiness is the roadmap's verdict for every edge.

    The rows below are the shape the roadmap resolves a section-scoped edge
    into: a status naming the target plan or the section's own declaration,
    and the roadmap's own ``satisfied`` verdict beside it. The two disagree on
    the first row — a shipped plan that declares no ``s9`` — so a reader that
    tests the status clears a wait the roadmap keeps. Every other row agrees,
    which is what makes the first one a disagreement rather than a fixture.
    """

    from reckon import mcp_views
    from reckon.roadmap import section_readiness_verdict

    rows = [
        {
            "ref": "plan-shipped#s9",
            "source_section": "s5",
            "stage": "s9",
            "scope": "local",
            "project": "sample",
            "slug": "plan-shipped",
            "found": True,
            "status": "shipped",
            "section_found": False,
            "satisfied": False,
        },
        {
            "ref": "plan-active#s2",
            "source_section": "s6",
            "stage": "s2",
            "scope": "local",
            "project": "sample",
            "slug": "plan-active",
            "found": True,
            "status": "done",
            "section_found": True,
            "satisfied": True,
        },
        {
            "ref": "plan-active#s3",
            "source_section": "s7",
            "stage": "s3",
            "scope": "local",
            "project": "sample",
            "slug": "plan-active",
            "found": True,
            "status": "implementable",
            "section_found": True,
            "satisfied": False,
        },
    ]

    published = {
        row["section"]: row["ready"] for row in mcp_views._section_blocking(rows)
    }
    pairs: list[harness.Pair] = []
    for waiting in sorted({str(row["source_section"]) for row in rows}):
        unmet = [
            dict(row)
            for row in rows
            if str(row["source_section"]) == waiting and not row["satisfied"]
        ]
        owner = section_readiness_verdict(
            waiting,
            plan_ready=True,
            plan_blockers=(),
            section_blockers=unmet,
        )
        pairs.append(
            harness.Pair(
                "section",
                f"{waiting} readiness",
                "read_plan(view='summary')['section_blocking'] readiness",
                "section_readiness_verdict(..., section_blockers=unmet)['ready']",
                published.get(waiting, True),
                owner["ready"],
            )
        )

    messages = [pair.message for pair in harness.mismatches(pairs)]
    assert messages == [], "\n".join(messages)


def test_every_surface_agrees_and_the_pair_count_is_stated(
    tmp_path, monkeypatch
) -> None:
    _rendered, returned, pairs = _case(tmp_path, monkeypatch, with_rollup=True)

    endpoint_count = len(returned["endpoints"])
    expected = 8 + (10 * endpoint_count + 2) + 2 + 6
    assert len(pairs) == expected, (
        f"pair count drifted from {expected} ({endpoint_count} endpoints) to "
        f"{len(pairs)}; update the stated digit"
    )
    assert all(
        "rendered" in pair.message and "≠ returned" in pair.message
        for pair in pairs
    )

    messages = [pair.message for pair in harness.mismatches(pairs)]
    assert messages == [], "\n".join(messages)


# ── The companion: a mutation on one side must bring a mismatch ─────────────


def _patch_and_rebuild(base_rendered, base_returned, mutate) -> list[harness.Pair]:
    rendered = copy.deepcopy(base_rendered)
    returned = copy.deepcopy(base_returned)
    mutate(rendered, returned)
    return harness.build_pairs(rendered, returned)


def _sprint_state_rendered(rendered, _returned):
    rendered["sprints"]["scaffold"]["state"] = "shipped"


def _sprint_drift_returned(_rendered, returned):
    scaffold = next(row for row in returned["sprints"] if row["id"] == "scaffold")
    scaffold["state_drift"]["stored"] = "active"


def _endpoint_closure_returned(_rendered, returned):
    named_deep = next(row for row in returned["endpoints"] if row["slug"] == "named-deep")
    named_deep["completion"]["total"] += 1


def _rollup_count_returned(_rendered, returned):
    returned["project_rows"][0]["plans_count"] += 1


def _rollup_summary_rendered(rendered, _returned):
    rendered["rollup"]["summary"]["active"] += 1


def _schedule_lane_returned(_rendered, returned):
    returned["schedule"]["lane_count"] = 999


@pytest.mark.parametrize(
    "mutate",
    [
        _sprint_state_rendered,
        _sprint_drift_returned,
        _endpoint_closure_returned,
        _rollup_count_returned,
        _rollup_summary_rendered,
        _schedule_lane_returned,
    ],
    ids=[
        "sprint-state-rendered",
        "sprint-drift-returned",
        "endpoint-closure-returned",
        "rollup-count-returned",
        "rollup-summary-rendered",
        "schedule-lane-returned",
    ],
)
def test_a_single_patched_value_on_one_side_makes_the_comparison_fail(
    tmp_path, monkeypatch, mutate
) -> None:
    rendered, returned, pairs = _case(tmp_path, monkeypatch, with_rollup=True)

    assert not harness.mismatches(pairs), "the undisturbed fixture must agree"
    patched_pairs = _patch_and_rebuild(rendered, returned, mutate)

    bad = harness.mismatches(patched_pairs)
    assert bad, (
        f"mutation {mutate.__name__} changed a value yet the parity check "
        f"reported agreement — the check cannot distinguish a real drift from "
        f"a comparison of a value with itself"
    )
    for pair in bad:
        assert "rendered" in pair.message and "≠ returned" in pair.message, pair.message
        assert pair.dimension in {"sprint", "endpoint", "rollup", "schedule"}
