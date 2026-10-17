"""A pytest failure id that contains a space is read whole, not cut at it.

pytest prints a parametrised node id whole, for example
``FAILED t.py::f[opus 4.7] - AssertionError``. A reader that takes only the
token up to the first space records ``t.py::f[opus``, so two ids that differ
only after a space — two parametrisations of one test — collapse into one. An
added failure then reads as present at base, which hides it.

These tests drive ``added_failures_from_gate_logs`` with synthesised pytest
logs whose ids carry a space inside their brackets, and require the id between
the ``FAILED`` prefix and pytest's `` - `` reason separator, or the end of the
line, to be the id the reviewer compares. A plain id and a line with no reason
must read exactly as before.
"""

from __future__ import annotations

from reckon.crew import review as review_module

BASE_ONE_ID = "FAILED t.py::f[opus 4.7]\n1 failed\n"
HEAD_OTHER_ID = "FAILED t.py::f[opus 5.5]\n1 failed\n"


def _count(base_text: str, head_text: str) -> tuple[int | None, list[str]]:
    return review_module.added_failures_from_gate_logs(base_text, head_text)


def test_a_spaced_id_is_read_whole_and_counts_as_added() -> None:
    """Two ids differing only after a space are one added failure, not none."""
    count, added = _count(BASE_ONE_ID, HEAD_OTHER_ID)

    assert count == 1, (
        "a head id differing from the base id only after a space was not read; "
        "cut at the space, both ids collapse to the same prefix and the added "
        f"failure is hidden as a counted zero ({added!r})"
    )
    assert added == ["t.py::f[opus 5.5]"], (
        "the added id was not captured whole, spaces inside its brackets "
        f"included ({added!r})"
    )


def test_a_spaced_id_with_a_reason_stops_at_the_separator() -> None:
    """The id ends at pytest's reason separator, not at the end of the line."""
    base = "FAILED t.py::f[opus 4.7] - AssertionError: boom\n1 failed\n"
    head = "FAILED t.py::f[opus 5.5] - AssertionError: boom\n1 failed\n"

    count, added = _count(base, head)

    assert (count, added) == (1, ["t.py::f[opus 5.5]"]), (
        "the reason text after pytest's `` - `` separator leaked into the id, "
        f"so two ids could not be compared ({count!r}, {added!r})"
    )


def test_a_no_space_id_still_reads_as_before() -> None:
    """A plain id, with and without a reason, is unchanged by the new reading."""
    base = "FAILED tests/test_gate.py::test_alpha - AssertionError: boom\n1 failed\n"
    head = (
        "FAILED tests/test_gate.py::test_alpha - AssertionError: boom\n"
        "FAILED tests/test_gate.py::test_beta - AssertionError: boom\n"
        "1 failed\n"
    )

    count, added = _count(base, head)

    assert (count, added) == (1, ["tests/test_gate.py::test_beta"]), (
        f"a plain, space-free node id no longer reads as before ({count!r}, {added!r})"
    )


def test_a_line_with_no_reason_reads_the_id_whole() -> None:
    """A failure line carrying no reason reads its whole id to the line end."""
    assert review_module.gate_log_failure_ids(
        "FAILED t.py::f[opus 5.5]\n1 failed\n"
    ) == {"t.py::f[opus 5.5]"}, (
        "a no-reason failure line did not read its id whole to the end of the line"
    )
