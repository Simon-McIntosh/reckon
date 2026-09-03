"""The ticker is a fixed grid, so every column must land on one screen column.

A reader scans this pane down a column rather than across a line: which worker,
what state, how many still running. A field that shifts by a character between
rows defeats that, and free text that overruns the margin wraps the row into two
and halves a pane that only shows about eight lines at a time.
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import struct
import termios

import pytest

from reckon.crew import ticker as ticker_module

ESCAPES = re.compile(r"\x1b\[[0-9;]*m")

# The fleet counter block, wherever it sits on the row. Located by its own
# shape rather than by searching from the right edge: the reason is the last
# column now, so a letter at the end of a line belongs to free text.
COUNTERS = re.compile(r"(\s?\d{1,2})w( ·\s+\d{1,2})b( ·\s+\d{1,2})u")


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
        "agent": "gpt-5.6-sol/medium",
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
    """Node, arrow, agent and every stat letter share a column across rows."""
    rows = [
        plain(grid.render(_event(from_state=None, to_state="dispatched"))),
        plain(grid.render(_event(from_state="complete", to_state="promoted"))),
        plain(grid.render(_event(working=12, blocked=0, unpromoted=3))),
    ]
    assert len({row.index("→") for row in rows}) == 1
    # Located by the counter block's own shape: the reason is the trailing
    # column, so a `w` at the end of a row is free text rather than a suffix.
    for letter in ("w", "b", "u"):
        assert len({letter_columns(row)[letter] for row in rows}) == 1, letter


def test_stat_digits_align_across_one_and_two_digit_counts(grid):
    """`12w` stacks under `1w` rather than shunting the suffix."""
    one = plain(grid.render(_event(working=1)))
    twelve = plain(grid.render(_event(working=12)))
    assert one.index(" 1w") == twelve.index("12w")


def test_the_fleet_counters_render_as_digits_followed_by_one_letter(grid):
    """Each counter is its number followed by the state's single letter.

    `2 working · 4 blocked · 1 unpromoted` becomes `2w · 4b · 1u`: the word is
    gone from the count column, and a zero still shows rather than vanishing.
    """
    line = plain(grid.render(_event(working=2, blocked=4, unpromoted=1)))
    assert " 2w ·  4b ·  1u" in line


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


def test_the_baseline_glyph_is_not_a_glyph_the_row_already_uses(grid):
    """The arrow column must say something no other column repeats.

    The counters separate their three numbers with middle dots, so a baseline
    marked with one would put four indistinguishable dots on a row and say
    nothing a reader could locate.
    """
    assert ticker_module.BASELINE_ARROW != ticker_module.TRANSITION_ARROW
    row = plain(grid.render(_event(event="baseline", to_state="working")))
    assert row.count(ticker_module.BASELINE_ARROW) == 1


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


# ── The role column: the dispatch vocabulary, verbatim, left of the node ────


def test_every_dispatch_role_renders_its_four_characters_in_its_own_column(grid):
    """Each configured role derives to its first four characters, lowercased.

    The derivation is mechanical — no display table to stay in step with — and
    the six forms are all distinct: impl, inve, revi, test, clea, docu.
    """
    rows = {}
    for role in sorted(ticker_module.DISPATCH_ROLES):
        rows[role] = plain(grid.render(_event(role=role, node="n-target")))

    expected = {
        "implement": "impl",
        "cleanup": "clea",
        "review": "revi",
        "investigate": "inve",
        "test": "test",
        "documentation": "docu",
    }
    for role, shown in expected.items():
        assert shown in rows[role], role
    assert len(set(expected.values())) == 6

    # Every row's role text starts at the same screen column.
    positions = {rows[role].index(shown) for role, shown in expected.items()}
    assert len(positions) == 1


def test_documentation_derives_to_docu_and_sets_no_wider_column():
    """`documentation` is thirteen characters; `docu` is what actually ships."""
    line = plain(
        ticker_module.Ticker(width=180).render(
            _event(role="documentation", node="n-target")
        )
    )
    assert "docu" in line
    assert "documentation" not in line
    assert len(line) == 180


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
    assert line.index("revi") < line.index("n-west")


def test_the_role_column_is_stable_across_every_configured_role(grid):
    """Every role's column start lines up, whatever the node name is doing."""
    rows = [
        plain(grid.render(_event(role="implement", node="n-a"))),
        plain(grid.render(_event(role="test", node="n" * 40))),
        plain(grid.render(_event(role="investigate", node="n-c"))),
    ]
    tokens = ("impl", "test", "inve")
    assert len({row.index(token) for row, token in zip(rows, tokens, strict=True)}) == 1


def test_the_role_is_dim_rather_than_hued():
    """Colour answers which worker and does-this-need-me; role gets neither."""
    painter = ticker_module.Ticker(theme="light", color=True)
    line = painter.render(_event(role="review", node="n-west"))

    padded_role = (
        re.escape(ticker_module._DIM) + r"revi" + re.escape(ticker_module._RESET)
    )
    assert re.search(padded_role, line) is not None
    # The role text is never wrapped in a hue selector, unlike the node
    # beside it (which does carry one, on the same coloured line).
    assert re.search(r"\x1b\[38;5;\d+mrevi\x1b\[0m", line) is None
    assert re.search(r"\x1b\[38;5;\d+m", line) is not None


