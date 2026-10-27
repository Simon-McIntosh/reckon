"""Every default MCP read answers inside the budget its view declares.

A view that serialises whole records answers over the transport ceiling and is
refused, which sends the caller to the files instead of to the MCP. The size
contract therefore lives where code can enforce it: :data:`VIEW_BUDGETS` in
:mod:`reckon.mcp_budget` maps every read view to the most characters a call
using only that view's required arguments may return, the budget module reads
the row to label a refusal, and this test holds every view to its row.

The fixture is a mounted project of 60 plans and 400 sections, built here rather
than taken from the shared builder, so the population is the one this contract
names.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import reckon.mcp as mcp_module
from reckon import mcp_budget as budget_module
from reckon._plan_html import write_state
from reckon.mcp_budget import serialised_characters
from tests.mcp_family_reload import reload_mcp_family

#: The declared table, read through its module so that this file also runs
#: against a revision that predates it: there the rows are absent and the size
#: assertions fail on what a view answered rather than at import.
VIEW_BUDGETS: dict[tuple[str, str], int] = dict(
    getattr(budget_module, "VIEW_BUDGETS", {}) or {}
)


def view_budget(tool: str, arguments: dict[str, Any]) -> int | None:
    """The row a call is held to, or ``None`` where the table declares none."""

    resolver = getattr(budget_module, "view_budget", None)
    return resolver(tool, arguments) if resolver is not None else None


PLANS = 60
SECTIONS = 400

#: The paths a project's findings are grouped by, and the reviews naming each of
#: them: enough paths that one default page of groups is a fraction of the
#: answer, and enough rows that the unpaged answer, which repeats each row beside
#: its group, passes the row the size table declares for the view.
FINDING_PATHS = 60
FINDING_REVIEWS = 1

#: The row the size table declares for ``crew view=findings``, held beside the
#: fixture so a base revision that declares no row is still measured against it.
FINDINGS_ROW = 24_000

# The sweep calls every view of every read tool against a 60-plan project, which
# costs more than one focused test costs, so the module declares its own bound
# rather than inheriting the suite default.
pytestmark = pytest.mark.timeout(900)

#: The two views that are explicit lossless opt-ins rather than defaults: a
#: caller asking for one has already chosen the whole record over a bounded
#: answer, so the runtime ceiling, not a compact row, is what stands behind
#: them. Every other view is held to its declared row.
LOSSLESS_VIEWS = {("roadmap", "raw"), ("audit", "raw")}

ROADMAP_VIEWS = ("summary", "detail", "raw")
READ_PLAN_VIEWS = (
    "summary",
    "detail",
    "history",
    "version",
    "raw",
    "schema",
    "section",
    "why",
)
AUDIT_VIEWS = ("summary", "detail", "raw")
CREW_VIEWS = (
    "summary",
    "flight",
    "live",
    "scopes",
    "drain",
    "records",
    "ledger",
    "budget",
    "lanes",
    "routing",
    "directory",
    "fleet",
    "runs",
    "forgotten",
    "obligations",
    "models",
    "velocity",
    "scores",
    "plan-review",
    "findings",
)


@pytest.fixture()
def big_project(tmp_path, monkeypatch):
    """Mount a synthetic project of ``PLANS`` plans holding ``SECTIONS`` sections."""

    project = "sample"
    docs = tmp_path / "repo" / "docs"
    docs.mkdir(parents=True)
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({project: str(docs)}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    # A live server bounds each read; this suite measures the size contract, so
    # the bound is lifted past what a 60-plan fixture costs on a loaded host.
    monkeypatch.setenv("RECKON_MCP_READ_DEADLINE_SECONDS", "120")
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_module

    serve_module._MOUNTS_FILE = mounts
    serve_module._STATE_ROOT = state_root
    serve_module._DISC_CACHE.clear()
    reload_mcp_family()

    _populate(docs, project=project)
    return project, docs


@pytest.fixture()
def findings_project(tmp_path, monkeypatch):
    """Mount a project whose committed reviews name ``FINDING_PATHS`` paths."""

    project = "sample"
    root = tmp_path / "repo"
    docs = root / "docs"
    (docs / "plans").mkdir(parents=True)
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({project: str(docs)}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_MCP_READ_DEADLINE_SECONDS", "120")
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_module

    serve_module._MOUNTS_FILE = mounts
    serve_module._STATE_ROOT = state_root
    serve_module._DISC_CACHE.clear()
    reload_mcp_family()

    stored = _store_findings(
        root, project, paths=FINDING_PATHS, reviews=FINDING_REVIEWS
    )
    return project, root, stored


def _populate(docs: Path, *, project: str) -> int:
    per = SECTIONS // PLANS
    slugs = [f"plan-{index:03d}" for index in range(1, PLANS + 1)]
    written = 0
    for index, slug in enumerate(slugs):
        count = per + (SECTIONS - per * PLANS) if index == 0 else per
        body: list[str] = []
        declarations: dict[str, str] = {}
        for number in range(1, count + 1):
            identity = f"s{number}"
            declarations[identity] = "implementable"
            body.append(f'<h2 id="{identity}">Section {number}</h2>')
            body.append(
                f'<section data-reckon="section" data-id="{identity}" '
                'data-effort-hours="1.0" data-capability-version="1.0" '
                'data-capability-class="general" '
                'data-capability-reasoning="standard" '
                'data-capability-verification="standard" '
                'data-capability-risk="low" data-status="implementable" '
                'data-attempts="0" data-links=""></section>'
            )
            body.append(f"<p>{'authored section prose ' * 40}</p>")
        bare = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="docs-project" content="{project}">'
            f"<title>{slug}</title></head>"
            f'<body><main class="plan-doc">{"".join(body)}</main></body></html>'
        )
        path = docs / "plans" / f"{slug}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            write_state(
                bare,
                {
                    "slug": slug,
                    "title": slug.title(),
                    "status": "active",
                    "depends_on": [slugs[index - 1]] if index else [],
                    "decisions": {},
                    "gates": [],
                    "followups": [
                        {"id": "next-action", "prompt": f"/reckon-build {slug}"}
                    ],
                    "version": 0,
                    "section_declarations": declarations,
                },
            ),
            encoding="utf-8",
        )
        written += count
    assert written == SECTIONS
    return written


def _store_findings(root: Path, project: str, *, paths: int, reviews: int) -> int:
    """Store ``reviews`` review records, each naming every one of ``paths``.

    One record is one review run, so a path named by two records has a
    recurrence of two. Records are written through the store's own committed
    writer, which is the only path into the tree the findings reader walks.
    """

    from reckon.crew import review as review_module

    stored = 0
    for review_index in range(reviews):
        findings = [
            {
                "file": f"reckon/module_{index:03d}.py",
                "line": str(10 + index),
                "text": "the retry guard counts a failed attempt twice",
                "severity": "follow-on",
            }
            for index in range(paths)
        ]
        review_module.store_committed_review(
            {
                "project": project,
                "reviewed_run_id": f"r-reviewed-{review_index}",
                "review_run_id": (
                    f"r-20261021T0900{review_index}00000000-review-{review_index}"
                ),
                "reviewed_head_sha": "0" * 40,
                "scores": {"evidence": 17},
                "findings": findings,
            },
            root=root,
        )
        stored += len(findings)
    return stored


def _call(tool_name: str, **arguments: Any) -> Any:
    tool = mcp_module.mcp._tool_manager._tools[tool_name]
    return asyncio.run(tool.fn(**arguments))


def _calls(project: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Every read view of every reckon MCP tool, with only its required arguments."""

    calls: list[tuple[str, str, dict[str, Any]]] = [
        ("roadmap", view, {"project": project, "view": view}) for view in ROADMAP_VIEWS
    ]
    for view in READ_PLAN_VIEWS:
        arguments: dict[str, Any] = {"project": project, "view": view}
        if view in {"summary", "detail", "history", "raw", "section", "why"}:
            arguments["slug"] = "plan-001"
        if view == "section":
            arguments["section"] = "s1"
        calls.append(("read_plan", view, arguments))
    calls.extend(
        ("audit", view, {"project": project, "view": view}) for view in AUDIT_VIEWS
    )
    calls.extend(
        ("crew", view, {"project": project, "view": view}) for view in CREW_VIEWS
    )
    return calls


