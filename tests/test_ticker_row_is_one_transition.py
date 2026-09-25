"""A follower row is one aligned transition with one attention signal."""

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
    attention = ticker_module.CLOCK + ticker_module.GAP
    model = attention + ticker_module.ATTENTION + ticker_module.GAP
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
        "attention": attention,
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


def _attention_expected(state: str) -> bool:
    displayed = ticker_module.DISPLAY.get(state, state)
    return (
        state in ticker_module.ATTENTION_STATES
        or displayed in ticker_module.ATTENTION_STATES
    )


def test_every_classifier_transition_keeps_one_grid() -> None:
    """Every input extreme preserves each column and every two-space gutter."""
    assert set(CLASSIFIER_STATES) == ticker_module.CLASSIFIER_STATE_WORDS
    assert max(map(len, CLASSIFIER_STATES)) == ticker_module.STATE_WORD
    columns = _columns(ticker_module.MODEL)
    observed = {
        name: set() for name in ("attention", "arrow", "elapsed", "counters", "reason")
    }

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

        observed["attention"].add(columns["attention"])
        observed["arrow"].add(columns["arrow"])
        observed["elapsed"].add(columns["elapsed"])
        observed["counters"].add(columns["counters"])
        observed["reason"].add(columns["reason"])

        expected_attention = "!" if _attention_expected(new) else " "
        assert row[columns["attention"]] == expected_attention
        assert row[columns["arrow"]] == (ticker_module.ARROW if previous else " ")
        assert row[columns["node"] : columns["node"] + ticker_module.NODE].strip()

        gutters = (
            (ticker_module.CLOCK, columns["attention"]),
            (columns["attention"] + ticker_module.ATTENTION, columns["model"]),
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


def test_reported_examples_keep_attention_and_name_gutters() -> None:
    examples = (
        (
            "review-of-hook-stop-mode-silent-without-follower",
            "abandoned",
            "recover",
            "!",
        ),
        (
            "review-of-obligations-are-a-derived-view",
            "working",
            "observe",
            " ",
        ),
    )
    columns = _columns(ticker_module.MODEL)

    for index, (node, state, action, attention) in enumerate(examples):
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
        assert row[columns["attention"]] == attention
        assert (
            row[columns["node"] + ticker_module.NODE : columns["transition"]]
            == " " * ticker_module.GAP
        )
        assert action not in row[columns["transition"] : columns["elapsed"]]
