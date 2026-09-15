"""One fixture project read through both of its surfaces.

A surface renders a value; a reader returns it. The derivation fence holds
when, for every value the SPA draws, the rendered value and the returned value
are equal: each sprint's derived state and drift, the endpoint set with each
closure's membership count, each project's rollup counts and activity-series
length, and the schedule's lane count and chain far end.

This module builds one fixture (a single project inventory carrying both the
dependency graph and the schedule chain) and renders both sides of every pair.
The rendered side is a node evaluation of the *authored JSX* helpers — no
React runtime and no re-implementation: the sprint helper slice is the one the
sprint tests evaluate, the graph helpers are the two endpoint adapters the
Graph tab uses, the schedule side is the same first-fit lane packer the crew
flow draws with, and the home side is the fleet-home summary and activity
projection. The returned side is the Python read the rest of the codebase
uses. ``build_pairs`` then compares them field for field, and every mismatch
message names its two sources.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckon import serve
from reckon.fleet_index import compute_project_row
from reckon.roadmap import build_roadmap, schedule_report

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "sample"

# Every recorded plan and live run in the schedule chain is anchored to one
# reference instant, so bars, lane count and the chain far end are the same
# numbers on any host and any day.
REFERENCE = datetime(2026, 9, 4, 4, 0, tzinfo=UTC)


# ─── The fixture ────────────────────────────────────────────────────────────


def _plan(
    slug: str,
    *,
    status: str = "pending",
    impl: float = 0.0,
    depends: list[str] | None = None,
    wall_clock_hours: float = 6.0,
    effort_hours: float = 6.0,
    sprint: str | None = None,
    handle: str | None = None,
    decisions: list[dict] | None = None,
    edited: str | None = None,
    **extra,
) -> dict:
    plan = {
        "slug": slug,
        "title": slug,
        "type": "plan",
        "project": PROJECT,
        "status": status,
        "impl": impl,
        "depends_on": depends or [],
        "effort": "M",
        "roi": "high",
        "effort_hours": effort_hours,
        "wall_clock_hours": wall_clock_hours,
        "sprint": sprint,
        "blockers": 0,
        "gates": [],
        "decisions": decisions or [],
        "followups": [],
    }
    if handle:
        plan["graph_handle"] = handle
    if edited:
        plan["edited"] = edited
    plan.update(extra)
    return plan


def fixture_inventory() -> tuple[list[dict], list[dict], list[dict]]:
    """The whole fixture project: the dependency graph that produces the
    endpoint closures over four sprints, plus the schedule chain that produces
    the lane packing and the chain far end. One inventory feeds every reader,
    exactly as a served project would."""
    inventory = [
        # ── the dependency graph ───────────────────────────────────────────
        _plan("foundation", status="shipped", impl=1.0, sprint="scaffold"),
        _plan(
            "middle",
            status="active",
            impl=0.5,
            depends=["foundation"],
            sprint="scaffold",
            decisions=[{"key": "m1"}],
        ),
        _plan(
            "named-deep",
            status="blocked",
            impl=0.2,
            depends=["middle"],
            sprint="rolling",
            handle="deep",
        ),
        _plan(
            "named-shallow",
            status="active",
            impl=0.4,
            depends=["foundation"],
            sprint="rolling",
            handle="shallow",
        ),
        _plan("unnamed-deep", status="pending", depends=["middle"], sprint="rolling"),
        _plan(
            "unnamed-left", status="active", impl=0.3, depends=["foundation"], sprint="rolling"
        ),
        _plan(
            "unnamed-right",
            status="active",
            impl=0.3,
            depends=["foundation"],
            sprint="rolling",
        ),
        _plan("wrapped", status="shipped", impl=1.0, sprint="wrapped-up"),
        # ── the schedule chain ─────────────────────────────────────────────
        _plan(
            "recorded-early",
            status="shipped",
            impl=1.0,
            wall_clock_hours=8,
            edited=(REFERENCE - timedelta(hours=30)).isoformat(),
        ),
        _plan(
            "recorded-wide",
            status="shipped",
            impl=1.0,
            wall_clock_hours=32,
            edited=(REFERENCE - timedelta(hours=10)).isoformat(),
        ),
        _plan("active-work", status="active", wall_clock_hours=8),
        _plan("pending-first", status="pending", wall_clock_hours=14, depends=["active-work"]),
        _plan("pending-second", status="draft", wall_clock_hours=12, depends=["pending-first"]),
    ]
    sprints = [
        {"id": "scaffold", "status": "planned", "items": ["foundation", "middle"]},
        {
            "id": "rolling",
            "status": "active",
            "items": [
                "named-deep",
                "named-shallow",
                "unnamed-deep",
                "unnamed-left",
                "unnamed-right",
            ],
        },
        {"id": "unfilled", "status": "planned", "items": []},
        {"id": "wrapped-up", "status": "blocked", "items": ["wrapped"]},
    ]
    runs = [
        {
            "run_id": "live-active",
            "project": PROJECT,
            "plan": "active-work",
            "role": "implement",
            "dispatched_at": (REFERENCE - timedelta(hours=4)).isoformat(),
        }
    ]
    return inventory, sprints, runs


def _rollup_repo(tmp_path: Path) -> Path:
    """A one-project repository whose docs directory carries two authored
    plans, committed inside the activity window so the fleet reader produces a
    populated 30-day activity series."""
    docs = tmp_path / "rollup" / PROJECT / "docs"
    plans = docs / "plans"
    plans.mkdir(parents=True)
    for slug in ("first", "second"):
        plans.joinpath(f"{slug}.html").write_text(
            f"""<!doctype html>