def test_every_view_declares_a_row_and_the_rows_name_every_view() -> None:
    declared = {(tool, view) for tool, view in VIEW_BUDGETS}
    expected = (
        {("roadmap", view) for view in ROADMAP_VIEWS}
        | {("read_plan", view) for view in READ_PLAN_VIEWS}
        | {("audit", view) for view in AUDIT_VIEWS}
        | {("crew", view) for view in CREW_VIEWS}
    )

    assert declared == expected


def test_every_default_read_fits_its_row(big_project) -> None:
    project, _docs = big_project
    failures: list[str] = []
    for tool_name, view, arguments in _calls(project):
        try:
            result = _call(tool_name, **arguments)
        except Exception as exc:  # noqa: BLE001 — a view that raises is named here
            failures.append(f"{tool_name} view={view}: raised {exc!r}")
            continue
        if not isinstance(result, dict):
            continue
        size = serialised_characters(result)
        row = view_budget(tool_name, arguments)
        if row is None:
            failures.append(f"{tool_name} view={view}: no row declared")
            continue
        if size > row:
            failures.append(f"{tool_name} view={view}: {size} characters over {row}")
        if (
            result.get("error") == "response_over_budget"
            and (
                tool_name,
                view,
            )
            not in LOSSLESS_VIEWS
        ):
            failures.append(f"{tool_name} view={view}: returned response_over_budget")
    assert not failures, "\n".join(failures)


