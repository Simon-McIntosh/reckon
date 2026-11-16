"""An unexpected launch failure carries the frame that raised it."""

from __future__ import annotations

from reckon.crew.admission import attempts
from reckon.crew.admission.shared import CrewError


def _raise(exc_type: type[BaseException], message: str) -> None:
    raise exc_type(message)


def _raised(exc_type: type[BaseException], message: str) -> BaseException:
    try:
        _raise(exc_type, message)
    except exc_type as exc:
        return exc
    raise AssertionError("the helper raised nothing")


def test_an_unexpected_failure_names_its_innermost_frame():
    text = attempts._failure_with_origin(_raised(RuntimeError, "keys changed"))

    assert text.startswith("RuntimeError: keys changed (raised in _raise at ")
    assert "test_a_launch_failure_names_where_it_was_raised.py:" in text


def test_a_crew_refusal_keeps_its_own_account():
    text = attempts._failure_with_origin(_raised(CrewError, "the lane is paused"))

    assert text == "CrewError: the lane is paused"


def test_a_failure_with_no_traceback_keeps_its_message():
    assert attempts._failure_with_origin(ValueError("no frames")) == (
        "ValueError: no frames"
    )
