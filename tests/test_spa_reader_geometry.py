import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.spa_browser_harness import (
    BrowserProbeError,
    file_spa,
    installed_browser,
    run_browser_probe,
    served_spa,
    temporary_browser_profile,
)
from tests.test_spa_rendered_semantics import INDEX_STATE, NODE_PROBE, PLAN_HTML

ROOT = Path(__file__).parents[1]
READER_CSS = (ROOT / "docs" / "ui" / "reader.css").read_text()
PLAN_SOURCE = (ROOT / "docs" / "ui" / "plan.jsx").read_text()
TITLE_SOURCE = (ROOT / "docs" / "ui" / "shell-titlebar.jsx").read_text()


@pytest.fixture(scope="module")
def rendered_browser(tmp_path_factory) -> str:
    browser = installed_browser()
    if browser is None:
        pytest.skip("no supported browser binary is installed")
    try:
        run_browser_probe(
            tmp_path_factory.mktemp("browser-capability"),
            browser,
            "<!doctype html><html><body>ready</body></html>",
            "document.body.textContent",
        )
    except BrowserProbeError as error:
        pytest.skip(f"browser unavailable ({error.classification}): {error}")
    return browser


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
    headings = _declarations(
        ".r-reading:not(.is-focus-mode) h2,\n.r-reading:not(.is-focus-mode) .r-plan-html h2"
    )
    metadata = _declarations(".r-reading-controls")
    buttons = _declarations(".r-reading-controls button")

    assert headings["font-weight"] == "600"
    assert headings["font-size"] == "23px"
    assert headings["letter-spacing"] == "-0.015em"
    assert metadata["font-family"] == "var(--mono)"
    assert metadata["font-size"] == "11.5px"
    assert buttons["font-size"] == "11.5px"
    assert buttons["padding"] == "4px 10px"
    assert buttons["flex"] == "none"


def test_focus_mode_is_a_full_viewport_surface_with_canvas_reading_measure() -> None:
    surface = _declarations(".r-reading.is-focus-mode")
    header = _declarations(".r-reading.is-focus-mode .r-reading-controls")
    viewport = _declarations(".r-reading.is-focus-mode .r-reading-viewport")
    content = _declarations(".r-reading.is-focus-mode .r-reading-content")

    assert surface["position"] == "fixed"
    assert surface["inset"] == "0"
    assert surface["display"] == "flex"
    assert surface["flex-direction"] == "column"
    assert surface["z-index"] == "70"
    assert header["padding"] == "12px 22px"
    controls = _declarations(".r-reading-controls")
    assert controls["border-bottom"] == "1px solid var(--line)"
    assert controls["font-size"] == "11.5px"
    assert viewport["flex"] == "1"
    assert viewport["overflow"] == "auto"
    assert content["max-width"] == "820px"
    assert content["padding"] == "36px 24px 120px"


def test_focus_header_paging_and_attachment_bars_keep_existing_navigation_paths() -> (
    None
):
    assert 'className="r-reading-position" role="status"' in TITLE_SOURCE
    assert 'className="r-reading-paging"' in TITLE_SOURCE
    assert "onStep(-1)" in TITLE_SOURCE
    assert "onStep(1)" in TITLE_SOURCE
    assert 'className="r-reader-attachment-bars"' in PLAN_SOURCE
    assert 'onNav?.({ view: "plan", slug: key })' in PLAN_SOURCE

    bars = _declarations(".r-reader-attachment-bars")
    cards = _declarations(".r-reader-attachment-entries > button")
    assert bars["display"] == "flex"
    assert bars["flex-direction"] == "column"
    assert cards["padding"] == "12px 14px"
    assert cards["gap"] == "9px"


def _reader_state() -> dict[str, object]:
    inventory = [
        {
            "slug": "first",
            "title": "First active",
            "type": "plan",
            "status": "active",
            "effective_status": "active",
            "impl": 0.2,
            "sprint": "current",
            "created": 30,
        },
        {
            "slug": "second",
            "title": "Second active",
            "type": "plan",
            "status": "active",
            "effective_status": "active",
            "impl": 0.3,
            "sprint": "current",
            "created": 20,
        },
        {
            "slug": "third",
            "title": "Third active",
            "type": "plan",
            "status": "active",
            "effective_status": "active",
            "impl": 0.4,
            "sprint": "current",
            "created": 10,
        },
        {
            "slug": "hidden",
            "title": "Pending row",
            "type": "plan",
            "status": "pending",
            "effective_status": "pending",
            "impl": 0,
            "sprint": "current",
            "created": 40,
        },
    ]
    return {
        "project": "reckon",
        "projects": [{"project": "reckon", "plans_count": len(inventory)}],
        "inventory": inventory,
        "plans": {item["slug"]: item for item in inventory},
        "sprints": [{"id": "current", "status": "active", "items": []}],
        "milestones": [],
        "north_stars": [],
        "timeline": [],
        "blockers": [],
        "attachment_relations": [],
    }


