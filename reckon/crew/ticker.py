"""Render one fleet transition as a line of a fixed grid.

The pane this feeds is read down a column rather than across a line — which
worker, what state, how many still running — and it shows roughly eight lines at
a time. Two consequences shape everything here. Every field occupies the same
screen column on every row, so a scan does not have to re-find it. And no line
ever wraps, because a wrapped row costs a quarter of the visible history; free
text is truncated to the room the grid leaves rather than allowed to overrun.

Colour carries two questions that must not share an axis. *Which worker is
this?* is answered by the node's own hue, handed out in order of first
appearance. *Does this need me?* is answered by the destination state, painted
by the verdict that state names. Identity is kept perceptually clear of the four
verdict hues, so a worker's colour is never mistaken for a verdict about that
worker.
"""

from __future__ import annotations

import fcntl
import os
import re
import struct
import termios
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from numbers import Real
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
# Model and effort are two cells so a reader scans the effort down a column
# instead of parsing it out of a composed label. The cells sit one space apart
# — PAIR_GAP, not GAP — so effort begins at the same screen column on every
# row while no row carries padding wider than the alias it pads. The model
# cell is sized from the longest alias the resolved flight config declares, so
# a pane whose rows all carry a configured alias lands its effort column on one
# screen column. MODEL is the width used only when the config declares no alias
# at all: with nothing to size from, a wider cell would spend columns on
# nothing. A model id outside the configured set is cut to the cell with an
# ellipsis rather than allowed to overflow, so the grid never shifts — the cost
# is that a long unaliased id shows a prefix, which is the same trade every
# fixed-width column in this row makes.
MODEL = 10
PAIR_GAP = 1
EFFORT = 7
GAP = 2

# The measure block: wall time and generation rate, in that order, behind the
# fleet counters so the always-populated columns lead. Every cell is
# right-aligned to a fixed width with a single space between cells, so the two
# columns a reader scans stay put as figures change. The row no longer carries
# model seconds, charged tokens or the notional dollar figure — a meter's spend
# is a wider question than a pane can answer line by line, so those facts stay
# in the record and only wall and rate reach the row, which frees exactly the
# columns the reason now spends. The rate is generated output over model
# seconds; it deliberately does not divide into the wall figure, and the two
# readings are neighbours, not factors. SPEND counts content and the single
# inter-cell space; the leading space before the wall cell is separate.
WALL = 7
RATE = 4
SPEND_GAP = 1
SPEND = WALL + SPEND_GAP + RATE

# A cell with no measurement renders this, never a zero: a zero asserts a
# measurement that was never taken, and this fleet holds large populations of
# both facts at once — an unpriced lane really did spend nothing chargeable,
# while an unobserved run's tokens are simply unknown.
DIM_MARKER = "\N{EN DASH}"

# A wait whose condition has never been probed is not the same situation as one
# whose probe has run and not yet met its terminal: nothing is testing the
# first, so its condition cannot lift on its own and a reader waiting for it
# waits for nothing. The record separates them — the probe's verdict is absent
# when no probe has run — but the row rendered both identically, which is how a
# wait nobody was checking read as a slow dependency. It takes the same glyph
# the measure cells use for a figure that was never taken, because that is what
# the probe's verdict is: a measurement nobody has made.
UNPROBED_MARKER = DIM_MARKER

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
        "unwritten": 124,
        "held": 97,
        "needs-help": 124,
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
        "unwritten": 203,
        "held": 104,
        "needs-help": 203,
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
        "launch-failed",
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

# The row shows the state a run moved into and not the one it left. A
# from-state column cost ten columns and an arrow between them three more, and
# both were read as decoration: the question a reader brings to a row is where
# the run is now. Dropping the pair is what pays for the reason clause, which
# is the row's payload.
#
# What a baseline row prints in the state's own cell, instead of a bullet
# beside it. A baseline is inventory the follower emitted because it attached,
# and a restart emits one per live run inside a second or two; a lone glyph at
# small size was read as a fresh dispatch, so the record says what it is in a
# word. Read from the kind the log records, never from an absent from-state: a
# genuine transition into a first sighting also has no source, and conflating
# the two makes a restart read as a burst of news.
BASELINE_MARKER = "now"

