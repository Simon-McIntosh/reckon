"""A follower row is one aligned transition with no attention mark."""

from __future__ import annotations

import itertools
import re

from reckon.crew import recovery
from reckon.crew import ticker as ticker_module

ESCAPES = re.compile(r"\x1b\[[0-9;]*m")
ACTION_WORDS = frozenset(recovery.RECOVERY_VERBS.values())
CLASSIFIER_STATES = tuple(
    sorted(
        set(ticker_module.STATE_HUE["light"])
        | set(recovery.RECOVERY_CLASSES)
        | set(recovery.RECOVERY_CLASSIFICATIONS)
        | {"completed_unpromoted"}
    )
)
NAMES = (
    "n" * (ticker_module.NODE - 1),
    "n" * ticker_module.NODE,
    "n" * (ticker_module.NODE + 1),
)
ELAPSED_SECONDS = (0, 59, 60, 3_599, 3_600, 10_800, 108_000)


def plain(line: str) -> str:
    return ESCAPES.sub("", line)


def _event(**overrides):
    event = {
        "event": "transition",
        "observed_at": "2026-09-25T12:00:00Z",
        "run_id": "r-row",
        "node": "row",
        "model": "gpt-sol",
        "effort": "high",
        "role": "review",
        "from_state": "dispatched",
        "to_state": "working",
        "working": 1,
        "blocked": 0,
        "unpromoted": 0,
        "waiting": 0,
        "spend_wall_seconds": 0,
        "detail": "the worker is active",
    }
    event.update(overrides)
    return event


def _columns(model_width: int) -> dict[str, int]:
    model = ticker_module.CLOCK + ticker_module.GAP
    effort = model + model_width + ticker_module.GAP
    role = effort + ticker_module.EFFORT + ticker_module.GAP
    node = role + ticker_module.ROLE + ticker_module.GAP
    transition = node + ticker_module.NODE + ticker_module.GAP
    arrow = transition + ticker_module.STATE_WORD + ticker_module.ARROW_GAP
    elapsed = (
        transition + ticker_module.STATE + ticker_module.GAP + ticker_module.SPEND_GAP
    )
    counters = elapsed + ticker_module.WALL + ticker_module.GAP
    reason = counters + ticker_module.STATS + ticker_module.GAP
    return {
        "model": model,
        "effort": effort,
        "role": role,
        "node": node,
        "transition": transition,
        "arrow": arrow,
        "elapsed": elapsed,
        "counters": counters,
        "reason": reason,
    }


