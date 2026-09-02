"""Render one fleet transition as a line of a fixed grid.

The pane this feeds is read down a column rather than across a line — which
worker, what state, how many still running — and it shows roughly eight lines at
a time. Two consequences shape everything here. Every field occupies the same
screen column on every row, so a scan does not have to re-find it. And no line
ever wraps, because a wrapped row costs a quarter of the visible history; free
text is truncated to the room the grid leaves rather than allowed to overrun.

Colour carries two questions that must not share an axis. *Which worker is
this?* is answered by the node's own hue, handed out in order of first
appearance. *Does this need me?* is answered by the destination state. The two
palettes are disjoint, so a worker's colour can never read as an alarm.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

CLOCK = 8
NODE = 36
SESSION = 18
STATE = 10
AGENT = 18
GAP = 2

# Identity hues, chosen for one background each and deliberately excluding every
# hue the state palette uses. Ten is past any fleet size the dispatcher opens, so
# assignment recycles only in a session that has run a very long time.
PALETTE = {
    "light": [25, 30, 91, 61, 126, 24, 54, 23, 63, 133],
    "dark": [110, 146, 141, 109, 175, 116, 152, 79, 183, 117],
}

# Severity, not identity. Only a destination worth reacting to is painted; a
# routine dispatch stays in the body colour so the exceptions carry the contrast.
STATE_HUE = {
    "light": {
        "blocked": 124,
        "failed": 124,
        "stopped": 124,
        "abandoned": 124,
        "stalled": 166,
        "complete": 28,
        "promoted": 22,
    },
    "dark": {
        "blocked": 203,
        "failed": 203,
        "stopped": 203,
        "abandoned": 203,
        "stalled": 179,
        "complete": 78,
        "promoted": 71,
    },
}

# States a reader must act on. They are the only ones allowed to carry a reason,
# and only when they are being entered.
NEEDS_ACTION = frozenset({"blocked", "failed", "stalled", "stopped", "abandoned"})

# An internal classification longer than the column it must occupy. The display
# term matches the bucket the fleet counter already reports, so one word means
# one thing across the whole line.
DISPLAY = {"completed_unpromoted": "unpromoted"}

_CELLS = ("working", "blocked", "unpromoted")
# Two digits and a space per label, joined by " · ". Two digits cover any fleet
# the dispatcher opens; a wider count pushes its own label rather than silently
# misaligning the column beside it.
STATS = sum(2 + 1 + len(label) for label in _CELLS) + 3 * (len(_CELLS) - 1)

# The widest the fixed columns can be, plus the stats block and one gap. A width
# below this cannot be honoured without wrapping, so it is raised to this.
MIN_WIDTH = (
    (CLOCK + GAP + NODE + GAP + SESSION + GAP + (STATE * 2 + 3) + GAP + AGENT + GAP)
    + STATS
    + GAP
)
DEFAULT_WIDTH = 180
DEFAULT_THEME = "light"

# Below this there is no room for a clause worth reading, and a two-word stub is
# worse than the whitespace it replaces.
MIN_REASON = 12

_RESET = "\x1b[0m"
_DIM = "\x1b[2m"


def local_clock(observed: Any) -> str:
    """Render a stored UTC stamp as a wall clock in the reader's own zone.

    The record stays UTC because it is compared and sorted; the ticker is read
    by a person beside a harness that timestamps in local time, and two clocks
    two hours apart in one pane is a reading error waiting to happen.
    """
    text = str(observed or "")
    if len(text) < 19:
        return "--:--:--"
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[11:19]
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone().strftime("%H:%M:%S")


def single_clause(value: Any, *, limit: int = 96) -> str:
    """Collapse free text to one bounded clause that fits a ticker field.

    A worker writes prose; the grid has a column. Cutting at the first clause
    boundary keeps the sentence that states the problem and drops the elaboration
    after it, which is what survives a hard truncation anyway — and truncating
    alone would let a second clause occupy room the first one needed.
    """
    compact = " ".join(str(value or "").split())
    clause = re.split(
        r";|(?<=[.!?])\s+|\s+[\N{EM DASH}\N{EN DASH}]\s+", compact, maxsplit=1
    )[0].strip()
    if len(clause) <= limit:
        return clause
    boundary = clause.rfind(" ", 0, limit)
    if boundary < limit // 2:
        boundary = limit - 1
    return clause[:boundary].rstrip(" ,:") + "…"


def elide(text: str, width: int) -> str:
    """Fit text to a column, marking the cut so a reader knows it was one."""
    return text if len(text) <= width else text[: width - 1] + "…"


def _display_state(state: Any) -> str:
    return elide(DISPLAY.get(str(state or ""), str(state or "")), STATE)


class Ticker:
    """Renders transitions into one grid, remembering each worker's hue.

    Hues are per instance because identity only has to hold within the pane a
    reader is watching. Hashing the name instead would survive a restart, at the
    cost of collisions — and two live workers sharing a colour defeats the only
    question the colour answers.
    """

    def __init__(
        self,
        *,
        width: int = DEFAULT_WIDTH,
        theme: str = DEFAULT_THEME,
        color: bool = False,
    ) -> None:
        self.theme = theme if theme in PALETTE else DEFAULT_THEME
        self.width = max(int(width), MIN_WIDTH)
        # NO_COLOR is the caller's environment overriding the caller's flag, per
        # the convention; any non-empty value disables.
        self.color = bool(color) and not os.environ.get("NO_COLOR")
        self._hues: dict[str, int] = {}

    def hue(self, node: str) -> int:
        """The node's colour, claimed on first sighting and kept thereafter."""
        if node not in self._hues:
            palette = PALETTE[self.theme]
            self._hues[node] = palette[len(self._hues) % len(palette)]
        return self._hues[node]

    def render(self, event: Mapping[str, Any], *, with_session: bool = False) -> str:
        """One transition as one line, exactly ``width`` visible characters.

        ``with_session`` names the owning session, which an unscoped reader
        needs and a session-scoped one does not: every line a scoped follower
        receives is its own by construction, so the column would only take room
        from the node beside it.
        """
        node = str(event.get("node") or event.get("run_id") or "unknown")
        to_state = _display_state(event.get("to_state") or "unknown")
        from_state = _display_state(event.get("from_state"))

        cells: list[tuple[str, Any]] = [
            (f"{local_clock(event.get('observed_at')):<{CLOCK}}", "dim"),
            (" " * GAP, None),
            (f"{elide(node, NODE):<{NODE}}", self.hue(node)),
        ]
        if with_session:
            owner = str(event.get("session") or "")
            cells += [(" " * GAP, None), (f"{elide(owner, SESSION):<{SESSION}}", "dim")]
        # Both sides of the arrow are painted by the same map. A transition is
        # read as a pair — where it came from and where it went — and colouring
        # only the destination makes `blocked → promoted` and `complete →
        # promoted` look identical, which is the difference between a recovery
        # and a routine gate pass.
        hues = STATE_HUE[self.theme]
        cells += [
            (" " * GAP, None),
            (f"{from_state:>{STATE}}", hues.get(from_state, "dim")),
            (" ", None),
            ("→", "dim"),
            (" ", None),
            (f"{to_state:<{STATE}}", hues.get(to_state, "dim")),
            (" " * GAP, None),
            (f"{elide(str(event.get('agent') or ''), AGENT):<{AGENT}}", "dim"),
            (" " * GAP, None),
        ]

        head = sum(len(text) for text, _ in cells)
        room = self.width - head - STATS - GAP
        reason = self._reason(event, to_state, room)
        cells.append((f"{reason:<{room}}", "dim") if room > 0 else ("", None))
        cells.append((" " * GAP, None))
        cells.extend(self._stats(event))
        return "".join(self._paint(text, style) for text, style in cells)

    def _reason(self, event: Mapping[str, Any], to_state: str, room: int) -> str:
        """The clause explaining an actionable state, bounded by the margin.

        Only the state being entered may explain itself. Keying on the state
        being left is how a promotion ends up still reporting the block it
        recovered from — describing a problem that is already over.
        """
        if to_state not in NEEDS_ACTION or room < MIN_REASON:
            return ""
        return single_clause(event.get("reason"), limit=room)

    def _stats(self, event: Mapping[str, Any]) -> list[tuple[str, Any]]:
        """The fleet after this transition, as a grid whose digits line up.

        A zero is dimmed rather than dropped: blanking it would leave trailing
        whitespace and take the right edge ragged, and a reader waiting for a
        drain needs to see the count reach zero, not see it disappear.
        """
        cells: list[tuple[str, Any]] = []
        for index, label in enumerate(_CELLS):
            if index:
                cells += [(" ", None), ("·", "dim"), (" ", None)]
            count = int(event.get(label) or 0)
            cells.append((f"{count:>2} {label}", None if count else "dim"))
        return cells

    def _paint(self, text: str, style: Any) -> str:
        if not self.color or style is None or not text:
            return text
        prefix = _DIM if style == "dim" else f"\x1b[38;5;{int(style)}m"
        return f"{prefix}{text}{_RESET}"
