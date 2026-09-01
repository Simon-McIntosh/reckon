from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from unittest import SkipTest

from reckon._plan_html import parse_meta
from reckon.mcp import _roadmap
from reckon.serve import discover_plans, load_mounts
from tests.spa_browser_harness import (
    file_spa,
    installed_browser_or_skip,
    temporary_browser_profile,
)

ROOT = Path(__file__).resolve().parents[1]
GRAPH = ROOT / "docs" / "ui" / "graph.jsx"
CAPTURE_INDEX = ROOT / "docs/figures/spa-surface-redesign/after/capture-index.json"
AUTHORITIES = (
    {"project": "nova", "handle": "hexgrid"},
    {"project": "reckon", "handle": "sprint-federation"},
)
PUBLICATION_WIDTHS = (1374, 1920)
VIEWPORT_HEIGHT = 900


def _evaluate_graph_helpers(inventory: list[dict], endpoint: str) -> dict:
    source = GRAPH.read_text(encoding="utf-8")
    helpers = source.split("function PathPromptModal", 1)[0]
    script = f"""
const inventory = {json.dumps(inventory)};
{helpers}
const measure = _dependencyChainMeasure(inventory);
const closure = _dependencyClosure({json.dumps(endpoint)}, measure.bySlug);
const members = [...closure].map(slug => measure.bySlug[slug]);
const view = _graphHandleView(measure.bySlug[{json.dumps(endpoint)}], members,
  measure.pathLen[{json.dumps(endpoint)}], "alpha");
console.log(JSON.stringify({{view, chains: _allDependencyChains(inventory)}}));
"""
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


def _authority_state(report: dict, captured_at: str) -> tuple[dict, str]:
    project = report["endpoint"]["project"]
    docs = load_mounts()[project]
    discovered = discover_plans(docs, project, None)
    member_slugs = {member["slug"] for member in report["members"]}
    inventory = [
        {**item, "project": project}
        for item in discovered["inventory"]
        if item["slug"] in member_slugs
    ]
    found = {item["slug"] for item in inventory}
    if found != member_slugs:
        raise AssertionError(
            f"composed state omitted roadmap members: {member_slugs - found}"
        )
    endpoint_slug = report["endpoint"]["slug"]
    authored_handle = (
        parse_meta(docs / "plans" / f"{endpoint_slug}.html").get("graph_handle") or ""
    )
    next(item for item in inventory if item["slug"] == endpoint_slug)[
        "graph_handle"
    ] = authored_handle
    state = {
        "today": captured_at[:10],
        "project": project,
        "projects": [{"project": project, "plans_count": len(inventory)}],
        "milestones": discovered.get("milestones", []),
        "north_stars": discovered.get("north_stars", []),
        "inventory": inventory,
        "source_format": discovered.get("source_format", "unknown"),
        "resource_versions": discovered.get("resource_versions", {}),
        "loaded_at": captured_at,
        "active_sprint_id": discovered.get("active_sprint_id"),
        "active_sprints": [],
        "active_sprint_conflict": False,
        "sprints": discovered.get("sprints", []),
        "sprint": None,
        "blockers": discovered.get("blockers", []),
        "timeline": discovered.get("timeline", []),
        "attachment_relations": [],
        "plans": {item["slug"]: item for item in inventory},
    }
    return state, authored_handle


MEASUREMENT_EXPRESSION = """(() => {
  const rect = element => {
    const value = element.getBoundingClientRect();
    return {x: value.x, top: value.top, right: value.right, bottom: value.bottom,
      width: value.width, height: value.height};
  };
  const nodes = [...document.querySelectorAll('.r-graph-node-card')];
  return {
    nodeCardCount: nodes.length,
    handleToken: document.querySelector('.r-graph-handle-token')?.textContent || '',
    emptyMessage: document.querySelector('.r-graph-empty')?.textContent || '',
    viewport: {width: innerWidth, height: innerHeight},
    document: {clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth},
    app: rect(document.querySelector('.r-app')),
    view: rect(document.querySelector('.r-canvas-view')),
    canvas: rect(document.querySelector('.r-graph-canvas-stage')),
    nodeCards: nodes.map(node => ({title: node.querySelector('strong')?.textContent || '',
      status: node.querySelector('span')?.textContent || '', ...rect(node)}))
  };
})()"""


