"""The ticker is a fixed grid, so every column must land on one screen column.

A reader scans this pane down a column rather than across a line: which worker,
what state, how many still running. A field that shifts by a character between
rows defeats that, and free text that overruns the margin wraps the row into two
and halves a pane that only shows about eight lines at a time.
"""

from __future__ import annotations

import re

import pytest

from reckon.crew import ticker as ticker_module

ESCAPES = re.compile(r"\x1b\[[0-9;]*m")


def plain(line: str) -> str:
    """The line as the grid measures it, with any colour removed."""
    return ESCAPES.sub("", line)


def _event(**overrides):
    event = {
        "observed_at": "2026-09-02T14:16:13+00:00",
        "run_id": "r-1",
        "node": "n-west-review-pr8-cut",
        "session": "ship-s10-20260901",
        "agent": "gpt-5.6-sol/medium",
        "from_state": "working",
        "to_state": "blocked",
        "working": 3,
        "blocked": 1,
        "unpromoted": 0,
    }
    event.update(overrides)
    return event


@pytest.fixture
def grid():
    return ticker_module.Ticker(width=180, theme="light", color=False)


def test_every_line_is_exactly_the_requested_width(grid):
    """A short line and a crowded line end on the same column.

    The stats block is right-aligned against the margin, so a line that stops
    early breaks the one edge a reader uses to compare counts.
    """
    lines = [
        grid.render(_event(from_state=None, to_state="dispatched")),
        grid.render(_event(to_state="complete", reason="")),
        grid.render(_event(reason="pytest exited 1")),
        grid.render(_event(node="n" * 80, working=12, blocked=9, unpromoted=7)),
    ]
    assert {len(plain(line)) for line in lines} == {180}


def test_columns_start_on_the_same_screen_column(grid):
    """Node, arrow, agent and every stat label share a column across rows."""
    rows = [
        plain(grid.render(_event(from_state=None, to_state="dispatched"))),
        plain(grid.render(_event(from_state="complete", to_state="promoted"))),
        plain(grid.render(_event(working=12, blocked=0, unpromoted=3))),
    ]
    assert len({row.index("→") for row in rows}) == 1
    # Searched from the right: a state named `working` or `blocked` appears
    # earlier on the same line, and matching that instead would compare the
    # arrow column against itself.
    for token in ("working", "blocked", "unpromoted"):
        assert len({row.rindex(token) for row in rows}) == 1, token


def test_stat_digits_align_across_one_and_two_digit_counts(grid):
    """`12 working` stacks under `1 working` rather than shunting the label."""
    one = plain(grid.render(_event(working=1)))
    twelve = plain(grid.render(_event(working=12)))
    assert one.index(" 1 working") == twelve.index("12 working")


def test_arrow_column_holds_when_there_is_no_previous_state(grid):
    """A first sighting has no source, and its arrow still lines up."""
    first = plain(grid.render(_event(from_state=None, to_state="dispatched")))
    later = plain(grid.render(_event(from_state="dispatched", to_state="complete")))
    assert first.index("→") == later.index("→")


def test_long_internal_state_names_render_within_the_column(grid):
    """`completed_unpromoted` is twenty characters against a ten-wide field.

    It renders as the word the fleet counter already uses for that bucket, so
    one term means one thing across the whole line.
    """
    line = plain(grid.render(_event(to_state="completed_unpromoted")))
    assert "unpromoted" in line
    assert "completed_unpromoted" not in line
    assert len(line) == 180


def test_a_node_name_past_the_column_is_elided_not_wrapped(grid):
    node = "clive-global-operator-contract-repair-independent-review"
    line = plain(grid.render(_event(node=node)))
    assert node not in line
    assert node[:10] in line
    assert "…" in line
    assert len(line) == 180


def test_only_the_state_being_entered_may_explain_itself(grid):
    """A recovery must not inherit the clause from the state it left.

    Keying the reason on the source is how a promotion ends up still reporting
    the block it recovered from, describing a problem that is over.
    """
    entering = plain(grid.render(_event(to_state="blocked", reason="disk full")))
    assert "disk full" in entering

    leaving = plain(
        grid.render(
            _event(from_state="blocked", to_state="promoted", reason="disk full")
        )
    )
    assert "disk full" not in leaving


def test_a_reason_is_truncated_to_the_room_the_grid_leaves(grid):
    """Free text is bounded by the margin, never by wrapping onto a second row.

    The pane shows about eight lines, so a row that wraps costs a quarter of the
    visible history.
    """
    reason = "the canonical installed writer named by the plan does not satisfy it"
    line = plain(grid.render(_event(reason=reason)))
    assert len(line) == 180
    assert "\n" not in line
    assert "…" in line
    assert line.count("…") == 1
    assert reason[:20] in line


def test_a_reason_that_fits_is_printed_whole(grid):
    line = plain(grid.render(_event(reason="pytest exited 1")))
    assert "pytest exited 1" in line
    assert "…" not in line


def test_colour_is_off_by_default_so_callers_get_a_plain_string():
    """The library default stays plain; only the CLI opts a reader into colour.

    There is no terminal to detect — the pane is a pipe — so the choice cannot
    be inferred, and a programmatic caller must not have to strip escapes.
    """
    default = ticker_module.Ticker()
    assert "\x1b" not in default.render(_event())


