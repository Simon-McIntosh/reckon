"""Render one fleet transition as a line of a fixed grid.

The pane this feeds is read down a column rather than across a line — which
worker, what state, how many still running — and it shows roughly eight lines at
a time. Two consequences shape everything here. Every field occupies the same
screen column on every row, so a scan does not have to re-find it. And no line
ever wraps, because a wrapped row costs a quarter of the visible history; free
text is truncated to the room the grid leaves rather than allowed to overrun.

Colour carries two questions that must not share an axis. *Which worker is
this?* is answered by the node's own hue, handed out in order of first
appearance. *Does this need me?* is answered by a one-character attention
column and by the destination state, painted by the verdict that state names.
Identity is kept perceptually clear of the four verdict hues, so a worker's
colour is never mistaken for a verdict about that worker.

The attention column is ``!`` for a run that needs a coordinator: a blocked,
failed, stalled, stopped, abandoned, unreadable, unwritten, interrupted or
otherwise help-seeking run; a wait that has aged or was never probed; and a
completion awaiting review or promotion. Running, dispatched, self-lifting
waits and promoted work leave it blank. The recovery vocabulary still owns the
specific remedy, but the row never prints that action word: the transition says
what happened, the attention column says whether to look, and the plain reason
clause says why.
"""

from __future__ import annotations

import fcntl
import os
import re
import struct
import termios
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from numbers import Real
from typing import Any

CLOCK = 8
NODE = 36
# A node name wider than its cell is cut from the middle, so the end that
# distinguishes it from the rest of its wave survives the cut. Two rows can
# still land on one text — two names that differ only where the cut falls, and
# two runs of one node, which share a name outright — and a row is the only
# place a reader can tell them apart, so each then carries the tail of its own
# run's minted stamp, appended inside the cell: the name is cut a little
# tighter to make the room rather than the suffix pushing the columns after the
# node cell. The stamp and never the id's own tail, which repeats the node name
# the cell already carries and so separates nothing.
RUN_STAMP_TAIL = 4
# One glyph answers whether the destination state needs the coordinator. It is
# a column rather than a prefix on the reason so its position never depends on
# how long the transition or explanation is.
ATTENTION = 1
# Model and effort are two cells so a reader scans the effort down a column
# instead of parsing it out of a composed label. Every boundary uses the same
# two-column gutter, so effort begins at the same screen column on every row
# while no row carries padding wider than the alias it pads. The model
# cell is sized from the longest alias the resolved flight config declares, so
# a pane whose rows all carry a configured alias lands its effort column on one
# screen column. MODEL is the width used only when the config declares no alias
# at all: with nothing to size from, a wider cell would spend columns on
# nothing. A model id outside the configured set is cut to the cell with an
# ellipsis rather than allowed to overflow, so the grid never shifts — the cost
# is that a long unaliased id shows a prefix, which is the same trade every
# fixed-width column in this row makes.
MODEL = 10
PAIR_GAP = GAP = 2
EFFORT = 7

# The measure block is the run's elapsed time, immediately after the transition
# and before the fleet counters. The cell is right-aligned to a fixed width, so
# the column a reader scans for a run over its budget stays put as the figure
# changes. It is the only measure the row carries. The generation rate sat
# beside it and rendered the absence marker on nearly every row of the fleet
# census: a rate is a reading about one model's throughput rather than about the
# run, so the column bought a dash where a figure almost never arrived. WALL is
# the widest token the elapsed format produces — the format's own ceiling, held
# where it is derived — and narrower readings pad to it rather than moving the
# cells after it. SPEND is the cell's width; the single space that separates it
# from the counters is separate.
WALL = 6
SPEND_GAP = 1
SPEND = WALL

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
        "launch-failed": 124,
        "stopped": 124,
        "abandoned": 124,
        "stalled": 130,
        "complete": 28,
        "promoted": 22,
        "withdrawn": 130,
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
        "launch-failed": 203,
        "stopped": 203,
        "abandoned": 203,
        "stalled": 179,
        "complete": 78,
        "promoted": 71,
        "withdrawn": 179,
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

