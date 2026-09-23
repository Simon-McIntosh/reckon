"""The project picker is filled from the mount list, not from the rollup.

The picker's names come from ``/_projects/mounts.json``, a static file, so the
menu is populated whether or not the computed rollup at
``/_projects/index.json`` ever answers. Other projects' inventories are fetched
only when a project is navigated to while the fleet home or the command palette
is shown, one project at a time.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.spa_browser_harness import file_spa, installed_browser_or_skip

ROOT = Path(__file__).resolve().parents[1]

# Several names so a partial or empty menu is unambiguous.
MOUNTED_PROJECTS = ("imas-codex", "imas-efit", "imas-ink", "nova", "reckon")


def _composed_state() -> dict[str, object]:
    return {
        "project": "reckon",
        "projects": [{"project": "reckon", "plans_count": 1}],
        "inventory": [],
        "plans": {},
        "sprints": [],
        "milestones": [],
        "north_stars": [],
        "timeline": [],
        "blockers": [],
        "active_sprints": [],
        "active_sprint_conflict": False,
    }


def _mocking_preload() -> str:
    """Replace the fetch layer: mounts answer, the rollup never resolves.

    ``/_projects/index.json`` returns a promise that is never settled, which is
    the reported failure: the rollup request hung and never produced a reply.
    """

    mounts = {name: f"/srv/{name}/docs" for name in MOUNTED_PROJECTS}
    return (
        "("
        "() => {\n"
        f"  const MOUNTS = {json.dumps(mounts)};\n"
        "  window.__requests = [];\n"
        "  window.__discoveryRequests = [];\n"
        "  const jsonResponse = payload => new Response(JSON.stringify(payload), {\n"
        '    status: 200, headers: { "Content-Type": "application/json" },\n'
        "  });\n"
        "  window.fetch = resource => {\n"
        "    const url = String(resource);\n"
        "    window.__requests.push(url);\n"
        '    if (url.endsWith("/_projects/mounts.json")) return Promise.resolve(jsonResponse(MOUNTS));\n'
        '    if (url.endsWith("/_projects/index.json")) return new Promise(() => {});\n'
        '    if (url.includes("/_discover/")) {\n'
        '      window.__discoveryRequests.push(url.slice(url.indexOf("/_discover/") + 11));\n'
        "      return Promise.resolve(jsonResponse({ inventory: [] }));\n"
        "    }\n"
        '    if (url.endsWith("/crew")) return Promise.resolve(jsonResponse({ runs: [] }));\n'
        "    return Promise.reject(new Error(`unmocked request: ${url}`));\n"
        "  };\n"
        "}"
        ")()"
    )


_WAIT_FOR = """
    const waitFor = async predicate => {
      const deadline = performance.now() + 3000;
      while (performance.now() < deadline) {
        if (predicate()) return true;
        await new Promise(resolve => setTimeout(resolve, 25));
      }
      return false;
    };
"""


def _picker_probe() -> str:
    """Read the picker menu names off the rendered topbar."""

    return (
        "(async () => {\n"
        + _WAIT_FOR
        + """
      await waitFor(() => document.querySelectorAll(".r-project-menu strong").length > 0);
      const rows = [...document.querySelectorAll(".r-project-menu strong")]
        .map(element => element.textContent.trim());
      return {
        rows,
        appMounted: Boolean(document.querySelector(".r-app")),
        indexRequested: window.__requests.some(url => url.endsWith("/_projects/index.json")),
      };
    })()"""
    )


def _palette_probe() -> str:
    """Count other-project inventories before and after the command palette."""

    return (
        "(async () => {\n"
        + _WAIT_FOR
        + f"""
      const expected = {len(MOUNTED_PROJECTS)};
      const before = [...window.__discoveryRequests];
      document.querySelector(".r-topbar-search").click();
      const opened = await waitFor(() => Boolean(document.querySelector(".r-cmdk")));
      await waitFor(() => new Set(window.__discoveryRequests).size >= expected);
      return {{ before, after: [...window.__discoveryRequests], opened }};
    }})()"""
    )


def test_project_list_is_immediate_without_the_rollup(tmp_path: Path) -> None:
    """With the rollup never answering, every mounted project is still listed."""

    with file_spa(
        tmp_path,
        installed_browser_or_skip(),
        _composed_state(),
        route="#plans",
    ) as spa:
        result = spa.run_probe(
            _picker_probe(),
            ready_expression=(
                'Boolean(document.querySelector(".r-app") '
                '&& document.querySelector(".r-project-menu"))'
            ),
            preload_expression=_mocking_preload(),
        )

    assert result["indexRequested"] is True
    assert result["rows"] == sorted(MOUNTED_PROJECTS)


def test_other_project_inventories_load_only_for_the_palette(tmp_path: Path) -> None:
    """A project's plan view fetches no other inventory until the palette opens."""

    with file_spa(
        tmp_path,
        installed_browser_or_skip(),
        _composed_state(),
        route="#plans",
    ) as spa:
        result = spa.run_probe(
            _palette_probe(),
            ready_expression=(
                'Boolean(document.querySelector(".r-app") '
                '&& document.querySelector(".r-topbar-search"))'
            ),
            preload_expression=_mocking_preload(),
        )

    assert result["before"] == []
    assert result["opened"] is True
    assert sorted(set(result["after"])) == sorted(MOUNTED_PROJECTS)
