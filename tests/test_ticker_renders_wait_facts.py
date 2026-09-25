"""A row tells a wait whose probe has never run from one already being tested.

A declared wait reaches the record with the probe's verdict beside it, and the
verdict is absent when no probe has run — the two situations are different:
nothing is testing an unprobed wait, so its condition cannot lift on its own,
while a probed wait is being read and simply has not gone terminal. The row
rendered them identically, which is how a condition nobody was checking read to
a reader as a slow dependency.

These tests bind the distinction to the rendered text: the same row, once with
a probe verdict and once without, must read differently, and over the domain of
wait facts — never run, run and unmet, run and met, and no wait declared.
"""

from __future__ import annotations

import re
from pathlib import Path

from reckon.crew import recovery
from reckon.crew import ticker as ticker_module

HORIZON = 3540
PROJECT = "fixture-project"


def row(**overrides) -> dict:
    """A transition event in the facts shape, with no wait declared."""
    event = {
        "observed_at": "2026-09-17T09:16:13+00:00",
        "run_id": "r-wait-facts",
        "node": "n-wait-facts",
        "session": "ship-s18-20260917",
        "role": "implement",
        "model": "dsv4-flash",
        "effort": "medium",
        "detail": "waiting 1870s on scheduler job 42",
        "from_state": "working",
        "to_state": "waiting",
        "working": 2,
        "blocked": 0,
        "unpromoted": 0,
        "waiting": 1,
    }
    event.update(overrides)
    return event


def grid(width: int = 180) -> ticker_module.Ticker:
    return ticker_module.Ticker(width=width, theme="light", color=False)


def plain(line: str) -> str:
    """The rendered row as a reader sees it, without any colour."""
    return re.sub(r"\x1b\[[0-9;]*m", "", line)


def rendered(event: dict, *, width: int = 180) -> str:
    return plain(grid(width).render(event))


def unprobed(*, to_state: str = "waiting", **overrides) -> dict:
    """A waiting row whose declared condition has never been probed."""
    return row(
        to_state=to_state,
        wait_overdue=False,
        wait_condition_state=None,
        wait_observed=None,
        expected_horizon_seconds=HORIZON,
        **overrides,
    )


def probed(*, wait_condition_state: str = "unknown", **overrides) -> dict:
    """A waiting row whose probe ran and whose terminal is not yet met."""
    return row(
        to_state="waiting",
        wait_overdue=False,
        wait_condition_state=wait_condition_state,
        wait_observed="PENDING",
        expected_horizon_seconds=HORIZON,
        **overrides,
    )


def test_an_unprobed_wait_renders_differently_from_a_pending_one() -> None:
    """The falsifier: one row, with the probe's verdict and without it.

    The verdict is the record of execution, so removing it must change the row
    a reader sees. At base both rendered identically — the verdict never
    reached the renderer — and the marker is the difference it carries now.
    """
    never = rendered(unprobed())
    pending = rendered(probed())

    assert never != pending, "an unprobed wait still renders as a pending one"

    never_clause = grid()._reason(unprobed(), "waiting", 45)
    pending_clause = grid()._reason(probed(), "waiting", 45)
    assert never_clause == ticker_module.UNPROBED_MARKER + " " + pending_clause


def test_the_reason_carries_the_marker_and_the_verdict_it_stands_for() -> None:
    """Every case in the domain, read at the clause the row renders."""
    t = grid()
    marker = ticker_module.UNPROBED_MARKER
    never = t._reason(unprobed(), "waiting", 45)
    pending = t._reason(probed(), "waiting", 45)
    met = t._reason(probed(wait_condition_state="met"), "waiting", 45)
    bare = t._reason(row(), "waiting", 45)
    assert never.startswith(marker + " ")
    assert never.endswith(pending)
    assert not pending.startswith(marker)
    assert not met.startswith(marker)
    assert not bare.startswith(marker)