# A transition is two state words around one arrow. The previous word is
# right-aligned and the destination is left-aligned, which pins the arrow to one
# screen column even when either word changes length. A first sighting has no
# previous word, so the left half and arrow are blank while the destination
# keeps its column.
STATE_WORD = max(len(word) for word in STATE_HUE["light"])
ARROW = "→"
ARROW_GAP = 1
STATE = STATE_WORD + ARROW_GAP + len(ARROW) + ARROW_GAP + STATE_WORD
NEW_STATE_OFFSET = STATE_WORD + ARROW_GAP + len(ARROW) + ARROW_GAP

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

# Every destination or recovery classification that makes the attention column
# visible. The recovery module imports this renderer, so the display contract
# cannot import its actionable set without a cycle; this is the one documented
# display copy, and the property test covers the classifier's full vocabulary.
ATTENTION_STATES = NEEDS_ACTION | frozenset(
    {
        "complete",
        "completed_unpromoted",
        "ended-without-manifest",
        "held",
        "interrupted",
        "needs-help",
        "promotable",
        "ready",
        "refused-at-admission",
        "scoring",
        "unpromoted",
        "unwritten",
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

# A baseline is inventory the follower emits when it attaches. It has no
# previous state, so the transition's left half and arrow stay blank while its
# destination keeps the same column as a later transition. The legacy marker
# word remains exported only so compatibility checks can assert it is absent.
BASELINE_MARKER = "now"

# The state region is the transition plus its two-column gutter. ``MARKER`` is
# retained as a geometry alias for callers that locate the destination word:
# it now means the fixed offset from the transition's start to that word.
STATE_REGION = STATE + GAP
MARKER = NEW_STATE_OFFSET

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
# the same amount it widens the cell), the effort cell at seven, the four fleet
# counters and the elapsed cell in full. The waiting counter's spelled word is
# five columns wider than the single letter it replaced, so this floor carries
# that much and the reason's share of a fixed pane carries it too. Against the
# 180-column DEFAULT_WIDTH budget that still leaves more than 60 for the reason,
# and more than 80 on the 208-column pane this workstation measures (its
# observed cut, read directly with no inset subtracted) — both clear the
# 12-column floor below which a clause is not worth reading, and the 180-column
# figure is what a later added column spends first.
MIN_WIDTH = (
    CLOCK
    + GAP
    + ATTENTION
    + GAP
    + MODEL
    + GAP
    + EFFORT
    + GAP
    + ROLE
    + GAP
    + NODE
    + GAP
    + STATE
    + GAP
    + SPEND_GAP
    + SPEND
    + GAP
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


def _elapsed(seconds: float) -> str:
    """Render an elapsed span in hours and minutes, minutes alone under one.

    A clock's seconds are noise on a line a reader scans for a run over its
    budget: the reading decides at the minute, and every column a cell spends
    is a column the reason clause does not get below. So the unit grows with
    the figure — ``57m`` under an hour, ``1h33m`` past it — and the shape of
    the token says the magnitude without a unit legend.

    The token is held to the widest reading the cell can carry, six columns
    for ``99h59m``; a run older than that is claimed as ``99h+`` rather than
    allowed to widen the field and move every column after it.
    """
    minutes = max(0, round(seconds)) // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, rest = divmod(minutes, 60)
    if hours > 99:
        return "99h+"
    return f"{hours}h{rest:02d}m"


def _bucket_of(state: Any, classification: Any) -> str | None:
    """Which counter bucket a run's state belongs to, or None off the fleet.

    Membership is read from the fleet partition rather than restated, so the
    letter a counter prints always names the population it counts. The import
    happens here rather than at module scope because the partition imports this
    module: taking the names at import time would freeze them before the
    partition has finished defining them. A held run counts as waiting, which
    is the partition's own reading of a refusal that has been accepted rather
    than acted on.
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

    A clause still wider than the field is cut at the last word boundary inside
    it, so what a reader keeps is a whole word: a cut through a word reads as a
    typo rather than as a cut, and the word a reader scans for is where the
    clause's own sense stops. Only a clause whose first word is at least as wide
    as the field has no boundary to cut at, and then the cut falls inside that
    word and fills the field, because the head of it is all the room can show.
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
    if boundary < 0:
        boundary = limit - 1
    return clause[:boundary].rstrip(" ,:") + "…"


def elide(text: str, width: int, *, keep_end: bool = False) -> str:
    """Fit text to a column, marking the cut so a reader knows it was one.

    The cut keeps the head by default, which is where a state word, a model id
    and a reason clause carry what distinguishes them. A node name is the
    exception: the runs of one wave share a long head and differ at the end, so
    ``keep_end`` cuts from the middle and keeps both ends, one column of
    ellipsis between them, and a column too narrow to show two ends falls back
    to the head cut rather than dropping the mark of the cut.
    """
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if not keep_end or width < 3:
        return text[: width - 1] + "…"
    tail = width // 2
    head = width - 1 - tail
    return text[:head] + "…" + text[len(text) - tail :]


# The stamp ``new_run_id`` mints into a run id: ``r-<stamp>-<node token>``,
# with the token repeating the node's own name. Held as a pattern rather than
# as a field position, because only a field that is a stamp identifies a run:
# read by position, the middle field of any dashed id parses as one, and two
# ids agreeing on that field would carry the same suffix.
RUN_STAMP = re.compile(r"^r-(\d{8}T\d{6}\d{6})-")


def minted_stamp(run_id: str) -> str:
    """The part of a run id that tells two runs of one node apart.

    Two runs of one node share everything in an id but the minted stamp, and
    the id's tail is the node name outright, so a suffix meant to separate two
    rows has to come from here. An id that carries no minted stamp — one from
    a reader or a fixture rather than from ``new_run_id`` — has no such part to
    draw from, so the whole id is the run's identity and its own tail is what
    the row can show.
    """
    minted = RUN_STAMP.match(run_id)
    return minted.group(1) if minted else run_id


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
    return elide(DISPLAY.get(str(state or ""), str(state or "")), STATE_WORD)


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


def declared_model_aliases(project: str | None = None) -> tuple[str, ...]:
    """The aliases the resolved flight config declares, deduped and sorted.

    Every backend may declare an alias — the display label rendered in the pane
    in place of its model identifier — so the pane can size one column to the
    whole configured vocabulary. Resolution reads the same four layers a
    dispatch does — shipped, host, project and override — with ``project``
    selecting the project layer, so the column the reader's pane uses is the one
    a run sent through that configuration would carry. A row names its own
    project, and the pane is sized from that project's layer rather than a
    project-less resolution the dispatch would never take.

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
        config = flight_module.resolve(project).config
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
        project: str | None = None,
    ) -> None:
        self.theme = theme if theme in PALETTE else DEFAULT_THEME
        # The model cell is sized from the aliases the configuration declares,
        # so effort lands on one screen column and an id outside that
        # vocabulary is cut rather than allowed to shift the cells after it. An
        # explicit set overrides the resolved config, which lets a caller size
        # the column for a vocabulary it already holds. Otherwise a row's own
        # sets which layers the config resolves through: each row names its
        # project, so a project alias widens the cell for that project's rows,
        # matching what a dispatch through the same configuration would carry.
        self._requested_width = int(width)
        self._fixed_aliases = None if model_aliases is None else tuple(model_aliases)
        self._project = project
        self._alias_widths: dict[str | None, int] = {}
        self.model_width = self._model_width(project)
        self.width = self._grid_width(self.model_width)
        # NO_COLOR is the caller's environment overriding the caller's flag, per
        # the convention; any non-empty value disables.
        self.color = bool(color) and not os.environ.get("NO_COLOR")
        self._hues: dict[str, int] = {}
        # The node texts this pane has rendered, by the run that claimed each
        # of them, and the runs whose texts collided. Both live as long as the
        # pane: a row seen once cannot be un-seen, and a later attach replays
        # its baselines through this same grid.
        self._node_claims: dict[str, str] = {}
        self._colliding_runs: set[str] = set()
        # The state this pane last put on screen for each run. A producer may
        # skip an intermediate observation in its own ``from_state``; the pane
        # must not rewrite the story it already showed, so every later left side
        # comes from here. Synthetic unit events that carry no event kind are
        # independent render probes rather than stream rows and do not enter the
        # chain.
        self._reported: dict[str, str] = {}

    def _model_width(self, project: str | None) -> int:
        """The model cell's width for ``project``, resolved once and remembered.

        A pane streaming one project resolves one width; a configured alias set
        held by the caller fixes it for every project. Cached so the config is
        read once per project rather than once per row.
        """
        if self._fixed_aliases is not None:
            return model_cell_width(self._fixed_aliases)
        if project not in self._alias_widths:
            self._alias_widths[project] = model_cell_width(
                declared_model_aliases(project)
            )
        return self._alias_widths[project]

    def _grid_width(self, model_width: int) -> int:
        """The whole grid's width: the request, raised to fit a wider model cell."""
        return max(self._requested_width, MIN_WIDTH - MODEL + model_width)

    def hue(self, node: str) -> int:
        """The node's colour, claimed on first sighting and kept thereafter."""
        if node not in self._hues:
            palette = PALETTE[self.theme]
            self._hues[node] = palette[len(self._hues) % len(palette)]
        return self._hues[node]

    def _node_cell(self, node: str, run_id: str) -> str:
        """The node name fitted to its cell, marked when two rows read alike.

        The name is cut from the middle so its end survives, because the node
        column answers *which worker is this?* and the peers of a wave share a
        long head — a right-hand cut renders every one of them as the same
        string, which is a reader attributing a row to the wrong worker. The
        hue cannot repair that: hues are handed out per name, so two names that
        cut to one text are two entries and two colours reading as one name.

        What is compared is the cell a reader sees, not the name behind it: two
        names that differ where the cut falls land on one text, and two runs of
        one node land on one text outright, so the run that claims a text is
        what a later row is measured against. The second run to claim a text
        marks both, and each then carries the tail of its own minted stamp
        after the name, spaced so the suffix reads as an identifier rather than
        as the name's own tail. The claim is remembered rather than applied
        once, because the copy already written to a pane cannot be recalled — a
        row for either run rendered after the collision carries its own suffix.
        """
        text = elide(node, NODE, keep_end=True)
        claimed = self._node_claims.get(text)
        if claimed is None:
            self._node_claims[text] = run_id
        elif claimed != run_id:
            self._colliding_runs.update((claimed, run_id))
        if run_id not in self._colliding_runs:
            return text
        tail = minted_stamp(run_id)[-RUN_STAMP_TAIL:]
        if not tail:
            return text
        return f"{elide(node, NODE - RUN_STAMP_TAIL - 1, keep_end=True)} {tail}"

    def render(self, event: Mapping[str, Any], *, with_session: bool = False) -> str:
        """One transition as one line, exactly ``width`` visible characters.

        ``with_session`` is accepted for call-site compatibility. Ownership is
        no longer a row column: the follower already scopes delivery, while an
        extra glyph between the time and model would make the lead's reading
        order conditional.

        Everything before the reason is fixed-width, in the reading order set
        by the pane: time, attention, model, effort, role, node, transition,
        elapsed and fleet counters. A row rendered wider than the pane can
        spare loses trailing free text and nothing else.
        """
        node = str(event.get("node") or event.get("run_id") or "unknown")
        # The row names its own project, so the model column is sized from the
        # project layer its dispatch would read rather than a project-less
        # resolution. A row with no project falls back to the instance's, and
        # then to the project-less layers.
        row_project = str(event.get("project") or "").strip() or self._project
        model_width = self._model_width(row_project)
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
        run_id = str(event.get("run_id") or node)
        stream_event = bool(str(event.get("event") or "").strip())
        reported = self._reported.get(run_id) if stream_event else None
        if reported == to_state:
            return ""
        from_state = reported
        if from_state is None:
            from_state = _display_state(event.get("from_state"))
        if stream_event:
            self._reported[run_id] = to_state

        unprobed = _unprobed_wait(event)
        needs_attention = (
            entry_state in ATTENTION_STATES
            or typed_state in ATTENTION_STATES
            or unprobed
        )
        attention = "!" if needs_attention else " "
        role = _display_role(event.get("role"))
        model_cell, effort_cell = _model_and_effort(event)
        hues = STATE_HUE[self.theme]

        cells: list[tuple[str, Any]] = [
            (f"{local_clock(event.get('observed_at')):<{CLOCK}}", "dim"),
            (" " * GAP, None),
            (f"{attention:<{ATTENTION}}", hues.get(to_state, "dim")),
            (" " * GAP, None),
            (f"{elide(model_cell, model_width):<{model_width}}", "dim"),
            (" " * GAP, None),
            (f"{effort_cell:<{EFFORT}}", "dim"),
            (" " * GAP, None),
            (f"{role:<{ROLE}}", "dim"),
            (" " * GAP, None),
            (
                f"{self._node_cell(node, run_id):<{NODE}}",
                self.hue(node),
            ),
            (" " * GAP, None),
        ]
        if from_state:
            cells.extend(
                [
                    (f"{from_state:>{STATE_WORD}}", hues.get(from_state, "dim")),
                    (" " * ARROW_GAP, None),
                    (ARROW, "dim"),
                    (" " * ARROW_GAP, None),
                ]
            )
        else:
            cells.append((" " * NEW_STATE_OFFSET, None))
        cells.extend(
            [
                (f"{to_state:<{STATE_WORD}}", hues.get(to_state, "dim")),
                (" " * GAP, None),
            ]
        )
        cells.extend(self._spend_cells(event, to_state))
        cells.append((" " * GAP, None))
        cells.extend(self._stats(event))
        cells.append((" " * GAP, None))

        head = sum(len(text) for text, _ in cells)
        room = max(self._grid_width(model_width) - head, 0)
        # The clause keeps the whole margin: every column ahead of it is sized
        # by what it carries, and what a row can say is what the pane has left
        # after those. A width that leaves the reason nothing renders the fixed
        # columns alone. The clause is one row and never wraps, so a bound on
        # its length is a bound on the row.
        reason = self._reason(event, entry_state, room)
        if room:
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

        The destination state's own word is derived here from the same input the
        row composes it from, so the clause can avoid saying it a second time.
        The remedy remains a structured event fact and is deliberately not
        printed: the attention column says whether the coordinator must look,
        while this clause says what happened.
        """
        explained = ATTENTION_STATES | {
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
        marker = ""
        if unprobed:
            # This glyph is a measurement fact, not the attention signal: it
            # says the declared probe never ran. The attention column carries
            # the separate fact that the coordinator must look at it.
            marker = UNPROBED_MARKER
        reserve = len(marker) + (1 if marker else 0)
        clause = single_clause(detail, limit=max(0, room - reserve))
        if not clause and entry_state == LAUNCH_FAULT_STATE:
            # The blocked bucket is where a launch failure's number arrives, so
            # the clause is the only place the row can say the stop is an
            # infrastructure fault. The classifier's cause normally fills it;
            # this names the fault itself for a record that carried the state
            # without one, rather than rendering a number with no reason.
            clause = single_clause(LAUNCH_FAULT_CLAUSE, limit=max(0, room - reserve))
        state_word = _display_state(entry_state)
        clause = self._strip_repeated_label(clause, state_word)
        if marker and clause:
            return f"{marker} {clause}"
        return marker or clause

    def _spend_cells(
        self, event: Mapping[str, Any], to_state: str
    ) -> list[tuple[str, Any]]:
        """Elapsed time, fed by the transition record's fact.

        The figure is right-aligned to its fixed width so the column a reader
        scans stays put as the span grows. An unmeasured span renders the dim
        absence marker, never a zero; a measured zero stays a zero. Elapsed time
        on a row is what the run has spent, so a transition into dispatched
        reads zero by definition and that cell blanks — blank rather than the
        absence marker, because the marker already means unmeasured and blanking
        noise with it would collapse two different facts. The row carries only
        this figure; model seconds, charged tokens and the dollar figure stay in
        the record, unrendered. The cell reads the record's own figure and
        shapes it only here, so the persisted event stays re-renderable.
        """
        value = event.get("spend_wall_seconds")
        if isinstance(value, Real):
            wall: str = _elapsed(float(value))
            wall_style: Any = None
        else:
            wall, wall_style = DIM_MARKER, "dim"
        if to_state == "dispatched":
            wall, wall_style = "", None
        return [
            (" " * SPEND_GAP, None),
            (f"{wall:>{WALL}}", wall_style),
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
        place — and how long the oldest member has waited belongs to the
        surface a reader goes to for it rather than to a row.
        """
        cells: list[tuple[str, Any]] = []
        # Every bucket the fleet can show renders on every row, at its own fixed
        # width, so the block never changes shape when a run is queued and the
        # columns after it never move. A zero is dimmed rather than dropped: a
        # reader watching a drain needs to see the count reach zero rather than
        # see the cell disappear and the row's right edge go ragged.
        for index, label in enumerate(_MAX_CELLS):
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

    @staticmethod
    def _reason_words(state_cell: str) -> set[str]:
        """The words the state cell already says, so a clause repeats none."""
        return set(re.findall(r"[A-Za-z0-9_-]+", state_cell))

    def _strip_repeated_label(self, clause: str, state_cell: str) -> str:
        """Drop a leading label whose words the state cell already carries.

        A producer that prefixes its clause with the remedy — ``resume: ready
        to resume: the worker process is gone`` — spends the shortest column on
        the row saying what the state cell now says, and in the worst case says
        it twice. The label is dropped when any of its words names the state or
        its action, so what remains is the explanation; a leading word that
        repeats the cell is dropped for the same reason.
        """
        if not clause:
            return clause
        own = self._reason_words(state_cell)
        words = clause.split()
        # A leading label — one or more words closed by a colon — spends the
        # row's first column saying something the state cell already says, so
        # the clause begins with the explanation instead. The search is bounded
        # to the opening words so a colon later in the sentence, which is
        # punctuation rather than a label, is left where it is; a single-word
        # label is dropped whether or not it names the cell, because a clause
        # that opens with ``word:`` is naming a category a reader already has.
        for index, word in enumerate(words[:5]):
            if not word.endswith(":"):
                continue
            label = set(re.findall(r"[A-Za-z0-9_-]+", " ".join(words[: index + 1])))
            if label & own or index == 0:
                words = words[index + 1 :]
            break
        while words:
            head = re.findall(r"[A-Za-z0-9_-]+", words[0])
            if head and head[0] in own:
                words = words[1:]
            else:
                break
        return " ".join(words).strip()

    def _paint(self, text: str, style: Any) -> str:
        if not self.color or style is None or not text:
            return text
        prefix = _DIM if style == "dim" else f"\x1b[38;5;{int(style)}m"
        return f"{prefix}{text}{_RESET}"