def test_colour_changes_only_presentation(grid):
    coloured = ticker_module.Ticker(width=180, theme="light", color=True)
    event = _event()
    assert "\x1b" in coloured.render(event)
    assert plain(coloured.render(event)) == grid.render(event)


def test_no_color_in_the_environment_disables_colour(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b" not in ticker_module.Ticker(color=True).render(_event())


def test_a_worker_keeps_one_colour_and_neighbours_differ():
    """Identity is the question the node colour answers, so it must be stable.

    Hues are handed out in order of first appearance rather than hashed from the
    name, because a hash collides two live workers onto one colour.
    """
    painter = ticker_module.Ticker(color=True)
    first = painter.render(_event(node="alpha"))
    again = painter.render(_event(node="alpha", to_state="complete"))
    other = painter.render(_event(node="beta"))

    def hue(line: str) -> str:
        return re.search(r"\x1b\[38;5;(\d+)m", line).group(1)

    assert hue(first) == hue(again)
    assert hue(first) != hue(other)


def test_no_identity_colour_is_also_a_verdict_colour():
    """A worker's colour must never read as a verdict about that worker.

    Identity may sit near a neutral state hue — a worker coloured like `working`
    is harmless, since the two occupy different columns and neither is a claim
    about the other. Sharing a hue with blocked, stalled, complete or promoted
    is a false verdict: a worker looks finished while it runs, or stuck while it
    is fine.
    """
    for theme in ("light", "dark"):
        identity = set(ticker_module.PALETTE[theme])
        verdicts = {
            ticker_module.STATE_HUE[theme][name] for name in ticker_module.VERDICTS
        }
        assert identity.isdisjoint(verdicts), theme


def test_every_state_the_snapshot_can_emit_has_a_colour():
    """Both sides of the arrow are painted, so a bare state is a gap.

    Extending the set of states without extending the palette leaves the new one
    rendering dim on a line where every neighbour is coloured, which reads as
    missing data rather than as a state.
    """
    # Every state _watch_snapshot can produce: the manifest statuses, the phases
    # it maps, the recovery classifications it falls through to, and the
    # promotion the transition fold synthesises.
    emitted = {
        "dispatched",
        "working",
        "running",
        "complete",
        "blocked",
        "failed",
        "stalled",
        "stopped",
        "abandoned",
        "unknown",
        "promoted",
    } | set(ticker_module.DISPLAY.values())
    for theme in ("light", "dark"):
        missing = emitted - set(ticker_module.STATE_HUE[theme])
        assert not missing, (theme, sorted(missing))


def test_the_session_column_does_not_eat_the_node_column():
    """An unscoped reader needs the owning session and still needs the node."""
    grid = ticker_module.Ticker(width=180, color=False)
    node = "n-catalog-yaml-review-format"
    line = plain(grid.render(_event(node=node), with_session=True))
    assert node in line
    assert "ship-s10-20260901" in line
    assert len(line) == 180


def test_a_narrow_width_is_widened_to_what_the_columns_need():
    """Asking for less than the grid occupies must not produce a wrapped row."""
    grid = ticker_module.Ticker(width=40, color=False)
    line = plain(grid.render(_event(), with_session=True))
    assert len(line) == grid.width
    assert grid.width >= ticker_module.MIN_WIDTH


def test_the_cli_theme_choices_match_the_palettes_they_select():
    """The CLI names the themes rather than importing them, and must stay true.

    Importing this module from the CLI would drag in the whole crew facade —
    measured at over two seconds — onto every `reckon --help`. The names are
    therefore restated there, and bound here so the copy cannot drift.
    """
    from reckon import cli

    assert set(cli.TICKER_THEMES) == set(ticker_module.PALETTE)
    assert set(cli.TICKER_THEMES) == set(ticker_module.STATE_HUE)


def test_both_sides_of_a_transition_are_painted_by_the_state_map():
    """A transition is a pair, and each half carries its own state's colour.

    Painting only the destination makes a recovery indistinguishable from a
    routine landing: `blocked → promoted` and `complete → promoted` would render
    the same, and the first is the one worth noticing.
    """
    painter = ticker_module.Ticker(theme="light", color=True)
    hues = ticker_module.STATE_HUE["light"]

    recovered = painter.render(_event(from_state="blocked", to_state="promoted"))
    assert f"\x1b[38;5;{hues['blocked']}m" in recovered
    assert f"\x1b[38;5;{hues['promoted']}m" in recovered

    routine = painter.render(_event(from_state="complete", to_state="promoted"))
    assert f"\x1b[38;5;{hues['blocked']}m" not in routine
    # The two read differently, which is the whole point of painting the source.
    assert plain(recovered) != plain(routine)


def test_the_action_set_is_one_set_with_three_readers():
    """The blocked bucket, the states that may explain themselves, and the ones
    the grid lets carry a reason are the same proposition.

    Written out separately they drifted: `unknown` counted toward the blocked
    number and was allowed to keep its detail, but rendered without it, so the
    count said something needed attention and the line would not say what.
    """
    from reckon.crew import recovery

    assert set(recovery.EXPLAINED_STATES) == set(ticker_module.NEEDS_ACTION)
    assert set(recovery.FLEET_BLOCKED_STATES) == set(ticker_module.NEEDS_ACTION)
    # And each one is painted, since a state that needs action must be visible.
    for theme in ("light", "dark"):
        for state in ticker_module.NEEDS_ACTION:
            assert state in ticker_module.STATE_HUE[theme], (theme, state)
