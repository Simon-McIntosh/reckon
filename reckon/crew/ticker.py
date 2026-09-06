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
NODE = 36
# One glyph, not a name. The only decision-relevant thing about the owning
# session is whether the row is the reader's to act on, and a run id spelled in
# full — a shadow's least of all, since it is synthesised from its primary's —
# spends eighteen columns saying it. An unscoped reader gets the glyph; a scoped
# reader gets nothing, because every row it receives is its own by construction.
OWNER = 1
STATE = 10
# A model at an effort is one routing fact, not two, so identity and effort
# share one cell rather than reading down separate columns: a gap wide enough
# for the widest pair left a hole in every ordinary row, which reads as a
# missing field rather than a column boundary. AGENT is wide enough for the
# widest real alias, one separator, and the widest configured effort word in
# full, and for every legacy composed model/effort string a line written
# before the facts switch carries.
AGENT = 18
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
        "waiting": 97,
        "wait-aged": 130,
        "unknown": 124,
        "unreadable": 124,
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
        "waiting": 104,
        "wait-aged": 179,
        "unknown": 203,
        "unreadable": 203,
        "unpromoted": 80,
    },
}

# States a reader must act on: the ones that have stopped progressing and want
# the coordinator. An overdue wait is in the set: its external condition has
# not lifted when expected, so the row carries the marker that tells a reader
# to look at it. That marker is deliberately not the fleet's `blocked` number —
# an overdue wait is marked but still counted as waiting — so the blocked
# bucket in recovery derives from this set minus the waiting family rather than
# from this set verbatim. One set serves the marker and the explanation
# together: `unknown` once counted as blocked while the line rendered without
# it, so the number said something needed attention and the line did not say
# what.
NEEDS_ACTION = frozenset(
    {
        "blocked",
        "failed",
        "stalled",
        "stopped",
        "abandoned",
        "unknown",
        "unreadable",
        "wait-aged",
    }
)

# An internal classification longer than the column it must occupy. The display
# term matches the bucket the fleet counter already reports, so one word means
# one thing across the whole line.
DISPLAY = {"completed_unpromoted": "unpromoted"}

# The dispatch vocabulary, verbatim. Kept here rather than derived from a
# config so that a role is known the moment it is dispatched; the word IS the
# display form, so no table or glossary has to stay in step with a new role.
DISPATCH_ROLES = frozenset(
    {"implement", "cleanup", "review", "investigate", "test", "documentation"}
)

# The role column is sized by the vocabulary above, not guessed: the word is
# the display form, so a longer role widens its own column rather than being
# cut to a prefix. The longest member today is documentation at thirteen
# characters, which sets the column width.
ROLE = max(len(word) for word in DISPATCH_ROLES)

# What an undispatched or unconfigured role renders as. A marker rather than a
# truncated word, because a cut-off word invites a reader to guess the rest
# and a wrong guess about *what kind of work this is* is worse than an
# admitted unknown.
ROLE_UNKNOWN = "?"

# What marks a row another session dispatched, on an unscoped stream. Another
# session's runs are not this reader's to act on, so the row is marked rather
# than named.
FOREIGN_OWNER = "~"

# The arrow column, which says what kind of record this is. A transition is
# something that happened; a baseline is inventory the follower emitted because
# it attached, and a restart emits one per live run inside a second or two. Read
# from the kind the log records, never from an absent from-state: a genuine
# transition into a first sighting also has no source, and conflating the two
# makes a restart read as a burst of news.
TRANSITION_ARROW = "\N{RIGHTWARDS ARROW}"
# A bullet rather than a middle dot: the counters already separate themselves
# with middle dots, and a glyph that appears three more times on the same row
# cannot be read — or searched for — as a statement about the record.
BASELINE_ARROW = "\N{BULLET}"

