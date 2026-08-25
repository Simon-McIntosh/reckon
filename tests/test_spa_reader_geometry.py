import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.spa_browser_harness import (
    installed_browser,
    served_spa,
    temporary_browser_profile,
)
from tests.test_spa_rendered_semantics import INDEX_STATE, NODE_PROBE, PLAN_HTML

ROOT = Path(__file__).parents[1]
READER_CSS = (ROOT / "docs" / "ui" / "reader.css").read_text()
PLAN_SOURCE = (ROOT / "docs" / "ui" / "plan.jsx").read_text()


def _declarations(selector: str) -> dict[str, str]:
    pattern = rf"(?:^|}})\s*{re.escape(selector)}\s*\{{([^}}]+)\}}"
    match = re.search(pattern, READER_CSS, flags=re.MULTILINE)
    assert match, f"missing CSS rule for {selector}"
    return {
        name.strip(): value.strip()
        for declaration in match.group(1).split(";")
        if ":" in declaration
        for name, value in [declaration.split(":", 1)]
    }


def test_reader_column_uses_the_canvas_flex_geometry() -> None:
    rule = _declarations(".r-reader-with-attachments > .r-body")

    assert rule["flex"] == "1"
    assert rule["min-width"] == "300px"
    assert rule["overflow"] == "auto"
    assert rule["padding"] == "22px 26px 34px"


def test_reader_headings_and_metadata_use_the_declared_type_scale() -> None:
    headings = _declarations(".r-reading:not(.is-focus-mode) h2,\n.r-reading:not(.is-focus-mode) .r-plan-html h2")
    metadata = _declarations(".r-reading-controls")
    buttons = _declarations(".r-reading-controls button")

    assert headings["font-weight"] == "600"
    assert headings["font-size"] == "23px"
    assert headings["letter-spacing"] == "-0.015em"
    assert metadata["font-family"] == "var(--mono)"
    assert metadata["font-size"] == "11.5px"
    assert buttons["font-size"] == "11.5px"
    assert buttons["padding"] == "2px 8px"
    assert buttons["flex"] == "none"


def test_focus_mode_is_a_full_viewport_surface_with_canvas_reading_measure() -> None:
    surface = _declarations(".r-reading.is-focus-mode")
    header = _declarations(".r-reading.is-focus-mode .r-reading-controls")
    viewport = _declarations(".r-reading.is-focus-mode .r-reading-viewport")
    content = _declarations(".r-reading.is-focus-mode .r-reading-content")
    title = _declarations(".r-reading-focus-title h1")

    assert surface["position"] == "fixed"
    assert surface["inset"] == "0"
    assert surface["display"] == "flex"
    assert surface["flex-direction"] == "column"
    assert header["padding"] == "10px 20px"
    assert header["border-bottom"] == "1px solid var(--line)"
    assert header["font-size"] == "11.5px"
    assert viewport["flex"] == "1"
    assert viewport["overflow"] == "auto"
    assert content["max-width"] == "720px"
    assert content["padding"] == "56px 24px 120px"
    assert title["font-size"] == "32px"
    assert title["letter-spacing"] == "-0.022em"


def test_focus_header_paging_and_attachment_bars_keep_existing_navigation_paths() -> None:
    assert 'className="r-reading-position" role="status"' in PLAN_SOURCE
    assert 'className="r-reading-paging"' in PLAN_SOURCE
    assert "onPage?.(-1)" in PLAN_SOURCE
    assert "onPage?.(1)" in PLAN_SOURCE
    assert 'className="r-reader-attachment-bars"' in PLAN_SOURCE
    assert 'onNav?.({ view: "plan", slug: key })' in PLAN_SOURCE

    bars = _declarations(".r-reader-attachment-bars")
    cards = _declarations(".r-reader-attachment-entries > button")
    assert bars["display"] == "flex"
    assert bars["flex-direction"] == "column"
    assert cards["padding"] == "12px 14px"
    assert cards["gap"] == "9px"


def test_titlebar_omits_absent_metadata_and_remains_one_line(tmp_path: Path) -> None:
    browser = installed_browser()
    if browser is None:
        pytest.skip("rendered titlebar checks require an installed Chromium browser")

    docs = tmp_path / "docs"
    plans = docs / "plans"
    state = docs / "state" / "reckon"
    plans.mkdir(parents=True)
    state.mkdir(parents=True)
    (docs / "index.html").symlink_to(ROOT / "docs" / "index.html")
    (plans / "rendered-contract.html").write_text(PLAN_HTML, encoding="utf-8")
    (state / "index.json").write_text(json.dumps(INDEX_STATE), encoding="utf-8")

    probe = r"""(() => {
      const row = document.querySelector(".r-titlebar > .row2");
      const bareDashFields = [...row.querySelectorAll(".meta-item")]
        .map(item => ({
          key: item.querySelector(".k")?.textContent.trim() || "",
          value: item.querySelector(".v")?.textContent.trim() || "",
        }))
        .filter(item => item.key && (item.value === "-" || item.value === "—"));
      return {
        height: row.getBoundingClientRect().height,
        bareDashFields,
      };
    })()"""

    measurements = {}
    with served_spa(tmp_path, browser, docs=docs, route="#plan/rendered-contract") as context:
        for width in (1374, 1920):
            configured_probe = NODE_PROBE.replace(
                '"--window-size=1374,900"',
                f'"--window-size={width},900"',
            ).replace("width: 1374,", f"width: {width},")
            if width != 1374:
                assert configured_probe != NODE_PROBE
            with temporary_browser_profile(tmp_path) as profile:
                result = subprocess.run(
                    [
                        "node",
                        "--input-type=module",
                        "-e",
                        configured_probe,
                        json.dumps(
                            {
                                "browser": browser,
                                "profile": str(profile),
                                "url": context.url,
                                "waitSelector": ".r-titlebar > .row2",
                                "probe": probe,
                                "removeSignal": "undefined",
                                "failPlanHtml": False,
                                "fixtureIndex": INDEX_STATE,
                            }
                        ),
                    ],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
            assert result.returncode == 0, result.stderr
            measurements[width] = json.loads(result.stdout)["baseline"]

    assert measurements[1374]["bareDashFields"] == []
    assert measurements[1920]["bareDashFields"] == []
    print(json.dumps(measurements, sort_keys=True))
    assert measurements[1374]["height"] == pytest.approx(
        measurements[1920]["height"],
        abs=4,
    )