def test_every_classifier_transition_keeps_one_grid() -> None:
    """Every input extreme preserves each column and every two-space gutter."""
    assert set(CLASSIFIER_STATES) == ticker_module.CLASSIFIER_STATE_WORDS
    # The cell is narrower than the widest classifier state, so the longer
    # recovery spellings elide to it; every rendered state fills exactly the cell
    # and the arrow below keeps one column whatever it is. The cell is at least
    # wide enough for the common lifecycle states to render whole.
    assert max(len(ticker_module._display_state(w)) for w in CLASSIFIER_STATES) <= (
        ticker_module.STATE_WORD
    )
    for state in ("dispatched", "abandoned", "blocked", "working", "unpromoted"):
        assert len(ticker_module._display_state(state)) <= ticker_module.STATE_WORD
    columns = _columns(ticker_module.MODEL)
    width = 208
    model_text = "gpt-sol"
    # Every column this test relies on is located on a rendered row, never read
    # back from the expression that built the same number: a renderer that moved
    # a cell must fail here rather than agree with the arithmetic that placed
    # it. The model column is the whole attention question — the mark was
    # dropped, so the cell starts at the first column after the time's two-space
    # gutter with no reserved blank column between them.
    probe = plain(
        ticker_module.Ticker(width=width, color=False, model_aliases=()).render(
            _event()
        )
    )
    assert probe.index(model_text) == ticker_module.CLOCK + ticker_module.GAP
    observed = {name: set() for name in ("model", "node", "arrow", "counters")}

    cases = itertools.product(
        (None, *CLASSIFIER_STATES),
        CLASSIFIER_STATES,
        NAMES,
        ELAPSED_SECONDS,
        (0, 1),
    )
    for index, (previous, new, node, elapsed, queued) in enumerate(cases):
        recovery_word = recovery.RECOVERY_VERBS.get(new, "")
        event = _event(
            run_id=f"r-row-{index}",
            node=node,
            from_state=previous,
            to_state=new,
            recovery_classification=new
            if new in recovery.RECOVERY_CLASSIFICATIONS
            else "",
            recovery=recovery_word,
            spend_wall_seconds=elapsed,
            waiting=queued,
        )
        row = plain(
            ticker_module.Ticker(width=width, color=False, model_aliases=()).render(
                event
            )
        )
        previous_seen = ticker_module._display_state(previous) if previous else ""
        if previous_seen == ticker_module._display_state(new):
            # A first sighting whose effective previous state is the state it
            # entered is not a transition: the left half and the arrow would only
            # restate the destination, so the row is not printed at all.
            assert row == "", (previous, new, row)
            continue
        assert len(row) == width, row

        measured = {
            "model": row.index(model_text),
            "node": row.index("n"),
            "counters": row.index(" 1w"),
        }
        # A first sighting has no remembered state, so its left half and arrow
        # are blank and only a later transition carries the arrow. The gutter
        # assertions below cover the blank half; the arrow is located here where
        # one was drawn.
        if previous_seen:
            measured["arrow"] = row.index(ticker_module.ARROW)
        for name, start in measured.items():
            observed[name].add(start)
            assert start == columns[name], (name, start, columns[name], row)
        # No row carries an attention mark, whatever the destination needs: the
        # mark was dropped, so no `!` appears anywhere on the row and the node
        # cell follows the role's gutter directly.
        assert "!" not in row, row
        assert row[columns["node"] : columns["node"] + ticker_module.NODE].strip()

        gutters = (
            (ticker_module.CLOCK, columns["model"]),
            (columns["model"] + ticker_module.MODEL, columns["effort"]),
            (columns["effort"] + ticker_module.EFFORT, columns["role"]),
            (columns["role"] + ticker_module.ROLE, columns["node"]),
            (columns["node"] + ticker_module.NODE, columns["transition"]),
            (columns["transition"] + ticker_module.STATE, columns["elapsed"]),
            (columns["elapsed"] + ticker_module.WALL, columns["counters"]),
            (columns["counters"] + ticker_module.STATS, columns["reason"]),
        )
        for start, stop in gutters:
            assert stop - start >= 2
            assert row[start:stop] == " " * (stop - start)

        between_transition_and_elapsed = row[columns["transition"] : columns["elapsed"]]
        assert not ACTION_WORDS & set(between_transition_and_elapsed.split())

    assert all(len(starts) == 1 for starts in observed.values())


def test_reported_state_chains_when_the_producer_skips_an_observation() -> None:
    grid = ticker_module.Ticker(width=208, color=False, model_aliases=())
    abandoned = plain(
        grid.render(
            _event(
                from_state="dispatched",
                to_state="abandoned",
                recovery_classification="abandoned",
                recovery="recover",
            )
        )
    )
    working = plain(
        grid.render(
            _event(
                from_state="dispatched",
                to_state="working",
                recovery_classification="running",
                recovery="observe",
            )
        )
    )

    assert "dispatched → abandoned" in abandoned
    assert "abandoned → working" in working
    assert "dispatched → working" not in working


def test_repeated_same_state_events_render_one_row() -> None:
    grid = ticker_module.Ticker(width=208, color=False, model_aliases=())
    first = grid.render(_event(to_state="working", detail="first observation"))
    repeated = grid.render(
        _event(
            from_state="working",
            to_state="working",
            detail="a changed reason does not make a transition",
            working=2,
            spend_wall_seconds=4_000,
        )
    )

    assert bool(first)
    assert repeated == ""


