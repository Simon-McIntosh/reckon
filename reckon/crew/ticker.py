"""Render one fleet transition as a line of a fixed grid.

The pane this feeds is read down a column rather than across a line — which
worker, what state, how many still running — and it shows roughly eight lines at
a time. Two consequences shape everything here. Every field occupies the same
screen column on every row, so a scan does not have to re-find it. And no line
ever wraps, because a wrapped row costs a quarter of the visible history; free
text is truncated to the room the grid leaves rather than allowed to overrun.

Colour carries two questions that must not share an axis. *Which worker is
this?* is answered by the node's own hue, handed out in order of first
appearance. *Does this need me?* is answered by the state, painted on both
sides of the arrow so a recovery out of a block reads differently from a routine
landing. Identity is kept perceptually clear of the four verdict hues, so a
worker's colour is never mistaken for a verdict about that worker.
"""

from __future__ import annotations

import fcntl
import os
import re
import struct
import termios
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

CLOCK = 8
ROLE = 4
NODE = 36
SESSION = 18
STATE = 10
# Model identity and effort each earn their own column: a reader scans effort
# down a column instead of parsing it out of a fused label. MODEL is wide enough
# to hold the widest real alias and every legacy composed model/effort string a
# line written before the facts switch carries; EFFORT fits the full effort word.
MODEL = 18
EFFORT = 8
GAP = 2

# Identity hues, one set per background. Picked by measurement rather than eye:
# each clears a 3.8:1 contrast ratio against its pane, sits in the cool arc
# (hue 190-330 degrees), and is at least 30 CIE76 units from every verdict hue
# below. Identity may sit near a NEUTRAL state hue — a worker coloured like
# `working` is harmless, because the two occupy different columns and neither is
# a claim about the other — but a worker that reads as blocked, stalled or
# finished is a false verdict, which is what the distance floor prevents.
PALETTE = {
    "light": [20, 61, 56, 97, 91, 127, 126, 125],
    "dark": [67, 170, 182, 104, 176, 169, 74],
}

# The verdict hues: the four a reader acts on the sight of. Identity is kept
# perceptually clear of exactly these, and a test asserts they never overlap.
VERDICTS = ("blocked", "stalled", "complete", "promoted")

# What the state means, on both sides of the arrow. Red for a run that has
# stopped and needs answering, amber for one that has gone quiet, green for
# delivered work, blue for a run making progress, teal for delivered work still
# waiting on its gate, and grey for one that has only just started.
#
# The light amber was 166 and measured 3.3:1 against the cream pane — the worst
# contrast in the set, on the colour whose whole job is to be noticed. It is 130
# at 4.1:1. `complete` and `promoted` are deliberately close, both being greens:
# they mean the same good thing one gate apart.
STATE_HUE = {
    "light": {
        "blocked": 124,
        "failed": 124,
        "stopped": 124,
        "abandoned": 124,
        "stalled": 130,
        "complete": 28,
        "promoted": 22,
        "dispatched": 241,
        "working": 26,
        "running": 26,
        "unknown": 124,
        "unpromoted": 30,
    },
    "dark": {
        "blocked": 203,
        "failed": 203,
        "stopped": 203,
        "abandoned": 203,
        "stalled": 179,
        "complete": 78,
        "promoted": 71,
        "dispatched": 245,
        "working": 75,
        "running": 75,
        "unknown": 203,
        "unpromoted": 80,
    },
}

# States a reader must act on: the ones that have stopped progressing and want
# the coordinator. One set serves three purposes that have to agree — it is the
# `blocked` bucket the fleet counter reports, the set of states allowed to carry
# a reason, and the set allowed to keep one. Written out three times they drifted:
# `unknown` counted as blocked and was permitted an explanation, but rendered
# without it, so the number said something needed attention and the line did not
# say what.
NEEDS_ACTION = frozenset(
    {"blocked", "failed", "stalled", "stopped", "abandoned", "unknown"}
)

# An internal classification longer than the column it must occupy. The display
# term matches the bucket the fleet counter already reports, so one word means
# one thing across the whole line.
DISPLAY = {"completed_unpromoted": "unpromoted"}

# The dispatch vocabulary, verbatim. Kept here rather than derived from a
# config so that a role is known the moment it is dispatched; the display form
# is derived from it mechanically (first four characters) so no table or
# glossary has to stay in step with a new role.
DISPATCH_ROLES = frozenset(
    {"implement", "cleanup", "review", "investigate", "test", "documentation"}
)

# What an undispatched or unconfigured role renders as. A marker rather than a
# truncated word, because a cut-off word invites a reader to guess the rest
# and a wrong guess about *what kind of work this is* is worse than an
# admitted unknown.
ROLE_UNKNOWN = "?"