def test_a_row_declaring_no_wait_is_unchanged_by_absent_or_none_facts() -> None:
    """The carriage is additive: the two event shapes a run with no wait can
    reach the renderer in produce one row.

    The two shapes are the pre-facts one, with none of the five keys present,
    and the facts one, with all five present and None — the shape the composer
    emits for a run that declares nothing. Compared here as one rendering, so a
    predicate that keyed on a fact's *presence* rather than its value would show
    the difference, and the clause is asserted empty because a row that
    declares no wait has nothing for the marker to stand for.
    """
    bare = row()
    explicit = row(
        wait_condition_state=None,
        wait_observed=None,
        wait_overdue=None,
        expected_horizon_seconds=None,
        resume_brief=None,
    )
    assert rendered(bare) == rendered(explicit)
    assert ticker_module.UNPROBED_MARKER + " " not in grid()._reason(
        bare, "waiting", 45
    )


def test_the_marker_costs_no_width_and_marker_and_unmarked_rows_align() -> None:
    """The marked row is exactly the requested width, columns and all."""
    never = rendered(unprobed(), width=180)
    pending = rendered(probed(), width=180)
    assert len(never) == len(pending) == 180
    counters = re.compile(r"(\s?\d{1,2})w(\u00b7\s?\d{1,2})b")
    marked = counters.search(never)
    unmarked = counters.search(pending)
    assert marked and unmarked
    assert marked.start() == unmarked.start()
    # The marker is one glyph in a cell held at one width on every row, so it
    # adds a mark without moving a column: the state cell lands on the same
    # screen position whether the row carries the marker or not.
    assert never.index("waiting") == pending.index("waiting")
    assert never.count(ticker_module.UNPROBED_MARKER) == (
        pending.count(ticker_module.UNPROBED_MARKER) + 1
    )


def test_the_marker_survives_a_clause_with_no_room_for_it() -> None:
    """A row too narrow for any reason still says the wait is unprobed.

    The reason column is the only one present in every state, so it is where
    the fact has to live; if it were dropped when the clause cannot fit, the
    narrow terminal that needs it most would be the one that cannot show it.
    """
    t = grid(width=ticker_module.MIN_WIDTH)
    clause = t._reason(unprobed(), "waiting", ticker_module.MIN_REASON - 1)
    assert clause == ticker_module.UNPROBED_MARKER
    assert t._reason(probed(), "waiting", ticker_module.MIN_REASON - 1) == ""


def test_a_live_run_whose_declaration_was_never_probed_carries_the_marker(
    tmp_path: Path,
) -> None:
    """The reachable shape: a run declaring a wait it cannot be trusted to probe.

    A valid declaration is probed on every snapshot, so the unprobed situation
    is reached by a declaration whose probe is missing. The classifier is right
    that nothing has been observed, and the row must not read as ordinary work
    while nothing tests the condition it named.
    """
    snapshot = _snapshot(
        tmp_path, run_id="r-unprobed-live", manifest_text=INCOMPLETE_WAIT_MANIFEST
    )
    event = _event(snapshot)
    assert event["wait_condition_state"] is None, "the fixture probed after all"
    line = rendered(event)
    assert ticker_module.UNPROBED_MARKER + " " in line


def test_a_declared_wait_that_was_probed_carries_no_marker(tmp_path: Path) -> None:
    """The control for the shape above: same node, a probe that does run."""
    snapshot = _snapshot(
        tmp_path, run_id="r-probed-live", manifest_text=PROBED_WAIT_MANIFEST
    )
    event = _event(snapshot)
    assert event["wait_condition_state"] is not None, "the probe's verdict is absent"
    clause = grid()._reason(event, "waiting", 45)
    assert not clause.startswith(ticker_module.UNPROBED_MARKER), clause


WAIT_STARTED_AT = "1970-01-01T00:00:00+00:00"

INCOMPLETE_WAIT_MANIFEST = (
    "status: waiting\n"
    "wait_condition: scheduler job 42\n"
    f"wait_started_at: {WAIT_STARTED_AT}\n"
    "resume_brief: collect the scheduler result\n"
)

PROBED_WAIT_MANIFEST = (
    "status: waiting\n"
    "wait_condition: scheduler job 42\n"
    'wait_probe: ["printf", "PENDING"]\n'
    'wait_terminal: ["COMPLETED", "FAILED"]\n'
    f"wait_started_at: {WAIT_STARTED_AT}\n"
    "resume_brief: collect the scheduler result\n"
)