def test_first_seen_same_state_prints_no_row() -> None:
    """A run seen for the first time after it did not move prints nothing.

    With no remembered state the row's left side falls back to the event's own
    ``from_state``, so a producer that emits the state the run is already in
    would otherwise print ``unpromoted → unpromoted`` — a transition claimed
    where the two sides are one state. The effective previous state is the
    remembered one when the pane has seen the run and the event's otherwise, and
    a row is printed only when that differs from the state entered. The
    collision pair is the same defect by display: the internal
    ``completed_unpromoted`` renders as ``unpromoted``, so the two sides are one
    word a reader can see.
    """
    grid = ticker_module.Ticker(width=208, color=False, model_aliases=())
    for index, state in enumerate(sorted(CLASSIFIER_STATES)):
        row = plain(
            grid.render(
                _event(
                    run_id=f"r-still-{index}",
                    from_state=state,
                    to_state=state,
                    recovery_classification="",
                    detail="",
                )
            )
        )
        assert row == "", (state, row)
    collided = plain(
        grid.render(
            _event(
                run_id="r-collided",
                from_state="completed_unpromoted",
                to_state="unpromoted",
                recovery_classification="",
                detail="",
            )
        )
    )
    assert collided == "", collided


def test_first_seen_transition_keeps_its_arrow() -> None:
    """A run seen for the first time after it moved renders the whole transition.

    The suppression above keys on the effective previous state, not on the
    absence of memory: where the event's ``from_state`` differs from the state
    it entered, the row keeps its left word and its arrow, so a re-armed pane
    reports the move it woke up to rather than a bare destination. The pair that
    does not differ is the row that must not be printed, arrow included.
    """
    grid = ticker_module.Ticker(width=208, color=False, model_aliases=())
    moved = plain(
        grid.render(
            _event(
                run_id="r-moved",
                from_state="dispatched",
                to_state="working",
                recovery_classification="running",
            )
        )
    )
    assert ticker_module.ARROW in moved, moved
    assert "dispatched → working" in moved, moved
    still = plain(
        grid.render(
            _event(
                run_id="r-still-arrow",
                from_state="unpromoted",
                to_state="unpromoted",
                recovery_classification="",
                detail="",
            )
        )
    )
    assert still == "", still


def test_reported_examples_keep_name_gutters_and_no_mark() -> None:
    examples = (
        ("review-of-hook-stop-mode-silent-without-follower", "abandoned", "recover"),
        ("review-of-obligations-are-a-derived-view", "working", "observe"),
    )
    columns = _columns(ticker_module.MODEL)

    for index, (node, state, action) in enumerate(examples):
        row = plain(
            ticker_module.Ticker(width=208, color=False, model_aliases=()).render(
                _event(
                    run_id=f"r-example-{index}",
                    node=node,
                    from_state="dispatched",
                    to_state=state,
                    recovery_classification=state,
                    recovery=action,
                )
            )
        )
        # The attention mark the lead saw beside this row is gone, and the model
        # cell holds the column the mark used to occupy.
        assert "!" not in row, row
        assert row[columns["model"] : columns["model"] + ticker_module.MODEL].strip()
        assert (
            row[columns["node"] + ticker_module.NODE : columns["transition"]]
            == " " * ticker_module.GAP
        )
        assert action not in row[columns["transition"] : columns["elapsed"]]