# The marker cell is held on every row, blank on a transition, so the state
# word beside it lands on one screen column whether the row is news or
# inventory. The cell carries the marker and the space that separates it from
# the state, because a marker butted against its state reads as one token
# (`nowworking`) with no boundary to scan. The trailing column is the gap that
# keeps the model cell off a ten-column state word.
MARKER = len(BASELINE_MARKER) + 1
STATE_REGION = MARKER + STATE + 1

# States a run does not leave. A baseline row for one of these is inventory
# about work that is already over — the alarming-looking rows a reattaching
# follower emits first — so it is suppressed rather than shown as news. A block
# or a stall is not here: it has stopped without finishing, and it is exactly
# what a reader attaching wants told.
SETTLED_STATES = frozenset(
    {"complete", "completed_unpromoted", "promoted", "failed", "stopped", "abandoned"}
)

_CELLS = ("working", "blocked", "unpromoted")
_WAIT_CELL = "queued"

# The event field each bucket's count arrives in. Only the queued bucket is
# named differently from its field: the scheduler's own word for a run it has
# admitted but not started is `waiting`, and the counter names the bucket so
# that its letter is the bucket's initial like every other one.
_COUNT_FIELD = {"queued": "waiting"}

# What each fleet counter renders its bucket name as, appended to the count.
# One character each, so the block stays narrow enough to leave the reason
# clause readable on every pane this workstation measures: a label spelled out
# in full costs every row five columns and takes them from the free text, which
# is the cell a reader is actually trying to read. Three labels are the first
# letter of the word the state column already prints — working, blocked,
# unpromoted — so the pane teaches those without carrying a legend. The waiting
# bucket's q is the initial of `queued`, the bucket's own name, which no column
# prints today; that gap is a naming question for the state vocabulary and not
# a reason to spend width here.
STAT_LETTER = {
    "working": "w",
    "blocked": "b",
    "unpromoted": "u",
    "queued": "q",
}

# The counter cells whose backlog belongs to the orchestrator: a blocked run
# needs a decision and an unpromoted one needs merging, reviewing or recording.
# The other two are work in progress and need nothing from the reader, so they
# carry no age. Which states land in which bucket stays the fleet partition's
# question — this names only the buckets that answer with an age, and the
# states themselves are read from that partition rather than restated here.
ACTIONABLE_CELLS = ("blocked", "unpromoted")

# The fixed width an age occupies, held constant whether a cell has an age to
# show or not: the block keeps one width as counts and ages change, so the
# columns beside it never move. Held rather than trimmed because a field that
# appears only when there is something to say shunts every column to its right
# on the row that finally has news to deliver. Four characters hold a minute
# count, an hour count and a day count, and the rare age past that is claimed
# as `99d+` rather than allowed to widen the field.
AGE = 4

# Two digits and the bucket label per counter, joined by a bare middle dot with
# no surrounding space. Two digits cover any fleet the dispatcher opens; a
# wider count pushes its own label rather than silently misaligning the column
# beside it. The label is one character for three buckets and the spelled word
# for the waiting one, so the block is sized from the labels themselves rather
# than assumed at one character each. The two-digit alignment keeps the block a
# constant width as counts change, so the right edge of the row never moves,
# and the reclaimed separator space funds the model and effort cells without
# taking width from the reason.
_MAX_CELLS = (*_CELLS, _WAIT_CELL)
STATS = sum(2 + len(STAT_LETTER[label]) for label in _MAX_CELLS) + (len(_MAX_CELLS) - 1)

