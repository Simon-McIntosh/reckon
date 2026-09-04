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
const view = _graphEndpointView(measure.bySlug[{json.dumps(endpoint)}], members,
  measure.pathLen[{json.dumps(endpoint)}], "alpha");
const rows = _graphEndpointRows(inventory, "alpha");
console.log(JSON.stringify({{view, rows}}));
"""
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


def _authority_state(
    report: dict, captured_at: str, docs: Path | None = None
) -> tuple[dict, str]:
    project = report["endpoint"]["project"]
    docs = docs if docs is not None else load_mounts()[project]
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
    handleToken: document.querySelector('.r-graph-detail .r-graph-handle-token')?.textContent || '',
    indexRowCount: document.querySelectorAll('.r-graph-index-row').length,
    shipControl: document.querySelector('.r-graph-detail .r-graph-ship')?.textContent || '',
    needsHandle: document.querySelector('.r-graph-needs-handle')?.textContent || '',
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


def _rendered_graph(state: dict, width: int) -> dict:
    """Measure the Graph tab in a browser at one viewport width."""

    browser = installed_browser_or_skip()
    with tempfile.TemporaryDirectory(prefix="graph-detail-") as temporary:
        scratch = Path(temporary)
        with file_spa(
            scratch, browser, state, project="reckon", route="#graph"
        ) as context:
            return context.run_probe(
                MEASUREMENT_EXPRESSION,
                viewport=(width, VIEWPORT_HEIGHT),
                ready_expression=("Boolean(document.querySelector('.r-graph-detail'))"),
            )


def _authored_endpoint(project: str, handle: str) -> str:
    """The plan carrying an authored graph handle, read from the served docs."""

    docs = ROOT / "docs"
    for page in sorted((docs / "plans").glob("*.html")):
        if (parse_meta(page).get("graph_handle") or "") == handle:
            return page.stem
    raise AssertionError(f"no plan in {project} authors the handle {handle}")


def _closure_state(project: str, handle: str) -> tuple[dict, int]:
    """Compose SPA state for one handle's closure, and the member count.

    The count is walked here in Python from `depends_on` alone — the same
    transitive closure the roadmap reports and the surface derives — so the
    assertion compares two independent computations rather than the rendering
    against itself. It also survives the endpoint shipping, which makes the
    roadmap refuse the target while the closure is still perfectly drawable.
    """

    endpoint_slug = _authored_endpoint(project, handle)
    # This repository's own docs tree, never a mount: a test reads the
    # repository under test, and a mounted sibling can be legitimately removed.
    docs = ROOT / "docs"
    discovered = discover_plans(docs, project, None)
    by_slug = {item["slug"]: item for item in discovered["inventory"]}

    members: set[str] = set()

    def walk(slug: str) -> None:
        if slug in members or slug not in by_slug:
            return
        members.add(slug)
        for reference in by_slug[slug].get("depends_on") or []:
            walk(str(reference).split("#", 1)[0].split(":")[-1])

    walk(endpoint_slug)
    report = {
        "endpoint": {"project": project, "slug": endpoint_slug},
        "members": [{"slug": slug} for slug in sorted(members)],
    }
    captured_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    state, authored_handle = _authority_state(report, captured_at, docs)
    assert authored_handle == handle
    return state, len(members)


def _sprint_federation_state() -> tuple[dict, int]:
    return _closure_state("reckon", "sprint-federation")


def test_the_detail_draws_one_card_per_closure_member_at_both_widths() -> None:
    state, member_count = _sprint_federation_state()
    for width in PUBLICATION_WIDTHS:
        measured = _rendered_graph(state, width)
        assert measured["viewport"]["width"] == width
        assert measured["emptyMessage"] == ""
        assert measured["handleToken"] == "sprint-federation"
        # One card per closure member, at the count the roadmap reports.
        assert measured["nodeCardCount"] == member_count, (
            f"{width}px rendered {measured['nodeCardCount']} cards "
            f"for {member_count} roadmap members"
        )
        assert measured["indexRowCount"] >= 1
        assert measured["shipControl"] == "/reckon-ship graph:sprint-federation"
        assert measured["needsHandle"] == ""
        assert measured["canvas"]["width"] > 0


def test_an_unnamed_endpoint_renders_the_same_detail_without_a_ship_target() -> None:
    state, member_count = _sprint_federation_state()
    unnamed = deepcopy(state)
    for item in unnamed["inventory"]:
        item.pop("graph_handle", None)
    unnamed["plans"] = {item["slug"]: item for item in unnamed["inventory"]}

    measured = _rendered_graph(unnamed, PUBLICATION_WIDTHS[0])
    # The empty state is gone: an endpoint without a handle still renders.
    assert measured["emptyMessage"] == ""
    assert measured["handleToken"] == "unnamed"
    assert measured["nodeCardCount"] == member_count
    # No invented command; the chip names the authoring act instead.
    assert measured["shipControl"] == ""
    assert measured["needsHandle"] == "needs plan-graph-handle"


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
    assert view["named"] is True
    assert view["handle"] == "release"
    assert view["shipLine"] == "/reckon-ship graph:release"
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
    assert [row["slug"] for row in result["rows"]] == ["endpoint"]
    source = GRAPH.read_text(encoding="utf-8")
    assert "disabled={view.openDecisions > 0}" in source
    assert "Derived closure membership" in source
    assert "repositories enter scope only through closure membership" in source


UNNAMED_INVENTORY = [
    {
        "slug": "source",
        "project": "alpha",
        "title": "Source",
        "status": "shipped",
        "depends_on": [],
        "decisions": [],
    },
    {
        "slug": "middle",
        "project": "alpha",
        "title": "Middle",
        "status": "blocked",
        "depends_on": ["source"],
        "decisions": [],
    },
    {
        "slug": "endpoint",
        "project": "alpha",
        "title": "Live endpoint",
        "status": "active",
        "depends_on": ["middle"],
        "decisions": [],
    },
]


def test_an_unnamed_endpoint_derives_the_same_closure_as_a_named_one() -> None:
    unnamed = _evaluate_graph_helpers(UNNAMED_INVENTORY, "endpoint")["view"]
    named_inventory = deepcopy(UNNAMED_INVENTORY)
    named_inventory[-1]["graph_handle"] = "release"
    named = _evaluate_graph_helpers(named_inventory, "endpoint")["view"]

    # The view exists for an endpoint with no authored handle at all.
    assert unnamed is not None
    assert unnamed["named"] is False
    assert unnamed["handle"] == "unnamed"

    # Membership and every derived metric are identical: a handle names a
    # closure, it does not create one.
    assert {member["slug"] for member in unnamed["members"]} == {
        member["slug"] for member in named["members"]
    }
    for metric in (
        "total",
        "shipped",
        "shippedPercent",
        "held",
        "openDecisions",
        "structuralDepth",
        "averageWidth",
        "repositories",
    ):
        assert unnamed[metric] == named[metric], metric

    # The only difference is the ship target.
    assert unnamed["shipLine"] is None
    assert named["shipLine"] == "/reckon-ship graph:release"


def test_the_ship_control_is_replaced_by_the_missing_precondition_chip() -> None:
    source = GRAPH.read_text(encoding="utf-8")
    # Named: a copyable command. Unnamed: a chip naming the authoring act, and
    # no invented target anywhere.
    assert "{view.shipLine}" in source
    assert "needs plan-graph-handle" in source
    assert 'title="Author a graph handle on this plan' in source
    assert "view.named ? (" in source
    assert "/reckon-ship graph:${handle}" in source


def test_index_rows_list_named_endpoints_before_unnamed() -> None:
    inventory = deepcopy(UNNAMED_INVENTORY)
    inventory.append(
        {
            "slug": "named-endpoint",
            "project": "alpha",
            "title": "Named endpoint",
            "status": "pending",
            "graph_handle": "release",
            "depends_on": ["source"],
            "decisions": [],
        }
    )
    rows = _evaluate_graph_helpers(inventory, "endpoint")["rows"]
    assert [row["slug"] for row in rows] == ["named-endpoint", "endpoint"]
    assert [row["named"] for row in rows] == [True, False]
    # A row carries the same shape figures the detail shows.
    unnamed_row = rows[1]
    assert unnamed_row["total"] == 3
    assert unnamed_row["structuralDepth"] == 3
    assert unnamed_row["shippedPercent"] == 33
    # One blocked member, no open decisions.
    assert unnamed_row["flag"] == "1 held"
    assert rows[0]["flag"] == "ready"


def test_the_flag_reads_open_decisions_before_held_members() -> None:
    inventory = deepcopy(UNNAMED_INVENTORY)
    inventory[1]["decisions"] = [{"key": "scope", "choice": "", "chosen": ""}]
    rows = _evaluate_graph_helpers(inventory, "endpoint")["rows"]
    assert rows[0]["flag"] == "1 open"
    assert rows[0]["flagKind"] == "open"


def test_the_empty_state_renders_only_when_nothing_depends_on_anything() -> None:
    standalone = [
        {"slug": "one", "project": "alpha", "status": "active", "depends_on": []},
        {"slug": "two", "project": "alpha", "status": "shipped", "depends_on": []},
    ]
    assert _evaluate_graph_helpers(standalone, "one")["rows"] == []

    # One dependency is enough for an endpoint to exist, even with nothing
    # live and no handle authored anywhere.
    shipped_chain = [
        {"slug": "one", "project": "alpha", "status": "shipped", "depends_on": []},
        {"slug": "two", "project": "alpha", "status": "shipped", "depends_on": ["one"]},
    ]
    rows = _evaluate_graph_helpers(shipped_chain, "two")["rows"]
    assert [row["slug"] for row in rows] == ["two"]
    assert rows[0]["done"] is True
    source = GRAPH.read_text(encoding="utf-8")
    assert "No plan in this project depends on another" in source
    assert "rows.length === 0" in source


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
