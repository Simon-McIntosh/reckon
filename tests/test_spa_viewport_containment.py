from __future__ import annotations

import os
from pathlib import Path

import pytest

from reckon.serve import discover_plans
from tests.spa_browser_harness import installed_browser_or_skip
from tests.spa_containment_harness import (
    OFFSCREEN_MARKER,
    VIEWPORT_WIDTHS,
    aggregate_verdict,
    assert_horizontally_contained,
    file_spa_with_bootstrap,
    routable_surfaces,
    run_containment_probe,
    write_verdict,
)

ROOT = Path(__file__).resolve().parents[1]


def _composed_state() -> dict[str, object]:
    state = discover_plans(ROOT / "docs", "reckon", ROOT / "docs" / "state")
    inventory = state.get("inventory", [])
    active = [
        sprint
        for sprint in state.get("sprints", [])
        if sprint.get("status") == "active"
    ]
    return {
        **state,
        "project": "reckon",
        "projects": [{"project": "reckon", "plans_count": len(inventory)}],
        "active_sprints": active,
        "active_sprint_conflict": len(active) > 1,
        "plans": {item["slug"]: item for item in inventory},
    }


def test_every_routable_surface_is_horizontally_contained(tmp_path: Path) -> None:
    browser = installed_browser_or_skip()
    state = _composed_state()
    expected_surfaces = {surface["name"] for surface in routable_surfaces(state)} | {
        "overlay-project-picker",
        "overlay-visibility-sheet",
        "overlay-command-palette",
        "harness-cases",
    }
    with file_spa_with_bootstrap(tmp_path, browser, state) as spa:
        width_results = [
            run_containment_probe(spa, state, width) for width in VIEWPORT_WIDTHS
        ]

    verdict = aggregate_verdict(width_results)
    destination = Path(
        os.environ.get(
            "RECKON_VIEWPORT_VERDICT",
            tmp_path / "spa-viewport-containment-verdict.json",
        )
    )
    write_verdict(destination, verdict)

    assert verdict["widths"] == list(VIEWPORT_WIDTHS)
    for width in VIEWPORT_WIDTHS:
        walked = {
            row["name"] for row in verdict["surfaces_walked"] if row["width"] == width
        }
        not_measured = {
            row["name"]
            for row in verdict["surfaces_not_measured"]
            if row["width"] == width
        }
        assert walked | not_measured == expected_surfaces
        assert not_measured <= {"overlay-command-palette"}

    for row in verdict["surfaces_not_measured"]:
        assert row["reason"] == (
            "the authored search action did not render .r-cmdk within 2000ms"
        )
        assert len(row["diagnostic"]["prior_attempts"]) == 4
        assert row["diagnostic"]["route"] == "#plans"
        assert row["diagnostic"]["button"]["displayed"] is True
        assert "runtime_exceptions" in row["diagnostic"]

    for width in VIEWPORT_WIDTHS:
        harness = next(
            row
            for row in verdict["surface_verdicts"]
            if row["width"] == width and row["name"] == "harness-cases"
        )
        shifted = [
            row
            for row in harness["violations"]
            if row["selector"] == "#containment-shifted-case"
        ]
        assert shifted == [
            {
                "selector": "#containment-shifted-case",
                "left_overflow_px": 0,
                "right_overflow_px": 40,
                "boundary": "viewport",
            }
        ]
        with pytest.raises(
            AssertionError,
            match=r"#containment-shifted-case: left 0px, right 40px past viewport",
        ):
            assert_horizontally_contained(shifted)
        assert not any(
            row["selector"] == "#containment-scroll-content"
            for row in harness["violations"]
        )
        assert {
            "selector": "#containment-offscreen-case",
            "marker": f"{OFFSCREEN_MARKER}=offscreen",
        } in harness["exemptions"]

    real_violations = [
        row for row in verdict["violations"] if row["surface"] != "harness-cases"
    ]
    try:
        assert_horizontally_contained(real_violations)
    except AssertionError as error:
        raise AssertionError(f"{error}; full verdict: {destination}") from error
