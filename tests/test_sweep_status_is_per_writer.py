"""The sweep status record keeps one entry per writer, not one per project.

A reader wanting to know whether THEIR follower is sweeping needs the record to
answer per writer: the file is written by every follower on the project, by
hand-run sweeps, and from the MCP surface, and the last stamp is only the last
write by any writer — twice measured to mislead a reader (a seventy-minute-old
follower stamp read as cadence, and a hand-run stamp read as follower health).
Each entry carries the kind of writer it came from, so the record answers from
its own contents without a process lookup, which is all a shared filesystem
can safely answer with.

Every sweep here is dry-run against a temporary configuration home; nothing
launches, and no writer ever reaches the real configuration directory.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew.resumption import (
    MAX_SWEEP_WRITERS,
    SWEEP_WRITER_KINDS,
    read_sweep_status,
    sweep,
    sweep_status_path,
    writer_sweep_status,
)

# A project name no real configuration uses, so the isolation assertion below
# cannot trip over a legitimate production sweep of the same name.
PROJECT = "sweep-record-probe"


def _real_crew_home() -> Path:
    """The crew home production resolves, ignoring this test's RECKON_HOME."""
    xdg = Path.home() / ".config" / "reckon"
    return (xdg if xdg.exists() else Path.home() / "docs-server") / "crew"


@pytest.fixture
def sweep_clock(monkeypatch: pytest.MonkeyPatch):
    """Give every sweep a distinct, increasing timestamp.

    The status keys on ``swept_at`` only for its bound, and two writes within
    the same second would tie it, so the bound test needs a clock that moves.
    """
    from reckon.crew import resumption

    state = {"stamp": datetime(2026, 9, 6, 19, 0, 0, tzinfo=UTC)}

    def _utc_now() -> str:
        state["stamp"] = state["stamp"].replace(second=state["stamp"].second + 1)
        return state["stamp"].isoformat(timespec="seconds").replace("+00:00", "Z")

    monkeypatch.setattr(resumption, "_utc_now", _utc_now)
    return state


def test_two_writers_in_sequence_both_remain_readable(sweep_clock: dict) -> None:
    """Neither writer's entry is overwritten by the other's later sweep.

    This is the incident's hard case reproduced as a record: two followers on
    one project, alternating, neither cadence readable from a single slot.
    """
    first = sweep(
        PROJECT, dry_run=True, writer={"kind": "follower", "session": "sess-a"}
    )
    second = sweep(
        PROJECT, dry_run=True, writer={"kind": "follower", "session": "sess-b"}
    )

    recorded = read_sweep_status(PROJECT)
    writers = recorded["writers"]
    assert writers["follower:sess-a"]["swept_at"] == first["swept_at"]
    assert writers["follower:sess-b"]["swept_at"] == second["swept_at"]
    assert set(writers) == {"follower:sess-a", "follower:sess-b"}


def test_reader_gets_one_writers_last_sweep_and_not_anothers(sweep_clock: dict) -> None:
    """Asking for one writer returns that writer's entry, refreshed on repeat."""
    first = sweep(
        PROJECT, dry_run=True, writer={"kind": "follower", "session": "sess-a"}
    )
    sweep_mid = dict(first)
    sweep(PROJECT, dry_run=True, writer={"kind": "follower", "session": "sess-b"})
    last = sweep(
        PROJECT, dry_run=True, writer={"kind": "follower", "session": "sess-a"}
    )

    # A reader asking for sess-a gets sess-a's MOST RECENT sweep, not the
    # earlier one and not sess-b's.
    entry = writer_sweep_status(PROJECT, {"kind": "follower", "session": "sess-a"})
    assert entry is not None
    assert entry["key"] == "sess-a"
    assert entry["swept_at"] == last["swept_at"]
    assert entry["swept_at"] != sweep_mid["swept_at"]
    assert (
        writer_sweep_status(PROJECT, {"kind": "follower", "session": "sess-b"})["key"]
        == "sess-b"
    )


def test_a_writer_that_never_wrote_reads_unknown_not_found_nothing(
    sweep_clock: dict,
) -> None:
    """Unknown and swept-and-found-nothing are different answers.

    A reader asking about a writer that never wrote must not be told the
    writer swept; the record says so by the writer being absent (None) rather
    than present with a zero count.
    """
    sweep(PROJECT, dry_run=True, writer={"kind": "follower", "session": "sess-live"})

    swept = writer_sweep_status(PROJECT, {"kind": "follower", "session": "sess-live"})
    never = writer_sweep_status(PROJECT, {"kind": "follower", "session": "sess-ghost"})

    assert swept is not None
    assert swept["checked"] == 0  # it ran and found nothing
    assert never is None  # it never wrote at all
    # A project no one has swept reads the same way at the project level.
    assert read_sweep_status("never-swept-project") == {}


