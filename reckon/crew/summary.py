from __future__ import annotations

import re
from typing import Any

from reckon.crew.node import SUMMARY_AXES

# ── The summary reflex ──────────────────────────────────────────────────────


def validate_summary(text: str, *, occasion: str) -> dict[str, Any]:
    """Check a four-axis summary, and that a reporting one carries evidence.

    One discipline binds the reflex to the gating reflex and is why the format
    earns its place: at completion, WHY carries the gate evidence. That forces
    every wave report to be quantitative, and makes a wave that cannot state its
    measure visibly incomplete rather than plausibly done. A hold is held to the
    same standard, because "we are out of budget" without a figure and a reset
    time is not a report a lead can act on.
    """
    axes: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        match = re.match(r"^(WHAT|WHY|HOW|WHEN)\b\s*(.*)$", line)
        if match:
            current = match.group(1)
            axes.setdefault(current, [])
            if match.group(2).strip():
                axes[current].append(match.group(2).strip())
        elif current and line:
            axes[current].append(line)
    findings = [
        f"axis {axis} is missing" for axis in SUMMARY_AXES if not axes.get(axis)
    ]
    findings += [
        f"axis {axis} runs to {len(lines)} lines; at most two"
        for axis, lines in sorted(axes.items())
        if len(lines) > 2
    ]
    if occasion in ("completion", "hold"):
        why = " ".join(axes.get("WHY", []))
        if not re.search(r"\d", why):
            findings.append(
                f"{occasion} WHY carries no quantitative evidence; state the "
                "measure and its value"
            )
    return {
        "ok": not findings,
        "axes": {k: list(v) for k, v in sorted(axes.items())},
        "findings": findings,
    }