def test_roadmap_detail_fits_by_paging_rather_than_by_refusing(big_project) -> None:
    """The base commit answers ``response_over_budget`` here; this asserts the fit."""

    project, _docs = big_project

    result = _call("roadmap", project=project, view="detail")

    assert result.get("error") != "response_over_budget"
    assert result["view"] == "detail"
    assert len(result["pending_plans"]) == PLANS
    assert serialised_characters(result) <= view_budget("roadmap", {"view": "detail"})


def test_crew_findings_answers_one_page_and_carries_the_next(
    findings_project,
) -> None:
    """The default findings read is one page, with the cursor that reaches the rest.

    Every stored finding is returned twice — once inside its path group and once
    in the flat list beside the groups — so the whole answer repeats its rows and
    grows with the project. The default read must be a page of groups carrying
    the page's own rows, its total, and the cursor that reaches the next page.
    """

    project, _root, stored = findings_project
    assert stored == FINDING_PATHS * FINDING_REVIEWS

    first = _call("crew", project=project, view="findings")
    size = serialised_characters(first)
    assert first.get("error") != "response_over_budget"
    assert size <= FINDINGS_ROW, (
        f"{size} characters over {FINDINGS_ROW}: "
        f"paths={len(first.get('paths') or [])} "
        f"rows={len(first.get('rows') or [])} "
        f"artefacts={len(first.get('artefacts') or [])} "
        f"pagination={first.get('pagination')} "
        f"row0={json.dumps(first['paths'][0]['rows'][0])}"
    )
    assert VIEW_BUDGETS.get(("crew", "findings")) == FINDINGS_ROW

    pagination = first["pagination"]
    assert pagination["total"] == FINDING_PATHS
    assert len(first["paths"]) == pagination["count"]
    assert len(first["paths"]) < FINDING_PATHS
    assert len(first["rows"]) == sum(len(group["rows"]) for group in first["paths"])

    cursor = pagination["next_cursor"]
    assert cursor, "a first page of a larger set carries the cursor to the next"

    second = _call("crew", project=project, view="findings", cursor=cursor)
    assert second["pagination"]["total"] == FINDING_PATHS
    assert second["paths"], "the cursor names a page that holds groups"
    assert {group["path"] for group in second["paths"]}.isdisjoint(
        group["path"] for group in first["paths"]
    )
    assert second["pagination"]["next_cursor"] != cursor

    last = _call(
        "crew",
        project=project,
        view="findings",
        cursor=second["pagination"]["next_cursor"],
    )
    assert last["pagination"]["next_cursor"] is None
    seen = (
        {group["path"] for group in first["paths"]}
        | {group["path"] for group in second["paths"]}
        | {group["path"] for group in last["paths"]}
    )
    assert len(seen) == FINDING_PATHS
