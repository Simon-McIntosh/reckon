"""The ready set answers who to ask about the work nobody is doing.

On 2026-10-22 at about 08:50Z a lane sat at one request of twelve while three
sections were implementable, dependency-ready and gate-clear with no run. No
read surfaced them: the ready set listed ready rows and nothing about the run on
each, so a row with no run and a row being worked on read alike, and no row said
which session to ask about the plan.

These cases pin the extension against a fixture rebuilt from power_convertor's
plan state at that instant — its plan documents as committed immediately before
``2026-10-22T08:50Z``, and the run records that decided each plan's coordinator
then, both copied into ``tests/fixtures/coordinator_board_0850``. The expected
coordinators are literals read by hand from those records, never the
derivation's own answer.

Every fixture is read from the checked-in tree and every write the read makes
lands under the test's own config home, so no case touches a mounted project.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon import serve
from reckon.crew import plan_coordinator
from reckon.mcp_budget import DEFAULT_RESPONSE_CEILING
from reckon.mcp_views import ready_set_view
from reckon.roadmap import build_roadmap

PROJECT = "power_convertor"

#: The instant the fixture reproduces.
NOW = datetime(2026, 10, 22, 8, 50, 0, tzinfo=UTC)

FIXTURE = Path(__file__).parent / "fixtures" / "coordinator_board_0850"

#: The three sections the incident found implementable, ready and idle, and the
#: coordinator each plan's most recent dispatched run names in the fixture.
IDLE_SECTIONS: dict[str, str] = {
    "iter-switched-terminal-transients#s13": "83d14db1-f84d-4588-b40a-bc508c081332",
    "iter-dummy-load-validation#s4": "83d14db1-f84d-4588-b40a-bc508c081332",
    "shared-coil-supply-model#s9": "cdb769a4-7af0-4304-a144-f39ecbf8c502",
}

#: The plans whose every declared section is resolved, so `close` is the write
#: they are waiting on.
CLOSABLE = ["jt60sa-coil-circuit-model", "tf-co-wound-tape-validation"]


@pytest.fixture()
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The fixture's roadmap, read hermetically at the fixture instant.

    The config home is a temp tree so the ledger, the live pointers and the
    metadata cache this read touches all resolve inside it.
    """

    home = tmp_path / "home"
    home.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts = home / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(FIXTURE / "docs")}), encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts)
    monkeypatch.setattr(serve, "_STATE_ROOT", state_root)
    serve._DISC_CACHE.clear()
    # The fleet's own live set is not this fixture's; a case that needs a run
    # writes its own pointer.
    monkeypatch.setattr("reckon.crew.list_live", lambda *a, **k: [])

    discovered = serve.discover_plans(
        FIXTURE / "docs", PROJECT, FIXTURE / "docs" / "state"
    )
    roadmap = build_roadmap(
        PROJECT,
        discovered["inventory"],
        discovered.get("sprints") or [],
        active_sprint_id=discovered.get("active_sprint_id"),
        project_manifest=discovered,
        review={},
        docs_dir=FIXTURE / "docs",
    )
    roadmap["_discovered"] = discovered
    return roadmap


def _coordinators() -> dict[str, dict[str, Any]]:
    """Each plan's coordinator as the crew derives it from the fixture records."""

    return plan_coordinator.resolve_plan_coordinators(
        PROJECT,
        root=FIXTURE,
        now=NOW,
        pointers=[],
    )["plans"]


def _filtered(board: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return ready_set_view(
        board, coordinators=_coordinators(), ready_idle=True, **kwargs
    )


def _pointer(*, run_id: str, section: str, plan: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "project": PROJECT,
        "member": "worker-one",
        "created_at": "2026-10-22T08:40:00Z",
        "phase": "working",
        "process_alive": True,
        "launcher_host": "another-login-node",
        "node": {"id": f"{plan}-{section}", "plan": plan, "section": section},
    }


def test_the_idle_sections_name_their_coordinators(board: dict[str, Any]) -> None:
    """The filtered view names the incident's three idle sections and who owns
    each plan, in one call that fits the transport ceiling."""

    view = _filtered(board)
    rows = {str(row.get("slug")): row for row in view["ready"]}

    for section, coordinator in IDLE_SECTIONS.items():
        assert section in rows, (section, sorted(rows))
        assert rows[section]["live_run"] is None, section
        assert rows[section]["coordinator"] == coordinator, section
        assert rows[section]["plan_owner"] == "Simon McIntosh", section

    assert len(json.dumps(view, separators=(",", ":"))) < DEFAULT_RESPONSE_CEILING


def test_the_view_names_the_closable_plans_and_what_still_holds_them(
    board: dict[str, Any],
) -> None:
    """A plan whose declared work is all resolved is named closable, with the
    blockers `close` would have to clear."""

    view = _filtered(board)
    closable = {row["plan"]: row for row in view["closable"]}

    assert sorted(closable) == CLOSABLE
    for row in closable.values():
        assert isinstance(row["closure_blockers"], list)
        assert row["plan_owner"] == "Simon McIntosh"


def test_a_section_with_a_live_run_leaves_the_filtered_set(
    board: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: the filter is the live run.

    A section given a run must leave the ready-idle set. A view that skipped the
    filter, or matched the run against the wrong row, leaves it in and fails
    here.
    """

    section = "shared-coil-supply-model#s9"
    plan, _, section_id = section.partition("#")
    pointer = _pointer(run_id="run-live-s9", section=section_id, plan=plan)
    monkeypatch.setattr("reckon.crew.list_live", lambda *a, **k: [pointer])
    busy = build_roadmap(
        PROJECT,
        board["_discovered"]["inventory"],
        board["_discovered"].get("sprints") or [],
        active_sprint_id=board["_discovered"].get("active_sprint_id"),
        project_manifest=board["_discovered"],
        review={},
        docs_dir=FIXTURE / "docs",
    )

    rows = {str(row.get("slug")): row for row in _filtered(busy)["ready"]}
    assert section not in rows, sorted(rows)
    # The run is reported where the unfiltered view reads it, so the section's
    # absence is the filter and not a row that never carried the run.
    unfiltered = {
        str(row.get("slug")): row
        for row in ready_set_view(busy, coordinators=_coordinators())["ready"]
    }
    assert unfiltered[section]["live_run"] == "run-live-s9"


def test_the_filtered_view_pages_with_its_own_cursor(board: dict[str, Any]) -> None:
    """The filtered set is a page of the same view, so a coordinator reading a
    large project walks it with the cursor every other view answers with."""

    whole = _filtered(board)
    first = _filtered(board, limit=2)

    assert first["pagination"]["total"] == len(whole["ready"]) > 2
    assert len(first["ready"]) == 2
    collected = list(first["ready"])
    cursor = first["pagination"]["next_cursor"]
    while cursor:
        page = _filtered(board, limit=2, cursor=cursor)
        collected.extend(page["ready"])
        cursor = page["pagination"]["next_cursor"]

    assert [row["slug"] for row in collected] == [row["slug"] for row in whole["ready"]]
