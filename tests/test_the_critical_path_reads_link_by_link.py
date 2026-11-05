"""The critical path reads link by link, on the rows it already reports.

On 2026-10-22 at about 08:50Z a coordinator could find the critical path only as
a list of ``plan#section`` refs, and had to look each one up before it knew
whether the head carried a run, waited on a peer or stood behind a gate. The
path is the one chain a reader starts on, so each link carries its own answer:
the first that applies of in flight, ready-idle, gated and waiting, with every
open gate and every unmet wait beside it in one read.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from reckon import serve
from reckon.roadmap import (
    DECISION_BLOCKER,
    GATED,
    IN_FLIGHT,
    READY_IDLE,
    WAITING,
    _critical_path_links,
    build_roadmap,
)

PROJECT = "power_convertor"

#: The instant the fixture reproduces.
NOW = datetime(2026, 10, 22, 8, 50, 0, tzinfo=UTC)

FIXTURE = Path(__file__).parent / "fixtures" / "coordinator_board_0850"
DOCS = FIXTURE / "docs"

#: The head of the path the fixture's own edges force: it is the deepest
#: prerequisite the chain runs through.
HEAD = "shared-coil-supply-model#s9"

#: The session the fixture's most recent dispatched row for that plan names —
#: ``r-20261022T083726456965-plan-review-of-shared-coil-supply-model``, read by
#: hand from the fixture's run record and never from the derivation under test.
HEAD_COORDINATOR = "cdb769a4-7af0-4304-a144-f39ecbf8c502"

#: A link both gated and waiting: the open gate names it and it waits on the
#: head's own section. Both holds must be reported, so the link reads as gated
#: with the wait still visible beside it.
GATED_LINK = "iter-switched-terminal-transients#s6"
GATED_GATE = "g-istt-6"
GATED_HOLDS = [GATED_GATE, HEAD]


FRAGMENT = (
    Path(__file__).parent.parent
    / "docs"
    / "evidence"
    / "fragments"
    / "a-coordinator-sees-the-board"
    / "the-critical-path-reads-link-by-link.html"
)


def _build(*, now: datetime | None = NOW) -> dict[str, Any]:
    """The fixture's roadmap, built from the tree it carries.

    Discovery reads the plans off the fixture, so the payload is the one a
    served read composes; the clock is the fixture's own instant, because the
    fixture's ledger rows were written then and a coordinator is judged against
    a staleness window.
    """

    discovered = serve.discover_plans(DOCS, PROJECT, DOCS / "state")
    return build_roadmap(
        PROJECT,
        discovered["inventory"],
        discovered.get("sprints") or [],
        active_sprint_id=discovered.get("active_sprint_id"),
        project_manifest=discovered,
        review={},
        docs_dir=DOCS,
        now=now,
    )


@pytest.fixture()
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The fixture's roadmap, read hermetically at the fixture instant."""

    home = tmp_path / "home"
    home.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts = home / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(DOCS)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts)
    monkeypatch.setattr(serve, "_STATE_ROOT", state_root)
    serve._DISC_CACHE.clear()
    # The fleet's own live set is not this fixture's; a case that needs a run
    # writes its own pointer.
    monkeypatch.setattr("reckon.crew.list_live", lambda *a, **k: [])
    return _build()


def _links(roadmap: dict[str, Any]) -> list[dict[str, Any]]:
    """The critical path's link rows, in the order the path runs through them."""

    return list((roadmap.get("critical_path") or {}).get("links") or [])


def _link(roadmap: dict[str, Any], ref: str) -> dict[str, Any]:
    for row in _links(roadmap):
        if row.get("ref") == ref:
            return row
    raise AssertionError(f"{ref} is not on the critical path: {_links(roadmap)}")


def _pointer(*, run_id: str, plan: str, section: str) -> dict[str, Any]:
    """One live run of a plan's section, as the fleet's live set reports it."""

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


def _live_runs(monkeypatch: pytest.MonkeyPatch, *pointers: dict[str, Any]) -> None:
    """Report exactly these runs as the fleet's live set."""

    monkeypatch.setattr("reckon.crew.list_live", lambda *a, **k: list(pointers))


def test_the_head_reads_ready_idle_with_its_coordinator_named(
    board: dict[str, Any],
) -> None:
    """The path's first link says it is idle, in fields and in words."""

    critical = board["critical_path"]
    assert critical["head"] == HEAD
    assert critical["sections"][0] == HEAD

    head = _link(board, HEAD)
    assert head["primary_state"] == READY_IDLE
    assert head["coordinator"] == HEAD_COORDINATOR
    assert head["holds"] == []

    # An idle head is said so in words, naming who to ask about it.
    note = str(critical["head_note"])
    assert "idle" in note
    assert HEAD in note
    assert HEAD_COORDINATOR in note