<html><head>
<meta name="docs-project" content="{PROJECT}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{slug}">
<meta name="plan-status" content="active">
</head><body><main class="plan-doc"></main></body></html>
""",
            encoding="utf-8",
        )
    repo = docs.parent
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "add", "docs/plans"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "seed two plans"],
        check=True,
        capture_output=True,
        text=True,
    )
    return docs


# ─── Rendering the authored JSX in node ────────────────────────────────────


def _surface_slice(
    path: str,
    *,
    start_marker: str | None = None,
    end_marker: str,
    strip_react_line: bool = False,
) -> str:
    source = (ROOT / path).read_text(encoding="utf-8")
    if start_marker is not None:
        source = source[source.index(start_marker):]
    if strip_react_line:
        source = source.replace(
            'const { useMemo: useHomeMemo, useState: useHomeState } = React;\n', "", 1
        ).replace('const { useEffect, useState } = React;\n', "", 1)
    return source.split(end_marker, 1)[0]


SPRINT_SLICE = _surface_slice(
    "docs/ui/sprint.jsx",
    start_marker="const CLOSED_ITEM_STATUSES",
    end_marker="function SprintDetail(",
)
GRAPH_SLICE = _surface_slice("docs/ui/graph.jsx", end_marker="const DAG_GEOMETRY")
CREW_SLICE = _surface_slice(
    "docs/ui/crew.jsx", end_marker="function DerivedFlow(", strip_react_line=True
)
HOME_SLICE = _surface_slice(
    "docs/ui/home.jsx", end_marker="function FleetHome(", strip_react_line=True
)


def _eval(slice_text: str, fixtures: dict, expression: str, *, filename: str):
    assignments = "".join(
        f"{name} = {json.dumps(value)}; " for name, value in fixtures.items()
    )
    instrumented = f"{slice_text}\n{assignments}\nconsole.log(JSON.stringify({expression}));\n"
    compiled = serve.compile_jsx(instrumented, filename=filename).decode()
    script = f"globalThis.window = globalThis;\n{compiled}"
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


# ─── The two sides of the fixture ──────────────────────────────────────────


def returned_read(
    *,
    inventory: list[dict],
    sprints: list[dict],
    runs: list[dict],
    rollup_dir: Path | None = None,
    monkeypatch=None,
) -> dict:
    """The Python reads: the roadmap's sprints and endpoints, the schedule
    report — all over one inventory — and, when a rollup directory is given,
    the fleet reader over a synthesized repository."""
    report = build_roadmap(PROJECT, inventory, sprints)
    schedule = schedule_report(PROJECT, inventory, runs, reference=REFERENCE)
    rollup_rows: list[dict] = []
    if rollup_dir is not None:
        if monkeypatch is not None:
            monkeypatch.setattr("reckon.crew.list_live", lambda **kwargs: [])
        rollup_rows = [
            # naive local, matches the commit timestamps the activity series reads
            compute_project_row(rollup_dir, PROJECT, state_root=None, now=datetime.now())  # noqa: DTZ005
        ]
    return {
        "sprints": report["sprints"],
        "endpoints": report["endpoints"],
        "schedule": schedule,
        "project_rows": rollup_rows,
    }


def rendered_read(*, returned: dict, inventory: list[dict]) -> dict:
    """The SPA's draws over the same payload: sprint state rows, the two graph
    endpoint adapters, the crew-flow lane pack, and the fleet-home summary."""

    sprint_rows = _eval(
        SPRINT_SLICE,
        {"__sprints": returned["sprints"], "__inventory": inventory},
        "(() => { const byId = new Map(sprintStateRows(__sprints, __inventory).map(r => [r.sprint.id, {state: r.state, flag: r.flag}])); return Object.fromEntries(byId.entries()); })()",
        filename="sprint_parity_probe.jsx",
    )

    endpoint_payload_rows = _eval(
        GRAPH_SLICE,
        {"__endpoints": returned["endpoints"]},
        "_roadmapEndpointRows(__endpoints).map(r => ({slug: r.slug, total: r.total, shipped: r.shipped, held: r.held, openDecisions: r.openDecisions, depth: r.structuralDepth, done: r.done}))",
        filename="graph_payload_parity_probe.jsx",
    )
    endpoint_inventory_rows = _eval(
        GRAPH_SLICE,
        {"__inventory": inventory, "__project": PROJECT},
        "_graphEndpointRows(__inventory, __project).map(r => ({slug: r.slug, total: r.total, shipped: r.shipped, held: r.held, openDecisions: r.openDecisions, depth: r.structuralDepth, done: r.done}))",
        filename="graph_inventory_parity_probe.jsx",
    )

    lane_count = _eval(
        CREW_SLICE,
        {"__bars": returned["schedule"]["bars"]},
        "flowPackLanes(__bars).length",
        filename="crew_lane_parity_probe.jsx",
    )

    project_rows = returned["project_rows"]
    if project_rows:
        home = _eval(
            HOME_SLICE,
            {"__projects": project_rows, "__runs": []},
            "(() => { const summary = homeVisibleSummary(__projects, __runs); const activity = homeActivityProjection(__projects[0].activity30); return { summary: {moving: summary.moving, plans: summary.plans, active: summary.active, held: summary.held, shipped: summary.shipped}, rows: homeProjectRows(__projects).map(p => p.project), dormant: homeDormantRows(__projects).length, activityLength: activity ? activity.line.split(' ').length : 0, activityTotal: activity ? activity.total : 0 }; })()",
            filename="home_parity_probe.jsx",
        )
    else:
        home = {
            "summary": {"moving": 0, "plans": 0, "active": 0, "held": 0, "shipped": 0},
            "rows": [],
            "dormant": 0,
            "activityLength": 0,
            "activityTotal": 0,
        }

    return {
        "sprints": sprint_rows,
        "endpoint_payload": endpoint_payload_rows,
        "endpoint_inventory": endpoint_inventory_rows,
        "schedule": {
            "lane_count": lane_count,
            "far_end_hours": returned["schedule"]["high"],
        },
        "rollup": home,
    }


# ─── The field-by-field comparison ─────────────────────────────────────────


@dataclass(frozen=True)
class Pair:
    dimension: str
    key: str
    rendered_source: str
    returned_source: str
    rendered_value: object
    returned_value: object

    @property
    def message(self) -> str:
        return (
            f"{self.dimension} {self.key}: rendered {self.rendered_source} = "
            f"{self.rendered_value!r} ≠ returned {self.returned_source} = "
            f"{self.returned_value!r}"
        )


def _endpoint_field(source: str) -> tuple[str, str]:
    """Map a rendered field name and a returned endpoint row to (rendered key,
    returned key) so each compared value is addressed by the two surfaces it
    came from."""
    return {
        "total": ("total", "completion.total"),
        "shipped": ("shipped", "completion.shipped"),
        "held": ("held", "held"),
        "openDecisions": ("openDecisions", "open_decision_count"),
        "depth": ("depth", "structural_depth"),
        "done": ("done", "shipped fraction == 1"),
    }[source]


def _endpoint_expected(endpoint: dict, returned_key: str):
    completion = endpoint.get("completion") or {}
    if returned_key == "completion.total":
        return completion.get("total")
    if returned_key == "completion.shipped":
        return completion.get("shipped")
    if returned_key == "held":
        return endpoint.get("held")
    if returned_key == "open_decision_count":
        return endpoint.get("open_decision_count")
    if returned_key == "structural_depth":
        return endpoint.get("structural_depth")
    if returned_key == "shipped fraction == 1":
        total = completion.get("total") or 0
        return total > 0 and completion.get("shipped") == total
    return endpoint.get(returned_key)


def build_pairs(rendered: dict, returned: dict) -> list[Pair]:
    """One Pair per compared value, each naming its two sources so a failing
    test can say where the disagreement sits."""

    pairs: list[Pair] = []

    # Sprint derived state and drift: the surface reads the served fields, so
    # the pair is the state and the flag it renders against the state's
    # authored-drift record the roadmap returned.
    for expected in returned["sprints"]:
        sprint_id = expected["id"]
        surfaced = rendered["sprints"].get(sprint_id, {})
        pairs.append(
            Pair(
                "sprint",
                f"{sprint_id} state",
                f"sprintStateRows(served.sprints)['{sprint_id}'].state",
                f"build_roadmap(...)['sprints'] id '{sprint_id}' derived_state",
                surfaced.get("state"),
                expected["derived_state"],
            )
        )
        drift = expected.get("state_drift")
        expected_flag = (
            f"was {drift['stored']}"
            if drift
            else (f"{expected.get('blocked', 0)} held" if expected.get("blocked") else None)
        )
        pairs.append(
            Pair(
                "sprint",
                f"{sprint_id} drift flag",
                f"sprintStateRows(served.sprints)['{sprint_id}'].flag",
                f"build_roadmap(...)['sprints'] id '{sprint_id}' state_drift",
                surfaced.get("flag"),
                expected_flag,
            )
        )

    # Endpoint closures: every returned endpoint is compared through both graph
    # adapters — the payload adapter that reformats the served rows, and the
    # inventory adapter that recomputes each closure from the raw graph, so a
    # drift in either direction between reader and surface shows up.
    by_endpoint = {row["slug"]: row for row in returned["endpoints"]}
    payload_by_slug = {row["slug"]: row for row in rendered["endpoint_payload"]}
    inventory_by_slug = {row["slug"]: row for row in rendered["endpoint_inventory"]}
    for slug, endpoint in by_endpoint.items():
        payload_row = payload_by_slug.get(slug, {})
        inventory_row = inventory_by_slug.get(slug, {})
        for rendered_key in ("total", "shipped", "held", "openDecisions", "depth", "done"):
            r_key, t_key = _endpoint_field(rendered_key)
            pairs.append(
                Pair(
                    "endpoint",
                    f"{slug} {rendered_key}",
                    f"_roadmapEndpointRows(served.endpoints)['{slug}'].{r_key}",
                    f"build_roadmap(...)['endpoints'] '{slug}' {t_key}",
                    payload_row.get(r_key),
                    _endpoint_expected(endpoint, t_key),
                )
            )
        for rendered_key in ("total", "shipped", "held", "done"):
            r_key, t_key = _endpoint_field(rendered_key)
            pairs.append(
                Pair(
                    "endpoint",
                    f"{slug} closure {rendered_key}",
                    f"_graphEndpointRows(served.inventory)['{slug}'].{r_key}",
                    f"build_roadmap(...)['endpoints'] '{slug}' {t_key}",
                    inventory_row.get(r_key),
                    _endpoint_expected(endpoint, t_key),
                )
            )

    # The endpoint *sets* must match too — a closure enumerated on only one
    # side is a drift even when every shared slug agrees.
    rendered_set_slugs = sorted(row["slug"] for row in rendered["endpoint_payload"])
    inventory_set_slugs = sorted(row["slug"] for row in rendered["endpoint_inventory"])
    returned_set_slugs = sorted(by_endpoint)
    pairs.append(
        Pair(
            "endpoint",
            "set membership (payload adapter)",
            "_roadmapEndpointRows(served.endpoints) slugs",
            "build_roadmap(...)['endpoints'] slugs",
            rendered_set_slugs,
            returned_set_slugs,
        )
    )
    pairs.append(
        Pair(
            "endpoint",
            "set membership (inventory adapter)",
            "_graphEndpointRows(served.inventory) slugs",
            "build_roadmap(...)['endpoints'] slugs",
            inventory_set_slugs,
            returned_set_slugs,
        )
    )

    # Schedule: the lane pack the crew flow draws and the chain far end its
    # axis reads.
    pairs.append(
        Pair(
            "schedule",
            "lane count",
            "flowPackLanes(served.schedule.bars).length",
            "schedule_report(...)['lane_count']",
            rendered["schedule"]["lane_count"],
            returned["schedule"]["lane_count"],
        )
    )
    pairs.append(
        Pair(
            "schedule",
            "chain far end",
            "served.schedule.high (ReckonCrewSchedule.farEnd)",
            "schedule_report(...)['far_end_hours']",
            rendered["schedule"]["far_end_hours"],
            returned["schedule"]["far_end_hours"],
        )
    )

    # Rollup: the fleet-home summary over the served project rows against the
    # fleet reader, plus the activity series both the surface projects and the
    # reader returns.
    for row in returned["project_rows"]:
        summary = rendered["rollup"]["summary"]
        for rendered_key, returned_key in (
            ("plans", "plans_count"),
            ("active", "active"),
            ("held", "blocked"),
            ("shipped", "shipped"),
        ):
            pairs.append(
                Pair(
                    "rollup",
                    f"project {returned_key}",
                    f"homeVisibleSummary(served.projects)['{rendered_key}']",
                    f"compute_project_row(...)['{returned_key}']",
                    summary[rendered_key],
                    row[returned_key],
                )
            )
        pairs.append(
            Pair(
                "rollup",
                f"project {row['project']} activity series length",
                "homeActivityProjection(served.activity30) points",
                "compute_project_row(...)['activity30'] length",
                rendered["rollup"]["activityLength"],
                len(row["activity30"]),
            )
        )
        pairs.append(
            Pair(
                "rollup",
                f"project {row['project']} activity total",
                "homeActivityProjection(served.activity30).total",
                "sum(compute_project_row(...)['activity30'])",
                rendered["rollup"]["activityTotal"],
                sum(row["activity30"]),
            )
        )

    return pairs


def mismatches(pairs: list[Pair]) -> list[Pair]:
    return [pair for pair in pairs if pair.rendered_value != pair.returned_value]


def mismatch_messages(pairs: list[Pair]) -> list[str]:
    return [pair.message for pair in mismatches(pairs)]
