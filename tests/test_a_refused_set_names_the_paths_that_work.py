"""An unsupported plan set path is refused with the paths that do work.

A caller that misspells a settable path, or reaches for a keyed form the writer
does not handle, is told what it can write instead: the settable top-level
paths and the keyed forms, plus the nearest settable path when one is close. The
base refusal names none of these, so every case below that reads a path out of
the message fails there. A supported path still sets unchanged.

The declared negative control restores the bare refusal, so the listed-paths
case fails again.
"""

from __future__ import annotations

import re

import pytest

from reckon import _store
from reckon._store import OpError, apply_ops


def _plan_state() -> dict:
    return {
        "type": "plan",
        "decisions": {},
        "followups": [],
        "sections": [],
        "gates": [],
    }


def _refusal_message(path: str) -> str:
    with pytest.raises(OpError) as excinfo:
        apply_ops(
            _plan_state(),
            [{"op": "set", "path": path, "value": 1}],
            is_index=False,
        )
    return str(excinfo.value)


def test_the_refusal_lists_the_settable_paths_and_the_keyed_forms():
    message = _refusal_message("no_such_field")

    assert "unsupported plan set path 'no_such_field'" in message
    for form in (
        "section_depends_on.SECTION",
        "sections.SECTION.FIELD",
        "gates.ID.FIELD",
    ):
        assert form in message
    assert "effort_hours" in message
    assert "status" in message


def test_the_refusal_reads_the_stores_own_settable_table():
    message = _refusal_message("no_such_field")

    for path in _store._PLAN_SET_TOP:
        assert path in message
    for head, form in _store._PLAN_SET_KEYED_FORMS.items():
        assert head in message
        assert form in message


def test_a_near_miss_names_the_settable_path():
    message = _refusal_message("effort-hours")

    assert "nearest settable path 'effort_hours'" in message


def test_a_typo_names_the_settable_path_it_is_closest_to():
    message = _refusal_message("capabilty")

    assert "nearest settable path 'capability'" in message


def test_a_keyed_head_is_not_offered_as_a_bare_settable_path():
    """A keyed head is settable through its dotted form, never bare.

    Suggesting ``gates`` or ``sections`` as a nearest settable path sends a
    caller that follows the suggestion straight back into this refusal, which
    is the dead end the message exists to remove. An input that reaches a keyed
    head is answered by the forms line alone.
    """
    for path in ("gate", "section", "gates", "sections"):
        message = _refusal_message(path)
        assert "nearest settable path" not in message
        assert "gates.ID.FIELD" in message


def test_every_suggested_path_is_one_the_writer_accepts_bare():
    """A suggestion must not send the caller into a second refusal.

    The suggested name is held against the store's own table of settable
    top-level fields, so a keyed head — settable only through its dotted form —
    can never be offered as a whole path.
    """
    for typo in ("effort-hours", "capabilty", "effort_hurs", "northstars"):
        message = _refusal_message(typo)
        match = re.search(r"nearest settable path '([^']+)'", message)
        assert match is not None, typo
        assert match.group(1) in _store._PLAN_SET_TOP, typo

    # An input near a keyed head gets no suggestion rather than a name the
    # writer would refuse again.
    for typo in ("sectons", "gats"):
        assert "nearest settable path" not in _refusal_message(typo), typo


def test_a_supported_path_still_sets():
    working = _plan_state()

    apply_ops(
        working,
        [{"op": "set", "path": "effort_hours", "value": 3.5}],
        is_index=False,
    )

    assert working["effort_hours"] == 3.5
    assert working["effort_calibrated"] is True