def test_no_configured_model_alias_is_truncated(tmp_path, monkeypatch) -> None:
    """The model cell holds every alias the flight config declares, whole.

    A grid sized to the longest declared alias lands no ellipsis on any row
    carrying one, so a reader sees the ground with the longest alias it must
    hold and no configured alias is cut to a prefix. The alias set is read
    through the module's own resolver, against a config written into a
    temporary home, so the check runs the same resolution a live pane runs
    without reading the machine's own flight config.
    """
    config = tmp_path / "flight.yaml"
    config.write_text(
        "version: 1\n"
        "default_backend: local\n"
        "backends:\n"
        "  local:\n"
        "    launch: in-harness\n"
        "    sandbox: worktree-full\n"
        "    alias: dsv4.1-flash\n"
        "  fork:\n"
        "    launch: cli\n"
        "    command: codex\n"
        "    sandbox: worktree-full\n"
        "    alias: luna5.6\n"
        "  alt:\n"
        "    launch: cli\n"
        "    command: codex\n"
        "    sandbox: worktree-full\n"
        "    alias: sol5.6\n"
        "  big:\n"
        "    launch: cli\n"
        "    command: codex\n"
        "    sandbox: worktree-full\n"
        "    alias: astra6\n"
        "roles:\n"
        "  implement: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(config))

    aliases = ticker_module.declared_model_aliases(None)
    assert aliases, "the fixture config declares no alias, so the check is vacuous"
    assert max(map(len, aliases)) == len("dsv4.1-flash")
    grid = ticker_module.Ticker(width=208, color=False)
    assert grid.model_width == len("dsv4.1-flash")

    for index, alias in enumerate(aliases):
        row = plain(
            grid.render(_event(run_id=f"r-alias-{index}", alias=alias, model=""))
        )
        assert alias in row, (alias, row)
        assert "\N{HORIZONTAL ELLIPSIS}" not in row, (alias, row)


def test_the_reason_starts_at_or_before_column_120_at_the_default_width() -> None:
    """The clause's own column, measured off a rendered row, is at most 120.

    The column is read from the row the renderer produced rather than from the
    expression that built the grid: a renderer that moved a cell must fail here
    rather than agree with the arithmetic that placed it. Columns are counted
    from zero, the same base the row's index uses. A clause exactly sixty
    characters long is the budget's own statement — the fixed cells must leave
    sixty columns for the reason at the 180-column default, and eighty-eight on
    the 208-column pane — so it is asserted whole and unelided, and the room
    beside it is measured rather than assumed.
    """
    width = ticker_module.DEFAULT_WIDTH
    assert width == 180
    clause = "zeppelin the gate lifted and the pane has room to say it all"
    assert len(clause) == 60
    grid = ticker_module.Ticker(width=width, color=False, model_aliases=())

    row = plain(grid.render(_event(run_id="r-room", to_state="blocked", detail=clause)))
    assert len(row) == width, row
    reason_start = row.index("zeppelin")
    assert reason_start <= 120, (reason_start, row)
    assert clause in row, row
    assert width - reason_start >= 60, (reason_start, row)
    assert "\N{HORIZONTAL ELLIPSIS}" not in row, row

    # The pane this workstation measures is wider, and the clause's own column
    # is fixed, so the extra columns land entirely on the reason's room.
    wide = ticker_module.Ticker(width=208, color=False, model_aliases=())
    wide_row = plain(
        wide.render(_event(run_id="r-room-wide", to_state="blocked", detail=clause))
    )
    wide_start = wide_row.index("zeppelin")
    assert wide_start == reason_start, (wide_start, reason_start, wide_row)
    assert 208 - wide_start >= 88, (wide_start, wide_row)


def test_a_reason_whose_first_word_does_not_fit_still_prints_characters() -> None:
    """A clause longer than its field is cut inside the word, never emptied.

    The regression this pins: a clause whose first word repeated the destination
    state was cut to that word and then stripped of it, so the field carried the
    ellipsis alone with the reason's whole content gone. Two shapes are checked
    on a rendered row at the default width — a clause whose head is a long token,
    and a clause the state label shares a word with — and each must leave
    readable characters in the field rather than an empty ellipsis.
    """
    width = ticker_module.DEFAULT_WIDTH
    grid = ticker_module.Ticker(width=width, color=False, model_aliases=())

    claim = "hook-stop-mode-silent-without-follower"
    labelled = plain(
        grid.render(
            _event(
                run_id="r-labelled",
                to_state="blocked",
                detail=f"blocked {claim}",
            )
        )
    )
    assert claim in labelled, labelled

    long_token = "w" * 200
    overlong = plain(
        grid.render(
            _event(
                run_id="r-overlong",
                to_state="blocked",
                detail=f"blocked {long_token}",
            )
        )
    )
    # The counters spell a single `w`; ten in a row can only come from the
    # clause, so the field kept the word's head rather than collapsing.
    assert overlong.count("w") >= 10, overlong
    assert overlong.rstrip().endswith("\N{HORIZONTAL ELLIPSIS}"), overlong
