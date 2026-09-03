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
from reckon import crew
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
        ("new-node", "blocked", "complete"),
        ("new-node", "complete", "promoted"),
        ("existing-node", "dispatched", "promoted"),
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
    rendered = recovery.format_watch_transition(transitions[2])
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
    # Which node, then what it did, then what ran it: the varying fields come
    # first, and the near-constant agent label sits after the state rather than
    # in the position the eye reaches straight after the clock.
    assert line.index("ticker-node") < line.index("working") < line.index("blocked")
    assert "working" in line and "→" in line
    # The counts are a fixed grid whose digits share a column, each number
    # followed by its state's single letter.
    assert " 3w ·  1b ·  0u" in line
    # Free text is bounded to one clause and stays on the line; a second row
    # would cost a quarter of a pane that shows about eight.
    assert "first clause" in line
    assert "second clause" not in line
    assert "\n" not in line


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
    assert " 3w ·  1b ·  0u" in line


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
    assert "gpt-5.6-sol" in line
    # The effort is spelled in full in its own column, never fused to the model
    # by a separator the machine reader would have to parse back.
    assert "high" in line
    assert "gpt-5.6-sol/high" not in line
    assert "gpt-5.6-sol\u00b7high" not in line
    # After the state, not before the node. On a uniform wave this column
    # repeats the same value on every row, so it must not occupy the position
    # the eye reaches first; the node and its state vary and go there instead.
    assert line.index("ticker-node") < line.index("gpt-5.6-sol") < line.index("high")
    assert line.index("gpt-5.6-sol") > line.index(
        ticker_module.local_clock(_event()["observed_at"])
    )

    # Half a label beats none; no label at all renders without a stray column.
    assert recovery.agent_label({"agent": {"model": "gpt-5.6-sol"}}) == "gpt-5.6-sol"
    assert recovery.agent_label({"agent": {"effort": "high"}}) == "high"
    assert recovery.agent_label({}) == ""
    assert "  \u00b7" not in recovery.format_watch_transition(_event(model=""))


def test_a_departure_carries_no_explanation_from_the_state_it_left() -> None:
    """A run promoted out of a block must not still report the block.

    The promotion is synthesised from the run's last known snapshot, so the
    clause explaining why it stopped travels with it unless it is cleared. The
    result described a problem that was already over, on the very line saying it
    was resolved.
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

    events, remaining = recovery.fleet_transitions(known, {})

    assert remaining == {}
    (snapshot, previous, state, _counts) = events[0]
    assert (previous, state) == ("blocked", "promoted")
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
    assert snapshot["state"] == "complete"
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
    return event


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


def _arrow_column(row: str) -> int:
    """Where the arrow column sits, whichever glyph this row put in it."""
    for glyph in (ticker_module.TRANSITION_ARROW, ticker_module.BASELINE_ARROW):
        if glyph in row:
            return row.index(glyph)
    raise AssertionError(row)


def test_a_baseline_row_reads_differently_from_a_transition_into_it(
    monkeypatch,
) -> None:
    """Inventory at attach must not read as news, and the kind decides.

    A reattaching follower emits one baseline per live run within a second or
    two, and a baseline that renders identically to a transition turns a restart
    into a burst of things that look like they just happened. The distinction is
    taken from the kind the log records: a genuine first sighting also has no
    from-state, so inferring it from a null source would mark real transitions
    as inventory and hide them.
    """
    rows = _follow_rows(
        monkeypatch,
        [
            _fact_event(event="baseline", from_state=None, to_state="working"),
            _fact_event(from_state="dispatched", to_state="working"),
            _fact_event(node="fresh-node", from_state=None, to_state="dispatched"),
            _fact_event(node="settled-node", event="baseline", to_state="complete"),
        ],
    )

    baseline, transition, first_sighting = rows[0], rows[1], rows[2]
    # A run that had already finished when the follower attached is inventory
    # about work that is over, so its row is absent rather than misleading.
    assert len(rows) == 3
    assert "settled-node" not in "\n".join(rows)

    # Same destination state, two different records, two different rows.
    assert "working" in baseline and "working" in transition
    assert baseline != transition
    assert ticker_module.BASELINE_ARROW in baseline
    assert ticker_module.TRANSITION_ARROW not in baseline
    # A baseline claims no movement, so it shows no state it came from.
    assert "dispatched" not in baseline
    assert "dispatched" in transition

    # The kind, never the absent source: this transition has no from-state and
    # is still rendered as a transition.
    assert ticker_module.TRANSITION_ARROW in first_sighting
    assert ticker_module.BASELINE_ARROW not in first_sighting


def test_effort_stands_in_its_own_column_and_a_legacy_line_leaves_it_empty(
    monkeypatch,
) -> None:
    """The effort is a fact of its own, spelled in full beside the model.

    A line written before the facts switch has model and effort already fused
    into one string and nothing to split, so it renders that string whole in the
    model column and leaves the effort column empty rather than raising or
    re-parsing a composed value into a guess.
    """
    rows = _follow_rows(
        monkeypatch,
        [
            _fact_event(),
            _fact_event(
                node="legacy-node",
                agent="dsv4-flash·xh",
                model="",
                alias="",
                effort="",
            ),
        ],
    )
    facts, legacy = rows

    assert "sonnet5" in facts
    assert "medium" in facts
    # Separate columns, so a machine reader is handed neither a separator to
    # parse nor an abbreviation to expand.
    assert "sonnet5/medium" not in facts
    assert "sonnet5·medium" not in facts
    assert facts.index("sonnet5") < facts.index("medium")

    # The composed legacy value renders whole, and the column beside it — the
    # same screen columns the effort word occupies above — is blank.
    assert "dsv4-flash·xh" in legacy
    start = facts.index("medium")
    assert legacy[start : start + len("medium")].strip() == ""


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

    for row, counts in zip(rows, (" 3w ·  1b ·  0u", "12w ·  9b ·  7u"), strict=True):
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
    rows = _follow_rows(
        monkeypatch,
        [
            _fact_event(event="baseline", from_state=None, to_state="working"),
            _fact_event(),
            _fact_event(node="shadow-node", lineage={"kind": "shadow"}),
            _fact_event(node="own-node", session="", working=12, unpromoted=7),
        ],
    )

    assert len(rows) == 4
    assert {len(row) for row in rows} == {180}
    assert len({_arrow_column(row) for row in rows}) == 1
    assert len({row.index("sonnet5") for row in rows}) == 1
    assert len({row.index("implement"[:4]) for row in rows}) == 1
    for letter, spelling in (("w", "working"), ("b", "blocked"), ("u", "unpromoted")):
        columns = {
            re.search(r"\d{1,2}w ·\s+\d{1,2}b ·\s+\d{1,2}u", row).end(0) for row in rows
        }
        assert len(columns) == 1, (letter, spelling)

    # The owner column is one glyph, and it marks rather than names: a row this
    # unscoped reader does not own is flagged, and its session id never appears.
    owner_column = rows[1].index(ticker_module.FOREIGN_OWNER)
    assert "ship-s15-20260903" not in "\n".join(rows)
    assert rows[0][owner_column] == ticker_module.FOREIGN_OWNER
    assert rows[3][owner_column] == " "


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
