"""Hermetic state-transition ticker contracts for a watched fleet."""

from __future__ import annotations

import io
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, ledger
from reckon.crew import recovery
from reckon.crew import ticker as ticker_module


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Move all pointers and watcher records into a temporary home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _write_pointer(home: Path, run_id: str, node: str, *, phase: str) -> None:
    stream = home / "streams" / f"{run_id}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n')
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "node": {"id": node, "plan": "plan-a", "time_budget": "20m"},
            "phase": phase,
            "created_at": datetime.now(tz=UTC).isoformat(),
            "manifest_path": str(home / "manifests" / f"{run_id}.md"),
            "log_path": str(stream),
            "process_alive": None,
        },
    )


def _set_phase(run_id: str, phase: str) -> None:
    pointer = crew.read_pointer(run_id)
    pointer["phase"] = phase
    crew._write_json(crew.pointer_path(run_id), pointer)


def _deliver(home: Path, run_id: str, status: str, *, blocker: str = "") -> None:
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: ticker-node\nstatus: {status}\ncommits: HEAD\n"
        f"blockers: {blocker or 'none'}\n"
    )


def _record_ledger_row(run_id: str) -> None:
    """Write the promotion row a completed run leaves behind when it lands.

    A departure is not a fact the pointer records: a promotion and a pointer
    that vanished with nothing behind it look identical from the fleet. The
    ledger decides which one it was, so a fixture that models a landing records
    the row a promotion would have written.
    """
    path = ledger.run_path("proj", run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": run_id}))


def _event(**overrides) -> dict:
    event = {
        "project": "proj",
        "event": "transition",
        "observed_at": "2026-08-24T19:35:45Z",
        "run_id": "r-ticker",
        "node": "ticker-node",
        "from_state": "working",
        "to_state": "blocked",
        "working": 3,
        "blocked": 1,
        "unpromoted": 0,
        "reason": "first clause",
    }
    event.update(overrides)
    if "run_id" not in overrides:
        event["run_id"] = f"r-{event['node']}"
    return event


def test_ticker_opens_with_one_baseline_per_live_run(home) -> None:
    _write_pointer(home, "r-first", "first-node", phase="starting")
    _write_pointer(home, "r-second", "second-node", phase="working")
    stream = recovery.watch_ticker("proj", stall_window="1h")

    try:
        baseline = [next(stream), next(stream)]
    finally:
        stream.close()

    assert [event["event"] for event in baseline] == ["baseline", "baseline"]
    assert [event["node"] for event in baseline] == ["first-node", "second-node"]
    assert [event["from_state"] for event in baseline] == [None, None]
    assert [event["to_state"] for event in baseline] == ["dispatched", "working"]
    assert {
        (event["working"], event["blocked"], event["unpromoted"]) for event in baseline
    } == {(2, 0, 0)}


def test_ticker_emits_only_changes_and_ends_after_the_last_promotion(home) -> None:
    _write_pointer(home, "r-existing", "existing-node", phase="starting")
    sleeps = 0

    def advance(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            return
        if sleeps == 2:
            _write_pointer(home, "r-new", "new-node", phase="starting")
        elif sleeps == 3:
            _set_phase("r-new", "working")
        elif sleeps == 4:
            _deliver(
                home,
                "r-new",
                "blocked",
                blocker="dependency unavailable; retry after configuration changes",
            )
        elif sleeps == 5:
            _deliver(home, "r-new", "complete")
            _record_ledger_row("r-new")
        elif sleeps == 6:
            crew.pointer_path("r-new").unlink()
        elif sleeps == 7:
            crew.pointer_path("r-existing").unlink()
        else:
            pytest.fail("ticker did not end after the last pointer was reconciled")

    stream = recovery.watch_ticker(
        "proj", stall_window="1h", poll_interval=0, sleeper=advance
    )
    baseline = next(stream)
    transitions = list(stream)

    assert baseline["event"] == "baseline"
    assert [
        (event["node"], event["from_state"], event["to_state"]) for event in transitions
    ] == [
        ("new-node", None, "dispatched"),
        ("new-node", "dispatched", "working"),
        ("new-node", "working", "blocked"),
        # A completed manifest without an independent review sits as
        # completed_unpromoted until the review lands, which is the state a
        # departure then leaves.
        ("new-node", "blocked", "completed_unpromoted"),
        ("new-node", "completed_unpromoted", "promoted"),
        ("existing-node", "dispatched", "withdrawn"),
    ]
    # Each triple is the fleet after that transition, and the three buckets
    # partition it — a blocked or delivered run leaves the working count, which
    # is what a reader takes "working" to mean.
    assert [
        (event["working"], event["blocked"], event["unpromoted"])
        for event in transitions
    ] == [
        (2, 0, 0),
        (2, 0, 0),
        (1, 1, 0),
        (1, 0, 1),
        (1, 0, 0),
        (0, 0, 0),
    ]
    # The log stores the full detail as a fact; the one clause a reader acts on
    # is derived at render time, so nothing is truncated before it is stored.
    assert (
        transitions[2]["detail"]
        == "dependency unavailable; retry after configuration changes"
    )
    assert "reason" not in transitions[2]
    rendered = recovery.format_watch_transition(
        transitions[2], ticker=ticker_module.Ticker(width=208, color=False)
    )
    assert "dependency unavailable" in rendered
    assert "retry after configuration" not in rendered
    assert sleeps == 7


def test_ticker_line_is_compact_and_bounds_free_text_to_one_clause() -> None:
    line = recovery.format_watch_transition(
        _event(reason="first clause; second clause that must not reach the terminal")
    )

    # The stamp is stored UTC and rendered in the reader's own zone, because
    # the pane sits beside a harness that timestamps locally.
    assert line.startswith(ticker_module.local_clock(_event()["observed_at"]))
    assert line.index("ticker-node") < line.index("working")
    # The row carries one transition, centred on its fixed arrow.
    assert "working → blocked" in line
    assert ticker_module.BASELINE_MARKER not in line
    # The counts are a fixed grid whose digits share a column, each number
    # followed by its state's single letter, one space between the cells.
    assert " 3w  1b  0u" in line
    # Free text is bounded to one clause and stays on the line; a second row
    # would cost a quarter of a pane that shows about eight.
    assert "first clause" in line
    assert "second clause" not in line
    assert "\n" not in line


def test_counter_cells_stay_apart_at_every_digit_width() -> None:
    """Two counts never run into each other, whatever their digit width.

    Every counter is its count right-aligned to a fixed digit width and followed
    by the bucket's letter, with one space between cells, so ``10w 12b`` is two
    tokens at any digit width and the abutting ``10w12b`` is never printed as
    one. The right alignment is what lets that hold together with the block's
    own width: a single-digit count pads inside its own cell rather than
    shifting the letters beside it, so every letter keeps its column and the
    text after the block starts on one column whatever the counts. Both ends are
    read off the rendered row rather than from the module's width constant,
    because the property is the row's — a renderer that stops reserving the
    width inside its cells shortens the block and has to fail here.
    """
    escapes = re.compile(r"\x1b\[[0-9;]*m")
    counters = re.compile(r"(\d+)w\s+(\d+)b\s+(\d+)u\s+(\d+)q")

    block_ends: set[int] = set()
    clause_starts: set[int] = set()
    cases = (
        (10, 12, 0, 0),
        (99, 99, 0, 0),
        (99, 99, 99, 99),
        (1, 0, 0, 0),
        (3, 1, 0, 0),
    )
    for index, (working, blocked, unpromoted, queued) in enumerate(cases):
        # A grid per case: the renderer remembers the last destination state of
        # a run, so reusing one run id would suppress the rows after the first.
        grid = ticker_module.Ticker(width=180, color=False, model_aliases=())
        row = escapes.sub(
            "",
            grid.render(
                _event(
                    run_id=f"r-digits-{index}",
                    working=working,
                    blocked=blocked,
                    unpromoted=unpromoted,
                    waiting=queued,
                )
            ),
        )
        match = counters.search(row)
        assert match is not None, (working, blocked, unpromoted, queued, row)
        assert match.groups() == (
            str(working),
            str(blocked),
            str(unpromoted),
            str(queued),
        ), row
        # Each count is its own whitespace-separated token, and no bucket's
        # letter is followed straight away by the next count's digits.
        assert len(match.group(0).split()) == 4, row
        for left, right, letter in (
            (working, blocked, "w"),
            (blocked, unpromoted, "b"),
            (unpromoted, queued, "u"),
        ):
            assert f"{left}{letter}{right}" not in row, row
        # Both ends of the block come off the rendered row itself. The block's
        # right edge is where the four counts stop and the row's own free text
        # is where it appears, so a renderer that stops reserving the block's
        # width moves one of them and fails here rather than agreeing with a
        # constant both sides read.
        block_ends.add(match.end())
        clause_starts.add(row.index(_event()["reason"]))

    # The block's reserved right edge holds whatever the counts, so the free
    # text after it begins at one column on every row: a smaller count's spare
    # columns are the space inside its own cell, not a shift of everything after
    # it.
    assert len(block_ends) == 1, sorted(block_ends)
    assert len(clause_starts) == 1, sorted(clause_starts)
    assert clause_starts.pop() > block_ends.pop()


def test_cli_follow_prints_compact_transition_lines_by_default(
    home, monkeypatch
) -> None:
    monkeypatch.setattr(
        recovery,
        "watch_follow",
        lambda *_args, **_kwargs: iter([_event()]),
    )

    result = CliRunner().invoke(
        cli_module.main,
        ["crew", "watch", "--project", "proj", "--follow", "--no-color"],
    )

    assert result.exit_code == 0
    assert result.output.count("\n") == 1
    assert "ticker-node" in result.output
    assert "working → blocked" in result.output
    assert not result.output.startswith("{")


def test_follow_renders_at_the_resolved_terminal_width(home, monkeypatch) -> None:
    """An unspecified --width builds the grid at the terminal a reader owns,
    not at the stated fallback, so the counters land flush on the resolved
    width. The measurement is asserted through the command's own grid — the
    call site the module-level resolver had been bypassed from — rather than
    by instantiating a Ticker directly.
    """
    monkeypatch.setattr(ticker_module, "resolve_terminal_width", lambda: 207)
    monkeypatch.setattr(
        recovery,
        "watch_follow",
        lambda *_args, **_kwargs: iter([_event()]),
    )

    result = CliRunner().invoke(
        cli_module.main,
        ["crew", "watch", "--project", "proj", "--follow", "--no-color"],
    )

    assert result.exit_code == 0
    line = result.output.splitlines()[0]
    assert len(line) == 207
    # The counters hold their own column inside the resolved-width grid, ahead
    # of the reason a clipping pane is allowed to cut.
    assert " 3w  1b  0u" in line


def test_follow_explicit_width_beats_the_measurement(home, monkeypatch) -> None:
    """A stated --width still wins over the resolved terminal, so a caller who
    asks for a specific pane gets it even when the measurement differs."""
    monkeypatch.setattr(ticker_module, "resolve_terminal_width", lambda: 207)
    monkeypatch.setattr(
        recovery,
        "watch_follow",
        lambda *_args, **_kwargs: iter([_event()]),
    )

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "watch",
            "--project",
            "proj",
            "--follow",
            "--no-color",
            "--width",
            "180",
        ],
    )

    assert result.exit_code == 0
    # Read without stripping: the reason is the trailing column, so a row whose
    # clause is short ends in padding that carries the grid out to its width.
    line = result.output.splitlines()[0]
    assert len(line) == 180