_CELLS = ("working", "blocked", "unpromoted")

# The single-letter suffix each fleet counter renders as. The pane teaches the
# mapping without a legend in the stream: the same words stand in the state
# column on other rows, so a reader already knows b means blocked before they
# reach the count column.
STAT_LETTER = {"working": "w", "blocked": "b", "unpromoted": "u"}

# Two digits and a single-letter suffix per counter, joined by " · ". Two digits
# cover any fleet the dispatcher opens; a wider count pushes its own label
# rather than silently misaligning the column beside it.
STATS = sum(2 + 1 for _ in _CELLS) + 3 * (len(_CELLS) - 1)

# The widest the fixed columns can be, plus the stats block and one gap. A width
# below this cannot be honoured without wrapping, so it is raised to this.
MIN_WIDTH = (
    (
        CLOCK
        + GAP
        + ROLE
        + GAP
        + NODE
        + GAP
        + SESSION
        + GAP
        + (STATE * 2 + 3)
        + GAP
        + MODEL
        + GAP
        + EFFORT
        + GAP
    )
    + STATS
    + GAP
)
DEFAULT_WIDTH = 180
DEFAULT_THEME = "light"

# One column of inset between the hosting terminal's width and the text grid, so
# the counters never press on the very edge the pane still owns. PROVISIONAL and
# awaiting calibration against a real pane — over-filling loses the counters off
# the right edge, while under-filling only leaves a harmless gap.
INSET = 1


def _ancestor_terminal_paths():
    """Yield tty device paths up the process tree, nearest ancestor first.

    The follower's own stdout is a pipe, so its window is undetectable where it
    writes; the pane it fills is owned by a harness process higher up. Walk the
    ancestry (each /proc/<pid>/stat names its parent), collecting any stdio
    descriptor that points at a real terminal so the nearest owner is read first.
    """
    pid = os.getpid()
    seen: set[int] = set()
    while pid and pid not in seen:
        seen.add(pid)
        for fd in (0, 1, 2):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if target.startswith("/dev/"):
                yield target
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
                rest = handle.read().split(")")[-1].split()
            nxt = int(rest[1])  # field four, read as the parent pid
        except (OSError, IndexError, ValueError):
            break
        if nxt == pid:
            break
        pid = nxt


def _columns_of(path: str) -> int | None:
    """The current column count of the terminal at ``path``, or None.

    ``path`` may be any open descriptor target, so a non-tty char device simply
    fails the ioctl and reports no width rather than raising.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
    except OSError:
        return None
    finally:
        os.close(fd)
    return struct.unpack("HHHH", packed)[1]


def resolve_terminal_width() -> int:
    """The pane's current width for this renderer, or the stated fallback.

    The width a line must fit is not on the stream it is written to; it lives on
    the terminal an ancestor owns and tracks a resize. Walk the ancestry to the
    first readable terminal, subtract the inset, and floor the result at the
    grid's minimum so a narrower pane still never wraps. A detached follower has
    no such ancestor — collector or nohup'd — and falls back to the stated
    default. ``--width`` overrides this at the call site.
    """
    for path in _ancestor_terminal_paths():
        columns = _columns_of(path)
        if columns:
            return max(int(columns) - INSET, MIN_WIDTH)
    return DEFAULT_WIDTH


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

    A clause that collapses to bare punctuation is refused outright: a parse
    failure upstream (a block-scalar indicator returned as if it were the value)
    must not render as a "reason" no reader can act on. The refusal lives here,
    where the clause is derived, so the producer never has to make it.
    """
    compact = " ".join(str(value or "").split())
    clause = re.split(
        r";|(?<=[.!?])\s+|\s+[\N{EM DASH}\N{EN DASH}]\s+", compact, maxsplit=1
    )[0].strip()
    if clause and not re.search(r"[A-Za-z0-9]", clause):
        return ""
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


def _display_role(role: Any) -> str:
    """The role's first four characters lowercased, or the marker.

    Every role in the dispatch vocabulary derives its four-character form
    mechanically — no display table to stay in step with, so a role word
    configured for the first time still renders. The first four of the living
    set are all distinct: impl, inve, revi, test, clea, docu. A role not in the
    vocabulary is never truncated to fit — that would show a plausible-looking
    but wrong word — so anything unrecognised renders the marker instead.
    """
    spelled = str(role or "")
    if spelled not in DISPATCH_ROLES:
        return ROLE_UNKNOWN
    return spelled[:4].lower()


def _derive_effort(effort: Any) -> str:
    """The effort word in full, lowercased — no table, so a fresh word works.

    Full spelling is the legibility target the alias work existed to serve: a
    two-character prefix made medium and max land one character apart in a dim
    narrow column, and that is the one pair on the ladder a reader must not have
    to decode. An effort word invented next month still renders with no code
    change, because there is still no enumeration to stay in step with.
    """
    word = str(effort or "").strip()
    return word.lower()


