"""A follower row is one aligned transition with no attention mark."""

from __future__ import annotations

import itertools
import re

from reckon.crew import recovery, runs
from reckon.crew import ticker as ticker_module

ESCAPES = re.compile(r"\x1b\[[0-9;]*m")
ACTION_WORDS = frozenset(recovery.RECOVERY_VERBS.values())
# The words a departed run's absence carries, from the departure classifier.
DEPARTURE_STATES = {"withdrawn", "discarded", "departed"}
# The vocabulary a producer or classifier can emit, stated by its owners rather
# than read from the renderer's own tables: a set read out of the renderer
# moves with whatever word is added there, a display alias included, so the
# binding would agree with itself through any change to the renderer.
CLASSIFIER_STATES = tuple(
    sorted(
        set(runs.WATCH_ATTENTION_STATES)
        | set(runs.WATCH_PROGRESS_STATES)
        | set(recovery.RECOVERY_CLASSES)
        | set(recovery.RECOVERY_CLASSIFICATIONS)
        | DEPARTURE_STATES
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
    """Where each cell of the row begins, from the renderer's own constants.

    The head is the fixed frame — clock, stage, job, transition, elapsed and the
    counters — and the lane's rate, the model and the effort follow it at the
    row's right end, each added only where the pane has room. All widths come
    from the constants the renderer sizes its cells by, so a cell added or
    widened without the cells after it moving fails here rather than agreeing
    with the arithmetic that placed them.
    """
    stage = ticker_module.CLOCK + ticker_module.GAP
    job = stage + ticker_module.ROLE + ticker_module.GAP
    transition = job + ticker_module.NODE + ticker_module.GAP
    arrow = transition + ticker_module.STATE_WORD + ticker_module.ARROW_GAP
    elapsed = (
        transition + ticker_module.STATE + ticker_module.GAP + ticker_module.SPEND_GAP
    )
    counters = elapsed + ticker_module.WALL + ticker_module.GAP
    rate = counters + ticker_module.STATS + ticker_module.GAP
    model = rate + ticker_module.RATE + ticker_module.GAP
    effort = model + model_width + ticker_module.GAP
    return {
        "stage": stage,
        "job": job,
        "transition": transition,
        "arrow": arrow,
        "elapsed": elapsed,
        "counters": counters,
        "rate": rate,
        "model": model,
        "effort": effort,
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
    # it. The model cell is the row's rightmost measured column of the frame's
    # neighbours: it is added after the counters and the lane's rate, so it lands
    # on a column the cells ahead of it fix rather than one a reserved blank
    # would hold open.
    probe = plain(
        ticker_module.Ticker(width=width, color=False, model_aliases=()).render(
            _event()
        )
    )
    assert probe.index(model_text) == columns["model"]
    observed = {name: set() for name in ("stage", "job", "model", "arrow", "counters")}

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
        # Each case uses a fresh painter, so its first row always opens from
        # the arrival word even when the stream names an older internal state.
        assert "new" in row, (previous, new, row)
        assert len(row) <= width, row

        measured = {
            "stage": row.index("review"),
            "job": row.index("n"),
            "model": row.index(model_text),
            "counters": row.index(" 1w  0h"),
        }
        measured["arrow"] = row.index(ticker_module.ARROW)
        for name, start in measured.items():
            observed[name].add(start)
            assert start == columns[name], (name, start, columns[name], row)
        # No row carries an attention mark, whatever the destination needs: the
        # mark was dropped, so no `!` appears anywhere on the row and the node
        # cell follows the role's gutter directly.
        assert "!" not in row, row
        assert row[columns["job"] : columns["job"] + ticker_module.NODE].strip()

        gutters = (
            (ticker_module.CLOCK, columns["stage"]),
            (columns["stage"] + ticker_module.ROLE, columns["job"]),
            (columns["job"] + ticker_module.NODE, columns["transition"]),
            (columns["transition"] + ticker_module.STATE, columns["elapsed"]),
            (columns["elapsed"] + ticker_module.WALL, columns["counters"]),
            (columns["counters"] + ticker_module.STATS, columns["rate"]),
            (columns["rate"] + ticker_module.RATE, columns["model"]),
            (columns["model"] + ticker_module.MODEL, columns["effort"]),
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

    assert "new → halted" in abandoned
    assert "halted → working" in working
    assert "working → working" not in working


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


def test_a_repeat_of_a_first_seen_state_prints_no_row() -> None:
    """The first row opens from new, then a repeated pane word is silent.

    Two internal states that share one pane word also make a silent repeat.
    """
    grid = ticker_module.Ticker(width=208, color=False, model_aliases=())
    for index, state in enumerate(sorted(CLASSIFIER_STATES)):
        event = _event(
            run_id=f"r-still-{index}",
            from_state=state,
            to_state=state,
            recovery_classification="",
            detail="",
        )
        assert "new →" in plain(grid.render(event)), state
        assert grid.render(event) == "", state
    event = _event(
        run_id="r-collided",
        to_state="completed_unpromoted",
        recovery_classification="",
        detail="",
    )
    assert "new → delivered" in plain(grid.render(event))
    event.update(from_state="completed_unpromoted", to_state="unpromoted")
    assert grid.render(event) == ""


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
    assert "new → working" in moved, moved
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
    assert "new → delivered" in still, still
    assert (
        grid.render(
            _event(
                run_id="r-still-arrow",
                from_state="unpromoted",
                to_state="unpromoted",
                recovery_classification="",
                detail="",
            )
        )
        == ""
    )


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
        # The attention mark was dropped, so no `!` appears on the row; the
        # model cell holds its own column at the row's right end and the job
        # cell keeps its gutter ahead of the transition.
        assert "!" not in row, row
        assert row[columns["model"] : columns["model"] + ticker_module.MODEL].strip()
        assert (
            row[columns["job"] + ticker_module.NODE : columns["transition"]]
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


def test_every_pane_word_is_a_hued_table_key() -> None:
    """Internal recovery distinctions do not become extra pane words."""
    assert not hasattr(ticker_module, "DISPLAY")
    assert set(ticker_module.PANE_STATES) == {
        "queued",
        "working",
        "waiting",
        "halted",
        "delivered",
        "landed",
        "dropped",
    }
    for theme in ("light", "dark"):
        for word in ticker_module.PANE_STATES:
            assert ticker_module._state_hue(theme, word) != "dim", (theme, word)