# The widest the fixed columns can be, plus the stats block and one gap. A width
# below this cannot be honoured without wrapping, so it is raised to this.
# Everything before the reason consumes exactly this many columns with the role
# word at its widest (documentation, thirteen), the model cell at its default
# width of ten (a grid sized from a longer configured alias raises this floor by
# the same amount it widens the cell), the
# effort cell at seven, the four fleet counters and the measure cells (wall and
# rate) in full — the measures sit behind the counters now, and the row no
# longer carries model seconds, tokens or the dollar figure, which frees exactly
# the columns that moved to the free text. The waiting counter's spelled word is
# five columns wider than the single letter it replaced, so this floor moved by
# that much and the reason's share of a fixed pane moved with it. Against the
# 180-column DEFAULT_WIDTH budget that still leaves more than 30 for the reason,
# and more than 60 on the 208-column pane this workstation measures (its
# observed cut, read directly with no inset subtracted) — both clear the
# 12-column floor below which a clause is not worth reading, and the 180-column
# figure is what a later added column spends first.
MIN_WIDTH = (
    CLOCK
    + GAP
    + ROLE
    + GAP
    + NODE
    + GAP
    + OWNER
    + STATE_REGION
    + MODEL
    + PAIR_GAP
    + EFFORT
    + SPEND_GAP
    + SPEND
    + STATS
    + GAP
)
DEFAULT_WIDTH = 180
DEFAULT_THEME = "light"


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


def calibrated_width(observed_cut: int) -> int:
    """The grid width a pane's measured column count resolves to, floored.

    The width comes from that observed reading — the terminal's column count,
    which is where the pane ends — not from a constant between a terminal and
    the text grid. A cut narrower than the fixed columns can honour is still
    raised so no line ever wraps, and a detached follower with no terminal is
    handled at the call site.
    """
    return max(int(observed_cut), MIN_WIDTH)


def resolve_terminal_width() -> int:
    """The pane's current width for this renderer, or the stated fallback.

    The width a line must fit is not on the stream it is written to; it lives on
    the terminal an ancestor owns and tracks a resize. Walk the ancestry to the
    first readable terminal and read its column count as the measured width — no
    inset is subtracted, because where the pane ends IS the width the grid must
    fit — and floor the result at the grid's minimum so a narrower pane still
    never wraps. A detached follower has no such ancestor — collector or
    nohup'd — and falls back to the stated default. ``--width`` overrides this
    at the call site.
    """
    for path in _ancestor_terminal_paths():
        columns = _columns_of(path)
        if columns:
            return calibrated_width(columns)
    return DEFAULT_WIDTH


# Below this there is no room for a clause worth reading, and a two-word stub is
# worse than the whitespace it replaces.
MIN_REASON = 12

# A run that never wrote a stream record reached no model, so the counter's
# blocked bucket holds it and the clause is the only place the line can say the
# stop is an infrastructure fault rather than a worker turn wanting a decision.
# The classification normally supplies the cause and this clause is not reached;
# it is here for the record that carries the state without one, because a
# blocked number rendering no reason is the defect this line exists to close.
LAUNCH_FAULT_STATE = "launch-failed"
LAUNCH_FAULT_CLAUSE = "the launch failed before any turn and no model was reached"

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


def _clock(seconds: float) -> str:
    """Render a duration as h:mm:ss (or m:ss under an hour), seconds rounded."""
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _epoch(observed: Any) -> float | None:
    """A stored UTC stamp as epoch seconds, or None when it is no reading.

    A stamp that does not say which zone it is in cannot be placed on a clock,
    so it yields no age rather than one computed against whatever zone the
    reader happens to be sitting in. An age nobody could observe is not a
    reading about the backlog, and the row already has a blank for it.
    """
    text = str(observed or "")
    if len(text) < 19:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.timestamp()