def _pointer(tmp_path: Path, *, run_id: str, manifest_text: str) -> dict:
    worktree = tmp_path / "worktrees" / run_id
    worktree.mkdir(parents=True, exist_ok=True)
    manifest = tmp_path / f"{run_id}-manifest.md"
    manifest.write_text(manifest_text, encoding="utf-8")
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": f"session-{run_id}",
        "role": "implement",
        "node": {"id": run_id, "plan": "plan-a", "section": "s4", "role": "implement"},
        "phase": "working",
        "process_alive": True,
        "worktree": str(worktree),
        "log_path": str(tmp_path / "stream.jsonl"),
        "stderr_path": str(tmp_path / "stderr.log"),
        "manifest_path": str(manifest),
    }


def _snapshot(tmp_path: Path, *, run_id: str, manifest_text: str) -> dict:
    return recovery._watch_snapshot(
        _pointer(tmp_path, run_id=run_id, manifest_text=manifest_text),
        moment=0.0,
        stall_seconds=3600,
    )


def _event(snapshot: Path) -> dict:
    return recovery._watch_transition(
        PROJECT,
        kind="transition",
        snapshot=snapshot,
        previous=None,
        current=str(snapshot.get("state") or ""),
        counts={"working": 0, "blocked": 0, "unpromoted": 0, "waiting": 1},
        spend_runs=[],
        rate_statuses={},
    )


def test_unprobed_marker_is_a_clause_glyph_not_an_attention_column() -> None:
    """A wait nobody probes on a run that also needs help still says so.

    The unprobed marker says the wait's condition is not being tested, and it
    rides the clause rather than a column of its own: the separate attention
    column was dropped, so a row that needs help says it through the
    destination state's colour and the clause alone.
    """
    t = grid()
    held = unprobed(to_state="needs-help", needs_help_complete=True)
    clause = t._reason(held, "needs-help", 45)
    line = rendered(held)
    assert clause.startswith(ticker_module.UNPROBED_MARKER + " ")
    assert "!" not in line
    assert line[ticker_module.CLOCK + ticker_module.GAP] != " "


def test_a_real_run_declaring_no_wait_carries_no_marker(tmp_path: Path) -> None:
    """The payload control: an ordinary manifest, through the whole pipeline.

    The synthetic rows above are built to the shape the composer produces, and
    a predicate reading the wrong fact would still pass them — a run that
    declares nothing emits None for all five, but so would any value the
    composer defaults. This reads the event the composer actually builds from
    a manifest that declares no wait, which is the population most rows belong
    to and the one a marker must never touch.
    """
    snapshot = _snapshot(
        tmp_path, run_id="r-no-wait", manifest_text="status: working\n"
    )
    event = _event(snapshot)
    for fact in (
        "wait_condition_state",
        "wait_observed",
        "wait_overdue",
        "expected_horizon_seconds",
        "resume_brief",
    ):
        assert event[fact] is None, f"{fact} was invented for a run declaring no wait"
    # The marker can only live in the reason clause, so an empty clause is
    # the statement that the row is unchanged for a run that declares nothing.
    assert grid()._reason(event, str(event.get("to_state") or ""), 45) == ""


def test_a_real_waiting_manifest_that_cannot_be_probed_carries_the_marker(
    tmp_path: Path,
) -> None:
    """The reachable shape, read end to end: a manifest asking to wait.

    A complete declaration is probed on every snapshot, so the unprobed case is
    a manifest that asks to wait and cannot be read as one — the classifier
    renders it working and the row said nothing about the wait it declared.
    """
    snapshot = _snapshot(
        tmp_path, run_id="r-waiting-bare", manifest_text="status: waiting\n"
    )
    event = _event(snapshot)
    assert event["wait_condition_state"] is None, "the fixture produced a verdict"
    assert event["expected_horizon_seconds"] is not None, "no wait was declared at all"
    clause = grid()._reason(event, str(event.get("to_state") or ""), 45)
    assert clause.startswith(ticker_module.UNPROBED_MARKER)
