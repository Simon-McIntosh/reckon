"""Read bounded duration overrides shared by command and server paths."""

import math
import os


def read_duration_override(
    name: str, default: float, minimum: float, inclusive: bool
) -> float:
    """Return a finite environment duration within the caller's bound."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value):
        return default
    if value < minimum or (value == minimum and not inclusive):
        return default
    return value
