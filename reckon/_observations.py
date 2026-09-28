"""Decoders for numeric observation payloads.

A measurement read from a journal, a lane document or a run record arrives as
``Any``: the field may hold a number, a stringified figure, a flag, or nothing
at all. The one rule shared by these decoders is that a value is a measurement
only when it is a real number, and ``bool`` is refused explicitly because
``True`` is an ``int`` subclass and would otherwise decode to ``1.0``, leaving a
flag and a measurement indistinguishable at the call site.

This module holds decoders whose policy is exactly that — no coercion, no
finiteness or range filtering. A caller needing a stricter contract (rejecting
a non-finite figure, or a negative one) narrows the result itself rather than
growing this primitive.
"""

from __future__ import annotations

from typing import Any


def optional_number(value: Any) -> float | None:
    """Return ``value`` as a float, or ``None`` when it is not a real number.

    ``bool`` is refused before the numeric check, so ``True`` and ``False``
    decode to ``None`` rather than to ``1.0`` and ``0.0``. A string is refused
    rather than parsed: a caller that wants to read a figure out of text says so
    at its own boundary.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
