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
    assert max(map(len, CLASSIFIER_STATES)) == ticker_module.STATE_WORD
    columns = _columns(ticker_module.MODEL)
    observed = {name: set() for name in ("arrow", "elapsed", "counters", "reason")}
    # The column between the time and the model is the whole attention question:
    # the mark was dropped, so the model cell must start at the first column
    # after the time's two-space gutter, leaving no reserved blank column.
    assert columns["model"] == ticker_module.CLOCK + ticker_module.GAP

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
            ticker_module.Ticker(width=208, color=False, model_aliases=()).render(event)
        )
        assert len(row) == 208

        observed["arrow"].add(columns["arrow"])
        observed["elapsed"].add(columns["elapsed"])
        observed["counters"].add(columns["counters"])
        observed["reason"].add(columns["reason"])
        # No row carries an attention mark, whatever the destination needs: the
        # mark was dropped, so no `!` appears anywhere on the row and the model
        # cell follows the time's gutter directly.
        assert "!" not in row, row
        assert row[columns["arrow"]] == (ticker_module.ARROW if previous else " ")
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