def test_rendered_reader_owns_the_viewport_and_steps_the_published_list(
    tmp_path: Path, rendered_browser: str
) -> None:
    probe = r"""(async () => {
      const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
      const waitFor = async predicate => {
        const deadline = performance.now() + 3000;
        while (performance.now() < deadline) {
          if (predicate()) return true;
          await delay(25);
        }
        return false;
      };
      const activeCreatedOrder = ['first', 'second', 'third'].map(slug => window.STATE.plans[slug]);
      window.ReckonShell.title.publishRenderedReaderList('plan', activeCreatedOrder);
      await waitFor(() => document.querySelector('.r-reading-position')?.textContent.trim() === '2 / 3');

      const paletteInput = document.createElement('input');
      paletteInput.className = 'r-cmdk-input';
      document.body.appendChild(paletteInput);
      paletteInput.focus();
      document.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}));
      await delay(50);
      const hashWhilePaletteFocused = location.hash;
      paletteInput.remove();

      document.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}));
      const stepped = await waitFor(() => location.hash === '#plan/third');
      const readout = document.querySelector('.r-reading-position')?.textContent.trim();

      document.dispatchEvent(new KeyboardEvent('keydown', {key: 'f', bubbles: true}));
      const focused = await waitFor(() => document.querySelector('.r-reading')?.dataset.focusMode === 'true');
      const reader = document.querySelector('.r-reading');
      const rect = reader.getBoundingClientRect();
      const topbarPointOwner = document.elementFromPoint(18, 23)?.closest('.r-reading') === reader;
      document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
      const restored = await waitFor(() => document.querySelector('.r-reading')?.dataset.focusMode === 'false');
      return {
        appliedStatus: JSON.parse(localStorage.getItem('reckon:reckon:filters')).status[0],
        appliedSort: localStorage.getItem('reckon:reckon:groupBy'),
        hashWhilePaletteFocused,
        stepped,
        readout,
        focused,
        rect: {left: rect.left, top: rect.top, width: rect.width, height: rect.height},
        viewport: {width: innerWidth, height: innerHeight},
        topbarPointOwner,
        restored,
      };
    })()"""
    preload = r"""
localStorage.setItem('reckon:reckon:filters', JSON.stringify({status: ['active']}));
localStorage.setItem('reckon:reckon:groupBy', 'created');
"""
    with file_spa(
        tmp_path,
        rendered_browser,
        _reader_state(),
        route="#plan/second",
    ) as spa:
        result = spa.run_probe(
            probe,
            viewport=(1374, 900),
            ready_expression="Boolean(document.querySelector('.r-reading-trail-current'))",
            preload_expression=preload,
        )

    assert result["appliedStatus"] == "active"
    assert result["appliedSort"] == "created"
    assert result["hashWhilePaletteFocused"] == "#plan/second"
    assert result["stepped"] is True
    assert result["readout"] == "3 / 3"
    assert result["focused"] is True
    assert result["rect"] == {
        "left": 0,
        "top": 0,
        "width": result["viewport"]["width"],
        "height": result["viewport"]["height"],
    }
    assert result["topbarPointOwner"] is True
    assert result["restored"] is True


def test_reader_metadata_omits_absent_values_and_remains_one_line(
    tmp_path: Path, rendered_browser: str
) -> None:
    browser = rendered_browser

    docs = tmp_path / "docs"
    plans = docs / "plans"
    state = docs / "state" / "reckon"
    plans.mkdir(parents=True)
    state.mkdir(parents=True)
    (docs / "index.html").symlink_to(ROOT / "docs" / "index.html")
    (plans / "rendered-contract.html").write_text(PLAN_HTML, encoding="utf-8")
    (state / "index.json").write_text(json.dumps(INDEX_STATE), encoding="utf-8")

    probe = r"""(() => {
      const row = document.querySelector(".r-reading-metadata");
      const bareDashFields = [...row.querySelectorAll(":scope > span")]
        .map(item => item.textContent.trim())
        .filter(value => value.endsWith(" -") || value.endsWith(" —"));
      return {
        height: row.getBoundingClientRect().height,
        bareDashFields,
      };
    })()"""

    measurements = {}
    with served_spa(
        tmp_path, browser, docs=docs, route="#plan/rendered-contract"
    ) as context:
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
                                "waitSelector": ".r-reading-metadata",
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