# States a run does not leave. A baseline row for one of these is inventory
# about work that is already over — the alarming-looking rows a reattaching
# follower emits first — so it is suppressed rather than shown as news. A block
# or a stall is not here: it has stopped without finishing, and it is exactly
# what a reader attaching wants told.
SETTLED_STATES = frozenset(
    {"complete", "completed_unpromoted", "promoted", "failed", "stopped", "abandoned"}
)

_CELLS = ("working", "blocked", "unpromoted")
_WAIT_CELL = "waiting"

# The single-letter suffix each fleet counter renders as. The pane teaches the
# mapping without a legend in the stream: the same words stand in the state
# column on other rows, so a reader already knows b means blocked before they
# reach the count column.
STAT_LETTER = {
    "working": "w",
    "blocked": "b",
    "unpromoted": "u",
    "waiting": "q",
}

# Two digits and a single-letter suffix per counter, joined by " · ". Two digits
# cover any fleet the dispatcher opens; a wider count pushes its own label
# rather than silently misaligning the column beside it.
_MAX_CELLS = (*_CELLS, _WAIT_CELL)
STATS = sum(2 + 1 for _ in _MAX_CELLS) + 3 * (len(_MAX_CELLS) - 1)

# The widest the fixed columns can be, plus the stats block and one gap. A width
# below this cannot be honoured without wrapping, so it is raised to this.
# Everything before the reason consumes exactly this many columns with the role
# word at its widest (documentation, thirteen): against the 180-column
# DEFAULT_WIDTH budget that leaves 46 for the reason, and 73 on the 208-column
# pane this workstation measures (208 minus the inset) — either leaves room for
# the whole role vocabulary, so the 46 at the default is what a later added
# column spends first.
MIN_WIDTH = (
    CLOCK
    + GAP
    + ROLE
    + GAP
    + NODE
    + GAP
    + OWNER
    + GAP
    + (STATE * 2 + 3)
    + GAP
    + AGENT
    + GAP
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
    """The role word spelled in full, or the marker.

    The whole dispatch word is the display form — no table or derivation to
    stay in step with, so a role word configured for the first time renders
    exactly as it was dispatched. The pane has the room and the column is sized
    by the vocabulary, so nothing is cut to a prefix. A role not in the
    vocabulary is never truncated to fit — that would show a plausible-looking
    but wrong word — so anything unrecognised renders the marker instead.
    """
    spelled = str(role or "")
    if spelled not in DISPATCH_ROLES:
        return ROLE_UNKNOWN
    return spelled


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


def _agent_column(event: Mapping[str, Any]) -> str:
    """The routing identity and its effort as one cell, joined by one separator.

    A model at an effort is one fact a reader compares row to row, not a pair to
    parse apart, so the two share a column rather than reading down separate
    ones. A record carrying no effort renders the identity alone — appending a
    bare separator would read as a missing field rather than the absence it
    actually is.
    """
    base = _model_label(event)
    effort = _effort_label(event)
    if not effort:
        return base
    return f"{base}\N{MIDDLE DOT}{effort}" if base else effort


def _display_marker(event: Mapping[str, Any]) -> str:
    """The needs-action glyph a blocked state may carry, derived at render time.

    New-shape lines persist ``needs_help_complete`` — the fact — and the glyph is
    derived from it. A legacy line persisted the glyph itself and has no fact
    underneath, so it renders its persisted value. Never written back to the log.
    """
    if "needs_help_complete" in event:
        return "?" if event.get("needs_help_complete") else "!"
    return str(event.get("marker") or "")


def is_shadow(event: Mapping[str, Any]) -> bool:
    """Whether this row is a shadow run: evidence that will never merge.

    Read from the lineage the record carries rather than from the run id, which
    only encodes the relationship by convention. Either spelling counts, so a
    producer that flattens the lineage to a flag still reads the same.
    """
    lineage = event.get("lineage")
    if isinstance(lineage, Mapping) and str(lineage.get("kind") or "") == "shadow":
        return True
    return bool(event.get("shadow"))


def is_baseline(event: Mapping[str, Any]) -> bool:
    """Whether the record is inventory taken at attach rather than an event."""
    return str(event.get("event") or "") == "baseline"


def settled_at_attach(event: Mapping[str, Any]) -> bool:
    """Whether this row is a baseline for work that had already finished.

    A follower emits one baseline per live run the moment it attaches, and a
    run that is already complete or promoted produces a row that reads exactly
    like a landing that just happened. Nothing further will happen to it, so the
    row is inventory and the reader is better served by its absence. Keyed on
    the recorded kind, so a genuine transition into the same state still shows.
    """
    if not is_baseline(event):
        return False
    return str(event.get("to_state") or "") in SETTLED_STATES


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

        ``with_session`` marks the owning session, which an unscoped reader
        needs and a session-scoped one does not: every line a scoped follower
        receives is its own by construction, so the column would only take room
        from the node beside it.

        The counters precede the reason because the pane clips its own right
        edge. Everything before the reason is fixed-width, so a row rendered
        wider than the pane can spare loses trailing free text and nothing else;
        with the counters last, a width read one column too wide silently ate
        the fleet's numbers instead.
        """
        node = str(event.get("node") or event.get("run_id") or "unknown")
        to_state = _display_state(event.get("to_state") or "unknown")
        baseline = is_baseline(event)
        # A baseline has no source state to show even when the record carries
        # one, because nothing moved: showing a from-state would claim a
        # transition the fleet never made.
        from_state = "" if baseline else _display_state(event.get("from_state"))
        role = _display_role(event.get("role"))

        cells: list[tuple[str, Any]] = [
            (f"{local_clock(event.get('observed_at')):<{CLOCK}}", "dim"),
            (" " * GAP, None),
            (f"{role:<{ROLE}}", "dim"),
            (" " * GAP, None),
            (f"{elide(node, NODE):<{NODE}}", self.hue(node)),
        ]
        if with_session:
            owner = FOREIGN_OWNER if str(event.get("session") or "") else " "
            cells += [(" " * GAP, None), (f"{owner:<{OWNER}}", "dim")]
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
            (BASELINE_ARROW if baseline else TRANSITION_ARROW, "dim"),
            (" ", None),
            (f"{to_state:<{STATE}}", hues.get(to_state, "dim")),
            (" " * GAP, None),
            (f"{elide(_agent_column(event), AGENT):<{AGENT}}", "dim"),
            (" " * GAP, None),
        ]
        cells.extend(self._stats(event))
        cells.append((" " * GAP, None))

        head = sum(len(text) for text, _ in cells)
        room = max(self.width - head, 0)
        reason = self._reason(event, to_state, room)
        cells.append((f"{reason:<{room}}", "dim") if room else ("", None))
        # A shadow will never merge, so the row says so about itself end to end
        # rather than spending a column on an identifier a reader cannot use.
        shadow = is_shadow(event)
        return "".join(
            self._paint(text, "dim" if shadow and style is not None else style)
            for text, style in cells
        )

    def _reason(self, event: Mapping[str, Any], to_state: str, room: int) -> str:
        """The clause explaining an actionable state, bounded by the margin.

        Only the state being entered may explain itself. Keying on the state
        being left is how a promotion ends up still reporting the block it
        recovered from — describing a problem that is already over. A blocked
        entry carries a glyph saying whether a resume can answer it, derived from
        the persisted fact at render time rather than written into the record.
        """
        explained = NEEDS_ACTION | {"waiting", "wait-aged"}
        if to_state not in explained or room < MIN_REASON:
            return ""
        detail = event.get("detail")
        if detail is None:
            detail = event.get("reason")
        if to_state == "blocked":
            marker = _display_marker(event)
        else:
            marker = "!" if to_state == "wait-aged" else ""
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
        labels = (*_CELLS, _WAIT_CELL) if _WAIT_CELL in event else _CELLS
        for index, label in enumerate(labels):
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