def _screenshot(context, browser: str, destination: Path, width: int) -> bytes:
    with temporary_browser_profile(destination.parent) as profile:
        result = subprocess.run(
            [
                browser,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--hide-scrollbars",
                "--run-all-compositor-stages-before-draw",
                "--virtual-time-budget=3000",
                f"--user-data-dir={profile}",
                f"--window-size={width},{VIEWPORT_HEIGHT}",
                f"--screenshot={destination}",
                context.url,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    if result.returncode:
        raise RuntimeError(f"screenshot failed: {result.stderr.strip()}")
    image = destination.read_bytes()
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AssertionError("browser screenshot is not a PNG")
    return image


def _owned_browser_processes(scratch: Path) -> list[int]:
    marker = os.fsencode(str(scratch))
    owned = []
    for command_line in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            content = command_line.read_bytes()
        except OSError:
            continue
        if marker in content and b"--user-data-dir=" in content:
            owned.append(int(command_line.parent.name))
    return owned


def _capture_authority(
    scratch: Path, browser: str, report: dict, captured_at: str
) -> tuple[list[dict], dict]:
    state, authored_handle = _authority_state(report, captured_at)
    expected_count = len(report["members"])
    captures = []
    with file_spa(
        scratch,
        browser,
        state,
        project=report["endpoint"]["project"],
        route="#graph",
    ) as context:
        for width in PUBLICATION_WIDTHS:
            geometry = context.run_probe(
                MEASUREMENT_EXPRESSION,
                viewport=(width, VIEWPORT_HEIGHT),
                ready_expression=(
                    "document.querySelectorAll('.r-graph-node-card').length === "
                    f"{expected_count} && "
                    "document.querySelector('.r-graph-handle-token')?.textContent === "
                    f"{json.dumps(authored_handle)}"
                ),
            )
            image = _screenshot(
                context,
                browser,
                scratch / f"{report['handle']}-{width}.png",
                width,
            )
            captures.append(
                {
                    "view": "graph",
                    "status": "populated",
                    "project": report["endpoint"]["project"],
                    "handle": report["handle"],
                    "authoredHandle": authored_handle,
                    "endpoint": report["endpoint"],
                    "width": width,
                    "nodeCardCount": geometry["nodeCardCount"],
                    "roadmapMemberCount": expected_count,
                    "emptyStateVisible": bool(geometry["emptyMessage"]),
                    "geometry": geometry,
                    "image": {
                        "mediaType": "image/png",
                        "sha256": hashlib.sha256(image).hexdigest(),
                        "bytes": len(image),
                        "data": "data:image/png;base64,"
                        + base64.b64encode(image).decode(),
                    },
                }
            )

    without_handle = deepcopy(state)
    for item in without_handle["inventory"]:
        item.pop("graph_handle", None)
    without_handle["plans"] = {
        item["slug"]: item for item in without_handle["inventory"]
    }
    with file_spa(
        scratch,
        browser,
        without_handle,
        project=report["endpoint"]["project"],
        route="#graph",
    ) as context:
        negative = context.run_probe(
            """(() => ({
              nodeCardCount: document.querySelectorAll('.r-graph-node-card').length,
              handleToken: document.querySelector('.r-graph-handle-token')?.textContent || '',
              emptyMessage: document.querySelector('.r-graph-empty')?.textContent || ''
            }))()""",
            viewport=(PUBLICATION_WIDTHS[0], VIEWPORT_HEIGHT),
            ready_expression="Boolean(document.querySelector('.r-graph-empty'))",
        )
    return captures, {
        "project": report["endpoint"]["project"],
        "removedHandle": authored_handle,
        **negative,
    }


def build_capture_index() -> dict:
    browser = installed_browser_or_skip()
    captured_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    captures, controls, reports = [], [], []
    with tempfile.TemporaryDirectory(prefix="graph-capture-") as temporary:
        scratch = Path(temporary)
        for authority in AUTHORITIES:
            report = _roadmap(f"graph:{authority['handle']}", view="raw")
            if report["endpoint"]["project"] != authority["project"]:
                raise AssertionError(f"graph authority moved: {report['endpoint']}")
            reports.append(
                {
                    "target": report["target"],
                    "handle": report["handle"],
                    "endpoint": report["endpoint"],
                    "memberCount": len(report["members"]),
                    "members": report["members"],
                    "completion": report["completion"],
                }
            )
            rendered, control = _capture_authority(
                scratch, browser, report, captured_at
            )
            captures.extend(rendered)
            controls.append(control)
        residue = {
            "chromeProcesses": len(_owned_browser_processes(scratch)),
            "profileDirectories": len(list(scratch.glob("browser-profile-*"))),
            "documentDirectories": len(list(scratch.glob("file-spa-*"))),
        }
        if any(residue.values()):
            raise AssertionError(f"browser residue remains: {residue}")
    return {
        "browser": Path(browser).name,
        "capturedAt": captured_at,
        "delivery": "self-contained-file-url",
        "viewportHeight": VIEWPORT_HEIGHT,
        "publicationWidths": list(PUBLICATION_WIDTHS),
        "roadmapAuthorities": reports,
        "captures": captures,
        "negativeControls": controls,
        "residual": residue,
    }


def test_graph_handle_derives_membership_authority_and_ship_hold() -> None:
    result = _evaluate_graph_helpers(
        [
            {
                "slug": "source",
                "project": "beta",
                "status": "shipped",
                "depends_on": [],
                "decisions": [],
            },
            {
                "slug": "middle",
                "project": "alpha",
                "status": "active",
                "depends_on": ["beta:source"],
                "decisions": [{"key": "scope", "choice": "", "chosen": ""}],
            },
            {
                "slug": "endpoint",
                "project": "alpha",
                "title": "Release endpoint",
                "status": "pending",
                "graph_handle": "release",
                "depends_on": ["middle"],
                "decisions": [],
            },
        ],
        "endpoint",
    )
    view = result["view"]
    assert view["handle"] == "release"
    assert view["shipLine"] == "/reckon-ship release"
    assert {member["slug"] for member in view["members"]} == {
        "source",
        "middle",
        "endpoint",
    }
    assert view["repositories"] == [
        {"repository": "alpha", "count": 2},
        {"repository": "beta", "count": 1},
    ]
    assert view["openDecisions"] == 1
    assert view["structuralDepth"] == 3
    assert view["averageWidth"] == "1.00"
    assert result["chains"][0][-1] == "endpoint"
    source = GRAPH.read_text(encoding="utf-8")
    assert "disabled={graphHandle.openDecisions > 0}" in source
    assert "Derived closure membership" in source
    assert "repositories enter scope only through closure membership" in source


def test_hop_count_is_labelled_as_structural_not_execution_ordering() -> None:
    source = GRAPH.read_text(encoding="utf-8")
    assert "longest dependency chain by hop count" in source
    assert "structural depth only; not execution ordering" in source
    assert "earliest finish" not in source


def test_capture_index_proves_both_live_graph_authorities_are_populated() -> None:
    evidence = json.loads(CAPTURE_INDEX.read_text(encoding="utf-8"))
    expected = {
        (authority["project"], authority["handle"], width)
        for authority in AUTHORITIES
        for width in PUBLICATION_WIDTHS
    }
    captures = evidence["captures"]
    assert len(captures) == len(expected)
    assert {
        (row["project"], row["handle"], row["width"]) for row in captures
    } == expected
    for capture in captures:
        assert capture["status"] == "populated"
        assert capture["nodeCardCount"] > 0
        assert capture["nodeCardCount"] == capture["roadmapMemberCount"]
        assert capture["geometry"]["nodeCardCount"] == capture["nodeCardCount"]
        assert capture["geometry"]["handleToken"] == capture["authoredHandle"]
        assert capture["authoredHandle"] == capture["handle"]
        assert capture["geometry"]["viewport"]["width"] == capture["width"]
        assert capture["emptyStateVisible"] is False
        assert capture["geometry"]["emptyMessage"] == ""
        assert capture["image"]["data"].startswith("data:image/png;base64,")
        image = base64.b64decode(capture["image"]["data"].split(",", 1)[1])
        assert capture["image"]["bytes"] == len(image)
        assert capture["image"]["sha256"] == hashlib.sha256(image).hexdigest()
    assert all(control["emptyMessage"] for control in evidence["negativeControls"])
    assert all(
        control["nodeCardCount"] == 0 for control in evidence["negativeControls"]
    )
    assert all(control["handleToken"] == "" for control in evidence["negativeControls"])
    assert evidence["residual"] == {
        "chromeProcesses": 0,
        "profileDirectories": 0,
        "documentDirectories": 0,
    }


if __name__ == "__main__":
    try:
        index = build_capture_index()
    except SkipTest as error:
        print(f"SKIP: {error}")
        raise SystemExit(77) from error
    CAPTURE_INDEX.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "path": str(CAPTURE_INDEX),
                "captures": len(index["captures"]),
                "counts": {
                    row["handle"]: row["memberCount"]
                    for row in index["roadmapAuthorities"]
                },
                "negativeControls": len(index["negativeControls"]),
                "residual": index["residual"],
            },
            sort_keys=True,
        )
    )
