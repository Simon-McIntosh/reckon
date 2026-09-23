"""The ticker is a fixed grid, so every column must land on one screen column.

A reader scans this pane down a column rather than across a line: which worker,
what state, how many still running. A field that shifts by a character between
rows defeats that, and free text that overruns the margin wraps the row into two
and halves a pane that only shows about eight lines at a time.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import struct
import termios

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon.crew import ticker as ticker_module

ESCAPES = re.compile(r"\x1b\[[0-9;]*m")

# The fleet counter block, wherever it sits on the row. Located by its own
# shape rather than by searching from the right edge: the reason is the last
# column now, so a letter at the end of a line belongs to free text. The
# counters are joined by a bare middle dot with no surrounding space (the gap
# was reclaimed to fund the model and effort cells).
COUNTERS = re.compile(r"(\s?\d{1,2})w(·\s?\d{1,2})b(·\s?\d{1,2})u")


def plain(line: str) -> str:
    """The line as the grid measures it, with any colour removed."""
    return ESCAPES.sub("", line)


def counters(line: str) -> re.Match[str]:
    """The counter block's match on a rendered row, or fail the test."""
    found = COUNTERS.search(plain(line))
    assert found, plain(line)
    return found


def letter_columns(line: str) -> dict[str, int]:
    """Each counter suffix's screen column on this row."""
    found = counters(line)
    return {letter: found.end(index) for index, letter in enumerate(("w", "b", "u"), 1)}