def _age_span(seconds: float) -> str:
    """An age as the coarsest unit that still carries its magnitude.

    Minutes under an hour, hours under two days, days beyond — the unit grows
    with the age so a reader comparing two backlogs is not left counting
    characters to find the larger. The scale coarsens where a reader's action
    does not change with it, which is why the two-day step is where hours stop
    being worth printing.
    """
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)}h"
    days = int(seconds // 86400)
    return "99d+" if days > 99 else f"{days}d"


def _bucket_of(state: Any, classification: Any) -> str | None:
    """Which counter bucket a run's state belongs to, or None off the fleet.

    Membership is read from the fleet partition rather than restated, so the
    counter and the age that follows it always describe the same population.
    The import happens here rather than at module scope because the partition
    imports this module: taking the names at import time would freeze them
    before the partition has finished defining them. A held run counts as
    waiting, which is the partition's own reading of a refusal that has been
    accepted rather than acted on.
    """
    from reckon.crew.recovery import (
        FLEET_BLOCKED_STATES,
        FLEET_UNPROMOTED_STATES,
        FLEET_WAITING_STATES,
        FLEET_WORKING_STATES,
    )

    if str(classification or "") == "held":
        return _WAIT_CELL
    word = str(state or "")
    if word in FLEET_UNPROMOTED_STATES:
        return "unpromoted"
    if word in FLEET_BLOCKED_STATES:
        return "blocked"
    if word in FLEET_WAITING_STATES:
        return _WAIT_CELL
    if word in FLEET_WORKING_STATES:
        return "working"
    return None


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


# The prefix the fleet surface prints ahead of the bound that is binding. A
# word rather than a glyph, because the reading is a sentence fragment a reader
# has to be able to search for, and because the same clause appears in a
# refusal where no legend is available to decode a symbol.
BOUND_PREFIX = "bind"
# What the clause says when no bound could be read. Distinct from a bound that
# reads as ample: an unreadable slice is a fact about the instrument, and a
# reader who sees it should go looking rather than assume headroom.
BOUND_UNKNOWN = "unknown"
# The widest the bound clause is allowed to grow before it is elided. The
# clause carries a label and a measured value, so it is longer than a counter;
# this holds the right edge of the row steady across readings.
BOUND_WIDTH = 46


def bound_clause(report: Mapping[str, Any] | None) -> str:
    """Name the binding bound with its measured value, for the fleet surface.

    A label alone says a resource was considered, not how close it is: the
    reader's next question is the figure, so the clause carries both and never
    reports the label on its own. A report that names no binding bound — every
    candidate unreadable or unbounded — says ``unknown`` rather than naming one,
    because there is nothing to name.
    """
    if not report or not report.get("binding"):
        return f"{BOUND_PREFIX} {BOUND_UNKNOWN}"
    value = single_clause(report.get("value"), limit=BOUND_WIDTH)
    clause = f"{BOUND_PREFIX} {report['binding']} {value}".strip()
    return elide(clause, BOUND_WIDTH)


def bound_cells(report: Mapping[str, Any] | None) -> list[tuple[str, Any]]:
    """The binding-bound clause as a ranked grid cell, or empty when absent.

    A transition whose record carries no bound reading adds no cell rather than
    an ``unknown`` one: the fleet surface says what it was told, and inventing
    an unknown for every older record would drown the reading where it exists.
    """
    if not report:
        return []
    return [(" " * GAP, None), (bound_clause(report), "dim")]


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


def declared_model_aliases() -> tuple[str, ...]:
    """The aliases the resolved flight config declares, deduped and sorted.

    Every backend may declare an alias — the display label rendered in the pane
    in place of its model identifier — so the pane can size one column to the
    whole configured vocabulary. Resolution reads the same layers a dispatch
    does (shipped, host, project, override), so the column the reader's pane
    uses is the one a run sent through this configuration would carry.

    A config that cannot be read yields no aliases rather than raising: this is
    a display surface, and a pane that will not render because a config file is
    malformed is worse than one whose column falls back to the default. The
    refusal an operator needs is already spelled on the dispatch and flight
    paths, which read the same config.
    """
    try:
        from reckon import flight as flight_module
    except ImportError:
        return ()
    try:
        config = flight_module.resolve().config
    except (OSError, ValueError, flight_module.FlightConfigError):
        return ()
    backends = config.get("backends")
    if not isinstance(backends, Mapping):
        return ()
    aliases: set[str] = set()
    for settings in backends.values():
        if isinstance(settings, Mapping):
            alias = str(settings.get("alias") or "").strip()
            if alias:
                aliases.add(alias)
    return tuple(sorted(aliases))


def model_cell_width(aliases: Iterable[str]) -> int:
    """The model cell's width: the longest declared alias, else the default."""
    longest = max((len(alias) for alias in aliases), default=0)
    return longest or MODEL


def _model_and_effort(event: Mapping[str, Any]) -> tuple[str, str]:
    """The two agent cells for a transition: model and effort, laid apart.

    A new-shape line persists model and effort as separate facts, so the model
    cell is the alias that shortens the model — or the model itself — and the
    effort cell is the whole effort word, each in its own column so a reader
    scans effort down the pane rather than parsing it out of a composed label.
    A legacy line carries a precomposed ``model/effort`` string instead and
    splits at the slash so it still lands in the two cells; the fragments are
    never re-parsed beyond that split. The model value is elided to the cell by
    the caller, so a value wider than the column is cut rather than allowed to
    shift the cells after it.
    """
    model = str(event.get("model") or "").strip()
    effort = str(event.get("effort") or "").strip()
    alias = str(event.get("alias") or "").strip()
    if model or effort or alias:
        return (alias or model), _derive_effort(effort)
    agent = str(event.get("agent") or "").strip()
    if "/" in agent:
        model, _, effort = agent.partition("/")
        return model.strip(), effort.strip()
    return agent, ""


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


def declares_a_wait(event: Mapping[str, Any]) -> bool:
    """Whether the row's run declared an external condition to wait on.

    Read from the declaration's own facts, never from the state word: a run
    whose declaration is incomplete is classified by what failed to parse,
    which is a different word from the one a well-formed wait renders on a
    skewed clock. A run declaring no wait emits None for all of them, so the
    marker cannot be derived from an ordinary row; a manifest asking to wait
    emits the horizon and the overdue flag even when nothing can be probed,
    which is the situation the row has to name.
    """
    if event.get("wait_condition_state") is not None:
        return True
    if event.get("wait_overdue") is not None:
        return True
    return isinstance(event.get("expected_horizon_seconds"), Real)


def probe_has_run(event: Mapping[str, Any]) -> bool:
    """Whether the declared wait's condition has been probed at all.

    The probe's verdict — met, pending or an observation matching no declared
    terminal — is written only when a probe answered, so its presence is the
    record of execution and its absence is the record of none. A run whose
    probe could not be launched at all still carries a verdict, because that
    failure is what the probe reported; only a wait that was never read has
    nothing to say, which is exactly the case a reader must not mistake for a
    condition still being tested.
    """
    return bool(str(event.get("wait_condition_state") or "").strip())


def _unprobed_wait(event: Mapping[str, Any]) -> bool:
    """Whether the row declares a wait no probe has ever been executed for."""
    return declares_a_wait(event) and not probe_has_run(event)


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
        model_aliases: Iterable[str] | None = None,
    ) -> None:
        self.theme = theme if theme in PALETTE else DEFAULT_THEME
        # The model cell is sized once, from the aliases the configuration
        # declares, so effort lands on one screen column and an id outside that
        # vocabulary is cut rather than allowed to shift the cells after it. An
        # explicit set overrides the resolved config, which lets a caller size
        # the column for a vocabulary it already holds.
        self.model_width = model_cell_width(
            declared_model_aliases() if model_aliases is None else model_aliases
        )
        self.width = max(int(width), MIN_WIDTH - MODEL + self.model_width)
        # NO_COLOR is the caller's environment overriding the caller's flag, per
        # the convention; any non-empty value disables.
        self.color = bool(color) and not os.environ.get("NO_COLOR")
        self._hues: dict[str, int] = {}
        # When each run entered the bucket it now sits in, keyed by run. The
        # record carries counts, not per-item ages, so the age of a bucket's
        # oldest member is derived from the transitions this pane has itself
        # seen: the instance lives as long as the reader's grid, and its first
        # sighting of a run is the baseline the watch emitted on attach. An age
        # is therefore the age within this pane's view — a re-attach restamps
        # every live run, which is a fact about the reading rather than a defect
        # in it, because an age nobody observed cannot be reported honestly.
        self._entered: dict[str, tuple[str, float]] = {}

    def hue(self, node: str) -> int:
        """The node's colour, claimed on first sighting and kept thereafter."""
        if node not in self._hues:
            palette = PALETTE[self.theme]
            self._hues[node] = palette[len(self._hues) % len(palette)]
        return self._hues[node]

    def _register(self, event: Mapping[str, Any], state: Any) -> None:
        """Note when this run entered the bucket it now occupies.

        A run already in the bucket keeps the stamp it arrived with, so the
        oldest member's age grows while the backlog stands still rather than
        resetting on every unrelated transition. A run that leaves every bucket
        is dropped, because it is no longer a member of anything the row counts.
        """
        run = str(event.get("run_id") or event.get("node") or "")
        moment = _epoch(event.get("observed_at"))
        if not run or moment is None:
            return
        bucket = _bucket_of(state, event.get("recovery_classification"))
        if bucket is None:
            self._entered.pop(run, None)
            return
        held = self._entered.get(run)
        if held is not None and held[0] == bucket:
            return
        self._entered[run] = (bucket, moment)

    def _oldest_age(self, label: str, now: float) -> str:
        """The age of the oldest run in ``label``'s bucket, or blank when empty.

        Blank rather than zero: nothing outstanding and something that just
        arrived are different facts, and a zero in this position reads as the
        second. An empty bucket is the common case, so the age field spends its
        width on nothing at all rather than on a figure a reader must discount.
        """
        stamps = [stamp for held, stamp in self._entered.values() if held == label]
        if not stamps:
            return ""
        return _age_span(max(0.0, now - min(stamps)))

    def render(self, event: Mapping[str, Any], *, with_session: bool = False) -> str:
        """One transition as one line, exactly ``width`` visible characters.

        ``with_session`` marks the owning session, which an unscoped reader
        needs and a session-scoped one does not: every line a scoped follower
        receives is its own by construction, so the column would only take room
        from the node beside it.

        The counters precede the optional measures, which both sit before the
        reason, because the pane clips its own right edge: the always-populated
        columns lead. Everything before the reason is fixed-width, so a row
        rendered wider than the pane can spare loses trailing free text and
        nothing else; with the counters last, a width read one column too wide
        silently ate the fleet's numbers instead.
        """
        node = str(event.get("node") or event.get("run_id") or "unknown")
        raw_to_state = str(event.get("to_state") or "unknown")
        typed_state = str(event.get("recovery_classification") or "")
        # The compatibility lifecycle state may group several stops as blocked.
        # The row spells the cause-specific type when the producer supplied one,
        # so the reader sees the recovery distinction. The full word is held
        # beside the elided one because the clause's gate reads it: the state
        # cell is ten columns wide and a longer state word is cut to fit it, so
        # a gate keyed on the rendered form matches no state at all.
        entry_state = (
            typed_state
            if typed_state in {"held", "needs-help", "unwritten"}
            else raw_to_state
        )
        to_state = _display_state(entry_state)
        baseline = is_baseline(event)
        # Recorded before the row is built, so a run that arrives in an
        # actionable bucket on this very transition is a member of it when the
        # counter beside it is rendered, and its age starts at this reading.
        self._register(event, raw_to_state)
        role = _display_role(event.get("role"))
        model_cell, effort_cell = _model_and_effort(event)

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
        # The state cell carries the destination alone. The marker cell ahead
        # of it is present on every row so the state column never moves; a
        # branch inside the cell (rather than a cell that appears) is what
        # keeps a baseline row on the grid the transitions sit on.
        hues = STATE_HUE[self.theme]
        cells += [
            (f"{BASELINE_MARKER if baseline else '':<{MARKER}}", "dim"),
            (f"{to_state:<{STATE}}", hues.get(to_state, "dim")),
            (" ", None),
            (f"{elide(model_cell, self.model_width):<{self.model_width}}", "dim"),
            (" " * PAIR_GAP, None),
            (f"{effort_cell:<{EFFORT}}", "dim"),
        ]
        cells.extend(self._stats(event))
        cells.extend(self._spend_cells(event, to_state))
        cells.append((" " * GAP, None))

        head = sum(len(text) for text, _ in cells)
        room = max(self.width - head, 0)
        # The ages are read against the counters they qualify, so they take the
        # fixed columns and the clause keeps the margin. Every column ahead of
        # the reason is sized by what it carries, so the ages are paid for out
        # of the free text — and only where the clause does not need those
        # columns. A clause that claims the margin keeps it whole and the age
        # blanks, which is the bargain the margin already makes: what is
        # present holds its room and what is absent holds none.
        reason = self._reason(event, entry_state, room)
        ages = self._age_cells(event)
        if ages and len(reason) <= room - len(ages):
            cells.append((ages, None))
            cells.append((f"{reason:<{room - len(ages)}}", "dim"))
        elif room:
            cells.append((f"{reason:<{room}}", "dim"))
        else:
            cells.append(("", None))
        # A shadow will never merge, so the row says so about itself end to end
        # rather than spending a column on an identifier a reader cannot use.
        shadow = is_shadow(event)
        return "".join(
            self._paint(text, "dim" if shadow and style is not None else style)
            for text, style in cells
        )

    def _reason(self, event: Mapping[str, Any], entry_state: str, room: int) -> str:
        """The clause explaining an actionable state, bounded by the margin.

        Only the state being entered may explain itself. Keying on the state
        being left is how a promotion ends up still reporting the block it
        recovered from — describing a problem that is already over. A blocked
        entry carries a glyph saying whether a resume can answer it, derived from
        the persisted fact at render time rather than written into the record.

        ``entry_state`` is the state word whole, not the form the state cell
        renders: the cell is narrower than the longest word the classifier emits,
        so the word is elided to fit it and the allow-list below holds the full
        spellings. A gate reading the rendered form is a gate that says nothing
        about work the pane is counting.
        """
        explained = NEEDS_ACTION | {
            "waiting",
            "wait-aged",
            "held",
            "needs-help",
            "unwritten",
        }
        unprobed = _unprobed_wait(event)
        # A wait no probe has run for is a fact about the wait alone, and the
        # state word beside it can be any the classifier emitted — a live run
        # with a broken declaration reads as ordinary working. So the marker is
        # reachable from every state, and survives a clause with no room for it.
        if entry_state not in explained and not unprobed:
            return ""
        if room < MIN_REASON:
            return UNPROBED_MARKER if unprobed else ""
        detail = event.get("detail")
        if detail is None:
            detail = event.get("reason")
        if entry_state in {"blocked", "needs-help"}:
            marker = _display_marker(event)
        else:
            marker = "!" if entry_state == "wait-aged" else ""
        if unprobed:
            # Additive, never a replacement: the age and needs-help glyphs are
            # signals about the run that a reader acts on, and a wait whose
            # probe never ran is one fact more rather than one of them
            # overruled. The marker's width is carried in the reserve below.
            marker = UNPROBED_MARKER + marker
        recovery = str(event.get("recovery") or "").strip()
        recovery_prefix = f"{recovery}: " if recovery else ""
        reserve = len(marker) + (1 if marker else 0) + len(recovery_prefix)
        clause = single_clause(detail, limit=max(0, room - reserve))
        if not clause and entry_state == LAUNCH_FAULT_STATE:
            # The blocked bucket is where a launch failure's number arrives, so
            # the clause is the only place the row can say the stop is an
            # infrastructure fault. The classifier's cause normally fills it;
            # this names the fault itself for a record that carried the state
            # without one, rather than rendering a number with no reason.
            clause = single_clause(LAUNCH_FAULT_CLAUSE, limit=max(0, room - reserve))
        if recovery_prefix:
            clause = recovery_prefix + clause if clause else recovery_prefix.rstrip()
        if marker and clause:
            return f"{marker} {clause}"
        return marker or clause

    def _spend_cells(
        self, event: Mapping[str, Any], to_state: str
    ) -> list[tuple[str, Any]]:
        """Wall time and generation rate, fed by the transition record's facts.

        Each fact is right-aligned to its fixed width with a single space
        between cells, so the columns a reader scans stay put as figures change.
        An unmeasured fact renders the dim absence marker, never a zero; a
        measured zero stays a zero. Wall time on a transition into dispatched is
        time zero by definition, so that cell blanks — blank rather than the
        absence marker, because the marker already means unmeasured and blanking
        noise with it would collapse two different facts. The row carries only
        these two figures; model seconds, charged tokens and the dollar figure
        stay in the record, unrendered. The cells read the record's own figures
        and shape them only here, so the persisted event stays re-renderable.
        """

        def cell(value: Any, formatter: Callable[[float], str]) -> tuple[str, Any]:
            if isinstance(value, Real):
                return formatter(float(value)), None
            return DIM_MARKER, "dim"

        wall, wall_style = cell(event.get("spend_wall_seconds"), _clock)
        if to_state == "dispatched":
            wall, wall_style = "", None
        rate, rate_style = cell(
            event.get("spend_generation_rate"), lambda value: f"{value:.0f}"
        )
        return [
            (" " * SPEND_GAP, None),
            (f"{wall:>{WALL}}", wall_style),
            (" " * SPEND_GAP, None),
            (f"{rate:>{RATE}}", rate_style),
        ]

    def _stats(self, event: Mapping[str, Any]) -> list[tuple[str, Any]]:
        """The fleet after this transition, as a grid whose digits line up.

        Each counter is its number followed by the initial of the bucket it
        counts, so the letter is decodable from the bucket's own name rather
        than from a legend the stream does not carry. A zero is dimmed rather than dropped: blanking it would leave
        trailing whitespace and take the right edge ragged, and a reader
        waiting for a drain needs to see the count reach zero, not see it
        disappear.

        The block carries no age itself: the count alone cannot separate a
        backlog being worked from one standing still — the same figure reads
        identically while a stalled item is cleared and a fresh one takes its
        place — and the ages that answer that question are rendered by
        ``_age_cells``, where the row has room for them.
        """
        cells: list[tuple[str, Any]] = []
        wait_field = _COUNT_FIELD.get(_WAIT_CELL, _WAIT_CELL)
        labels = (*_CELLS, _WAIT_CELL) if wait_field in event else _CELLS
        for index, label in enumerate(labels):
            if index:
                cells += [("·", "dim")]
            count = int(event.get(_COUNT_FIELD.get(label, label)) or 0)
            cells.append((f"{count:>2}{STAT_LETTER[label]}", None if count else "dim"))
        # The bound sits beside the counters the transition carries, because
        # the counters say how much work is in flight and the bound says what
        # limits it. A record written before the reading existed carries none,
        # so the clause is absent rather than unknown.
        cells += bound_cells(event.get("bounds"))
        return cells

    def _age_cells(self, event: Mapping[str, Any]) -> str:
        """The actionable buckets' oldest age, or empty when they hold nobody.

        The count alone cannot separate a backlog being worked from one
        standing still: clearing a stalled item while a fresh one arrives
        clears the figure and leaves its membership turned over entirely. The
        age of the oldest member is the fact that separates the two — it grows
        while nothing is cleared and drops when the oldest item is dealt with.
        The two buckets that are work in progress carry none, because nothing
        is asked of the reader there.

        Each age carries the same letter its counter prints, so a reading at
        the end of a row still names its bucket. The field holds its width, so
        the row's right edge never moves as counts and ages change. An empty
        bucket renders nothing rather than a zero, because a zero here reads
        as fresh work rather than as none.
        """
        now = _epoch(event.get("observed_at"))
        if now is None:
            return ""
        return "".join(
            f" {span:>{AGE}}{STAT_LETTER[label]}"
            for label in ACTIONABLE_CELLS
            if (span := self._oldest_age(label, now))
        )

    def _paint(self, text: str, style: Any) -> str:
        if not self.color or style is None or not text:
            return text
        prefix = _DIM if style == "dim" else f"\x1b[38;5;{int(style)}m"
        return f"{prefix}{text}{_RESET}"
