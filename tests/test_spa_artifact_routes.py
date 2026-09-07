from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
ROUTE_SOURCE = ROOT / "docs/ui/shell-route.jsx"


def _evaluate_routes(hashes: list[str] | None = None) -> dict[str, object]:
    source = ROUTE_SOURCE.read_text(encoding="utf-8")
    script = f"""
global.React = {{
  useCallback: value => value,
  useEffect: () => undefined,
  useMemo: value => value(),
  useRef: value => ({{current: value}}),
  useState: value => [typeof value === "function" ? value() : value, () => undefined],
}};
global.window = {{location: {{hash: ""}}, ReckonShell: {{}}}};
{source}
const publishedCases = ARTIFACT_ROUTES.flatMap(route => {{
  const readerSlug = `group/route-${{route.key}}`;
  return [
    {{hash: `#${{route.indexHash}}`, expected: {{view: route.key, slug: null}}}},
    {{
      hash: `#${{route.readerHash}}/${{encodeURIComponent(readerSlug)}}`,
      expected: {{view: route.key, slug: readerSlug}},
    }},
  ];
}});
const requestedHashes = {json.dumps(hashes)};
const cases = requestedHashes === null
  ? publishedCases
  : requestedHashes.map(hash => ({{hash, expected: null}}));
const routes = cases.map(candidate => {{
  const hash = candidate.hash;
  window.location.hash = hash;
  return parseHash();
}});
console.log(JSON.stringify({{
  routes,
  expectedRoutes: cases.map(candidate => candidate.expected),
  publishedKinds: ARTIFACT_ROUTES.map(route => route.key),
  tabs: ARTIFACT_TABS.map(tab => ({{label: tab.label, index: tab.index}})),
  expectedTabs: ARTIFACT_ROUTES.map(route => ({{
    label: route.label,
    index: {{view: route.key, slug: null}},
  }})),
}}));
"""
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_artifact_hashes_and_tabs_publish_one_route_contract() -> None:
    evaluated = _evaluate_routes()

    assert evaluated["routes"] == evaluated["expectedRoutes"]
    assert evaluated["tabs"] == evaluated["expectedTabs"]
    assert {route["view"] for route in evaluated["routes"]} == set(
        evaluated["publishedKinds"]
    )
    assert {tab["index"]["view"] for tab in evaluated["tabs"]} == set(
        evaluated["publishedKinds"]
    )


def test_unknown_hash_falls_back_to_home() -> None:
    evaluated = _evaluate_routes(["#not-a-reckon-view"])

    assert evaluated["routes"] == [{"view": "home"}]
