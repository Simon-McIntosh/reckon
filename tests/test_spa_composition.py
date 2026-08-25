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


def test_served_spa_composes_the_active_view_inside_its_container(tmp_path: Path) -> None:
    browser = installed_browser()
    if browser is None:
        pytest.skip(
            "SPA composition check requires an installed browser; tried "
            + ", ".join(BROWSER_NAMES)
        )

    with served_spa(tmp_path, browser) as context:
        passing = context.run_composition_probe()
        conflicting = context.run_composition_probe("--conflicting-width", "1600")

    assert passing.returncode == 0, passing.stderr
    geometry = json.loads(passing.stdout)
    assert geometry["visibleViewCount"] == 1
    assert geometry["app"]["width"] == pytest.approx(1374, abs=1)
    assert geometry["view"]["width"] == pytest.approx(geometry["app"]["width"], abs=1)
    assert geometry["view"]["top"] == pytest.approx(geometry["topbar"]["bottom"], abs=1)
    assert geometry["view"]["bottom"] == pytest.approx(geometry["app"]["bottom"], abs=1)

    assert conflicting.returncode == 1
    assert "composed container geometry mismatch" in conflicting.stderr
    assert "app width: 1600, expected 1374" in conflicting.stderr
