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
    # Searched from the right: the states' letters of the fleet counters sit at
    # the end of every line, so the last `w`, `b` and `u` are the counter
    # suffixes even when the same letter appears earlier in a state or role.
    for letter in ("w", "b", "u"):
        assert len({row.rindex(letter) for row in rows}) == 1, letter


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
        assert one.rindex(letter) == twelve.rindex(letter), letter


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


# ── The role column: the dispatch vocabulary, verbatim, left of the node ────


def test_every_dispatch_role_renders_whole_in_its_own_column(grid):
    """Each configured role appears intact, in a column all rows share."""
    rows = {}
    for role in sorted(ticker_module.DISPATCH_ROLES):
        rows[role] = plain(grid.render(_event(role=role)))

    expected = {
        "implement": "implement",
        "cleanup": "cleanup",
        "review": "review",
        "investigate": "investigate",
        "test": "test",
        "documentation": "docs",
    }
    for role, shown in expected.items():
        assert shown in rows[role], role

    # Every row's role text starts at the same screen column.
    positions = {rows[role].index(shown) for role, shown in expected.items()}
    assert len(positions) == 1


def test_documentation_is_narrowed_to_docs_and_sets_no_wider_column():
    """`documentation` is thirteen characters; `docs` is what actually ships."""
    line = plain(ticker_module.Ticker(width=180).render(_event(role="documentation")))
    assert "docs" in line
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
    line = plain(grid.render(_event(role="review", node="n-review-target")))
    assert line.index("review") < line.index("n-review-target")


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
    line = painter.render(_event(role="review", node="n-review-target"))

    padded_role = (
        re.escape(ticker_module._DIM) + r"review\s+" + re.escape(ticker_module._RESET)
    )
    assert re.search(padded_role, line) is not None
    # The role text is never wrapped in a hue selector, unlike the node
    # beside it (which does carry one, on the same coloured line).
    assert re.search(r"\x1b\[38;5;\d+mreview\s*\x1b\[0m", line) is None
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
    # The fleet counter letters, searched from the right: each sits at the end
    # of its row, so the last `w`, `b` and `u` are the counter suffixes even
    # when the same letter appears earlier in a state or role.
    for letter in ("w", "b", "u"):
        assert len({row.rindex(letter) for row in rows}) == 1, letter
    assert {len(row) for row in rows} == {180}


def test_a_narrow_width_still_widens_to_fit_the_role_column():
    grid = ticker_module.Ticker(width=40, color=False)
    assert grid.width >= ticker_module.MIN_WIDTH
    assert ticker_module.ROLE > 0
    line = plain(grid.render(_event()))
    assert len(line) == grid.width


# ── The agent column: a configured alias and effort spelling, or no table ────


def _stamped_agent(**overrides):
    """A run pointer's agent record as dispatch stamps it today."""
    return {
        "backend": "claude",
        "launch": "cli",
        "model": "claude-sonnet-5",
        "alias": "sonnet5",
        "effort": "medium",
        **overrides,
    }


def test_a_configured_alias_renders_in_place_of_the_model_id(grid):
    """The alias is the whole point: it spares the reader the model line.

    `claude-sonnet-5/medium` spends eighteen columns to say what `sonnet5·medium`
    says in thirteen, and the alias lives in configuration beside the model it
    shortens instead of in a vendor table in the renderer.
    """
    line = plain(grid.render(_event(agent=_stamped_agent(alias="sonnet5"))))
    assert "sonnet5·medium" in line
    assert "claude-sonnet-5" not in line
    assert len(line) == 180


def test_a_backend_with_no_alias_renders_a_mechanical_form_not_an_empty_cell(grid):
    """An unaliased model must not read as missing data.

    Without an alias the column shows the model itself — the mechanical
    fallback — rather than the empty cell it used to render.
    """
    long = _stamped_agent(model="deepseek-v4-flash", effort="xhigh", alias=None)
    line = plain(grid.render(_event(agent=long)))
    assert "deepseek-v4-flash" in line
    assert "…" in line
    assert len(line) == 180


def test_a_declared_effort_spelling_renders_in_place_of_the_derived_one(grid):
    """A declared spelling wins over the whole-word derivation.

    Both are stamped from configuration at dispatch; the declared one is the
    operator's explicit choice, so it is what displays.
    """
    agent = _stamped_agent(effort="high", effort_spelling="hi")
    line = plain(grid.render(_event(agent=agent)))
    assert "sonnet5·hi" in line


