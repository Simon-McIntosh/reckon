"""Every read surface lists the open decisions holding work.

A plan waiting on the lead carries no followup, so its open decision is the
only record that it waits and on whom. Two surfaces are read for that record:
the roadmap's default summary view and the crew payload the page polls. They
must list one derivation rather than two readings of the same plan files, so
these cases mount two projects holding three plans, ask both surfaces the same
question of each project, and compare their answers in order.
"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

import reckon.mcp as mcp_module
import reckon.serve as serve_module
from reckon._plan_html import write_state
from reckon._store import advance_write_generation
from reckon.mcp_views import roadmap_view
from reckon.roadmap import build_roadmap, waiting_decision_rows
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "sample"
COMPANION = "companion"
SLUG = "waiting-work"
SCOPE_SLUG = "whole-plan-wait"
COMPANION_SLUG = "companion-work"
SCOPED_SECTION = "s2"
#: The plans' own last-modified date. Fixed in the fixture rather than taken
#: from today, so the cases cannot pass on the day they are written and expire.
MODIFIED = "2026-10-01"
#: The open and gated decisions the sample project holds; its locked one does
#: not appear on any surface.
WAITING_IDS = {"transport", "scope-move", "retention"}


@pytest.fixture()
def mounted_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    docs = tmp_path / "repo" / "docs"
    other = tmp_path / "other" / "docs"
    docs.mkdir(parents=True)
    other.mkdir(parents=True)
    config_home = tmp_path / "config"
    state_root = config_home / "state"
    state_root.mkdir(parents=True)
    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(
        json.dumps({PROJECT: str(docs), COMPANION: str(other)}),
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    serve_module._MOUNTS_FILE = mounts_file
    serve_module._STATE_ROOT = state_root
    serve_module._DISC_CACHE.clear()
    reload_mcp_family()

    server = serve_module.ThreadingHTTPServer(("127.0.0.1", 0), serve_module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"docs": docs, "other": other, "port": server.server_port}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        serve_module._DISC_CACHE.clear()


def _plan_doc(slug: str, project: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{project}">'
        f"<title>{slug}</title></head>"
        '<body><main class="plan-doc">'
        '<h2 id="s1">§1 — The work</h2>'
        '<h2 id="s2">§2 — The payload</h2>'
        f"</main></body></html>"
    )


def _decision(title: str, **extra) -> dict:
    return {
        "title": title,
        "choices": ["accept", "reject"],
        "option_labels": {"accept": "accept", "reject": "reject"},
        "choice": "",
        "rationale": "",
        **extra,
    }


def _write_plan(docs: Path, slug: str, project: str, *, decisions, gates=()) -> Path:
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        write_state(
            _plan_doc(slug, project),
            {
                "slug": slug,
                "title": slug,
                "status": "active",
                "modified": MODIFIED,
                "decisions": decisions,
                "gates": list(gates),
                "followups": [],
                "version": 0,
            },
        ),
        encoding="utf-8",
    )
    return path


def _write_mounted_plans(
    docs: Path, other: Path, *, transport_choice: str = ""
) -> None:
    """Write the three plans the fixture reads.

    ``transport_choice`` locks the scoped decision when it is set, which is how
    a case shows a locked decision leaving both surfaces.
    """

    _write_plan(
        docs,
        SLUG,
        PROJECT,
        decisions={
            "transport": _decision(
                "Which transport carries the payload?",
                choice=transport_choice,
                recommended="accept",
                recommended_by="surface review",
                sections=[SCOPED_SECTION],
            ),
            "retention": _decision("How long do records remain?"),
            "naming": _decision("Which naming scheme holds?", choice="dotted"),
        },
        gates=[
            {
                "id": "retention-edge",
                "measure": "the retention choice is recorded",
                "transition": "decision-lockable",
                "decision": "retention",
                "verdict": "",
            }
        ],
    )
    _write_plan(
        docs,
        SCOPE_SLUG,
        PROJECT,
        decisions={"scope-move": _decision("Does the plan move as one?")},
    )
    _write_plan(
        other,
        COMPANION_SLUG,
        COMPANION,
        decisions={
            "cadence": _decision("How often does the companion poll?"),
            "timing": _decision("Which clock does it read?", choice="utc"),
        },
    )


def _arm(mounted: dict, *, transport_choice: str = "") -> int:
    """Write the fixture is served from, and return the port it polls."""

    _write_mounted_plans(
        mounted["docs"], mounted["other"], transport_choice=transport_choice
    )
    # The fixture writes the plan files directly, while a real decision lock
    # goes through edit_plan and write_plan and advances the plan-write
    # generation. A roadmap read answers a warm key from its snapshot, so a
    # write that moved no generation would be invisible until the revalidation
    # window expired; advancing it here makes the fixture's write the store
    # write a lock actually is.
    for project in (PROJECT, COMPANION):
        advance_write_generation(project)
    reload_mcp_family()
    serve_module._DISC_CACHE.clear()
    return mounted["port"]


def _summary(project: str) -> dict:
    return roadmap_view(
        mcp_module._roadmap(project), view="summary", cursor=None, limit=None
    )


def _crew(port: int) -> dict:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", "/crew")
        response = connection.getresponse()
        return json.loads(response.read())
    finally:
        connection.close()


def test_summary_and_crew_payload_list_the_same_open_decisions(
    mounted_projects,
) -> None:
    crew_rows = _crew(_arm(mounted_projects))["open_decisions"]

    for project in (PROJECT, COMPANION):
        assert _summary(project)["open_decisions"] == [
            row for row in crew_rows if row["project"] == project
        ], project


def test_summary_view_lists_open_and_gated_decisions_with_their_plan(
    mounted_projects,
) -> None:
    _arm(mounted_projects)
    summary = _summary(PROJECT)
    rows = {row["id"]: row for row in summary["open_decisions"]}

    assert set(rows) == WAITING_IDS
    assert summary["open_decision_count"] == len(WAITING_IDS)
    assert "naming" not in rows, "a locked decision holds no work"
    assert "cadence" not in rows, "another project's wait is not this one's"

    scoped = rows["transport"]
    assert scoped["status"] == "open"
    assert scoped["project"] == PROJECT
    assert scoped["plan"] == SLUG
    assert scoped["sections"] == [SCOPED_SECTION]
    assert scoped["recommended"] == "accept"
    assert scoped["recommended_by"] == "surface review"
    assert scoped["plan_modified"] == MODIFIED
    assert rows["retention"]["status"] == "gated"


def test_crew_payload_lists_every_mounted_projects_decisions(
    mounted_projects,
) -> None:
    rows = {
        (row["project"], row["id"]): row
        for row in _crew(_arm(mounted_projects))["open_decisions"]
    }

    assert set(rows) == {(PROJECT, key) for key in WAITING_IDS} | {
        (COMPANION, "cadence")
    }

    scoped = rows[(PROJECT, "transport")]
    assert scoped["sections"] == [SCOPED_SECTION]
    assert scoped["recommended_by"] == "surface review"
    assert scoped["plan_modified"] == MODIFIED
    assert rows[(PROJECT, "retention")]["status"] == "gated"
    assert (COMPANION, "timing") not in rows, "a locked decision holds no work"


def test_discovery_payload_carries_the_roadmap_waiting_decisions(
    mounted_projects,
) -> None:
    """The project's discovery payload serves the roadmap's own list.

    A surface that wants the decisions a project waits on without polling the
    crew route reads them here, so the payload must carry the report's
    derivation rather than a list that drifted from it.
    """

    _arm(mounted_projects)
    docs = mounted_projects["docs"]
    payload = serve_module.discover_plans(docs, PROJECT, serve_module._STATE_ROOT)
    report = build_roadmap(
        PROJECT,
        payload.get("inventory", []),
        payload.get("sprints", []),
        active_sprint_id=payload.get("active_sprint_id"),
        project_manifest=payload,
        review=payload.get("review") or {},
        docs_dir=docs,
    )

    rows = payload["waiting_decisions"]
    assert rows, "the fixture plans hold open decisions"
    assert rows == waiting_decision_rows(report)
    assert {row["id"] for row in rows} == WAITING_IDS


def test_a_whole_plan_wait_says_so_on_both_surfaces(mounted_projects) -> None:
    port = _arm(mounted_projects)
    summary = {row["id"]: row for row in _summary(PROJECT)["open_decisions"]}
    crew = {
        row["id"]: row
        for row in _crew(port)["open_decisions"]
        if row["project"] == PROJECT
    }

    whole = summary["scope-move"]
    assert whole["plan"] == SCOPE_SLUG
    assert whole["sections"] == []
    assert whole["holds_whole_plan"] is True

    scoped = summary["transport"]
    assert scoped["sections"] == [SCOPED_SECTION]
    assert scoped["holds_whole_plan"] is False

    assert crew["scope-move"] == whole, "both surfaces agree on the scope"
    assert crew["transport"]["holds_whole_plan"] is False


def test_locking_the_open_decision_removes_it_from_both_surfaces(
    mounted_projects,
) -> None:
    port = _arm(mounted_projects)
    assert {row["id"] for row in _summary(PROJECT)["open_decisions"]} == WAITING_IDS

    _arm(mounted_projects, transport_choice="accept")
    summary_rows = _summary(PROJECT)["open_decisions"]
    crew_rows = [
        row for row in _crew(port)["open_decisions"] if row["project"] == PROJECT
    ]

    assert {row["id"] for row in summary_rows} == WAITING_IDS - {"transport"}
    assert {row["id"] for row in crew_rows} == WAITING_IDS - {"transport"}
    assert summary_rows == crew_rows
