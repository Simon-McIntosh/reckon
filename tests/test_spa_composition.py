from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.spa_browser_harness import (
    BROWSER_NAMES,
    ServedSpa,
    installed_browser,
    served_spa,
)

VIEWPORT_WIDTHS = (1280, 1440, 1920)


def test_sprint_surface_consumes_composed_review_without_a_review_fetch() -> None:
    source = (Path(__file__).parents[1] / "docs/ui/sprint.jsx").read_text()

    assert "const review = M.review || null" in source
    assert "review.sprint_order" in source
    assert "row.metrics || {}" in source
    assert "review.priority || []" in source
    assert "review?.findings || []" in source
    assert "fetch(`/review" not in source
    assert "DOMParser" not in source


def test_sprint_surface_keeps_natural_fallback_and_landed_priority_tail() -> None:
    source = (Path(__file__).parents[1] / "docs/ui/sprint.jsx").read_text()

    assert "sort(compareNaturalSprintIds)" in source
    assert "Number(left.landed) - Number(right.landed)" in source
    assert 'className={row.landed ? "landed" : ""}' in source
    for reason in ("critical-path", "unlock", "deadline", "roi", "decision-first"):
        assert reason not in source


def test_browser_harness_reports_an_absent_binary_as_a_clean_skip(tmp_path: Path) -> None:
    absent = tmp_path / "browser-is-not-installed"
    context = ServedSpa(
        browser=str(absent),
        url="http://127.0.0.1:1/",
        tmp_path=tmp_path,
    )
    result = context.run_composition_probe()

    assert result.returncode == 77
    assert result.stderr.strip() == f"SKIP: browser binary is not present: {absent}"


@pytest.mark.parametrize("viewport_width", VIEWPORT_WIDTHS)
def test_plans_surface_never_exceeds_its_window(
    tmp_path: Path,
    viewport_width: int,
) -> None:
    browser = installed_browser()
    if browser is None:
        pytest.skip(
            "SPA composition check requires an installed browser; tried "
            + ", ".join(BROWSER_NAMES)
        )

    screenshot = tmp_path / f"plans-{viewport_width}.png"
    with served_spa(tmp_path, browser, route="#plans") as context:
        passing = context.run_composition_probe(
            expected_width=viewport_width,
            screenshot=screenshot,
        )

    assert passing.returncode == 0, passing.stderr
    geometry = json.loads(passing.stdout)
    assert geometry["visibleViewCount"] == 1
    assert geometry["document"] == {
        "clientWidth": viewport_width,
        "scrollWidth": viewport_width,
    }
    assert geometry["exceedingElementCount"] == 0
    assert geometry["app"]["width"] == pytest.approx(viewport_width, abs=1)
    assert geometry["view"]["width"] == pytest.approx(geometry["app"]["width"], abs=1)
    assert geometry["view"]["top"] == pytest.approx(geometry["topbar"]["bottom"], abs=1)
    assert geometry["view"]["bottom"] == pytest.approx(geometry["app"]["bottom"], abs=1)
    assert screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_window_containment_fails_when_a_width_floor_returns(tmp_path: Path) -> None:
    browser = installed_browser()
    if browser is None:
        pytest.skip(
            "SPA composition check requires an installed browser; tried "
            + ", ".join(BROWSER_NAMES)
        )

    with served_spa(tmp_path, browser, route="#plans") as context:
        conflicting = context.run_composition_probe(
            "--conflicting-width",
            "1374",
            expected_width=1280,
            screenshot=tmp_path / "plans-width-floor.png",
        )

    assert conflicting.returncode == 1
    assert "composed container geometry mismatch" in conflicting.stderr
    assert "document scroll width: 1374, client width" in conflicting.stderr
    assert "elements past viewport:" in conflicting.stderr
