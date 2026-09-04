import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.serve import discover_plans
from tests.spa_browser_harness import (
    BrowserProbeError,
    ServedSpa,
    installed_browser,
    write_file_spa_document,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def rendered_browser(tmp_path_factory) -> str:
    browser = installed_browser()
    if browser is None:
        pytest.skip("no supported browser binary is installed")
    capability_root = tmp_path_factory.mktemp("home-browser-capability")
    page = capability_root / "ready.html"
    page.write_text("<!doctype html><html><body>ready</body></html>", encoding="utf-8")
    context = ServedSpa(browser=browser, url=page.as_uri(), tmp_path=capability_root)
    try:
        assert context.run_probe("document.body.textContent") == "ready"
    except BrowserProbeError as error:
        pytest.skip(f"browser unavailable ({error.classification}): {error}")
    return browser


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


@contextmanager
def _file_home(tmp_path: Path, browser: str):
    page = write_file_spa_document(tmp_path / "home-index.html", _composed_state())
    document = page.read_text(encoding="utf-8")
    home_styles = (ROOT / "docs" / "ui" / "home.css").read_text(encoding="utf-8")
    page.write_text(
        document.replace("</head>", f"<style>{home_styles}</style></head>"),
        encoding="utf-8",
    )
    yield ServedSpa(browser=browser, url=f"{page.as_uri()}#home", tmp_path=tmp_path)


def _fleet_preload() -> str:
    now = datetime.now(UTC)
    projects = {
        "projects": [
            {
                "project": "alpha",
                "data": {
                    "projects": [
                        {
                            "project": "alpha",
                            "plans_count": 3,
                            "active": 1,
                            "blocked": 1,
                            "pending": 0,
                            "shipped": 1,
                            "last_edited": (now - timedelta(hours=1)).isoformat(),
                            "activity30": [0] * 27 + [1, 2, 3],
                            "active_sprint": {"id": "focus", "theme": "Current work"},
                        }
                    ]
                },
            },
            {
                "project": "beta",
                "data": {
                    "projects": [
                        {
                            "project": "beta",
                            "plans_count": 2,
                            "active": 1,
                            "blocked": 0,
                            "pending": 1,
                            "shipped": 0,
                            "last_edited": (now - timedelta(days=1)).isoformat(),
                            "activity30": [],
                            "active_sprint": None,
                        }
                    ]
                },
            },
            {
                "project": "quiet",
                "data": {
                    "projects": [
                        {
                            "project": "quiet",
                            "plans_count": 0,
                            "active": 0,
                            "blocked": 0,
                            "pending": 0,
                            "shipped": 0,
                            "last_edited": "",
                            "activity30": [],
                            "active_sprint": None,
                        }
                    ]
                },
            },
            {
                "project": "hidden",
                "data": {
                    "projects": [
                        {
                            "project": "hidden",
                            "plans_count": 7,
                            "active": 4,
                            "blocked": 2,
                            "pending": 0,
                            "shipped": 1,
                            "last_edited": now.isoformat(),
                            "activity30": [4],
                            "active_sprint": None,
                        }
                    ]
                },
            },
        ]
    }
    runs = {
        "runs": [
            {
                "project": "alpha",
                "run_id": "shown",
                "plan": "live-plan",
                "section": "delivery",
                "phase": "working",
                "elapsed_seconds": 300,
            },
            {
                "project": "hidden",
                "run_id": "excluded",
                "plan": "secret",
                "phase": "working",
            },
        ]
    }
    inventories = {
        "alpha": {
            "inventory": [
                {
                    "slug": "landed",
                    "title": "Fresh evidence",
                    "type": "evidence",
                    "edited": (now - timedelta(hours=2)).isoformat(),
                }
            ]
        },
        "beta": {"inventory": []},
        "quiet": {"inventory": []},
        "hidden": {"inventory": []},
    }
    return rf"""
      localStorage.setItem('reckon:hidden-projects', JSON.stringify(['hidden']));
      const homeNativeFetch = window.fetch.bind(window);
      const homeProjects = {json.dumps(projects)};
      const homeRuns = {json.dumps(runs)};
      const homeInventories = {json.dumps(inventories)};
      window.fetch = (resource, options) => {{
        const url = String(resource);
        if (url.endsWith('/_projects/index.json')) return Promise.resolve(new Response(JSON.stringify(homeProjects), {{status:200,headers:{{'Content-Type':'application/json'}}}}));
        if (url.endsWith('/crew')) return Promise.resolve(new Response(JSON.stringify(homeRuns), {{status:200,headers:{{'Content-Type':'application/json'}}}}));
        const match = url.match(/\/_discover\/(alpha|beta|quiet|hidden)$/);
        if (match) return Promise.resolve(new Response(JSON.stringify(homeInventories[match[1]]), {{status:200,headers:{{'Content-Type':'application/json'}}}}));
        return homeNativeFetch(resource, options);
      }};
    """


@pytest.mark.parametrize("viewport_width", [1374, 1920])
def test_home_has_no_horizontal_scroll_and_lower_panels_share_a_row(
    tmp_path: Path,
    rendered_browser: str,
    viewport_width: int,
) -> None:
    probe = """(() => {
      const root = document.documentElement;
      const panels = [...document.querySelectorAll('.r-home-lower > .r-home-panel')].map(panel => panel.getBoundingClientRect());
      return {
        clientWidth: root.clientWidth,
        scrollWidth: root.scrollWidth,
        panelTops: panels.map(box => box.top),
        panelWidths: panels.map(box => box.width),
        rowCount: document.querySelectorAll('.r-home-project').length,
        scope: document.querySelector('.r-home-scope')?.textContent.trim(),
        noActivity: document.querySelectorAll('.r-home-no-activity').length,
        polylines: document.querySelectorAll('.r-home-streak polyline').length,
        dormant: document.querySelector('.r-home-dormant > button')?.textContent.trim(),
        runs: document.querySelectorAll('.r-home-flight article').length,
      };
    })()"""
    with _file_home(tmp_path, rendered_browser) as spa:
        geometry = spa.run_probe(
            probe,
            viewport=(viewport_width, 900),
            preload_expression=_fleet_preload(),
            ready_expression="document.querySelectorAll('.r-home-project').length === 2",
        )

    assert geometry["scrollWidth"] == geometry["clientWidth"], geometry
    assert len(geometry["panelTops"]) == 2
    assert max(geometry["panelTops"]) - min(geometry["panelTops"]) <= 1
    assert min(geometry["panelWidths"]) > 350
    assert geometry["rowCount"] == 2
    assert geometry["scope"] == "3 of 4 shown"
    assert geometry["noActivity"] == 1
    assert geometry["polylines"] == 1
    assert geometry["dormant"].startswith("1 visible projects with no recorded work")
    assert geometry["runs"] == 1


def test_dormant_strip_is_not_authored_for_zero_dormant_projects() -> None:
    source = (ROOT / "docs" / "ui" / "home.jsx").read_text(encoding="utf-8")
    assert "dormant.length > 0 &&" in source