def test_cli_follow_keeps_machine_objects_behind_json_flag(home, monkeypatch) -> None:
    monkeypatch.setattr(
        recovery,
        "watch_follow",
        lambda *_args, **_kwargs: iter([_event()]),
    )

    result = CliRunner().invoke(
        cli_module.main,
        ["crew", "watch", "--project", "proj", "--follow", "--json"],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["ok"] is True
    assert payload["node"] == "ticker-node"
    assert payload["from_state"] == "working"
    assert payload["to_state"] == "blocked"
    assert (payload["working"], payload["blocked"], payload["unpromoted"]) == (3, 1, 0)


def test_follow_emit_path_preserves_painted_escape_codes() -> None:
    """The stream `_echo_follow_line` writes to is a pipe into a pane, not a
    terminal — exactly where Click's default auto-detection strips ANSI
    before a single reader ever sees it. A line the ticker painted must
    reach that stream with its escapes intact.
    """
    line = ticker_module.Ticker(color=True).render(_event())
    assert "\x1b[38;5;" in line  # sanity: the rendered line really is painted

    stream = io.StringIO()
    assert not stream.isatty()
    cli_module._echo_follow_line(line, stream=stream)

    assert "\x1b[38;5;" in stream.getvalue()


def test_cli_watch_follow_emit_site_preserves_painted_escape_codes(
    home, monkeypatch
) -> None:
    """`crew watch --follow` writes through its own emit site, not
    `_echo_follow_line` — it must not let Click strip colour there either."""
    monkeypatch.setattr(
        recovery,
        "watch_follow",
        lambda *_args, **_kwargs: iter([_event()]),
    )

    result = CliRunner().invoke(
        cli_module.main,
        ["crew", "watch", "--project", "proj", "--follow"],
    )

    assert result.exit_code == 0
    assert "\x1b[38;5;" in result.output


@pytest.mark.parametrize("suppress", ["flag", "env"])
def test_cli_watch_follow_emits_no_escapes_when_colour_is_off(
    home, monkeypatch, suppress
) -> None:
    """--no-color and NO_COLOR both leave the emitted line free of escapes."""
    monkeypatch.setattr(
        recovery,
        "watch_follow",
        lambda *_args, **_kwargs: iter([_event()]),
    )
    args = ["crew", "watch", "--project", "proj", "--follow"]
    if suppress == "flag":
        args.append("--no-color")
    else:
        monkeypatch.setenv("NO_COLOR", "1")

    result = CliRunner().invoke(cli_module.main, args)

    assert result.exit_code == 0
    assert "\x1b[" not in result.output
    assert "ticker-node" in result.output


def test_cli_watch_follows_without_being_asked(home, monkeypatch) -> None:
    """Following is the default, because the seat is what dispatch requires.

    A watcher that returns after one event releases the seat, so every landing
    would have to be followed by a re-arm before the next dispatch could run.
    """
    monkeypatch.setattr(
        recovery,
        "watch_follow",
        lambda *_args, **_kwargs: iter([_event()]),
    )

    result = CliRunner().invoke(
        cli_module.main,
        ["crew", "watch", "--project", "proj", "--no-color"],
    )

    assert result.exit_code == 0
    assert "ticker-node" in result.output
    assert "working → blocked" in result.output


def test_cli_watch_returns_after_one_event_only_when_asked(home, monkeypatch) -> None:
    called: dict[str, object] = {}

    def _single(project, **kwargs):
        called["project"] = project
        called["kwargs"] = kwargs
        return {
            "project": project,
            "event": "empty",
            "run_id": None,
            "classification": "no_live_pointers",
            "next_action": "none",
        }

    monkeypatch.setattr(crew, "watch", _single)

    result = CliRunner().invoke(
        cli_module.main,
        ["crew", "watch", "--project", "proj", "--once"],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["event"] == "empty"
    assert called["project"] == "proj"


def test_cli_watch_treats_exit_on_empty_as_selecting_single_event(
    home, monkeypatch
) -> None:
    """The flag only means anything to the single-event mode.

    Ignoring it under the new default would silently follow forever for a
    caller that explicitly asked to be told about an empty fleet.
    """
    monkeypatch.setattr(
        recovery,
        "watch_follow",
        lambda *_args, **_kwargs: pytest.fail("follow must not run here"),
    )

    result = CliRunner().invoke(
        cli_module.main,
        ["crew", "watch", "--project", "proj", "--exit-on-empty"],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["event"] == "empty"
    assert payload["classification"] == "no_live_pointers"


def test_the_ticker_states_the_model_and_effort_that_ran_the_node(home) -> None:
    """A reader judging progress needed this and had to leave the ticker for it.

    Read from the configuration persisted at dispatch, never from current flight
    config: a later config change must not silently restate what ran. The
    snapshot carries the model and the effort as separate facts, and the
    renderer decides how they read — so nothing shaped for a column is stored.
    """
    _write_pointer(home, "r-agent", "agent-node", phase="working")
    pointer = crew.read_pointer("r-agent")
    pointer["agent"] = {
        "backend": "codex",
        "model": "gpt-5.6-sol",
        "effort": "high",
        "launch": "cli",
    }
    crew._write_json(crew.pointer_path("r-agent"), pointer)

    snapshot = recovery._watch_snapshot(
        crew.read_pointer("r-agent"), moment=recovery._utc_seconds(), stall_seconds=900
    )
    assert (snapshot["model"], snapshot["effort"]) == ("gpt-5.6-sol", "high")
    assert (snapshot["backend"], snapshot["alias"]) == ("codex", "")
    assert "agent" not in snapshot

    line = recovery.format_watch_transition(
        _event(
            model=snapshot["model"],
            effort=snapshot["effort"],
            to_state="working",
            from_state="dispatched",
        )
    )
    # Model and effort are two facts a reader scans in two columns, so the
    # line places them apart rather than fusing them with a separator: the
    # model cell holds the model (or its alias) and the effort cell the whole
    # word, and nothing bridges the two cells but the gap between columns. The
    # cell is sized from the configuration the grid resolves, so the expected
    # cell is derived from the grid rather than assumed to hold the id whole.
    grid = ticker_module.Ticker(color=False)
    model_cell = ticker_module.elide(snapshot["model"], grid.model_width)
    assert model_cell in line
    assert "high" in line
    between = line[line.index(model_cell) : line.index("high")]
    assert "\u00b7" not in between
    assert "gpt-5.6-sol/high" not in line
    # The reading order puts model and effort immediately after the time.
    assert line.index(model_cell) < line.index("ticker-node")
    assert line.index(model_cell) > line.index(
        ticker_module.local_clock(_event()["observed_at"])
    )

    # Half a label beats none; no label at all renders without a stray column.
    assert recovery.agent_label({"agent": {"model": "gpt-5.6-sol"}}) == "gpt-5.6-sol"
    assert recovery.agent_label({"agent": {"effort": "high"}}) == "high"
    assert recovery.agent_label({}) == ""
    assert "  \u00b7" not in recovery.format_watch_transition(_event(model=""))


def test_a_departure_carries_no_explanation_from_the_state_it_left(home) -> None:
    """A run withdrawn out of a block must not still report the block.

    The withdrawal is synthesised from the run's last known snapshot, so the
    clause explaining why it stopped must be cleared. The result must not
    describe a problem that was already over on the line saying it vanished.
    """
    known = {
        "r-1": {
            "run_id": "r-1",
            "node": "n-blocked-then-cleared",
            "session": "s",
            "model": "gpt-5.6-sol",
            "effort": "medium",
            "state": "blocked",
            "detail": "the installed writer does not satisfy the interface",
            "needs_help_complete": True,
        }
    }

    events, remaining = recovery.fleet_transitions(
        known,
        {},
        ledger_run_ids=recovery._ledger_run_id_reader("proj"),
    )

    assert remaining == {}
    (snapshot, previous, state, _counts) = events[0]
    assert (previous, state) == ("blocked", "withdrawn")
    assert snapshot["detail"] == ""
    # The fact a glyph is derived from goes with it: a departure carrying one
    # would render a marker asking a reader to answer a block that is over.
    assert snapshot["needs_help_complete"] is None


def test_only_a_state_needing_action_keeps_the_clause_explaining_it(
    home,
) -> None:
    """Routine progress renders without a reason.

    The clearing rule was an allow-list of states, so any state added later kept
    whatever the classifier happened to attach and read as a warning. Naming the
    states that may explain themselves inverts that: a new one is silent until
    it is deliberately listed.
    """
    assert "complete" not in recovery.EXPLAINED_STATES
    assert "dispatched" not in recovery.EXPLAINED_STATES
    assert "working" not in recovery.EXPLAINED_STATES
    assert {"blocked", "failed", "stalled"} <= recovery.EXPLAINED_STATES

    _write_pointer(home, "r-done", "n-done", phase="working")
    _deliver(home, "r-done", "complete")
    snapshot = recovery._watch_snapshot(
        crew.read_pointer("r-done"),
        moment=recovery._utc_seconds(),
        stall_seconds=900,
    )
    # A completed manifest without an independent review sits as
    # completed_unpromoted, outside the explained set, so it still renders
    # without a reason.
    assert snapshot["state"] == "completed_unpromoted"
    assert snapshot["detail"] == ""


# ── What the follow command renders, asserted through the command itself ──
#
# These read the rendered rows out of `crew follow`, not out of a Ticker built
# in the test: the defects this section closes were all cases where a renderer
# was correct and the command handed it something else — a stated width, an
# event the log never carried, a row nobody suppressed. A helper cannot see any
# of that.


def _fact_event(**overrides) -> dict:
    """A log line in the facts shape: model, effort and detail, unrendered."""
    event = {
        "project": "proj",
        "event": "transition",
        "observed_at": "2026-09-03T12:16:13+00:00",
        "run_id": "r-facts",
        "node": "facts-node",
        "session": "ship-s15-20260903",
        "role": "implement",
        "backend": "anthropic",
        "model": "claude-sonnet-5",
        "alias": "sonnet5",
        "effort": "medium",
        "from_state": "working",
        "to_state": "blocked",
        "working": 3,
        "blocked": 1,
        "unpromoted": 0,
        "detail": "the installed writer does not satisfy the interface",
        "needs_help_complete": False,
    }
    event.update(overrides)
    if "run_id" not in overrides:
        event["run_id"] = f"r-{event['node']}"
    return event


def test_a_follower_delivers_only_lines_with_a_node_and_a_state_pair(monkeypatch):
    """Every delivered line is a transition, never a layout artifact.

    A coordinator consumes each line a follower delivers as a delivery, so a
    line that is calibration and not news — with no node and no state — would be
    handed to a reader as if it were a transition. Every delivered line must
    carry the node it happened to and the state it moved into; a json caller
    reads the same state, with its source, from the machine stream.
    """
    monkeypatch.setattr(
        cli_module,
        "_follow_watch_lines",
        lambda *_a, **_kw: iter([_event(), _event(node="second-node")]),
    )
    plain_lines = (
        CliRunner()
        .invoke(cli_module.main, ["crew", "follow", "--project", "proj", "--no-color"])
        .output.splitlines()
    )
    assert len(plain_lines) == 2
    for line, node in zip(plain_lines, ("ticker-node", "second-node"), strict=True):
        assert node in line, line
        assert "working → blocked" in line, line

    json_lines = CliRunner().invoke(
        cli_module.main, ["crew", "follow", "--project", "proj", "--json"]
    )
    assert json_lines.exit_code == 0, json_lines.output
    payloads = [json.loads(line) for line in json_lines.output.splitlines()]
    assert [row["node"] for row in payloads] == ["ticker-node", "second-node"]
    for row in payloads:
        assert row["from_state"] and row["to_state"], row


def _follow_rows(monkeypatch, events, *args) -> list[str]:
    """The rows `crew follow` prints for these log lines, colour off."""
    monkeypatch.setattr(
        cli_module, "_follow_watch_lines", lambda *_a, **_kw: iter(list(events))
    )
    result = CliRunner().invoke(
        cli_module.main,
        ["crew", "follow", "--project", "proj", "--no-color", *args],
    )
    assert result.exit_code == 0, result.output
    return result.output.splitlines()


def _state_cell_start(row: str, state: str) -> int:
    """Where the fixed-width state cell begins, reached through its own word.

    The state word is left-aligned in a cell of its own width on every row, so
    the cell begins where its word does, whoever the row belongs to and whatever
    it is reporting. The marker cell that once held the column ahead of it is
    retired, so there is no offset between the two.
    """
    return row.index(state) - ticker_module.NEW_STATE_OFFSET


def test_the_retired_baseline_marker_never_renders(
    monkeypatch,
) -> None:
    """Inventory at attach is still suppressed, and the marker word is gone.

    A reattaching follower emits one baseline per live run within a second or
    two, so a baseline for a run that has already finished is inventory about
    work that is over and is dropped rather than shown as news. The word the
    row used to print for the live ones is retired: the state cell names the
    state, which is the fact a reader scans for, and a label saying the row is
    inventory spends a column on what the log's own kind already records.
    """
    rows = _follow_rows(
        monkeypatch,
        [
            _fact_event(event="baseline", from_state=None, to_state="working"),
            _fact_event(from_state="working", to_state="blocked"),
            _fact_event(node="fresh-node", from_state=None, to_state="dispatched"),
            _fact_event(node="settled-node", event="baseline", to_state="complete"),
        ],
    )

    baseline, transition, first_sighting = rows[0], rows[1], rows[2]
    # A run that had already finished when the follower attached is inventory
    # about work that is over, so its row is absent rather than misleading.
    assert len(rows) == 3
    assert "settled-node" not in "\n".join(rows)

    # No row carries the retired word as a word of its own; the state cell is
    # the only label a row prints.
    assert not re.search(r"\bnow\b", "\n".join(rows))
    assert ticker_module.BASELINE_MARKER not in "\n".join(rows)
    assert "working" in baseline and "working → blocked" in transition
    # The baseline has no arrow, while the transition names both sides.
    assert "dispatched" not in baseline
    assert "dispatched" not in transition
    assert "\u2192" not in baseline
    assert "\u2192" in transition

    # The kind, never the absent source: this transition has no from-state and
    # is still rendered as a transition — no marker, and the state it moved
    # into is the only state word on the row.
    assert ticker_module.BASELINE_MARKER not in first_sighting
    assert "dispatched" in first_sighting


def test_a_baseline_row_keeps_the_state_column_of_a_transition(
    monkeypatch,
) -> None:
    """The state cell lands on one screen column on every row.

    A reader scans the state column down the pane, so it must sit at the same
    offset whether the row is inventory or news. The marker cell that used to
    hold that column is retired, and the state cell is the only thing ahead of
    the model, so the column is held by the cell's own fixed width rather than
    by a cell ahead of it.
    """
    rows = _follow_rows(
        monkeypatch,
        [
            _fact_event(event="baseline", from_state=None, to_state="working"),
            _fact_event(from_state="working", to_state="blocked"),
        ],
        "--width",
        "180",
    )
    baseline, transition = rows[0], rows[1]

    # The word is on no row, and the state is not printed twice on one.
    assert not re.search(r"\bnow\b", "\n".join(rows))
    assert baseline.count("working") == 1
    assert transition.count("blocked") == 1

    # The state cell begins at the same screen column on both rows.
    assert _state_cell_start(baseline, "working") == _state_cell_start(
        transition, "blocked"
    )


def test_effort_rejoins_the_alias_and_a_legacy_line_renders_two_cells(
    monkeypatch,
) -> None:
    """The effort is a fact of its own, spelled in full, in a column of its own.

    Model and effort are two facts a reader scans in two columns, so a
    new-shape line places them apart rather than fusing them with a separator.
    A line written before the facts switch carries a composed agent string that
    cannot be re-derived, so it lands in the model cell as it stands. Being
    outside the configured aliases the cell is sized from, it is cut to the
    cell with an ellipsis rather than allowed to shift the effort column.
    """
    rows = _follow_rows(
        monkeypatch,
        [
            _fact_event(),
            _fact_event(
                node="legacy-node",
                agent="deepseek-v4-flash·xhigh",
                model="",
                alias="",
                effort="",
            ),
        ],
    )
    facts, legacy = rows

    # Two cells, one column boundary: the alias and its effort word occupy
    # separate columns rather than sharing a composed label.
    assert "sonnet5" in facts
    assert "medium" in facts
    between = facts[facts.index("sonnet5") + len("sonnet5") : facts.index("medium")]
    assert "·" not in between
    assert "sonnet5/medium" not in facts

    # The model cell's width, read off the facts row it is rendered on: the
    # alias begins the cell and the effort follows the single tight gap.
    model_width = (
        facts.index("medium") - ticker_module.PAIR_GAP - facts.index("sonnet5")
    )
    # The composed legacy value is wider than the cell, so it is cut with an
    # ellipsis and starts on the same screen column as the new-shape model cell.
    assert "deepseek-v4-flash·xhigh" not in legacy
    cell = ticker_module.elide("deepseek-v4-flash·xhigh", model_width)
    assert cell.endswith("\N{HORIZONTAL ELLIPSIS}")
    assert cell in legacy
    assert legacy.index(cell) == facts.index("sonnet5")


def test_a_row_wider_than_the_pane_loses_reason_characters_and_no_counter(
    monkeypatch,
) -> None:
    """A width read one column too wide must cost free text, never a count.

    The pane clips its own right edge, and the failure is asymmetric: a width
    that is too small leaves dead space, while one that is too large drops
    whatever sits last. So every fixed column, the counters included, is placed
    before the reason, and the reason is what a clip takes.
    """
    pane = 190
    rows = _follow_rows(
        monkeypatch,
        [
            _fact_event(
                detail=(
                    "the canonical installed writer the plan names still does "
                    "not satisfy the gate it was added for"
                )
            ),
            _fact_event(node="wide-counts", working=12, blocked=9, unpromoted=7),
        ],
        "--width",
        "207",
    )

    for row, counts in zip(rows, (" 3w  1b  0u", "12w  9b  7u"), strict=True):
        assert len(row) == 207
        clipped = row[:pane]
        # The whole counter block survives the clip, at every count width.
        assert counts in clipped
        # And what the clip took is reason: everything up to the end of the
        # counters is still there, so only the trailing free text is missing.
        assert row.index(counts) + len(counts) < pane
        assert row[pane:].strip() in row[row.index(counts) + len(counts) :]
        for field in ("facts-node" if "facts" in row else "wide-counts", "sonnet5"):
            assert field in clipped
    assert "the canonical installed writer" in rows[0]


def test_every_field_holds_one_column_across_every_row_kind(monkeypatch) -> None:
    """Baseline, transition, shadow and foreign row all share one grid.

    A pane is read down a column, so a row kind that shifts a field by a
    character defeats the only way this display is used. The kinds differ in
    what they say about themselves — a glyph for a row another session owns, a
    dim row for a shadow that will never merge — and in nothing about geometry.
    """
    # The width is pinned by the invocation rather than read from whatever
    # terminal this test happens to run under, so the grid's geometry cannot
    # depend on the machine. What the test asserts is the property it exists
    # for — every row kind shares one width — not any particular number.
    rows = _follow_rows(
        monkeypatch,
        [
            _fact_event(event="baseline", from_state=None, to_state="working"),
            _fact_event(),
            _fact_event(node="shadow-node", lineage={"kind": "shadow"}),
            _fact_event(node="own-node", session="", working=12, unpromoted=7),
        ],
        "--width",
        "180",
    )

    assert len(rows) == 4
    assert len({len(row) for row in rows}) == 1
    assert (
        len(
            {
                _state_cell_start(row, state)
                for row, state in zip(
                    rows, ("working", "blocked", "blocked", "blocked"), strict=True
                )
            }
        )
        == 1
    )
    assert len({row.index("sonnet5") for row in rows}) == 1
    assert len({row.index("implement"[:4]) for row in rows}) == 1
    # The counter block starts at one column and holds every column the module
    # reserves for it whatever the counts, so the free text after it keeps one
    # column on every row kind. Each count is right-aligned to a fixed digit
    # width inside its own cell, so the block fills those columns whatever the
    # fleet is doing: the digits a single-digit count does not use are spent at
    # the left of its own cell, never by letting the block's own width follow
    # the counts and shift the field after it.
    starts = set()
    ends = set()
    for row in rows:
        # Each cell is its fixed digit width plus its letter and one space
        # separates the cells, so the pattern pins the whole block and cannot
        # start one column early on a two-digit count by absorbing the space
        # that separates the cells from each other.
        found = re.search(
            r"(?<=\s)([ \d]{2}w) ([ \d]{2}b) ([ \d]{2}u) ([ \d]{2}q)", row
        )
        assert found, row
        # Both edges come off the row: the first cell's own pad is where the
        # block's reserved columns begin whatever the counts, and the queued
        # cell's letter is where they end.
        starts.add(found.start(1))
        ends.add(found.end(4))
    assert len(starts) == 1, starts
    assert len(ends) == 1, ends
    block_end = ends.pop()
    for row in rows:
        assert (
            row[block_end : block_end + ticker_module.GAP] == " " * ticker_module.GAP
        ), row

    # Session identity is a delivery scope, not another row column.
    assert "ship-s15-20260903" not in "\n".join(rows)


def test_pinning_the_width_gives_the_same_rows_with_or_without_a_terminal(
    monkeypatch,
) -> None:
    """The render assertions must not read a width the host resolver found.

    The same invocation, its width pinned by an explicit flag, renders
    byte-identical rows whether an ancestor terminal would report a conflicting
    width or the resolver falls back to the stated default. A test that omitted
    the pin drew its width from the machine — 180 on a detached worker, the
    pane's real width under a terminal — and passed on one and failed on the
    other; pinning is what makes the answer the same on every machine.
    """
    fleet = [
        _fact_event(event="baseline", from_state=None, to_state="working"),
        _fact_event(),
        _fact_event(node="shadow-node", lineage={"kind": "shadow"}),
        _fact_event(node="own-node", session="", working=12, unpromoted=7),
    ]

    # A resolvable terminal announces a width that disagrees with the pin.
    monkeypatch.setattr(ticker_module, "resolve_terminal_width", lambda: 207)
    with_terminal = _follow_rows(monkeypatch, fleet, "--width", "180")

    # A detached follower's resolver falls back to the stated default.
    monkeypatch.setattr(
        ticker_module,
        "resolve_terminal_width",
        lambda: ticker_module.DEFAULT_WIDTH,
    )
    without_terminal = _follow_rows(monkeypatch, fleet, "--width", "180")

    assert with_terminal == without_terminal


def test_a_shadow_row_says_so_end_to_end_rather_than_by_identifier(
    monkeypatch,
) -> None:
    """A shadow is evidence that will never merge, and the whole row says it.

    Its identifier is the least readable thing on the line — synthesised from
    the primary run id and the candidate backend — so the row carries the fact
    a reader acts on instead: everything about it is dim, including the node
    hue that answers which worker a row belongs to.
    """
    monkeypatch.setattr(
        cli_module,
        "_follow_watch_lines",
        lambda *_a, **_kw: iter(
            [_fact_event(), _fact_event(node="shadow-node", shadow=True)]
        ),
    )
    result = CliRunner().invoke(
        cli_module.main, ["crew", "follow", "--project", "proj"]
    )

    assert result.exit_code == 0, result.output
    primary, shadow = result.output.splitlines()
    assert "\x1b[38;5;" in primary
    assert "\x1b[38;5;" not in shadow
    assert "\x1b[2m" in shadow