def test_a_role_column_still_leaves_every_other_column_on_its_own_position():
    """Adding the role column must not upset the column budget for the rest."""
    grid = ticker_module.Ticker(width=180, color=False)
    rows = [
        plain(
            grid.render(
                _event(role="implement", from_state=None, to_state="dispatched")
            )
        ),
        plain(
            grid.render(
                _event(role="review", from_state="complete", to_state="promoted")
            )
        ),
        plain(grid.render(_event(role="test", working=12, blocked=0, unpromoted=3))),
    ]
    assert len({row.index("→") for row in rows}) == 1
    for letter in ("w", "b", "u"):
        assert len({letter_columns(row)[letter] for row in rows}) == 1, letter
    assert {len(row) for row in rows} == {180}


def test_a_narrow_width_still_widens_to_fit_the_role_column():
    grid = ticker_module.Ticker(width=40, color=False)
    assert grid.width >= ticker_module.MIN_WIDTH
    assert ticker_module.ROLE > 0
    line = plain(grid.render(_event()))
    assert len(line) == grid.width


# ── The agent column: alias and effort fused into one cell, one separator ──


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


def test_a_declared_alias_renders_fused_with_its_effort(grid):
    """The alias spares the reader the model line, fused with its effort."""
    line = plain(grid.render(_fact_event(alias="sonnet5")))
    assert "sonnet5\N{MIDDLE DOT}medium" in line
    assert "claude-sonnet-5" not in line
    assert len(line) == 180


def test_a_backend_with_no_alias_renders_the_model_id_not_an_empty_cell(grid):
    """An unaliased model must not read as missing data.

    Without an alias the cell shows the model id itself rather than the empty
    identity it used to render. This model id alone reaches the column
    budget, so the fused effort elides rather than truncating the identity —
    the more useful half to keep.
    """
    line = plain(
        grid.render(_event(model="deepseek-v4-flash", alias="", effort="xhigh"))
    )
    assert "deepseek-v4-flash" in line
    assert len(line) == 180


def test_effort_renders_in_full_beside_the_alias(grid):
    """The effort renders whole, never abbreviated, beside the alias."""
    line = plain(grid.render(_fact_event(effort="xhigh")))
    assert "sonnet5\N{MIDDLE DOT}xhigh" in line


def test_the_derivation_lowercases_the_effort_word(grid):
    line = plain(grid.render(_fact_event(effort="MEDIUM")))
    assert "medium" in line


def test_max_renders_in_full_not_abbreviated(grid):
    """`max` spells in full beside the alias; no mx shorthand survives."""
    line = plain(grid.render(_fact_event(effort="max")))
    assert "sonnet5\N{MIDDLE DOT}max" in line
    assert "sonnet5\N{MIDDLE DOT}mx" not in line


def test_a_declared_effort_spelling_does_not_abbreviate_the_full_word(grid):
    """The fused cell spells the effort whole.

    The abbreviation served the fused cell before the twin-column split; the
    rejoin keeps the full word, so a declared spelling is not consulted here
    at all.
    """
    line = plain(grid.render(_fact_event(effort="high")))
    assert "sonnet5\N{MIDDLE DOT}high" in line


def test_the_model_and_effort_share_one_cell_with_one_separator(grid):
    """One cell, one separator: no gap wide enough to read as a missing field."""
    line = plain(
        grid.render(_fact_event(model="deepseek-v4-flash", alias="dsv4-flash"))
    )
    assert "dsv4-flash\N{MIDDLE DOT}medium" in line
    assert len(line) == 180


def test_the_widest_configured_pair_renders_without_elision(grid):
    """Ten characters of alias, one separator, seven of effort: eighteen exactly.

    dsv4-flash and minimal are the widest real alias and effort word; the
    resolved column must hold the whole pair with no padding beyond the single
    separator and no truncation.
    """
    line = plain(
        grid.render(
            _fact_event(model="deepseek-v4-flash", alias="dsv4-flash", effort="minimal")
        )
    )
    fused = "dsv4-flash\N{MIDDLE DOT}minimal"
    assert len(fused) == ticker_module.AGENT
    start = line.index(fused)
    assert line[start + len(fused)] == " "


def test_a_pointer_written_before_this_change_still_renders_a_label(grid):
    """A precomposed `model/effort` string must keep working, not raise.

    This is the backward-compatibility negative: a log line written before the
    facts switch carries a composed agent string and no facts underneath, and
    it renders whole rather than raising.
    """
    line = plain(grid.render(_event(agent="gpt-5.6-sol/medium")))
    assert "gpt-5.6-sol/medium" in line
    assert len(line) == 180


def test_a_record_with_no_effort_renders_the_alias_alone(grid):
    """No effort, no trailing separator: a bare separator would look like a
    truncated identity rather than the absence it is."""
    line = plain(
        grid.render(
            _event(model="claude-sonnet-5", alias="sonnet5", agent="", effort="")
        )
    )
    assert "sonnet5" in line
    assert "sonnet5\N{MIDDLE DOT}" not in line
    assert len(line) == 180


def test_an_effort_only_record_renders_without_a_leading_separator(grid):
    """Effort with no identity beside it renders alone, not prefixed by a
    separator that has nothing to attach to."""
    line = plain(grid.render(_event(agent="", effort="high")))
    assert "high" in line
    assert "\N{MIDDLE DOT}high" not in line
    assert len(line) == 180


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
    assert resolved == 208 - ticker_module.INSET


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
    assert "12w ·  9b ·  7u" in line
    narrow = plain(grid.render(_event(working=1, blocked=2, unpromoted=3)))
    assert letter_columns(line) == letter_columns(narrow)
    assert counters(line).end() < len(line)
