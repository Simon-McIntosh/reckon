from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from reckon.serve import discover_plans
from tests.spa_browser_harness import installed_browser_or_skip
from tests.spa_containment_harness import file_spa_with_bootstrap

ROOT = Path(__file__).resolve().parents[1]
VIEWPORT = (1374, 900)


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


def _vertical_probe() -> str:
    return r"""window.__measureVerticalScroll = async () => {
      const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
      const settle = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));

      async function waitFor(predicate, description, timeoutMs = 12000) {
        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
          if (predicate()) { await settle(); return; }
          await delay(50);
        }
        throw new Error(`timed out waiting for ${description} at ${location.hash}`);
      }

      function routeHash(route) {
        if (route.indexHash) return `#${route.indexHash}`;
        if (route.index.view === 'sprint' && route.index.sprint === null) return '#sprints';
        return `#${route.index.view}`;
      }

      function workScrollSelector(route) {
        if (route.index.view === 'sprint') return '.r-sprint-view .r-body';
        if (route.index.view === 'graph') return '.r-graph';
        return `.r-${route.index.view}-view`;
      }

      const published = window.ReckonShell.route;
      const inventory = window.STATE.inventory || [];
      const cases = [
        ...published.ARTIFACT_ROUTES.map(route => ({
          name: `${route.key}-index`,
          hash: routeHash(route),
          selector: '.r-artifact-feed',
        })),
        ...published.WORK_TABS.map(route => ({
          name: route.key,
          hash: routeHash(route),
          selector: workScrollSelector(route),
        })),
        ...published.ARTIFACT_ROUTES.map(route => {
          const item = inventory.find(candidate => (candidate.type || 'plan') === route.key);
          if (!item) throw new Error(`fixture has no ${route.key} reader item`);
          return {
            name: `${route.key}-reader`,
            hash: `#${route.readerHash}/${encodeURIComponent(item.nav_key || item.slug)}`,
            selector: '.r-reading-viewport',
          };
        }),
      ];

      function flexAncestors(target) {
        const rows = [];
        let current = target.parentElement;
        while (current) {
          const style = getComputedStyle(current);
          if (style.display === 'flex' || style.display === 'inline-flex') {
            rows.push({className: current.className, minHeight: style.minHeight});
          }
          if (current.classList.contains('r-app')) break;
          current = current.parentElement;
        }
        return rows;
      }

      function measure(name, target) {
        const ancestors = flexAncestors(target);
        return {
          name,
          hash: location.hash,
          scrollHeight: target.scrollHeight,
          clientHeight: target.clientHeight,
          documentScrollHeight: document.documentElement.scrollHeight,
          viewportHeight: innerHeight,
          flexAncestors: ancestors,
          flexAncestorsAllZero: ancestors.every(row => row.minHeight === '0px'),
        };
      }

      async function visit(candidate) {
        location.hash = candidate.hash;
        await waitFor(
          () => location.hash === candidate.hash && Boolean(document.querySelector(candidate.selector)),
          `${candidate.name} (${candidate.selector})`,
        );
        const target = document.querySelector(candidate.selector);
        const excess = document.createElement('div');
        excess.dataset.verticalScrollFixture = candidate.name;
        excess.style.cssText = 'display:block;flex:none;width:1px;height:1800px;pointer-events:none';
        target.append(excess);
        await settle();
        const result = measure(candidate.name, target);
        excess.remove();
        return result;
      }

      const results = [];
      for (const candidate of cases) results.push(await visit(candidate));

      const brokenHost = document.createElement('div');
      brokenHost.style.cssText = 'position:fixed;left:0;top:0;width:20px;height:100px;overflow:hidden';
      const brokenAncestor = document.createElement('div');
      brokenAncestor.style.cssText = 'display:flex;flex-direction:column;width:100%;height:100%';
      const brokenTarget = document.createElement('div');
      brokenTarget.style.cssText = 'display:block;flex:none;overflow:auto';
      const brokenContent = document.createElement('div');
      brokenContent.style.cssText = 'height:1800px;width:1px';
      brokenTarget.append(brokenContent);
      brokenAncestor.append(brokenTarget);
      brokenHost.append(brokenAncestor);
      document.querySelector('.r-app').append(brokenHost);
      await settle();
      const deliberatelyUnfixed = measure('deliberately-unfixed', brokenTarget);
      brokenHost.remove();

      return {
        publishedArtifactRouteCount: published.ARTIFACT_ROUTES.length,
        publishedWorkRouteCount: published.WORK_TABS.length,
        cases,
        results,
        deliberatelyUnfixed,
      };
    }"""


def _assert_scrolls_within_viewport(row: Mapping[str, object]) -> None:
    assert row["scrollHeight"] > row["clientHeight"], row
    assert row["documentScrollHeight"] == row["viewportHeight"] == VIEWPORT[1], row
    assert row["flexAncestors"], row
    assert row["flexAncestorsAllZero"], row


def test_every_published_view_and_reader_scrolls_within_viewport(
    tmp_path: Path,
) -> None:
    browser = installed_browser_or_skip()
    state = _composed_state()
    with file_spa_with_bootstrap(tmp_path, browser, state) as spa:
        result = spa.run_probe(
            "window.__measureVerticalScroll()",
            viewport=VIEWPORT,
            preload_expression=_vertical_probe(),
            ready_expression="Boolean(window.STATE && document.querySelector('.r-app'))",
        )

    assert len(result["cases"]) == (
        result["publishedArtifactRouteCount"] * 2 + result["publishedWorkRouteCount"]
    )
    for row in result["results"]:
        _assert_scrolls_within_viewport(row)

    with pytest.raises(AssertionError):
        _assert_scrolls_within_viewport(result["deliberatelyUnfixed"])
