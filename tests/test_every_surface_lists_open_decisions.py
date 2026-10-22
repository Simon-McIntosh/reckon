"""Every read surface lists the open decisions holding work.

A plan waiting on the lead carries no followup, so its open decision is the
only record that it waits and on whom. Two surfaces are read for that record:
the roadmap's default summary view and the crew payload the page polls. They
must list one derivation rather than two readings of the same plan files, so
these cases build one mounted project, ask both surfaces the same question and
compare their answers.
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
from reckon.mcp_views import roadmap_view
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "sample"
SLUG = "waiting-work"
#: The plan's own last-modified date. Fixed in the fixture rather than taken
#: from today, so the case cannot pass on the day it is written and expire.
MODIFIED = "2026-10-01"
SCOPED_SECTION = "s2"


@pytest.fixture()
def mounted_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    docs = tmp_path / "repo" / "docs"
    docs.mkdir(parents=True)
    config_home = tmp_path / "config"
    state_root = config_home / "state"
    state_root.mkdir(parents=True)
    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({PROJECT: str(docs)}), encoding="utf-8")
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
        yield docs, server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        serve_module._DISC_CACHE.clear()


def _write_waiting_plan(docs: Path, *, transport_choice: str = "") -> Path:
    """Write the one plan the fixture reads, with its three decisions.

    ``transport_choice`` locks the scoped decision when it is set, which is how
    a case shows a locked decision leaving both surfaces.
    """

    bare = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{SLUG}</title></head>"
        '<body><main class="plan-doc">'
        '<h2 id="s1">§1 — The work</h2>'
        '<h2 id="s2">§2 — The payload</h2>'
        "</main></body></html>"
    )
    path = docs / "plans" / f"{SLUG}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        write_state(
            bare,
            {
                "slug": SLUG,
                "title": "Waiting work",
                "status": "active",
                "modified": MODIFIED,
                "decisions": {
                    "transport": {
                        "title": "Which transport carries the payload?",
                        "choices": ["accept", "reject"],
                        "option_labels": {"accept": "accept", "reject": "reject"},
                        "choice": transport_choice,
                        "rationale": "",
                        "recommended": "accept",
                        "recommended_by": "surface review",
                        "sections": [SCOPED_SECTION],
                    },
                    "retention": {
                        "title": "How long do records remain?",
                        "choices": ["keep", "drop"],
                        "option_labels": {"keep": "keep", "drop": "drop"},
                        "choice": "",
                        "rationale": "",
                    },
                    "naming": {
                        "title": "Which naming scheme holds?",
                        "choices": ["dotted", "flat"],
                        "option_labels": {"dotted": "dotted", "flat": "flat"},
                        "choice": "dotted",
                        "rationale": "",
                    },
                },
                "gates": [
                    {
                        "id": "retention-edge",
                        "measure": "the retention choice is recorded",
                        "transition": "decision-lockable",
                        "decision": "retention",
                        "verdict": "",
                    }
                ],
                "followups": [],
                "version": 0,
            },
        ),
        encoding="utf-8",
    )
    return path


def _summary_view() -> dict:
    return roadmap_view(
        mcp_module._roadmap(PROJECT), view="summary", cursor=None, limit=None
    )


def _crew_payload(port: int) -> dict:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", "/crew")
        response = connection.getresponse()
        return json.loads(response.read())
    finally:
        connection.close()


def test_summary_and_crew_payload_list_the_same_open_decisions(mounted_project) -> None:
    docs, port = mounted_project
    _write_waiting_plan(docs)
    reload_mcp_family()
    serve_module._DISC_CACHE.clear()

    summary_rows = _summary_view()["open_decisions"]
    crew_rows = _crew_payload(port)["open_decisions"]

    assert summary_rows == crew_rows


def test_summary_view_lists_open_and_gated_decisions_with_their_plan(
    mounted_project,
) -> None:
    docs, _port = mounted_project
    _write_waiting_plan(docs)
    reload_mcp_family()
    serve_module._DISC_CACHE.clear()

    summary = _summary_view()
    rows = {row["id"]: row for row in summary["open_decisions"]}

    assert set(rows) == {"transport", "retention"}
    assert summary["open_decision_count"] == 2
    assert "naming" not in rows, "a locked decision holds no work"

    scoped = rows["transport"]
    assert scoped["status"] == "open"
    assert scoped["project"] == PROJECT
    assert scoped["plan"] == SLUG
    assert scoped["sections"] == [SCOPED_SECTION]
    assert scoped["recommended"] == "accept"
    assert scoped["recommended_by"] == "surface review"
    assert scoped["plan_modified"] == MODIFIED

    assert rows["retention"]["status"] == "gated"


def test_crew_payload_lists_the_open_and_gated_decisions(mounted_project) -> None:
    docs, port = mounted_project
    _write_waiting_plan(docs)
    reload_mcp_family()
    serve_module._DISC_CACHE.clear()

    payload = _crew_payload(port)
    rows = {row["id"]: row for row in payload["open_decisions"]}

    assert set(rows) == {"transport", "retention"}
    assert rows["transport"]["sections"] == [SCOPED_SECTION]
    assert rows["transport"]["recommended_by"] == "surface review"
    assert rows["transport"]["plan_modified"] == MODIFIED
    assert rows["retention"]["status"] == "gated"


def test_locking_the_open_decision_removes_it_from_both_surfaces(
    mounted_project,
) -> None:
    docs, port = mounted_project
    _write_waiting_plan(docs)
    reload_mcp_family()
    serve_module._DISC_CACHE.clear()
    assert {row["id"] for row in _summary_view()["open_decisions"]} == {
        "transport",
        "retention",
    }

    _write_waiting_plan(docs, transport_choice="accept")
    reload_mcp_family()
    serve_module._DISC_CACHE.clear()

    summary_rows = _summary_view()["open_decisions"]
    crew_rows = _crew_payload(port)["open_decisions"]

    assert {row["id"] for row in summary_rows} == {"retention"}
    assert {row["id"] for row in crew_rows} == {"retention"}
    assert summary_rows == crew_rows