def test_a_gated_link_reports_its_gate_and_every_hold_it_carries(
    board: dict[str, Any],
) -> None:
    """A link behind a gate reads as gated, with both of its holds beside it."""

    link = _link(board, GATED_LINK)
    assert link["primary_state"] == GATED
    assert link["gate"] == GATED_GATE
    # Both holds, in the order the plan names them: the gate first, the unmet
    # wait after it. A holds list that drops either one fails here.
    assert [hold["id"] for hold in link["holds"]] == GATED_HOLDS
    assert [hold["kind"] for hold in link["holds"]] == ["gate", "section-dependency"]


def test_each_link_carries_the_fact_that_decided_its_state(
    board: dict[str, Any],
) -> None:
    """Every state names its own evidence, and the fixture exercises three."""

    seen = set()
    for link in _links(board):
        state = link["primary_state"]
        seen.add(state)
        if state == IN_FLIGHT:
            assert link["run_id"], link
        elif state == READY_IDLE:
            assert "coordinator" in link, link
        elif state == GATED:
            assert link["gate"], link
        elif state == WAITING:
            assert link["wait"], link
            assert link["wait"] in [hold["id"] for hold in link["holds"]], link
    # The fixture forces a ready-idle head, a gated link and a link that only
    # waits, so a state that silently stopped being reported is missed here.
    assert seen == {READY_IDLE, GATED, WAITING}


def test_the_links_keep_the_paths_order_and_its_ref_list(
    board: dict[str, Any],
) -> None:
    """The head is first, and the ref list every other reader uses is unchanged."""

    critical = board["critical_path"]
    links = _links(board)
    assert links, critical
    assert [link["ref"] for link in links] == list(critical["sections"])
    assert critical["head"] == links[0]["ref"]
    assert all(isinstance(ref, str) for ref in critical["sections"])


def _evidence_rows(text: str) -> dict[str, str]:
    """The evidence table's rows by link ref, each as the row's own text."""

    rows: dict[str, str] = {}
    for block in text.split("<tr>")[1:]:
        row = block.split("</tr>", 1)[0]
        named = re.search(r"<code>([^<]+)</code>", row)
        if named:
            rows[named.group(1)] = row
    return rows


def test_the_evidence_table_records_every_link_and_every_hold(
    board: dict[str, Any],
) -> None:
    """The record a reviewer reads names every link the payload carries.

    The table is the only place a reader sees what the path looks like, so a
    link left out of it — or a hold dropped from a row — understates the
    payload the roadmap actually returns.
    """

    rows = _evidence_rows(FRAGMENT.read_text(encoding="utf-8"))
    for link in _links(board):
        row = rows.get(link["ref"])
        assert row is not None, (
            f"{link['ref']} is missing from the evidence table: {sorted(rows)}"
        )
        for hold in link["holds"]:
            assert hold["id"] in row, f"{link['ref']} omits its hold {hold['id']}"


def test_a_link_held_by_an_open_decision_names_that_decision() -> None:
    """A decision-held link reads as waiting on the decision, not as unexplained.

    The section rows carry governance decisions beside the gates and the unmet
    waits, so a link that dropped the decision would report no state and no
    holds for a section the row beside it says is held.
    """

    plan_slug, _, section = HEAD.partition("#")
    critical: dict[str, Any] = {"sections": [HEAD]}
    _critical_path_links(
        critical,
        section_runs={},
        ready_sections_by_plan={},
        section_holds_by_plan={
            plan_slug: {
                section: [
                    {
                        "kind": DECISION_BLOCKER,
                        "key": "d-winding-gauge",
                        "status": "open",
                    }
                ]
            }
        },
        resolve_coordinators=dict,
    )

    link = critical["links"][0]
    assert link["primary_state"] == WAITING
    assert link["wait"] == "d-winding-gauge"
    assert link["holds"] == [
        {"kind": DECISION_BLOCKER, "id": "d-winding-gauge", "status": "open"}
    ]


def test_a_live_run_on_the_head_turns_its_state_to_in_flight(
    board: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: the state is decided by the run, not by the ref.

    The head is ready-idle on the fixture. Given a live run it must read in
    flight naming that run, and the sentence must stop calling it idle — a
    path that reported a fixed state per row passes the first case and fails
    this one. The run's id is minted here rather than written down, and a
    second live run sits on another section of the same plan, so the id the
    head reports has to be the one the derivation matched to this section
    rather than whichever run the live set happened to hold.
    """

    assert _link(board, HEAD)["primary_state"] == READY_IDLE

    head_plan, _, head_section = HEAD.partition("#")
    on_head = f"run-live-{uuid4().hex[:8]}"
    elsewhere = f"run-live-{uuid4().hex[:8]}"
    _live_runs(
        monkeypatch,
        _pointer(run_id=on_head, plan=head_plan, section=head_section),
        _pointer(run_id=elsewhere, plan=head_plan, section="s1"),
    )
    busy = _build()

    head = _link(busy, HEAD)
    assert head["primary_state"] == IN_FLIGHT
    assert head["run_id"] == on_head
    assert head["run_id"] != elsewhere
    note = str(busy["critical_path"]["head_note"])
    assert "idle" not in note
    assert "in flight" in note
    assert on_head in note