def test_a_hand_run_and_a_follower_are_distinguishable_without_a_process(
    sweep_clock: dict,
) -> None:
    """The record tells a follower pass from a manual one by its own contents.

    No process table is consulted: everything here is read back from the file.
    """
    sweep(PROJECT, dry_run=True, writer="command")
    sweep(PROJECT, dry_run=True, writer={"kind": "follower", "session": "sess-live"})

    recorded = read_sweep_status(PROJECT)
    command = recorded["writers"]["command"]
    follower = recorded["writers"]["follower:sess-live"]
    assert command["kind"] == "command"
    assert follower["kind"] == "follower"
    assert command["kind"] != follower["kind"]
    assert {"command", "follower"} <= SWEEP_WRITER_KINDS
    # An undeclared caller is its own kind, not silently a follower.
    sweep(PROJECT, dry_run=True)
    assert recorded["writers"]
    assert read_sweep_status(PROJECT)["writers"]["other"]["kind"] == "other"


def test_project_level_view_still_answers_for_a_single_writer(
    sweep_clock: dict,
) -> None:
    """What the status file answered before, it still answers.

    A single writer's file keeps the fields the previous shape carried — the
    instant, the flags, the counts and the pid — and adds the writer's own
    entry under ``writers``.
    """
    report = sweep(PROJECT, dry_run=True)

    recorded = read_sweep_status(PROJECT)
    assert recorded["project"] == PROJECT
    assert recorded["swept_at"] == report["swept_at"]
    assert recorded["dry_run"] == report["dry_run"]
    assert recorded["checked"] == 0
    assert recorded["resumed"] == 0
    assert recorded["skipped"] == 0
    assert recorded["swept_by_pid"] > 0
    assert set(recorded["writers"]) == {"other"}
    assert recorded["writers"]["other"]["swept_at"] == report["swept_at"]
    assert recorded["writers"]["other"]["kind"] == "other"


def test_the_bound_removes_oldest_writers_and_nothing_else(sweep_clock: dict) -> None:
    """Writing past the bound evicts the least recently swept writers.

    The stated rule is an LRU cap: when the map passes MAX_SWEEP_WRITERS the
    oldest entries drop until it fits, the writer that just wrote always kept,
    and every other field survives untouched.
    """
    for number in range(MAX_SWEEP_WRITERS + 2):
        sweep(
            PROJECT,
            dry_run=True,
            writer={"kind": "follower", "session": f"sess-{number:02d}"},
        )

    recorded = read_sweep_status(PROJECT)
    writers = recorded["writers"]
    assert len(writers) == MAX_SWEEP_WRITERS
    # The two oldest are gone...
    assert set(writers) == {
        f"follower:sess-{number:02d}" for number in range(2, MAX_SWEEP_WRITERS + 2)
    }
    # ...and the survivors keep their own sweeps intact.
    for number in range(2, MAX_SWEEP_WRITERS + 2):
        name = f"sess-{number:02d}"
        entry = writers[f"follower:{name}"]
        assert entry["kind"] == "follower"
        assert entry["key"] == name
        assert entry["resumed"] == 0
    # The project-level view is the newest sweep, and projects/swept fields
    # were not touched by the eviction.
    newest = writers[f"follower:sess-{MAX_SWEEP_WRITERS + 1:02d}"]
    assert recorded["swept_at"] == newest["swept_at"]
    assert recorded["project"] == PROJECT


def test_the_real_configuration_directory_gains_no_file(sweep_clock: dict) -> None:
    """An isolated write stays isolated.

    The sweep writes to the temporary configuration home; the corresponding
    path in the real configuration directory must remain untouched whether it
    existed before or not.
    """
    real_status = _real_crew_home() / "recovery" / f"{PROJECT}.status.json"

    sweep(PROJECT, dry_run=True, writer={"kind": "follower", "session": "sess-x"})
    sweep(PROJECT, dry_run=True, writer="command")

    assert sweep_status_path(PROJECT).is_file()  # the isolated write landed
    assert not real_status.exists()  # and nothing escaped to the real home
    assert sweep_status_path(PROJECT).parent != real_status.parent
