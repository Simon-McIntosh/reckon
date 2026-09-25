"""An authored after edge reaches every roadmap reader, not only the parser.

A plan declares its relations as meta tags and the roadmap reads them off the
inventory rows it is handed. Two builders stand between the two — the
discovery scan and the MCP inventory normaliser — and a relation a builder
omits is invisible to every reader downstream while both call paths look
healthy. These cases drive each real path from a synthesised docs tree and
assert the edge on what the readers report: the carrier stays in the ready
set, ranks behind unsequenced work, and names the target it is sequenced
behind. The rows are never hand-built, so each case fails when its own
builder stops carrying the edge.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import mcp
from reckon.roadmap import build_roadmap
from reckon.serve import discover_plans

_PLAN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{slug}">
<meta name="plan-status" content="{status}">
<meta name="plan-impl" content="{impl}">
<meta name="plan-effort" content="M">
<meta name="plan-roi" content="high">
{relations}
<title>{slug}</title></head><body><main class="plan-doc"></main></body></html>
"""

#: Two ready plans and the unshipped target one of them is sequenced behind.
_PLANS = {
    "aa-consumer": (
        "active",
        "0.1",
        '<meta name="plan-after" content="mm-unshipped">',
    ),
    "zz-ready": ("active", "0.1", ""),
    "mm-unshipped": ("planned", "0.0", ""),
}


@pytest.fixture()
def docs_tree(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    (docs / "plans").mkdir(parents=True)
    for slug, (status, impl, relations) in _PLANS.items():
        (docs / "plans" / f"{slug}.html").write_text(
            _PLAN_HTML.format(slug=slug, status=status, impl=impl, relations=relations),
            encoding="utf-8",
        )
    return docs


def _row(rows: list[dict], slug: str) -> dict:
    return next(row for row in rows if row.get("slug") == slug)


def _order(rows: list[dict]) -> list[str]:
    return [str(row["slug"]) for row in rows]


def test_the_discovery_path_carries_the_after_edge_into_the_http_roadmap(
    docs_tree: Path,
) -> None:
    discovered = discover_plans(docs_tree, "sample", None)
    assert _row(discovered["inventory"], "aa-consumer")["after"] == ["mm-unshipped"]

    report = build_roadmap(
        "sample",
        discovered["inventory"],
        discovered["sprints"],
        active_sprint_id=discovered.get("active_sprint_id"),
        project_manifest=discovered,
        review={},
    )
    ready = _row(report["ready_now"], "aa-consumer")
    assert ready["came_after"] == ["mm-unshipped"]
    assert ready["after_hold"] is True
    # The edge orders and never blocks: both carriers stay in ready work.
    assert {"aa-consumer", "zz-ready"} <= set(_order(report["ready_now"]))
    assert "aa-consumer" not in _order(report["blocked"])
    assert _order(report["immediate_roadmap"]).index("aa-consumer") > _order(
        report["immediate_roadmap"]
    ).index("zz-ready")

    # The projection the discovery payload serves to the browser reads the same.
    projection = discovered["ready_set"]["ready"]
    assert "mm-unshipped" in _row(projection, "aa-consumer")["reason"]
    assert _order(projection).index("aa-consumer") > _order(projection).index(
        "zz-ready"
    )


def test_the_mcp_roadmap_carries_the_after_edge_through_its_inventory_rows(
    docs_tree: Path,
) -> None:
    root = docs_tree.parent
    discovered = mcp._discover_project("sample", str(root))
    rows = [mcp._inventory_row(item) for item in discovered["inventory"]]
    assert _row(rows, "aa-consumer")["after"] == ["mm-unshipped"]

    report = mcp._roadmap("sample", checkout_path=str(root))
    assert "ready_now" in report, report
    ready = _row(report["ready_now"], "aa-consumer")
    assert ready["came_after"] == ["mm-unshipped"]
    assert ready["after_hold"] is True
    assert "aa-consumer" not in _order(report["blocked"])
    assert _order(report["immediate_roadmap"]).index("aa-consumer") > _order(
        report["immediate_roadmap"]
    ).index("zz-ready")
