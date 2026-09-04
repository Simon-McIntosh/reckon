from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
ROUTE_SOURCE = ROOT / "docs/ui/shell-route.jsx"


def _evaluate_routes(hashes: list[str]) -> dict[str, object]:
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
const hashes = {json.dumps(hashes)};
const routes = hashes.map(hash => {{
  window.location.hash = hash;
  return parseHash();
}});
console.log(JSON.stringify({{
  routes,
  tabs: ARTIFACT_TABS.map(tab => ({{label: tab.label, index: tab.index}})),
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
    hashes = [
        "#plans",
        "#research",
        "#evidence",
        "#figures",
        "#plan/route-plan",
        "#research/route-research",
        "#evidence/route-evidence",
        "#figure/group%2Froute-figure.png",
    ]
    evaluated = _evaluate_routes(hashes)

    assert evaluated["routes"] == [
        {"view": "plan", "slug": None},
        {"view": "research", "slug": None},
        {"view": "evidence", "slug": None},
        {"view": "figure", "slug": None},
        {"view": "plan", "slug": "route-plan"},
        {"view": "research", "slug": "route-research"},
        {"view": "evidence", "slug": "route-evidence"},
        {"view": "figure", "slug": "group/route-figure.png"},
    ]
    assert evaluated["tabs"] == [
        {"label": "Plans", "index": {"view": "plan", "slug": None}},
        {"label": "Research", "index": {"view": "research", "slug": None}},
        {"label": "Evidence", "index": {"view": "evidence", "slug": None}},
        {"label": "Figures", "index": {"view": "figure", "slug": None}},
    ]


def test_unknown_hash_falls_back_to_home() -> None:
    evaluated = _evaluate_routes(["#not-a-reckon-view"])

    assert evaluated["routes"] == [{"view": "home"}]