def test_an_undeclared_effort_renders_the_whole_word(grid):
    """No table, so an effort nobody has configured still renders.

    The whole word derives in full and lowercased, so medium and max — the
    pair a two-character prefix put one character apart — are read as
    themselves rather than decoded.
    """
    line = plain(grid.render(_event(agent=_stamped_agent(effort="xhigh"))))
    assert "sonnet5·xhigh" in line


def test_the_derivation_lowercases_the_effort_word(grid):
    line = plain(grid.render(_event(agent=_stamped_agent(effort="MEDIUM"))))
    assert "sonnet5·medium" in line


def test_max_renders_mx_from_a_declared_configuration_spelling(grid):
    """`max` would derive `max`; the operator's declared spelling is `mx`.

    The override is configuration data — never a table in code — and it is the
    mechanism that keeps a declared effort spelling rendering in place of the
    derived one. The schema key and its mechanism remain even though the host
    layer no longer declares a spelling by default.
    """
    agent = _stamped_agent(effort="max", effort_spelling="mx")
    line = plain(grid.render(_event(agent=agent)))
    assert "sonnet5·mx" in line
    assert "sonnet5·max" not in line


def test_a_declared_spelling_is_honoured_in_full(grid):
    """A declared spelling is not cut to some fixed width.

    The derived fallback no longer caps at two characters, so a declared
    spelling — however long — is what displays rather than a prefix of it.
    """
    agent = _stamped_agent(effort="high", effort_spelling="extra")
    line = plain(grid.render(_event(agent=agent)))
    assert "sonnet5·extra" in line
    assert len(line) == 180


def test_the_widest_real_label_fits_the_agent_column_without_elision(grid):
    """`dsv4-flash·medium` is seventeen characters against an eighteen-wide field.

    The alias plus the full effort word is the widest label the shipped
    configuration produces, and it must land whole in the column rather than
    take the elision mark that tells a reader part of the effort was cut.
    """
    agent = _stamped_agent(model="deepseek-v4-flash", alias="dsv4-flash", effort="medium")
    line = plain(grid.render(_event(agent=agent)))
    assert "dsv4-flash·medium" in line
    assert "…" not in line
    assert len(line) == 180


def test_a_pointer_written_before_this_change_still_renders_a_label(grid):
    """A precomposed `model/effort` string must keep working, not raise.

    This is the backward-compatibility negative: a pointer authored before the
    agent record became a mapping still carries a string, and it renders as
    today's pane did.
    """
    line = plain(grid.render(_event(agent="gpt-5.6-sol/medium")))
    assert "gpt-5.6-sol/medium" in line
    assert len(line) == 180


def test_an_effort_only_record_renders_its_suffix_without_a_stray_separator(grid):
    line = plain(grid.render(_event(agent={"effort": "high"})))
    assert "high" in line
    assert "·high" not in line
    assert len(line) == 180


def test_dispatch_stamps_the_label_and_a_later_config_edit_cannot_restate_it(grid):
    """The alias and spelling are read at dispatch, never from current config.

    A configuration edit after the run starts must not silently rewrite what
    ran: the rendered label comes from the stamped record, so it is unchanged
    even though a fresh dispatch would now say something else.
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
    line = plain(grid.render(_event(agent=stamped)))
    assert "sonnet5·me" in line

    # The operator later edits the configuration — alias dropped, spelling gone.
    edited = {"launch": "cli", "model": "claude-sonnet-5", "effort": "medium"}

    # A dispatch under the edited configuration would now say the model. The
    # full-word label `claude-sonnet-5·medium` overruns the eighteen-wide agent
    # column and is elided, so this asserts the fallback to the model that the
    # freeze semantics guarantee — not the exact suffix, which the pane trims.
    restated = _stamp_agent_display(
        {"model": "claude-sonnet-5", "effort": "medium"}, edited
    )
    after = plain(grid.render(_event(agent=restated)))
    assert "sonnet5" not in after
    assert "claude-sonnet-5·" in after

    # But the already-recorded pointer still renders what actually ran.
    again = plain(grid.render(_event(agent=stamped)))
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
    width either way and the counters sit flush on its final column.
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
    # The fleet counters are flush at the final column, so the row ends with
    # the last counter's suffix rather than trailing padding.
    assert line.endswith("12w ·  9b ·  7u")
