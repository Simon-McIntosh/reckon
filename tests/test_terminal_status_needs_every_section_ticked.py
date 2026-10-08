"""A plan reaches shipped or done only when every section is ticked.

The terminal-status guard reads the open-section predicate and refuses a write
that would move a plan into ``shipped`` or ``done`` while any section is still
declared ``implementable`` or ``deferred``. The refusal names the sections still
open, in declaration order. A plan already at a terminal status is not
re-judged: the guard keys on the transition, so history that landed before the
guard existed stays editable.
"""

from __future__ import annotations

import pytest

from reckon import _store
from reckon._plan_html import (
    SECTION_DECLARATION_DEFERRED,
    SECTION_DECLARATION_DONE,
    SECTION_DECLARATION_IMPLEMENTABLE,
    open_sections,
)

PROJECT = "tick-project"
SLUG = "tick-guard"

# The landing write under test: set the plan's status to done.
LAND = {"op": "set", "path": "status", "value": "done"}


def _open_state(declaration: str) -> dict:
    """A plan holding one done section and one still open, plus a continuation.

    The open followup keeps the continuation rule satisfied, so the write under
    test is refused by the section guard rather than by the continuation rule.
    """
    return {
        "project": PROJECT,
        "type": "plan",
        "slug": SLUG,
        "title": "Tick guard fixture",
        "status": "active",
        "version": 1,
        "section_declarations": {
            "s1": SECTION_DECLARATION_DONE,
            "s2": declaration,
        },
        "followups": [
            {
                "id": "f-next",
                "status": "open",
                "prompt": f"/reckon-build {SLUG} §2",
            }
        ],
    }


def test_landing_with_an_implementable_section_is_refused() -> None:
    state = _open_state(SECTION_DECLARATION_IMPLEMENTABLE)
    assert open_sections(state) == ["s2"], "fixture holds s2 open"

    with pytest.raises(_store.OpError) as refusal:
        _store.apply_ops(state, [LAND], False)

    assert "s2" in str(refusal.value), "the refusal names the open section"


def test_landing_with_a_deferred_section_is_refused() -> None:
    state = _open_state(SECTION_DECLARATION_DEFERRED)
    assert open_sections(state) == ["s2"], "a deferred section is still open"

    with pytest.raises(_store.OpError) as refusal:
        _store.apply_ops(state, [LAND], False)

    assert "s2" in str(refusal.value), "the refusal names the deferred section"


def test_landing_succeeds_once_every_section_is_ticked() -> None:
    state = _open_state(SECTION_DECLARATION_IMPLEMENTABLE)

    _store.apply_ops(
        state,
        [
            {
                "op": "set",
                "path": "section_declarations.s2",
                "value": SECTION_DECLARATION_DONE,
            },
            LAND,
        ],
        False,
    )

    assert state["status"] == "done"
    assert open_sections(state) == []


def test_the_patch_writer_refuses_a_landing_with_an_open_section() -> None:
    state = _open_state(SECTION_DECLARATION_DEFERRED)

    with pytest.raises(_store.OpError) as refusal:
        _store.validate_landing_patch(state, {"status": "done"})

    assert "s2" in str(refusal.value)


def test_an_already_terminal_plan_is_not_rejudged() -> None:
    # A plan that landed while a section was open — grandfathered history — must
    # still accept an unrelated write.
    state = _open_state(SECTION_DECLARATION_IMPLEMENTABLE)
    state["status"] = SECTION_DECLARATION_DONE

    _store.apply_ops(state, [LAND], False)

    assert state["status"] == "done"