def _event(**overrides):
    event = {
        "observed_at": "2026-09-02T14:16:13+00:00",
        "run_id": "r-1",
        "node": "n-west-review-pr8-cut",
        "session": "ship-s10-20260901",
        "agent": "dsv4-flash/medium",
        "role": "implement",
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
    """Node, state, agent and every stat letter share a column across rows."""
    rows = [
        (
            plain(grid.render(_event(from_state=None, to_state="dispatched"))),
            "dispatched",
        ),
        (
            plain(grid.render(_event(from_state="complete", to_state="promoted"))),
            "promoted",
        ),
        (plain(grid.render(_event(working=12, blocked=0, unpromoted=3))), "blocked"),
    ]
    # The state cell begins on one column whether the row had a source or not.
    assert len({row.index(state) - ticker_module.MARKER for row, state in rows}) == 1
    # And no row carries a state it did not move into.
    assert not any("complete" in row for row, _ in rows)
    assert not any("→" in row for row, _ in rows)
    # Located by the counter block's own shape: the reason is the trailing
    # column, so a `w` at the end of a row is free text rather than a suffix.
    for letter in ("w", "b", "u"):
        assert len({letter_columns(row)[letter] for row, _ in rows}) == 1, letter


def test_stat_digits_align_across_one_and_two_digit_counts(grid):
    """`12w` stacks under `1w` rather than shunting the suffix."""
    one = plain(grid.render(_event(working=1)))
    twelve = plain(grid.render(_event(working=12)))
    assert one.index(" 1w") == twelve.index("12w")


def test_the_fleet_counters_render_as_digits_followed_by_one_letter(grid):
    """Each counter is its number followed by the state's single letter.

    `2 working · 4 blocked · 1 unpromoted` becomes `2w·4b·1u`: the word is
    gone from the count column, and a zero still shows rather than vanishing.
    The counts are joined by a bare middle dot with no surrounding space, so
    the separator adds nothing to the block a reader is summing.
    """
    line = plain(grid.render(_event(working=2, blocked=4, unpromoted=1)))
    assert " 2w· 4b· 1u" in line


def test_the_counter_separator_carries_no_surrounding_space(grid):
    """Each count ends at its letter and the bare dot follows it directly.

    The old separator wrapped its middle dot in two spaces; the reclaimed
    spaces are what fund the model and effort cells without taking width from
    the reason. Asserted on a rendered row: a space before the dot would read
    as the padding the change set out to remove.
    """
    line = plain(grid.render(_event(working=2, blocked=4, unpromoted=1)))
    assert "w· " in line
    assert "b· " in line
    assert " ·" not in line
    assert " · " not in line


def test_the_counter_block_holds_one_width_across_counts(grid):
    """The counted block keeps one width as counts change, so the row's right
    edge never moves.

    Each counter is two right-aligned digits and one letter: a single-digit
    count pads a leading space and a two-digit count does not, and the bare
    middle-dot separators add nothing per count. Asserted on rendered rows at
    the boundaries — 0, 9, 10 and 99 — where a naive renderer would drop the
    zero or widen the block.
    """
    rows = {
        count: letter_columns(plain(grid.render(_event(working=count))))
        for count in (0, 9, 10, 99)
    }
    for letter in ("w", "b", "u"):
        assert len({rows[count][letter] for count in rows}) == 1, letter


def test_stat_letters_align_at_a_fixed_column_across_one_and_two_digits(grid):
    """The three suffix letters hold their column at every count width.

    A two-digit working count sits beside one-digit blocked and unpromoted
    counts, and every letter still lands on the same screen column it would
    occupy when all three are one digit.
    """
    one = plain(grid.render(_event(working=1, blocked=3, unpromoted=2)))
    twelve = plain(grid.render(_event(working=12, blocked=3, unpromoted=2)))
    for letter in ("w", "b", "u"):
        assert letter_columns(one)[letter] == letter_columns(twelve)[letter], letter


def test_the_queued_counter_carries_the_initial_of_its_own_bucket(grid):
    """The waiting cell's letter is the initial of the bucket it counts.

    Three buckets take the initial of the state word the row's own state column
    prints, so a reader decodes them without a legend. The fourth counts runs
    the scheduler has admitted and not started, which that column prints as
    `waiting` — whose initial w the working bucket already holds. Naming the
    bucket `queued` restores the rule for it: q is that name's own initial, so
    every counter in the block is decoded the same way and none of them spends
    the width a spelled word would take from the reason clause.
    """
    line = plain(grid.render(_event(working=2, blocked=0, unpromoted=0, waiting=3)))
    assert ticker_module.STAT_LETTER["queued"] == "q"
    assert ticker_module._COUNT_FIELD["queued"] == "waiting"
    assert " 3q" in line
    # The count still arrives in the event's own `waiting` field; only the
    # bucket the counter names has changed.
    assert "queued" not in line


def test_the_queued_counter_keeps_the_block_a_constant_width_across_counts():
    """A count from zero to two digits moves no letter and no right edge.

    The spelled word is five columns wider than the letter it replaced, and the
    two-digit alignment that keeps the block's right edge fixed still holds with
    it: a reader scanning up the counter block finds every counter, and the
    whole row, on the same screen column whatever the fleet is doing.
    """
    for width in (180, 208):
        rows = {
            count: plain(
                ticker_module.Ticker(width=width, color=False).render(
                    _event(
                        working=count,
                        blocked=count,
                        unpromoted=count,
                        waiting=count,
                    )
                )
            )
            for count in (0, 9, 10, 99)
        }
        # Every row is exactly the requested width with the wider cell present.
        assert {len(row) for row in rows.values()} == {width}, width
        # The block's right edge — where the spelled word ends — holds its own
        # column at every count, so the right edge never moves.
        edges = {row.index("q", row.index("u")) + 1 for row in rows.values()}
        assert len(edges) == 1, (width, edges)
        # And the digit columns still align too: the block begins at one column
        # whether the count beside it is one digit or two.
        assert len({row.index(f"{count:>2}w") for count, row in rows.items()}) == 1


def test_the_queued_counter_still_renders_a_zero_rather_than_dropping_it():
    """A zero is dimmed, never blanked — the property the wider cell must keep.

    The cell is dimmed rather than dropped so a reader watching a drain sees the
    count reach zero instead of seeing it disappear; a zero that vanished would
    take the column with it and leave the right edge ragged. Spelling the word
    must not change that, so the zero still occupies its cell and still carries
    the dim style.
    """
    painter = ticker_module.Ticker(width=180, color=True)
    line = painter.render(_event(working=0, blocked=0, unpromoted=0, waiting=0))
    assert " 0q" in plain(line)
    assert ticker_module._DIM in line
    # The dimmed zero is the waiting cell's, not some neighbour's.
    assert ticker_module._DIM + " 0q" in line


def test_the_baseline_marker_is_a_word_the_row_does_not_use_elsewhere(grid):
    """The marker must say something no other cell repeats.

    A bullet at small size was read as a fresh dispatch, and a glyph the
    counters already separate their numbers with would put several
    indistinguishable marks on one row. The word is what the record is.
    """
    row = plain(grid.render(_event(event="baseline", to_state="working")))
    assert row.count(ticker_module.BASELINE_MARKER) == 1
    assert row.index(ticker_module.BASELINE_MARKER) < row.index("working")
    # No glyph marks the record: not the arrow the transition used to carry,
    # and not the bullet the baseline used to.
    assert "→" not in row
    assert "\N{BULLET}" not in row
    # And the marker is the baseline's alone.
    transition = plain(grid.render(_event(to_state="working")))
    assert ticker_module.BASELINE_MARKER not in transition


def test_the_state_cell_holds_its_column_when_there_is_no_previous_state(grid):
    """A first sighting has no source, and its state cell still lines up."""
    first = plain(grid.render(_event(from_state=None, to_state="dispatched")))
    later = plain(grid.render(_event(from_state="dispatched", to_state="complete")))
    assert first.index("dispatched") - ticker_module.MARKER == (
        later.index("complete") - ticker_module.MARKER
    )


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


def test_a_reason_clipped_at_the_margin_keeps_its_predicate_clause(grid):
    """A clip falls after the clause naming the predicate, never inside it.

    The measured defect: ``the process is gone without a complete manifest``
    truncated to ``the process is gone without a…`` collapsed a testable claim
    about a named file into a generic liveness remark. Free text cut at the
    margin must therefore keep that clause whole — the cut lands in trailing
    detail — and a reason that already fits renders verbatim, with no cut and
    no ellipsis.
    """
    long_reason = (
        "the process is gone without a complete manifest for the archive "
        "dry run which reported the marker moved before its files were read"
    )
    clipped = plain(grid.render(_event(to_state="blocked", reason=long_reason)))
    assert len(clipped) == 180
    assert "the process is gone without a complete manifest" in clipped
    assert "marker moved before its files" not in clipped
    assert "…" in clipped
    assert "\n" not in clipped

    short = plain(grid.render(_event(to_state="blocked", reason="disk full")))
    assert "disk full" in short
    assert "…" not in short
    assert len(short) == 180


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


def test_the_owner_column_marks_a_foreign_row_without_naming_it():
    """An unscoped reader needs to know whose row this is, not its identifier.

    The only decision the session answers is whether the row is the reader's to
    act on, so it costs one glyph rather than eighteen columns of run id — and
    the node it sits beside keeps its own column.
    """
    grid = ticker_module.Ticker(width=180, color=False)
    node = "n-catalog-yaml-review-format"
    line = plain(grid.render(_event(node=node), with_session=True))
    assert node in line
    assert "ship-s10-20260901" not in line
    assert ticker_module.FOREIGN_OWNER in line
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


def test_the_state_painted_on_the_row_is_the_one_it_moved_into():
    """The destination alone is painted, in its own hue.

    A state the row no longer carries must paint nothing: a row that still
    reported the state it left would describe a problem that is already over,
    which is the defect the reason clause was fixed for. Two rows into the same
    destination therefore read identically whatever they came from.
    """
    painter = ticker_module.Ticker(theme="light", color=True)
    hues = ticker_module.STATE_HUE["light"]

    recovered = painter.render(_event(from_state="blocked", to_state="promoted"))
    assert f"\x1b[38;5;{hues['promoted']}m" in recovered
    assert f"\x1b[38;5;{hues['blocked']}m" not in recovered

    routine = painter.render(_event(from_state="complete", to_state="promoted"))
    assert f"\x1b[38;5;{hues['complete']}m" not in routine
    # Both rows are the destination and nothing else.
    assert plain(recovered) == plain(routine)


def test_the_action_set_is_one_set_with_three_readers():
    """The blocked bucket, the states that may explain themselves, and the ones
    the grid lets carry a reason agree on one proposition, with the waiting
    family as the named exception that separates the marker from the count.

    Written out separately they drifted: `unknown` counted toward the blocked
    number and was allowed to keep its detail, but rendered without it, so the
    count said something needed attention and the line would not say what. The
    waiting family explains its own progress without asking a coordinator to
    act, so waiting and paused stay out of the action set while remaining
    explained. The overdue wait is the one member the two mechanisms split on:
    it is actionable, so it is in the action set and its row is marked, yet it
    is still a wait, so the counter keeps it in the waiting family — the count
    says what kind of run it is, the marker says a reader should look at it.
    """
    from reckon.crew import recovery

    # Every action state may explain itself, so an action state is never a bare
    # number with no line.
    assert set(recovery.EXPLAINED_STATES) >= ticker_module.NEEDS_ACTION
    # The waiting family that lifts itself is progress, not action, yet still
    # keeps its clause; only the overdue wait is actionable.
    assert not ticker_module.NEEDS_ACTION & {"waiting", "paused"}
    assert {"waiting", "paused"} <= set(recovery.EXPLAINED_STATES)
    # The blocked bucket is the action set minus the waiting family: an overdue
    # wait is marked (in the action set) but still counted as waiting (in the
    # waiting family), so the counter and the marker separate on it.
    assert set(recovery.FLEET_BLOCKED_STATES) == (
        set(ticker_module.NEEDS_ACTION) - set(recovery.WAITING_STATES)
    )
    # The overdue wait is both actionable and waiting — the one member on both
    # sides of the split rather than choosing between them.
    assert {"wait-aged"} <= ticker_module.NEEDS_ACTION
    assert {"wait-aged"} <= set(recovery.WAITING_STATES)
    # And each one is painted, since a state that needs action must be visible.
    for theme in ("light", "dark"):
        for state in ticker_module.NEEDS_ACTION:
            assert state in ticker_module.STATE_HUE[theme], (theme, state)


# ── The role column: the dispatch vocabulary, verbatim, left of the node ────


def test_every_dispatch_role_renders_as_its_complete_word_in_its_own_column(grid):
    """Each configured role spells itself in full; the vocabulary sets the width.

    The display form is the dispatch word itself — a role once configured
    renders exactly as it was dispatched, with no derivation and no prefix cut.
    The assertion iterates the vocabulary rather than listing roles, so a role
    added later is covered without editing this test.
    """
    rows = {
        role: plain(grid.render(_event(role=role, node="n-target")))
        for role in ticker_module.DISPATCH_ROLES
    }
    for role, line in rows.items():
        start = line.index(role)
        # The whole word, not a prefix of it, occupies its own column; the
        # truncation that used to ship would leave the tail outside the cell.
        assert line[start : start + ticker_module.ROLE].strip() == role, role

    # Every row's role text starts at the same screen column.
    positions = {line.index(role) for role, line in rows.items()}
    assert len(positions) == 1


def test_documentation_as_the_longest_role_sizes_the_column():
    """`documentation` at thirteen characters is what sets the column width.

    The column is derived from the vocabulary rather than guessed, so the
    longest member renders whole and the cell is exactly its width.
    """
    assert len("documentation") == ticker_module.ROLE
    line = plain(
        ticker_module.Ticker(width=180).render(
            _event(role="documentation", node="n-target")
        )
    )
    start = line.index("documentation")
    assert line[start : start + ticker_module.ROLE].strip() == "documentation"
    assert len(line) == 180


def test_the_widest_role_word_still_fits_the_width_budget(grid):
    """A row carrying the longest role word never exceeds the stated budget.

    The role column is sized by the vocabulary, so `documentation` renders
    whole and the fixed grid stays within DEFAULT_WIDTH; the budget is asserted
    on the rendered row, with the longest member present.
    """
    assert max(len(r) for r in ticker_module.DISPATCH_ROLES) == ticker_module.ROLE
    line = plain(grid.render(_event(role="documentation", node="n" * 8)))
    assert "documentation" in line
    assert len(line) <= ticker_module.DEFAULT_WIDTH


def test_an_unconfigured_role_renders_a_marker_not_a_truncated_word(grid):
    """A role outside the dispatch vocabulary must not show a plausible-looking
    but wrong word — it renders the marker instead."""
    line = plain(grid.render(_event(role="spike")))
    assert "spike" not in line
    assert "spi" not in line
    assert ticker_module.ROLE_UNKNOWN in line


def test_a_missing_role_also_renders_the_marker(grid):
    line = plain(grid.render(_event(role="")))
    assert ticker_module.ROLE_UNKNOWN in line


def test_the_role_column_sits_left_of_the_node_column(grid):
    line = plain(grid.render(_event(role="review", node="n-west")))
    assert line.index("review") < line.index("n-west")


def test_the_role_column_is_stable_across_every_configured_role(grid):
    """Every role's column start lines up, whatever the node name is doing."""
    rows = [
        plain(grid.render(_event(role="implement", node="n-a"))),
        plain(grid.render(_event(role="test", node="n" * 40))),
        plain(grid.render(_event(role="investigate", node="n-c"))),
    ]
    tokens = ("implement", "test", "investigate")
    assert len({row.index(token) for row, token in zip(rows, tokens, strict=True)}) == 1


def test_the_role_is_dim_rather_than_hued():
    """Colour answers which worker and does-this-need-me; role gets neither."""
    painter = ticker_module.Ticker(theme="light", color=True)
    line = painter.render(_event(role="review", node="n-west"))

    padded_role = (
        re.escape(ticker_module._DIM) + r"review\s*" + re.escape(ticker_module._RESET)
    )
    assert re.search(padded_role, line) is not None
    # The role text is never wrapped in a hue selector, unlike the node
    # beside it (which does carry one, on the same coloured line).
    assert re.search(r"\x1b\[38;5;\d+mreview", line) is None
    assert re.search(r"\x1b\[38;5;\d+m", line) is not None


def test_a_role_column_still_leaves_every_other_column_on_its_own_position():
    """Adding the role column must not upset the column budget for the rest."""
    grid = ticker_module.Ticker(width=180, color=False)
    rows = [
        plain(grid.render(_event(role="implement", to_state="dispatched"))),
        plain(grid.render(_event(role="review", to_state="promoted"))),
        plain(grid.render(_event(role="test", working=12, blocked=0, unpromoted=3))),
    ]
    assert len({len(row) for row in rows}) == 1
    assert not any("→" in row for row in rows)
    for letter in ("w", "b", "u"):
        assert len({letter_columns(row)[letter] for row in rows}) == 1, letter
    assert {len(row) for row in rows} == {180}


def test_a_narrow_width_still_widens_to_fit_the_role_column():
    grid = ticker_module.Ticker(width=40, color=False)
    assert grid.width >= ticker_module.MIN_WIDTH
    assert ticker_module.ROLE > 0
    line = plain(grid.render(_event()))
    assert len(line) == grid.width


# ── The agent: model and effort as two cells, one tight gap between them ────


def _fact_event(**overrides):
    """A new-shape event: model identity and effort as separate persisted facts."""
    event = {
        "model": "claude-sonnet-5",
        "alias": "sonnet5",
        "effort": "medium",
        "backend": "claude",
    }
    event.update(overrides)
    return _event(**event)


def test_a_declared_alias_and_its_effort_render_in_two_cells(grid):
    """The alias leads the model cell and the effort has a column of its own.

    The effort is read down the pane rather than parsed out of a composed
    label, so nothing bridges the two cells but the gap between columns.
    """
    line = plain(grid.render(_fact_event(alias="sonnet5")))
    assert "sonnet5" in line
    assert "medium" in line
    between = line[line.index("sonnet5") + len("sonnet5") : line.index("medium")]
    assert "\N{MIDDLE DOT}" not in between
    assert line.index("sonnet5") < line.index("medium")
    assert "claude-sonnet-5" not in line
    assert len(line) == 180


def test_a_backend_with_no_alias_renders_the_model_id_not_an_empty_cell(grid):
    """An unaliased model must not read as missing data.

    Without an alias the model cell shows the model id itself rather than the
    empty identity it used to render. The cell is sized from the configured
    aliases, so an id outside that set — this one is longer than the cell — is
    cut to it with an ellipsis rather than allowed to shift the effort column
    on its row, which is what keeps every row on one grid.
    """
    line = plain(
        grid.render(_event(model="deepseek-v4-flash", alias="", effort="xhigh"))
    )
    assert "deepseek-v4-flash" not in line
    cell = ticker_module.elide("deepseek-v4-flash", grid.model_width)
    assert cell.endswith("\N{HORIZONTAL ELLIPSIS}")
    assert cell in line
    # The cut cell is exactly the model column, so the effort keeps its offset.
    assert len(line) == 180
    assert line.index("xhigh") - ticker_module.PAIR_GAP == (
        line.index(cell) + grid.model_width
    )


def test_effort_renders_in_full_in_its_own_cell(grid):
    """The effort renders whole, never abbreviated, right of the model cell."""
    line = plain(grid.render(_fact_event(effort="xhigh")))
    assert "xhigh" in line
    assert line.index("xhigh") > line.index("sonnet5")


def test_the_derivation_lowercases_the_effort_word(grid):
    line = plain(grid.render(_fact_event(effort="MEDIUM")))
    assert "medium" in line


def test_max_renders_in_full_not_abbreviated(grid):
    """`max` spells in full in its own column; no mx shorthand survives."""
    line = plain(grid.render(_fact_event(effort="max")))
    assert "max" in line
    assert "mx" not in line


def test_a_declared_effort_spelling_does_not_abbreviate_the_full_word(grid):
    """The effort cell spells the whole word, never a declared prefix.

    The two-cell shape is what made the whole word affordable, so a declared
    abbreviation is not consulted anywhere on the display.
    """
    line = plain(grid.render(_fact_event(effort="high")))
    assert "high" in line


def test_the_model_and_effort_are_not_fused_into_one_cell(grid):
    """Two cells, one column boundary: no composed label and no separator."""
    line = plain(
        grid.render(_fact_event(model="deepseek-v4-flash", alias="dsv4-flash"))
    )
    assert "dsv4-flash" in line
    assert "medium" in line
    between = line[line.index("dsv4-flash") + len("dsv4-flash") : line.index("medium")]
    assert "\N{MIDDLE DOT}" not in between
    assert line.index("dsv4-flash") < line.index("medium")
    assert len(line) == 180


def test_the_longest_role_model_and_effort_land_whole_in_budget(grid):
    """The widest shipped vocabulary fits the fixed grid, whole, in budget.

    documentation (thirteen characters) is the longest dispatch role,
    dsv4-flash (ten) the widest model alias and minimal (seven) the widest
    effort word; the row carries all three with no elision mark, within the
    180-column default the module states as its budget.
    """
    line = plain(
        grid.render(
            _event(
                role="documentation",
                model="deepseek-v4-flash",
                alias="dsv4-flash",
                effort="minimal",
            )
        )
    )
    assert "documentation" in line
    assert "dsv4-flash" in line
    assert "minimal" in line
    assert "\N{HORIZONTAL ELLIPSIS}" not in line
    assert len(line) == ticker_module.DEFAULT_WIDTH
    assert len(line) <= grid.width


def test_a_pointer_written_before_this_change_still_renders_two_cells(grid):
    """A precomposed `model/effort` string splits into the two cells, not raises.

    This is the backward-compatibility negative: a log line written before the
    facts switch carries a composed agent string and no facts underneath, and
    it still lands in the two columns a reader scans. Its model half is cut to
    the cell with an ellipsis, exactly like any other value outside the
    configured aliases the cell is sized from.
    """
    line = plain(grid.render(_event(agent="legacy-model-id-longer-than-cell/medium")))
    assert "medium" in line
    assert "legacy-model-id-longer-than-cell" not in line
    cut = ticker_module.elide("legacy-model-id-longer-than-cell", grid.model_width)
    assert cut.endswith("\N{HORIZONTAL ELLIPSIS}")
    assert cut in line
    assert line.index(cut) + grid.model_width + ticker_module.PAIR_GAP == line.index(
        "medium"
    )
    assert len(line) == 180


def test_a_legacy_composed_string_renders_the_same_cells_as_separate_facts(
    grid,
):
    """A precomposed `model/effort` string gives the two cells facts would.

    The falsifier for the fallback: an older line that carries model and effort
    fused must render the same two cells as an equivalent line carrying them
    separately, so the columns a reader scans hold their offsets across record
    shapes.
    """
    legacy = plain(
        grid.render(_event(agent="dsv4-flash/minimal", model="", alias="", effort=""))
    )
    separate = plain(
        grid.render(
            _event(
                agent="",
                model="deepseek-v4-flash",
                alias="dsv4-flash",
                effort="minimal",
            )
        )
    )
    assert "dsv4-flash" in legacy
    assert "minimal" in legacy
    assert legacy.index("dsv4-flash") == separate.index("dsv4-flash")
    assert legacy.index("minimal") == separate.index("minimal")


def test_a_record_with_no_effort_renders_the_alias_alone(grid):
    """No effort, no separator: the model cell stands alone, effort is empty."""
    line = plain(
        grid.render(
            _event(model="claude-sonnet-5", alias="sonnet5", agent="", effort="")
        )
    )
    assert "sonnet5" in line
    assert "sonnet5\N{MIDDLE DOT}" not in line
    assert len(line) == 180


def test_an_effort_only_record_renders_without_a_leading_separator(grid):
    """Effort with no identity beside it renders in its own column, unprefixed."""
    line = plain(grid.render(_event(agent="", effort="high")))
    assert "high" in line
    assert "\N{MIDDLE DOT}high" not in line
    assert len(line) == 180


def test_the_effort_column_keeps_one_offset_whatever_the_alias_length(grid):
    """Two rows whose model aliases differ in length share one effort column.

    The property a reader scanning down the effort column depends on: the model
    cell is sized to the widest shipped alias, so the effort text begins at the
    same screen column on every row rather than riding the length of the alias
    beside it. Asserted on the rendered rows, never on the constant.
    """
    short = plain(grid.render(_fact_event(alias="sonnet5", effort="medium")))
    wide = plain(grid.render(_fact_event(alias="dsv4-flash", effort="medium")))
    assert short.index("sonnet5") == wide.index("dsv4-flash")
    assert short.index("medium") == wide.index("medium")


def test_no_row_pads_the_widest_alias_before_its_effort(grid):
    """A row pads its alias to the model cell, then the single tight gap.

    The model cell is sized from the widest alias the config declares, so an
    alias narrower than that cell is padded to it exactly and the effort cell
    follows after the one-space PAIR_GAP. Anything wider between the two would
    read as the row's columns having drifted, which is what a reader scanning a
    column cannot have.
    """
    line = plain(
        grid.render(
            _fact_event(
                model="deepseek-v4-flash",
                alias="dsv4-flash",
                effort="minimal",
            )
        )
    )
    between = line[line.index("dsv4-flash") + len("dsv4-flash") : line.index("minimal")]
    assert between == " " * (
        grid.model_width - len("dsv4-flash") + ticker_module.PAIR_GAP
    )


def test_the_model_cell_is_sized_from_the_longest_declared_alias(tmp_path, monkeypatch):
    """The column is sized from the aliases the resolved flight config declares.

    Every configured alias then lands its effort on one screen column, so a
    reader scans effort down the pane however the alias lengths differ — the
    reason a fixed ten-column cell failed, because dsv4.1-flash is twelve and
    shifted the cells after it on its rows, and a value outside the configured
    set is cut to the cell with an ellipsis rather than allowed to overflow.
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
        "    alias: astra6\n"
        "  hosted:\n"
        "    launch: cli\n"
        "    command: claude\n"
        "    sandbox: worktree-full\n"
        "    alias: sonnet 5\n"
        "roles:\n"
        "  implement: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(config))
    grid = ticker_module.Ticker(width=180, color=False)

    aliases = ("dsv4.1-flash", "astra6", "sonnet 5")
    rows = [
        plain(grid.render(_fact_event(alias=alias, effort="medium")))
        for alias in aliases
    ]
    # The measure: every configured alias lands its effort on one screen column.
    assert len({row.index("medium") for row in rows}) == 1
    assert (
        len({row.index(alias) for row, alias in zip(rows, aliases, strict=True)}) == 1
    )
    for row, alias in zip(rows, aliases, strict=True):
        assert alias in row
        assert row.index(alias) + grid.model_width + ticker_module.PAIR_GAP == (
            row.index("medium")
        )
    # That column is the longest declared alias, so a shorter one is padded.
    assert grid.model_width == len("dsv4.1-flash")

    unconfigured = "model-id-nineteen-xy"  # twenty characters, no alias declared
    assert len(unconfigured) == 20
    line = plain(grid.render(_fact_event(alias=unconfigured, model=unconfigured)))
    cut = ticker_module.elide(unconfigured, grid.model_width)
    assert cut.endswith("\N{HORIZONTAL ELLIPSIS}")
    assert cut in line
    assert unconfigured not in line
    assert line.index(cut) + grid.model_width + ticker_module.PAIR_GAP == (
        line.index("medium")
    )


def test_the_model_cell_reads_the_rows_own_project_layer(tmp_path, monkeypatch):
    """A row's project selects the project layer the model cell is sized from.

    A dispatch resolves its configuration through the project layer as well as
    shipped, host and override, so an alias a project declares is what a run on
    that project carries — and the pane must size its column to that same
    vocabulary. The row names its project, so a resolution that ignored it
    would size the cell from the project-less layers and clip the project's own
    alias. The host layer here declares no alias, so an alias in the cell can
    only have come from the project layer.
    """
    docs = tmp_path / "docs"
    (docs / "state" / "proj").mkdir(parents=True)
    (docs / "state" / "proj" / "flight.yaml").write_text(
        "version: 1\n"
        "default_backend: local\n"
        "backends:\n"
        "  local:\n"
        "    launch: in-harness\n"
        "    sandbox: worktree-full\n"
        "    alias: project-only-alias\n"
        "roles:\n"
        "  implement: {}\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    (home / "mounts.json").write_text(
        json.dumps({"mounts": {"proj": str(docs)}}), encoding="utf-8"
    )
    monkeypatch.setenv("RECKON_HOME", str(home))

    alias = "project-only-alias"
    grid = ticker_module.Ticker(width=180, color=False)
    # The project-less cell is narrower than the project's alias, so a row that
    # does not read the project layer cannot hold the alias whole.
    assert grid.model_width < len(alias)

    line = plain(
        grid.render(_event(project="proj", alias=alias, effort="high", model=""))
    )
    assert alias in line
    assert line.index("high") - ticker_module.PAIR_GAP == (
        line.index(alias) + len(alias)
    )


def test_dispatch_facts_flow_to_the_log_and_a_later_config_edit_cannot_restate_them(
    grid,
):
    """The alias and spelling are read at dispatch, never from current config.

    A configuration edit after the run starts must not silently rewrite what
    ran: the model and effort it persisted still render even though a fresh
    dispatch would now write different facts.
    """
    from reckon.crew.dispatch import _stamp_agent_display

    shipped = {
        "launch": "cli",
        "model": "claude-sonnet-5",
        "effort": "medium",
        "alias": "sonnet5",
        "effort_spelling": {"medium": "me", "max": "mx"},
    }
    stamped = _stamp_agent_display(
        {"model": "claude-sonnet-5", "effort": "medium"}, shipped
    )
    line = plain(
        grid.render(
            _event(
                model=str(stamped.get("model") or ""),
                alias=str(stamped.get("alias") or ""),
                effort=str(stamped.get("effort") or ""),
            )
        )
    )
    assert "sonnet5" in line
    assert "medium" in line
    assert "claude-sonnet-5" not in line

    # The operator later edits the configuration — alias dropped.
    edited = {"launch": "cli", "model": "claude-sonnet-5", "effort": "medium"}
    restated = _stamp_agent_display(
        {"model": "claude-sonnet-5", "effort": "medium"}, edited
    )
    after = plain(
        grid.render(
            _event(
                model=str(restated.get("model") or ""),
                alias=str(restated.get("alias") or ""),
                effort=str(restated.get("effort") or ""),
            )
        )
    )
    assert "sonnet5" not in after

    # But the already-recorded facts still render what actually ran.
    again = plain(
        grid.render(
            _event(
                model=str(stamped.get("model") or ""),
                alias=str(stamped.get("alias") or ""),
                effort=str(stamped.get("effort") or ""),
            )
        )
    )
    assert again == line


# ── The width: measured from the ancestor terminal, not stated ──────────────


def _no_ancestor_terminals() -> list:
    """The ancestry stub for a detached follower: no terminal anywhere above."""
    return []


def _open_terminal(columns: int) -> tuple[str, int, int]:
    """A real pty set to ``columns`` wide, returned as path, master, slave.

    A real terminal is used rather than a fabricated value so the width the
    grid adopts is read from the kernel by the same ioctl the renderer runs,
    against an actual device. The caller holds the fds open and closes them.
    """
    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, columns, 0, 0))
        return os.ttyname(slave), master, slave
    except BaseException:
        os.close(master)
        os.close(slave)
        raise


def test_the_width_is_read_from_the_ancestor_terminal(monkeypatch):
    """A follower's own stdout is a pipe; the terminal an ancestor owns is the
    pane, and its current column count is what the grid must fit."""
    path, master, slave = _open_terminal(208)
    try:
        monkeypatch.setattr(ticker_module, "_ancestor_terminal_paths", lambda: [path])
        resolved = ticker_module.resolve_terminal_width()
    finally:
        os.close(master)
        os.close(slave)
    # The pane's own cut IS the width: the column count passes straight through
    # as the grid width, with nothing guessed and subtracted on top of the read.
    assert resolved == 208


def test_a_detached_follower_falls_back_to_the_stated_width(monkeypatch):
    """No ancestor owns a terminal — collector or nohup'd — so the stated
    default is the width, exactly as it was before the ancestry walk existed."""
    monkeypatch.setattr(
        ticker_module, "_ancestor_terminal_paths", _no_ancestor_terminals
    )
    assert ticker_module.resolve_terminal_width() == ticker_module.DEFAULT_WIDTH


def test_a_non_terminal_device_offers_no_width(monkeypatch):
    """A stdio descriptor under /dev that is not a tty must not yield zero.

    The ancestry can surface /dev/null ahead of the real terminal; reading it
    as a width of zero would crowd the grid into the left edge. It reports no
    width and the fallback holds.
    """
    monkeypatch.setattr(
        ticker_module, "_ancestor_terminal_paths", lambda: ["/dev/null"]
    )
    assert ticker_module.resolve_terminal_width() == ticker_module.DEFAULT_WIDTH


def test_every_line_ends_at_the_resolved_width_when_crowded(monkeypatch):
    """A long node, a long reason and wide counts together never overflow.

    A wrapped row costs a quarter of the visible history, which is worse than a
    line that only falls short, so the grid composes to exactly the resolved
    width either way, and the counters hold their fixed column while the reason
    takes whatever remains.
    """
    path, master, slave = _open_terminal(150)
    try:
        monkeypatch.setattr(ticker_module, "_ancestor_terminal_paths", lambda: [path])
        resolved = ticker_module.resolve_terminal_width()
    finally:
        os.close(master)
        os.close(slave)

    grid = ticker_module.Ticker(width=resolved, color=False)
    line = plain(
        grid.render(
            _event(
                node="clive-global-operator-contract-repair-independent-review",
                reason="the canonical installed writer the plan names still does not satisfy the gate",
                working=12,
                blocked=9,
                unpromoted=7,
            )
        )
    )
    # Measured on the rendered row, not on the format string: the row is exactly
    # the resolved width and no more.
    assert len(line) == resolved
    assert len(line) <= resolved
    # The counters sit ahead of the reason, at the same column a one-digit row
    # puts them, so a pane clipping its own right edge takes free text and
    # never a count.
    assert "12w· 9b· 7u" in line
    narrow = plain(grid.render(_event(working=1, blocked=2, unpromoted=3)))
    assert letter_columns(line) == letter_columns(narrow)
    assert counters(line).end() < len(line)


# ── The measure cells: fixed widths, stable columns, absence vs zero ──────
#
# After the fleet counters sit wall time and generation rate, the only two
# spend figures the row carries — model seconds, charged tokens and the dollar
# figure stay in the record, unrendered. Every cell is right-aligned to a
# fixed width with a single space between cells, and an unmeasured fact renders
# the dim absence marker rather than a zero — a zero asserts a measurement
# that was never taken. Where a row state makes a figure noise (wall time
# entering dispatched), the cell blanks instead of marking absence.


def _spend_columns(model_width: int) -> list[tuple[int, int]]:
    """The (start, width) of the two measure cells on a plain rendered line.

    ``model_width`` is the grid's own model cell width — read from the grid
    under test — rather than the fixed default, so a config declaring a wider
    alias moves these columns with the row it renders.
    """
    prefix = (
        ticker_module.CLOCK
        + ticker_module.GAP
        + ticker_module.ROLE
        + ticker_module.GAP
        + ticker_module.NODE
        + ticker_module.STATE_REGION
        + model_width
        + ticker_module.PAIR_GAP
        + ticker_module.EFFORT
    )
    prefix += sum(3 for _ in ticker_module._CELLS) + (len(ticker_module._CELLS) - 1)
    prefix += ticker_module.SPEND_GAP
    columns: list[tuple[int, int]] = []
    for width in (ticker_module.WALL, ticker_module.RATE):
        columns.append((prefix, width))
        prefix += width + ticker_module.SPEND_GAP
    return columns


def test_a_line_with_spend_facts_is_exactly_the_requested_width_at_three_sizes():
    """The spend block holds the fixed grid at its floor and beyond.

    A rendered line is exactly the requested visible width with no wrapping, so
    the floor is asserted too (the narrowest width the grid can honour). The
    floor is read from the grid at its floor request, because a config
    declaring an alias wider than the default raises that floor by the model
    cell's growth.
    """
    event = _event(
        spend_wall_seconds=6_422.0,
        spend_model_seconds=3_133.5,
        spend_charged_tokens=6_400_000,
        spend_generation_rate=38.2,
        spend_notional_cost_usd=3.41,
    )
    floor = ticker_module.Ticker(width=ticker_module.MIN_WIDTH).width
    for width in (floor, 180, 208):
        for with_session in (False, True):
            line = plain(
                ticker_module.Ticker(width=width).render(
                    event, with_session=with_session
                )
            )
            assert len(line) == width, (width, len(line))


def test_the_two_measure_cells_render_measured_figures_in_their_own_cells():
    """wall 7, rate 4, right-aligned; no cells for the figures the row cut."""
    event = _event(
        spend_wall_seconds=6_422.0,
        spend_model_seconds=3_133.5,
        spend_charged_tokens=1_500_000,
        spend_generation_rate=44.0,
        spend_notional_cost_usd=0.12,
    )
    grid = ticker_module.Ticker(width=180)
    line = plain(grid.render(event))
    wall, rate = _spend_columns(grid.model_width)
    assert line[wall[0] : wall[0] + wall[1]] == "1:47:02"
    assert line[rate[0] : rate[0] + rate[1]] == "  44"
    # The cut figures stay in the record, not on the row.
    assert "52:14" not in line
    assert "1.5M" not in line
    assert "0.12" not in line


def test_the_two_measure_cells_occupy_the_same_columns_on_every_row():
    """A resumed run, a redispatched run and a shadow line up by column.

    A reader scans each figure down the pane; if a row's cells sat one column
    to the side, the scan would re-find a moving column and the width read
    would shift. The resume's measured figures and the shadow's absence markers
    must land in the same two columns.
    """
    rows = [
        _event(
            node="n-resumed",
            lineage={"kind": "resumed"},
            spend_wall_seconds=100.0,
            spend_generation_rate=12.0,
        ),
        _event(
            node="n-redispatch",
            lineage={"kind": "redispatch", "root_run_id": "r-shadow"},
            spend_wall_seconds=3_600.0,
            spend_generation_rate=44.0,
        ),
        _event(
            node="n-shadow",
            lineage={"kind": "shadow"},
            spend_wall_seconds=None,
            spend_generation_rate=None,
        ),
    ]
    expected = [
        ["   1:40", "  12"],
        ["1:00:00", "  44"],
        ["      \N{EN DASH}", "   \N{EN DASH}"],
    ]
    grid = ticker_module.Ticker(width=180)
    columns = list(zip(("wall", "rate"), _spend_columns(grid.model_width), strict=True))
    for event, cells in zip(rows, expected, strict=True):
        line = plain(grid.render(event))
        for (name, (start, width)), expected_cell in zip(columns, cells, strict=True):
            assert line[start : start + width] == expected_cell, (name, line)


def test_an_unmeasured_cell_and_a_measured_zero_are_distinct_strings():
    """The absence marker must never read as a zero.

    The same row rendered once with measured facts and once without must
    differ in both measure cells, and the unmeasured spelling is the marker —
    never ``0`` or ``0:00``, which assert a measurement that was never taken.
    """
    zero = _event(
        spend_wall_seconds=0.0,
        spend_generation_rate=0.0,
    )
    unmeasured = _event(
        spend_wall_seconds=None,
        spend_generation_rate=None,
    )
    zero_line = plain(ticker_module.Ticker(width=180).render(zero))
    marker_line = plain(ticker_module.Ticker(width=180).render(unmeasured))
    for start, width in _spend_columns(ticker_module.Ticker(width=180).model_width):
        zero_cell = zero_line[start : start + width]
        marker_cell = marker_line[start : start + width]
        assert zero_cell != marker_cell
        assert marker_cell.strip() == "\N{EN DASH}"
        assert zero_cell.strip() != "\N{EN DASH}"
        assert any(character.isdigit() for character in zero_cell)


def test_a_measure_noise_for_the_row_state_renders_blank_not_the_marker():
    """A figure the row state makes meaningless renders blank, not as absence.

    A transition into dispatched is at time zero by definition, so its wall
    cell is blank — and blank rather than the dim dash, because the dash
    already means unmeasured and two different facts must not share one glyph.
    The dash survives where a figure was genuinely unmeasured, on a state where
    it would have been meaningful, and a measured figure keeps its cell against
    both.
    """
    grid = ticker_module.Ticker(width=180)
    wall, rate = _spend_columns(grid.model_width)
    wall_span = slice(wall[0], wall[0] + wall[1])
    rate_span = slice(rate[0], rate[0] + rate[1])

    dispatched = plain(
        grid.render(
            _event(
                from_state=None,
                to_state="dispatched",
                spend_wall_seconds=1_234.0,
                spend_generation_rate=44.0,
            )
        )
    )
    assert dispatched[wall_span] == " " * wall[1]
    assert dispatched[rate_span] == "  44"

    working = plain(
        grid.render(
            _event(
                from_state="dispatched",
                to_state="working",
                spend_wall_seconds=None,
                spend_generation_rate=44.0,
            )
        )
    )
    assert working[wall_span].strip() == "\N{EN DASH}"
    # Blank and marker are distinct on the same cell: one keeps no glyph, the
    # other is a glyph a reader can find.
    assert working[wall_span] != dispatched[wall_span]

    measured = plain(
        grid.render(
            _event(
                from_state="dispatched",
                to_state="working",
                spend_wall_seconds=1_234.0,
                spend_generation_rate=44.0,
            )
        )
    )
    assert measured[wall_span] == "  20:34"


def test_calibration_consumes_an_observed_cut_position():
    """The width is the pane's measured column count — a reading, not a guess.

    The calibration path takes the observed cut position and adopts it as the
    grid width, with nothing in between to adjust; only a cut too narrow for
    the fixed columns is raised, so a line never wraps.
    """
    assert ticker_module.calibrated_width(208) == 208
    assert ticker_module.calibrated_width(45) == ticker_module.MIN_WIDTH
    assert not hasattr(ticker_module, "INSET"), (
        "the width must come from the observed cut, not a guessed literal"
    )


def test_the_measured_pane_width_leaves_the_reason_at_least_seventy_five_columns(
    monkeypatch,
):
    """The same pane that left 57 columns for the reason now spares at least 75.

    The grid no longer spends eighteen columns on model seconds, tokens and the
    dollar figure, so those columns move to the free text. Measured the way the
    renderer measures it: the pty's column count is the observed cut, the
    calibration path adopts it, and the reason then gets the whole trailing
    margin with nothing left unspent.
    """
    path, master, slave = _open_terminal(208)
    try:
        monkeypatch.setattr(ticker_module, "_ancestor_terminal_paths", lambda: [path])
        width = ticker_module.resolve_terminal_width()
    finally:
        os.close(master)
        os.close(slave)

    line = plain(
        ticker_module.Ticker(width=width, color=False).render(
            _event(to_state="blocked", reason="x" * 400)
        )
    )
    assert len(line) == width
    # The reason cell begins where the fixed columns end; everything after it
    # is free text, and the resolved pane must spare it at least 75 columns.
    assert width - line.index("x") >= 75


def test_the_fleet_counters_precede_the_measures():
    """The always-populated columns lead; the optional measures follow.

    The grid reads clock, role, node, state pair, model, effort, then the fleet
    counters, then wall and rate, then the reason last — the measures sit
    behind the counters, both ahead of the free text a clipping pane is
    allowed to cut.
    """
    grid = ticker_module.Ticker(width=180)
    line = plain(
        grid.render(_event(spend_wall_seconds=6_422.0, spend_generation_rate=38.0))
    )
    wall, _ = _spend_columns(grid.model_width)
    assert counters(line).end() <= wall[0]
    assert "1:47:02" in line[wall[0] :]


def test_a_follower_opened_at_a_pane_width_emits_a_grid_at_that_width(
    monkeypatch,
) -> None:
    """Every row a follower delivers is a transition, ending at the measured width.

    The width is read from the terminal an ancestor owns by the same ioctl; the
    delivered stream carries no calibration line. A follower attached to a pane
    of a known width emits its rows at exactly that width — and every delivered
    row carries a node and a state pair, so a coordinator reading each line as
    a delivery never sees geometry.
    """
    path, master, slave = _open_terminal(208)
    try:
        monkeypatch.setattr(ticker_module, "_ancestor_terminal_paths", lambda: [path])
        monkeypatch.setattr(
            cli_module,
            "_follow_watch_lines",
            lambda *_a, **_kw: iter([_event(), _event(node="second-node")]),
        )
        result = CliRunner().invoke(
            cli_module.main, ["crew", "follow", "--project", "proj", "--no-color"]
        )
    finally:
        os.close(master)
        os.close(slave)

    assert result.exit_code == 0, result.output
    rows = result.output.splitlines()
    # The measured width, not a guessed one: each row ends at the pane's 208
    # columns, and no layout line is delivered alongside the transitions.
    assert len(rows) == 2
    for row in rows:
        assert len(row) == 208
        assert ("n-west-review-pr8-cut" in row) or ("second-node" in row)
        # The destination is on the line and the state it moved from is not.
        assert "blocked" in row
        assert "working" not in row