def _model_label(event: Mapping[str, Any]) -> str:
    """Model column: the alias when one is declared, else the model id, else a
    legacy composed agent string.

    The facts are preferred and the composed string is only a fallback for a
    line written before the switch, when model and effort were fused at write
    time and no facts were persisted to re-derive from. A legacy value is never
    re-parsed, so it renders whole rather than holding a fragment.
    """
    alias = str(event.get("alias") or "").strip()
    if alias:
        return alias
    model = str(event.get("model") or "").strip()
    if model:
        return model
    return str(event.get("agent") or "").strip()


def _effort_label(event: Mapping[str, Any]) -> str:
    """Effort column: the full effort word lowercased, or empty.

    A new-shape line persists effort as a fact and renders it whole here. A
    legacy line carries no separate effort and shows nothing beside its composed
    agent string, which already holds the effort it could not split.
    """
    effort = str(event.get("effort") or "").strip()
    return _derive_effort(effort) if effort else ""


def _display_marker(event: Mapping[str, Any]) -> str:
    """The needs-action glyph a blocked state may carry, derived at render time.

    New-shape lines persist ``needs_help_complete`` — the fact — and the glyph is
    derived from it. A legacy line persisted the glyph itself and has no fact
    underneath, so it renders its persisted value. Never written back to the log.
    """
    if "needs_help_complete" in event:
        return "?" if event.get("needs_help_complete") else "!"
    return str(event.get("marker") or "")


def _agent_label(agent: Any) -> str:
    """The agent column label: alias plus the full effort word, or the record as it stands.

    A pointer written before this change carries a precomposed ``model/effort``
    string and must still render; a stamped pointer carries a mapping whose
    alias and effort spelling were decided at dispatch, so a later
    configuration edit cannot restate what ran. An unaliased model renders
    itself rather than an empty cell, and the effort is spelled in full — the
    one pair the prefix abbreviated, medium and max, is the pair a reader must
    not have to decode, so no character is saved there. A declared spelling is
    configuration data, never a table in code, and still wins when present.
    """
    if not isinstance(agent, Mapping):
        return str(agent or "")
    model = str(agent.get("model") or "").strip()
    base = str(agent.get("alias") or "").strip() or model
    effort = str(agent.get("effort") or "").strip()
    suffix = str(agent.get("effort_spelling") or "").strip() or _derive_effort(effort)
    if not suffix:
        return base
    return base + "·" + suffix if base else suffix


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
        role = _display_role(event.get("role"))

        cells: list[tuple[str, Any]] = [
            (f"{local_clock(event.get('observed_at')):<{CLOCK}}", "dim"),
            (" " * GAP, None),
            (f"{role:<{ROLE}}", "dim"),
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
            (f"{elide(_model_label(event), MODEL):<{MODEL}}", "dim"),
            (" " * GAP, None),
            (f"{elide(_effort_label(event), EFFORT):<{EFFORT}}", "dim"),
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
        recovered from — describing a problem that is already over. A blocked
        entry carries a glyph saying whether a resume can answer it, derived from
        the persisted fact at render time rather than written into the record.
        """
        if to_state not in NEEDS_ACTION or room < MIN_REASON:
            return ""
        detail = event.get("detail")
        if detail is None:
            detail = event.get("reason")
        marker = _display_marker(event) if to_state == "blocked" else ""
        reserve = len(marker) + (1 if marker else 0)
        clause = single_clause(detail, limit=max(0, room - reserve))
        if marker and clause:
            return f"{marker} {clause}"
        return marker or clause

    def _stats(self, event: Mapping[str, Any]) -> list[tuple[str, Any]]:
        """The fleet after this transition, as a grid whose digits line up.

        Each counter is its number followed by the state's single letter, so a
        zero is dimmed rather than dropped: blanking it would leave trailing
        whitespace and take the right edge ragged, and a reader waiting for a
        drain needs to see the count reach zero, not see it disappear.
        """
        cells: list[tuple[str, Any]] = []
        for index, label in enumerate(_CELLS):
            if index:
                cells += [(" ", None), ("·", "dim"), (" ", None)]
            count = int(event.get(label) or 0)
            cells.append((f"{count:>2}{STAT_LETTER[label]}", None if count else "dim"))
        return cells

    def _paint(self, text: str, style: Any) -> str:
        if not self.color or style is None or not text:
            return text
        prefix = _DIM if style == "dim" else f"\x1b[38;5;{int(style)}m"
        return f"{prefix}{text}{_RESET}"
