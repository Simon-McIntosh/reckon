"""The coordinator directory is the board: runs, held paths, queue, idle owners.

The directory view lists what a coordinator needs to decide where to dispatch
without messaging a peer: which section each live run is on, which write paths
its owner holds, where its queued requests stand, and which of its sections it
has left idle. It pages, so a workstation-scope read stays bounded.
"""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import _store
from reckon.crew.directory import directory
from reckon.mcp_views import serve_crew_view

PROJECT = "power_convertor"
REPOSITORY = "/repos/power_convertor"


def _plan_html(slug: str, *, sections: tuple[str, ...], owner: str = "") -> str:
    body = "".join(
        f'<h2 id="{section}">Section {index}</h2>'
        f'<section data-reckon="section" data-id="{section}" '
        'data-effort-hours="1.0" data-status="implementable" data-links="" '
        'data-capability-version="1.0" data-capability-class="general" '
        'data-capability-reasoning="standard" '
        'data-capability-verification="strict" '
        'data-capability-risk="low"></section>'
        for index, section in enumerate(sections, start=1)
    )
    return (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-effort" content="M">'
        f'<meta name="plan-owner" content="{owner}">'
        '<meta name="plan-section-declarations" content="'
        + html.escape(json.dumps(dict.fromkeys(sections, "implementable")))
        + '">'
        f'<title>{slug}</title></head><body><main class="plan-doc">{body}'
        "</main></body></html>"
    )


def _pointer(
    home: Path,
    *,
    run_id: str,
    session: str,
    node: str,
    plan: str,
    section: str,
    write_paths: list[str],
    phase: str = "working",
    alive: bool = True,
) -> None:
    log = home / "crew" / "runs" / run_id / "stream.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({"type": "system", "subtype": "init"}) + "\n")
    _store.write_json_atomically(
        home / "crew" / "live" / f"{run_id}.json",
        {
            "run_id": run_id,
            "session": session,
            "project": PROJECT,
            "repo": REPOSITORY,
            "worktree": f"{REPOSITORY}/.reckon-worktrees/{session}/{node}",
            "node": {
                "id": node,
                "plan": plan,
                "section": section,
                "write_paths": list(write_paths),
            },
            "phase": phase,
            "process_alive": alive,
            **(
                {"queued_at": datetime.now(UTC).isoformat()}
                if phase == "queued"
                else {}
            ),
            "log_path": str(log),
        },
    )


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    docs = tmp_path / "repo" / "docs"
    plans = docs / "plans"
    plans.mkdir(parents=True)
    # One plan with a ready section nobody is on, one with a section in flight,
    # and one whose only section is gated so it is neither ready nor idle.
    (plans / "coil-build.html").write_text(
        _plan_html("coil-build", sections=("s1", "s2"), owner="physics-lead"),
        encoding="utf-8",
    )
    (plans / "in-flight.html").write_text(
        _plan_html("in-flight", sections=("s1",)), encoding="utf-8"
    )
    (plans / "gated-only.html").write_text(
        _plan_html("gated-only", sections=()), encoding="utf-8"
    )
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(docs)}), encoding="utf-8"
    )
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv("RECKON_STATE_ROOT", raising=False)

    # The idle coordinator is the plan's coordinator by its own earlier
    # dispatch, and holds no live run at all.
    ledger_path = home / "state" / PROJECT / "crew.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps(
            {
                "data": {
                    "_version": 1,
                    "members": [],
                    "holds": [],
                    "runs": [
                        {
                            "run_id": "run-earlier",
                            "session": "idle-coordinator",
                            "project": PROJECT,
                            "plan": "coil-build",
                            "dispatched_at": datetime.now(UTC).isoformat(),
                        },
                        {
                            "run_id": "run-gated-owner",
                            "session": "gated-coordinator",
                            "project": PROJECT,
                            "plan": "gated-only",
                            "dispatched_at": datetime.now(UTC).isoformat(),
                        },
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    _pointer(
        home,
        run_id="run-live",
        session="live-coordinator",
        node="build-the-coil",
        plan="in-flight",
        section="s1",
        write_paths=["src/coil.py", "tests/test_coil.py"],
    )
    _pointer(
        home,
        run_id="run-queued",
        session="live-coordinator",
        node="second-node",
        plan="in-flight",
        section="s1",
        write_paths=["src/supply.py"],
        phase="queued",
        alive=False,
    )
    return home


def test_directory_names_section_held_paths_and_ready_idle_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _seed(tmp_path, monkeypatch)
    view = directory(PROJECT)

    by_session = {row["session"]: row for row in view["coordinators"]}

    live = by_session["live-coordinator"]
    run = next(item for item in live["runs"] if item["run_id"] == "run-live")
    assert run["section"] == "§1"
    assert run["plan_section"] == "in-flight#§1"
    held = {(item["path"], item["run_id"]) for item in live["held_paths"]}
    assert ("src/coil.py", "run-live") in held
    assert ("src/supply.py", "run-queued") in held
    assert all("binding" in item for item in live["held_paths"])
    running = next(item for item in live["held_paths"] if item["run_id"] == "run-live")
    assert running["binding"] is True
    assert "run-live" in running["disposition_reason"]

    queued = live["queued_runs"]
    assert [item["run_id"] for item in queued] == ["run-queued"]
    assert queued[0]["position"] == 1

    # The negative control: a coordinator whose only plan has no ready section
    # and no run is not on the board.
    assert "gated-coordinator" not in by_session

    idle = by_session["idle-coordinator"]
    assert idle["runs"] == []
    assert idle["state"] == "idle"
    assert sorted(ref.partition("#")[2] for ref in idle["ready_idle_sections"]) == [
        "s1",
        "s2",
    ]
    assert idle["plan_owner"] == "physics-lead"
    assert view["ready_idle_join"] == "project"
    assert home.is_dir()


def test_directory_pages_coordinator_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path, monkeypatch)
    whole = serve_crew_view("directory", project=PROJECT)
    assert whole["page"]["total"] >= 2

    first = serve_crew_view("directory", project=PROJECT, limit=1)
    assert first["page"]["count"] == 1
    assert first["page"]["total"] == whole["page"]["total"]
    assert first["page"]["next_cursor"]

    rest = serve_crew_view(
        "directory", project=PROJECT, limit=10, cursor=first["page"]["next_cursor"]
    )
    listed = [row["session"] for row in first["coordinators"]] + [
        row["session"] for row in rest["coordinators"]
    ]
    assert sorted(listed) == sorted(row["session"] for row in whole["coordinators"])
    refused = serve_crew_view("directory", project=PROJECT, limit=0)
    assert refused["ok"] is False
    assert refused["error"] == "invalid_limit"
