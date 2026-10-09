"""Render one fleet transition as a line of a fixed grid.

The pane this feeds is read down a column rather than across a line — which
worker, what state, how many still running — and it shows roughly eight lines at
a time. Two consequences shape everything here. Every field occupies the same
screen column on every row, so a scan does not have to re-find it. And no line
ever wraps, because a wrapped row costs a quarter of the visible history; free
text is truncated to the room the grid leaves rather than allowed to overrun.

Colour carries two questions. *Which worker is this?* is answered by the node's
own hue, handed out in order of first appearance. *Does this need me?* is
answered by the destination state, painted by the verdict that state names.
Identity is kept perceptually clear of the four verdict hues, so a worker's
colour is never mistaken for a verdict about that worker; the reading order is
time, model, effort, role, node, transition, elapsed, fleet counters and the
plain reason.

The row prints no separate attention mark. ``!`` beside the transition was
tried and dropped: the orchestrator reads the transition and decides, and the
destination state's colour already carries urgency, so a mark only repeats what
the new state says. The recovery vocabulary still owns the specific remedy, but
the row never prints that action word either: the transition says what happened
and the plain reason clause says why.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import struct
import termios
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from numbers import Real
from pathlib import Path
from typing import Any, NamedTuple

from reckon._timestamps import parse_iso, parse_utc
from reckon.crew import lane_document as _lane_document

CLOCK = 8
# The cell is 28, not 36: a name already elides from the middle, so the eight
# columns the cell gave up are spent on the reason clause, the one column a
# reader is actually trying to read. The cut below still keeps both ends of a
# truncated name, so a name that fits the narrower cell keeps its whole text and
# only the ones that already elided lose a little more of their middle.
NODE = 28
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
        "lane-event": 124,
        "stalled": 130,
        "complete": 28,
        "promoted": 22,
        "recorded": 28,
        "withdrawn": 130,
        "discarded": 130,
        "departed": 130,
        "dispatched": 241,
        "working": 26,
        "running": 26,
        "waiting": 97,
        "queued": 97,
        "wait-aged": 130,
        "unknown": 124,
        "unreadable": 124,
        "exited-unfinished": 124,
        "unwritten": 124,
        "held": 97,
        "needs-help": 124,
    },
    "dark": {
        "blocked": 203,
        "failed": 203,
        "launch-failed": 203,
        "stopped": 203,
        "abandoned": 203,
        "lane-event": 203,
        "stalled": 179,
        "complete": 78,
        "promoted": 71,
        "recorded": 78,
        "withdrawn": 179,
        "discarded": 179,
        "departed": 179,
        "dispatched": 245,
        "working": 75,
        "running": 75,
        "waiting": 104,
        "queued": 104,
        "wait-aged": 179,
        "unknown": 203,
        "unreadable": 203,
        "exited-unfinished": 203,
        "unwritten": 203,
        "held": 104,
        "needs-help": 203,
    },
}

# The hue of a word the cell spells that is not the word the fleet emits. The
# state cell shows the display form (see :data:`DISPLAY`), and the display form
# is what the renderer resolves a hue for, so the aliases carry their hue in
# their own table rather than in :data:`STATE_HUE`: that table is the source of
# :data:`CLASSIFIER_STATE_WORDS`, which names only words a producer or the
# recovery classifier can put on either side of the transition.
DISPLAY_HUE = {
    "light": {"lane event": 124, "unpromoted": 30},
    "dark": {"lane event": 203, "unpromoted": 80},
}

# Every state word the producer or recovery classifier can put on either side
# of the transition. The recovery module imports this renderer, so the display
# vocabulary cannot import its tuple without a cycle; the exhaustive property
# test binds this set to that classifier vocabulary.
CLASSIFIER_STATE_WORDS = frozenset(STATE_HUE["light"]) | frozenset(
    {
        "completed_unpromoted",
        "ended-without-manifest",
        "interrupted",
        "paused",
        "promotable",
        "ready",
        "refused-at-admission",
        "scoring",
    }
)

# A transition is two state words around one arrow. The previous word is
# right-aligned and the destination is left-aligned, which pins the arrow to one
# screen column even when either word changes length. A first sighting has no
# previous word, so the left half and arrow are blank while the destination
# keeps its column.
#
# Each half is ten columns, not the twenty-two the longest classifier word
# (ended-without-manifest) would ask for. The two halves are the widest pair of
# cells the row can fund, and giving up twelve of their columns is what buys the reason
# clause its readable width: the row's whole purpose is the explanation beside
# the transition, and a state word is decodable from its head while a clause cut
# to nothing is not. The five words wider than the half are the rare recovery
# spellings (ended-without-manifest, refused-at-admission, launch-failed,
# interrupted, completed_unpromoted); each keeps the head that distinguishes it
# and is elided the same way the model cell elides an id outside its alias set.
# Every common lifecycle state — dispatched, abandoned, blocked, working,
# unpromoted — fits whole.
STATE_WORD = 10
ARROW = "→"
ARROW_GAP = 1
STATE = STATE_WORD + ARROW_GAP + len(ARROW) + ARROW_GAP + STATE_WORD
NEW_STATE_OFFSET = STATE_WORD + ARROW_GAP + len(ARROW) + ARROW_GAP

# States a reader must act on: the ones that have stopped progressing and want
# the coordinator. An overdue wait is in the set: its external condition has not
# lifted when expected, so a reader should look at it. That reading is
# deliberately not the fleet's `blocked` number — an overdue wait is actionable
# but still counted as waiting, so the blocked bucket in recovery derives from
# this set minus the waiting family rather than from this set verbatim. One set
# serves the count and the explanation together: `unknown` once counted as
# blocked while the number said something needed attention and the line did not
# say what.
NEEDS_ACTION = frozenset(
    {
        "blocked",
        "failed",
        "launch-failed",
        "stalled",
        "stopped",
        "abandoned",
        "lane-event",
        "unknown",
        "unreadable",
        "exited-unfinished",
        "wait-aged",
    }
)

# Every destination or recovery classification whose clause may explain itself.
# A state outside this set renders a bare transition with no reason, because
# only a state a reader must act on has anything to explain. The recovery module
# imports this renderer, so the display contract cannot import its own set
# without a cycle; this is the one documented display copy, and the property
# test covers the classifier's full vocabulary. The three lift states are the
# one non-run member: a lift row carries the group, its form and multiple and
# what ended it in its clause, and no classifier emits them, so they are added
# here rather than reached for from the lift module, which would close the cycle
# this set exists to avoid.
CLAUSE_STATES = NEEDS_ACTION | frozenset(
    {
        "complete",
        "completed_unpromoted",
        "ended-without-manifest",
        "held",
        "interrupted",
        "lift-ended",
        "lift-granted",
        "lift-in-force",
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
DISPLAY = {"completed_unpromoted": "unpromoted", "lane-event": "lane event"}

# The dispatch vocabulary, verbatim. Kept here rather than derived from a
# config so that a role is known the moment it is dispatched; the word IS the
# display form, so no table or glossary has to stay in step with a new role.
DISPATCH_ROLES = frozenset(
    {"implement", "cleanup", "review", "investigate", "test", "documentation"}
)

# The role column is nine wide, the longest of the three roles a fleet node
# carries as its ordinary work (implement, review, test). The rare
# documentation and investigate roles, thirteen and eleven, are elided to the
# cell by `_display_role` exactly as the model cell elides an id outside its
# alias set: the four columns the cell gives up are spent on the reason clause,
# and a role word keeps its head while a clause cut to nothing keeps nothing.
ROLE = 9

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
    {
        "complete",
        "completed_unpromoted",
        "promoted",
        "recorded",
        "failed",
        "stopped",
        "abandoned",
        "withdrawn",
        "discarded",
    }
)

# The subset of the settled states whose inventory rows the human pane
# withholds. It is the settled set minus the states whose row is the news a
# reader attaching needs: a run that finished and is waiting to be promoted,
# one that failed, and one that was abandoned have all stopped, and each of
# them asks the coordinator for something. Withholding those rows hid work that
# only the coordinator can advance. What remains is the work that asks for
# nothing — complete and reviewed, promoted, recorded, stopped — and those rows
# stay inventory the pane does not re-show.
HIDDEN_AT_ATTACH = SETTLED_STATES - frozenset(
    {"completed_unpromoted", "failed", "abandoned"}
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

# Each counter is its count right-aligned to a fixed digit width followed by the
# bucket's letter, and one space separates each cell from the next. Right
# alignment holds every letter on its own column, so the block reads down a pane
# whatever the counts are: `1w` stacks under `12w` rather than shunting the
# letter beside it. The fixed digit width also means the cells measure the same
# whether a fleet is single-digit or two-digit, and the spare columns a smaller
# count leaves sit at the left of its own cell rather than at the block's right
# edge — so the block's width does not follow the counts and the clause after it
# keeps its column. The separating space is what stops two counts touching:
# `10w 12b` is two tokens and `10w12b` is not a shape this block prints. A count
# above the fixed width carries its extra digits and widens its own cell, and so
# its row's block, rather than eliding the figure a reader came for.
# The label is one character for three buckets and the spelled word for the
# waiting one, so the block is sized from the labels themselves rather than
# assumed at one character each. Its width is the four widest cells plus the
# three separators between them; those three columns are the price of keeping
# the counts apart, and the reason's room pays them.
STAT_DIGITS = 2
_MAX_CELLS = (*_CELLS, _WAIT_CELL)
STATS = sum(STAT_DIGITS + len(STAT_LETTER[label]) for label in _MAX_CELLS) + (
    len(_MAX_CELLS) - 1
)

# The widest the fixed columns can be, plus the stats block and one gap. A width
# below this cannot be honoured without wrapping, so it is raised to this.
# Everything before the reason consumes exactly this many columns with the role
# cell at nine, the model cell at its default width of ten (a grid sized from a
# longer configured alias raises this floor by the same amount it widens the
# cell), the effort cell at seven, the node cell at twenty-eight, each
# transition half at ten, the four fleet counters and the elapsed cell in full.
# Against the 180-column DEFAULT_WIDTH budget this leaves 57 columns for the
# reason — 85 on the 208-column pane this workstation measures — measured at the
# default model cell width of ten, and each column a longer configured alias
# adds to that cell comes straight off both figures. The narrowing that funded
# the reason is the four cells this row cut: the node cell from thirty-six to
# twenty-eight, the role cell from thirteen to nine, the transition halves from
# twenty-two to ten each, and the counter block's separators, which are single
# spaces the block keeps rather than abutting cells — the three columns they
# spend are the price of keeping two counts apart, and the reason's room
# pays them. The clause starts at column 123 on every row at the default width;
# the arrow still holds one fixed column and every boundary keeps its two-space
# gutter.
MIN_WIDTH = (
    CLOCK
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
    if text != text.strip() or text.endswith("z"):
        return text[11:19]
    moment = parse_utc(text)
    if moment is None:
        return text[11:19]
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


def _first_clause(value: Any) -> str:
    """Collapse free text to its first clause, with no width cut yet.

    A worker writes prose; the grid has a column. Cutting at the first clause
    boundary keeps the sentence that states the problem and drops the elaboration
    after it, which is what survives a hard truncation anyway — and truncating
    alone would let a second clause occupy room the first one needed.

    A clause that collapses to bare punctuation is refused outright: a parse
    failure upstream (a block-scalar indicator returned as if it were the value)
    must not render as a "reason" no reader can act on. The refusal lives here,
    where the clause is derived, so the producer never has to make it.

    The width cut is deliberately a separate step: a caller that must also drop
    a leading label or a word the state cell already says needs the whole clause
    to test, because a field already cut has already eaten the label it matches.
    """
    compact = " ".join(str(value or "").split())
    clause = re.split(
        r";|(?<=[.!?])\s+|\s+[\N{EM DASH}\N{EN DASH}]\s+", compact, maxsplit=1
    )[0].strip()
    if clause and not re.search(r"[A-Za-z0-9]", clause):
        return ""
    return clause


def _fit_clause(clause: str, limit: int) -> str:
    """Cut a clause to a field, never to a bare ellipsis.

    A clause wider than its field is cut at the last word boundary inside it, so
    what a reader keeps is a whole word: a cut through a word reads as a typo
    rather than as a cut. A clause whose first word alone is wider than the field
    has no boundary to cut at, so the cut falls inside that word and keeps as
    much of its head as the ellipsis leaves room for. It never returns just the
    ellipsis: a field one column wide returns its first character alone, because
    a reason that shows a reader nothing to start reading is worse than a word
    cut short.
    """
    if limit <= 0:
        return ""
    if len(clause) <= limit:
        return clause
    boundary = clause.rfind(" ", 0, limit)
    if boundary > 0:
        head = clause[:boundary].rstrip(" ,:")
        if head:
            return head + "…"
    if limit < 2:
        return clause[:1]
    return clause[: limit - 1].rstrip(" ,:") + "…"


def single_clause(value: Any, *, limit: int = 96) -> str:
    """Collapse free text to one clause bounded to fit a ticker field."""
    return _fit_clause(_first_clause(value), limit)


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


# The lane cell: the run's own generation rate beside the lane's mean, so the
# row carries the denominator that makes its own figure readable. A rate
# without the lane's mean cannot separate a slow worker from a slow lane — the
# row was read as the second by two coordinators independently — and a measure
# that misleads in the direction of the reader's own node is the failure this
# cell exists to prevent.
LANE_LABEL = "lane"
LANE_RATE = 5
# What the cell prints after the run's own rate when it can name no mean, one
# clause per cause. A clause rather than a zero or a bare absence marker: a
# document that has not published a figure is not a lane that measured none, and
# a reader must be able to tell the two apart. Each names its cause, because a
# word that reads as an outage is read as one — a lane whose document is merely
# late is not a lane that is down.
LANE_MEAN_UNPUBLISHED = "lane mean unpublished"
LANE_DOC_STALE = "lane doc stale"
# The clause holds one width in every state — the figure and each phrase alike —
# so the reason after it begins at one screen column whichever state the lane is
# in and the run's own figure stays put as it moves down a pane.
LANE_CLAUSE_WIDTH = max(len(LANE_MEAN_UNPUBLISHED), len(LANE_DOC_STALE))
LANE_MEAN_WIDTH = LANE_CLAUSE_WIDTH - len(LANE_LABEL) - len(" ")
LANE_WIDTH = LANE_RATE + len(" ") + len(LANE_LABEL) + len(" ") + LANE_MEAN_WIDTH
# How long a read lane document serves the pane's later rows. The lane
# republishes on its own cadence and the pane draws rows in bursts, so one
# resolution and one read cover every row inside the window rather than one of
# each per row.
LANE_DOCUMENT_REUSE_SECONDS = 15.0


def lane_rate_text(value: Any) -> str:
    """One generation rate as the pane prints it, or the absence marker.

    Two decimals below ten and one above it, so the figures a reader compares
    against each other keep the resolution that separates them while a large
    rate spends no column on a precision nothing measures; a bool and anything
    non-numeric render the absence marker, because a rate that was never taken
    is not a zero. A rate wider than any real one is folded to thousands rather
    than allowed to push the cells after it off their columns.
    """
    if isinstance(value, bool) or not isinstance(value, Real):
        return DIM_MARKER
    number = float(value)
    if number < 10:
        return f"{number:.2f}"
    if number < 1000:
        return f"{number:.1f}"
    return f"{number / 1000:.0f}k"


def lane_document_path(project: str | None, backend: str) -> str | None:
    """The lane document the row's own backend declares, or None.

    A backend declares the local JSON its lane publishes about itself, read
    here through the same four-layer resolution a dispatch takes, so the pane
    reads the document of the lane the run was actually sent through. A row
    naming no backend, a config that cannot be read and a backend declaring no
    document all yield None rather than raising: this is a display surface, and
    a pane that refused to render because a config file is malformed would be
    worse than a cell that names its own absence.
    """
    if not backend:
        return None
    try:
        from reckon import flight as flight_module
    except ImportError:
        return None
    try:
        config = flight_module.resolve(project).config
    except (OSError, ValueError, flight_module.FlightConfigError):
        return None
    backends = config.get("backends")
    if not isinstance(backends, Mapping):
        return None
    settings = backends.get(backend)
    if not isinstance(settings, Mapping):
        return None
    declared = settings.get("lane_document")
    if not isinstance(declared, str) or not declared.strip():
        return None
    return str(Path(declared).expanduser())


def lane_mean_from_text(raw: str | None, *, now: datetime) -> float | str:
    """The lane's mean generation rate, or the clause naming why none was read.

    The document is read through the lane document module's own readers — the
    same resolution a dispatch carries — so the freshness judgment and the
    throughput block are the module's rather than a spelling of its keys kept
    here. A document past its own shelf life returns the stale clause, because a
    figure read past it describes a lane that may have changed since. A document
    that is absent, unparsable, not an object, or publishing no numeric mean
    returns the unpublished clause, because the lane has not published a figure
    the cell could print. The clause is the cell's text after the run's own
    rate, clause width and all. Neither is a zero a reader would take for a
    measurement.
    """
    try:
        payload: Any = None if raw is None else json.loads(raw)
    except ValueError:
        payload = None
    document = _lane_document.read_lane_document(payload, now=now)
    if document.get("stale"):
        return LANE_DOC_STALE
    reading = _lane_document.read_lane_reading_fields(payload)
    throughput = _lane_document.read_lane_throughput(
        payload,
        reading_stamp=str(reading.get("observed_at") or ""),
        reading_age_seconds=int(document.get("age_seconds") or 0),
        now=now,
    )
    mean = throughput.get("mean_tokens_per_second")
    if isinstance(mean, bool) or not isinstance(mean, Real):
        return LANE_MEAN_UNPUBLISHED
    return float(mean)


def _display_state(state: Any) -> str:
    return elide(DISPLAY.get(str(state or ""), str(state or "")), STATE_WORD)


def _state_hue(theme: str, word: str) -> Any:
    """The hue for a word on the row, its display alias included.

    The row's state cells carry display forms, so the lookup answers for both
    tables: a classifier word finds its hue in :data:`STATE_HUE`, and a display
    alias finds its own in :data:`DISPLAY_HUE`. A word in neither renders dim.
    """
    hues = STATE_HUE[theme]
    if word in hues:
        return hues[word]
    return DISPLAY_HUE[theme].get(word, "dim")


def _display_role(role: Any) -> str:
    """The role word, or the marker.

    The whole dispatch word is the display form — no table or derivation to
    stay in step with, so a role word configured for the first time renders
    exactly as it was dispatched. The cell is nine wide, the longest of the
    roles a node carries as its ordinary work; the two rare roles wider than
    that keep their head and are elided to the cell, the same cut every fixed
    column makes, so a longer role never pushes the columns after it. A role not
    in the vocabulary renders the marker rather than a cut prefix, because an
    unrecognised word cut to fit would show a plausible-looking but wrong word,
    which is worse than an admitted unknown.
    """
    spelled = str(role or "")
    if spelled not in DISPATCH_ROLES:
        return ROLE_UNKNOWN
    return elide(spelled, ROLE)


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


def hidden_at_attach(event: Mapping[str, Any]) -> bool:
    """Whether this row is inventory the human pane withholds at attach.

    The same judgement as :func:`settled_at_attach` narrowed to
    :data:`HIDDEN_AT_ATTACH`: a settled run whose state asks the reader for
    nothing is withheld, while a settled run with a duty outstanding — waiting
    to be promoted, failed, abandoned — reaches the pane. A row that is not
    inventory is never withheld here.
    """
    if not is_baseline(event):
        return False
    return str(event.get("to_state") or "") in HIDDEN_AT_ATTACH


# ── The follower's row policy ───────────────────────────────────────────────
#
# The 2026-09-25 signal audit (docs/research/follower-signal-audit.html) sorts
# every transition kind a follower can emit into four classes. A *coordinator*
# row is a transition that leaves the orchestrator a duty, so it always prints,
# carrying the plain reason that says what the duty is. An *observer* row
# explains the shape of the fleet without creating work, so it prints unmarked.
# A *counter* row has no transition, so it moves the standing counter alone and
# never prints. A *noise* row is a re-derivation of a state already on screen or
# one side of a launch flicker, an exit phantom or a stall flap, and is dropped.
#
# The three noise pairs are held rather than blacklisted, because a blacklist
# would also hide a genuine abandonment, block or stall that lacks its recovery
# evidence. The row that opens a pair is withheld for a hold window; if the pair
# completes inside that window both sides are the noise and neither prints, and
# otherwise the opener prints late. A stall that does not recover, an
# abandonment that does not revert and a block whose worker survives are
# therefore still coordinator-visible.

ROW_COORDINATOR = "coordinator"
ROW_OBSERVER = "observer"
ROW_COUNTER = "counter"

# The transition kinds the audit classes as coordinator: the row owes a duty
# once the noise hold has been applied. A kind absent here is observer context.
#
# The list is the audit's own, in two parts. Every kind the census table
# (docs/research/follower-signal-audit.html, the policy class column) marks
# ``coordinator`` is here, and so are the three kinds that open a held noise
# pair — they are classed ``drop`` in that column, but the policy section says a
# block, a stall or an abandonment that lacks its recovery evidence stays
# coordinator-visible, and the hold is exactly what decides that. A kind the
# audit does not name belongs to its catch-all rule rather than to this set, so
# the set is pinned against the document rather than grown by taste.
COORDINATOR_KINDS = frozenset(
    {
        ("abandoned", "complete"),
        ("abandoned", "unreadable"),
        ("blocked", "complete"),
        ("complete", "blocked"),
        ("complete", "completed_unpromoted"),
        ("complete", "stalled"),
        ("completed_unpromoted", "blocked"),
        ("completed_unpromoted", "complete"),
        ("dispatched", "abandoned"),
        ("dispatched", "blocked"),
        ("dispatched", "complete"),
        ("dispatched", "completed_unpromoted"),
        ("dispatched", "stalled"),
        ("dispatched", "waiting"),
        ("unreadable", "blocked"),
        ("unreadable", "complete"),
        ("waiting", "blocked"),
        ("working", "blocked"),
        ("working", "complete"),
        ("working", "completed_unpromoted"),
        ("working", "stalled"),
        ("working", "unreadable"),
        ("working", "waiting"),
    }
)

# The holdable noise pairs, keyed by the kind that opens them: the resolution
# kind that completes the pair, and the window the opener is withheld for. The
# launch window is the grace a dispatch gets before its first abandonment is
# believed; the exit and stall windows are the span in which the completing
# receipt is looked for. A pair whose two sides sit further apart than its
# window is a real event and the opener prints.
NOISE_PAIRS: dict[tuple[Any, Any], tuple[tuple[str, str], float]] = {
    ("dispatched", "abandoned"): (("abandoned", "working"), 90.0),
    ("working", "blocked"): (("blocked", "completed_unpromoted"), 300.0),
    ("working", "stalled"): (("stalled", "working"), 300.0),
}

# A noise pair's resolution on its own carries no duty: the opener already told
# the reader the run left, and this side only closes it.
NOISE_RESOLUTIONS = frozenset(resolution for resolution, _ in NOISE_PAIRS.values())

# ── The arrival hold ─────────────────────────────────────────────────────────
#
# A launch opens the pane with an arrival row and then, within the same minute,
# the run's first transition out of ``dispatched``. The arrival says nothing the
# dispatch payload did not already carry — every run opens with it — so the pair
# costs the pane two rows where one suffices. The arrival is held like a noise
# opener, but its pair collapses to the transitioning row rather than to
# nothing: the resolving row already reads ``dispatched -> <state>``, so keeping
# it prints one row where the launch printed two. The window is the same grace
# the launch flicker gets: an arrival that has not moved by its end prints late,
# because a run still sitting in ``dispatched`` is a duty the pane must show.
ARRIVAL_KIND: tuple[Any, Any] = (None, "dispatched")
# The state an arrival opens from. A resolution is any row whose left side is
# this and whose destination is anything else; a rewrite that repeats it is not
# a move out and leaves the hold standing.
ARRIVAL_STATE = "dispatched"
ARRIVAL_WINDOW = 90.0

# ── The settle hold ───────────────────────────────────────────────────────────
#
# A run reaches a settled state through a chain of rows written seconds apart —
# ``working -> exited-unfinished -> blocked`` within two seconds,
# ``blocked -> wait-aged -> blocked`` within three to eight. Each link is a
# coordinator row on its own, so a pane that printed them one by one would draw
# a chain the reader has to reassemble. A printing coordinator row is therefore
# held for the settle window, and a later coordinator row of the same run inside
# it supersedes the opener: the chain prints once, as its latest row. A later
# row the policy holds back — the terminal echo ``complete -> recorded`` is the
# usual one — is not a coordinator row, so it supersedes nothing and the opener
# still prints. A run that changes faster than the settle window would otherwise
# never clear the hold, so the settle cap bounds the wait from the chain's first
# held row: past the cap the latest row prints whatever else is arriving.
SETTLE_WINDOW = 20.0
SETTLE_CAP = 120.0


def _arrival_resolution(event: Mapping[str, Any]) -> Mapping[str, Any]:
    """A resolving row rewritten to carry the arrival's own left side.

    When the arrival's hold passes to the run's first transition and a held
    flicker then resolves, the row that prints is that resolution wearing the
    arrival's left side, so the launch reads ``dispatched -> <state>`` rather
    than the flicker's ``abandoned -> <state>``.
    """
    resolved = dict(event)
    resolved["from_state"] = ARRIVAL_STATE
    return resolved


def transition_class(from_state: Any, to_state: Any) -> str:
    """Whether a transition is a coordinator duty, observer context or a counter.

    The audit assigns the class per kind, after the noise predicate. The noise
    kinds answer here as coordinator or observer, because whether they print is
    decided by the hold and not by the kind alone; a caller wants this for the
    row it is about to hand to ``RowPolicy``, which is where the hold is applied.
    """
    if from_state is not None and from_state == to_state:
        return ROW_COUNTER
    if (from_state, to_state) in COORDINATOR_KINDS:
        return ROW_COORDINATOR
    return ROW_OBSERVER


# A withheld row, awaiting the event that decides its fate. ``kind`` is which
# hold the entry is: an arrival awaiting its first transition, a noise opener
# awaiting its pair, or a coordinator row awaiting the rest of its chain.
# ``first_held`` is the moment the row's settle chain began, carried across the
# supersessions so the settle cap measures from the chain and not from its
# latest row. The other kinds ignore it.
class _Hold(NamedTuple):
    deadline: float
    opener: Mapping[str, Any]
    carries_arrival: bool
    kind: str
    first_held: float


class RowPolicy:
    """Decide which rows reach the pane, holding back the ones that ask for none.

    One instance follows one pane. Three rules stack. A row that opens an
    arrival or a noise pair is held for its own measured window, so a launch
    flicker never reaches the pane. A coordinator row is held for the settle
    window, so a chain written seconds apart prints once as its latest row. And
    a row that carries no duty — observer context that only traces the fleet's
    shape — is held back altogether, unless it is the news the reader is
    waiting for: a recovery from a duty the pane already printed, an
    unexplained end, or the resolution of a held noise pair into an action
    state. A pane built with ``show_observer`` prints observer context as it
    did before the hold existed, and applies no settle hold.
    """

    def __init__(self, *, show_observer: bool = False) -> None:
        self._show_observer = bool(show_observer)
        self._reported: dict[str, str] = {}
        # run_id -> the withheld row and its hold. A run carries one hold at
        # most: a second hold for the same run supersedes the first where the
        # settle rule says so, and is otherwise handled on its own account.
        self._held: dict[str, _Hold] = {}
        # run_id -> whether the pane has been told something actionable for the
        # run since its arrival. A recovery is news only once a duty was shown,
        # and an unexplained end is news only while none has been.
        self._told: dict[str, bool] = {}

    def seed(self, run_id: str, state: str) -> None:
        """Remember a state the pane already shows, before this pane's first row.

        A re-arm restores the states the previous pane drew, so the policy must
        start from that memory: without it a re-derived baseline for a run the
        reader has already seen would print as a fresh transition, which is the
        noise the counter-only rule exists to keep off the pane.
        """
        if run_id and state:
            self._reported[str(run_id)] = str(state)
            if self._lands_in_an_action_state({"to_state": str(state)}):
                self._told[str(run_id)] = True

    def _row_class(self, event: Mapping[str, Any], kind: tuple[Any, Any]) -> str:
        """The class of a row: coordinator, observer or counter.

        The audit assigns the class per kind, but a kind outside
        :data:`COORDINATOR_KINDS` that the classifier named actionable still
        needs the coordinator: ``working -> exited-unfinished`` and its kin do
        not appear in the audit's list, and growing that list by hand is what
        the classifier exists to avoid. The classifier owns "needs the
        coordinator", so its actionable verdict is read here rather than
        restated.
        """
        cls = transition_class(kind[0], kind[1])
        if cls == ROW_OBSERVER and (
            self._actionable(event) or self._lands_in_an_action_state(event)
        ):
            return ROW_COORDINATOR
        return cls

    @staticmethod
    def _lands_in_an_action_state(event: Mapping[str, Any]) -> bool:
        """Whether the row lands the run in a state the surface calls an action.

        A run finished behind a held block arrives as ``completed_unpromoted``
        while its classification is not one the classifier calls actionable, and
        the promotion the reader owes the run is the news that row must carry.
        The watch surface already owns the vocabulary that decides this, so it is
        read here rather than restated.
        """
        from reckon.crew.runs import WATCH_ATTENTION_STATES

        return str(event.get("to_state") or "") in WATCH_ATTENTION_STATES

    @staticmethod
    def _actionable(event: Mapping[str, Any]) -> bool:
        """Whether the row's own classification names a coordinator duty."""
        from reckon.crew.recovery import ACTIONABLE_RECOVERY_CLASSIFICATIONS

        return (
            str(event.get("recovery_classification") or "")
            in ACTIONABLE_RECOVERY_CLASSIFICATIONS
        )

    def _asks_for_coordinator(
        self, event: Mapping[str, Any], kind: tuple[Any, Any]
    ) -> bool:
        """Whether a row lands the run in a state a coordinator acts on.

        A noise pair's resolution prints only when it lands in an action state.
        That is a wider test than the row's own classification: a run that
        finished behind a held block arrives as the state ``completed_unpromoted``
        while its classification is not one the classifier calls actionable, and
        the promotion the reader owes the run is the news the pair must carry.
        """
        return self._row_class(event, kind) == ROW_COORDINATOR

    def _note_printed(
        self, run_id: str, event: Mapping[str, Any], *, observed: bool = False
    ) -> None:
        """Record what a released row showed, so a later re-derivation is silent.

        The state is remembered for every row the pane is shown, and the run is
        marked told when the row carried a duty — a coordinator row, or an
        observer row released on one of its exceptions.
        """
        if not run_id:
            return
        to_state = str(event.get("to_state") or "")
        self._reported[run_id] = to_state
        carries_duty = (
            observed
            or self._row_class(event, (event.get("from_state"), to_state))
            == ROW_COORDINATOR
        )
        if carries_duty:
            self._told[run_id] = True

    def _release_expired(self, now: float, out: list[Mapping[str, Any]]) -> None:
        """Print every held row whose window has passed uncompleted."""
        for run_id in [r for r, hold in self._held.items() if hold.deadline <= now]:
            hold = self._held.pop(run_id)
            out.append(hold.opener)
            self._note_printed(run_id, hold.opener)

    def _observer_exception(self, event: Mapping[str, Any], run_id: str) -> bool:
        """Whether an observer row prints despite the hold.

        Two kinds do. A recovery — the run leaves a state into a live one after
        the pane printed a duty for it — is the news a reader waiting on a
        repair looks for. And an unexplained end — the run reaches a settled
        state without the pane ever having shown a duty for it — is the only
        notice that a run which never asked for anything has stopped.
        """
        to_state = str(event.get("to_state") or "")
        told = bool(self._told.get(run_id))
        if told and to_state not in SETTLED_STATES:
            return True
        return to_state in SETTLED_STATES and not told

    def _hold_settle(
        self,
        event: Mapping[str, Any],
        run_id: str,
        now: float,
        chain_start: float,
    ) -> list[Mapping[str, Any]]:
        """Hold a coordinator row, or print it when its chain has run too long.

        The row is held for the settle window so a later coordinator row of the
        same run supersedes it. The cap is measured from the chain's first held
        row, so a run changing faster than the window still prints within the
        cap. ``chain_start`` is that first row's moment.
        """
        if now >= chain_start + SETTLE_CAP:
            self._held.pop(run_id, None)
            self._note_printed(run_id, event)
            return [event]
        deadline = min(now + SETTLE_WINDOW, chain_start + SETTLE_CAP)
        self._held[run_id] = _Hold(deadline, event, False, "settle", chain_start)
        self._told[run_id] = True
        return []

    def _resolve_arrival(
        self,
        out: list[Mapping[str, Any]],
        event: Mapping[str, Any],
        run_id: str,
        kind: tuple[Any, Any],
        now: float,
    ) -> list[Mapping[str, Any]]:
        """Resolve a held arrival with the run's first transition out of ``dispatched``.

        A noise opener leaving dispatched takes the hold on, so the launch flicker
        still collapses into whatever resolves it. A coordinator row prints as
        the arrival collapsed into it, wearing the arrival's left side. A move
        into an observer kind prints nothing: the launch said nothing the
        dispatch call had not already returned.
        """
        pair = NOISE_PAIRS.get(kind)
        if pair is not None:
            _, window = pair
            self._held[run_id] = _Hold(now + window, event, True, "noise", 0.0)
            return out
        if self._row_class(event, kind) == ROW_COORDINATOR:
            resolved = _arrival_resolution(event)
            out.append(resolved)
            self._note_printed(run_id, resolved)
            return out
        if self._show_observer:
            out.append(event)
            self._note_printed(run_id, event)
            return out
        if run_id:
            self._reported[run_id] = str(event.get("to_state") or "")
        return out

    def feed(self, event: Mapping[str, Any], *, now: float) -> list[Mapping[str, Any]]:
        """Take one delivered row and return the rows that should print for it."""
        out: list[Mapping[str, Any]] = []
        self._release_expired(now, out)

        run_id = str(event.get("run_id") or "")
        from_state = event.get("from_state")
        to_state = str(event.get("to_state") or "")
        kind = (from_state, to_state)

        # A settle hold stands until a coordinator row supersedes it. A row that
        # is not a coordinator row leaves the hold in place and is judged on its
        # own account below, so the terminal echo supersedes nothing.
        chain_start: float | None = None
        held = self._held.get(run_id, None) if run_id else None
        if held is not None and held.kind == "settle":
            if self._row_class(event, kind) == ROW_COORDINATOR:
                chain_start = held.first_held
                self._held.pop(run_id, None)
                held = None
            else:
                held = None

        if held is not None:
            self._held.pop(run_id, None)
            opener = held.opener
            opener_kind = (opener.get("from_state"), opener.get("to_state"))
            resolution = NOISE_PAIRS.get(opener_kind)
            if resolution is not None and kind == resolution[0]:
                # The pair completed inside the window: both sides are the noise
                # the hold exists to drop, unless the resolution lands in an
                # action state — a run finishing behind a held block reaches the
                # coordinator as the net change and nothing else would carry it.
                resolved = (
                    _arrival_resolution(event)
                    if held.carries_arrival
                    else {**event, "from_state": opener.get("from_state")}
                )
                if run_id:
                    self._reported[run_id] = to_state
                if self._asks_for_coordinator(event, kind):
                    out.append(resolved)
                    self._note_printed(run_id, resolved, observed=True)
                return out
            if opener_kind == ARRIVAL_KIND:
                if kind == ARRIVAL_KIND:
                    # A second arrival — a re-arm re-deriving the baseline — is
                    # not a move out, so the hold stands and the launch still
                    # collapses to its first transition rather than printing an
                    # extra arrival on the way.
                    self._held[run_id] = held
                    return out
                if from_state == ARRIVAL_STATE and to_state != ARRIVAL_STATE:
                    # A transition out of dispatched resolves the arrival: a
                    # noise opener takes the hold on, a coordinator row prints
                    # as the arrival collapsed into it, and a move into an
                    # observer kind prints nothing.
                    return self._resolve_arrival(out, event, run_id, kind, now)
                if from_state is not None and from_state == to_state:
                    # A rewrite that repeats dispatched is not a move out, so
                    # the arrival keeps its hold and the counter moves alone.
                    self._held[run_id] = held
                    if run_id:
                        self._reported[run_id] = to_state
                    return out
                # The run moved without a dispatched left side: the arrival is
                # stale, so it prints late and this row is handled below.
                out.append(opener)
            else:
                # The pair did not complete, so the opener was a real event
                # after all; it prints late, handled on this row's own account.
                out.append(opener)

        # A same-state rewrite carries no transition, so it moves the counter
        # block alone and never prints a row.
        if from_state is not None and from_state == to_state:
            if run_id:
                self._reported[run_id] = to_state
            return out

        # A row restating the state the pane already shows is a
        # re-derivation rather than news, so it is silent — unless it declares
        # an external wait, whose condition the coordinator must be able to
        # read even when its state word repeats. A suppressed duty is the
        # failure this policy exists to prevent, so the exemption is on the
        # duty's own evidence rather than on the kind alone.
        if (
            run_id
            and self._reported.get(run_id) == to_state
            and not declares_a_wait(event)
        ):
            return out

        if run_id and kind == ARRIVAL_KIND:
            # The run's first sighting in dispatched: held so its own row and
            # the transition out of dispatched print as one.
            self._held[run_id] = _Hold(
                now + ARRIVAL_WINDOW, event, True, "arrival", 0.0
            )
            return out

        if run_id and kind in NOISE_PAIRS:
            _, window = NOISE_PAIRS[kind]
            self._held[run_id] = _Hold(now + window, event, False, "noise", 0.0)
            return out

        cls = self._row_class(event, kind)
        if cls == ROW_COORDINATOR:
            if self._show_observer or not run_id:
                out.append(event)
                self._note_printed(run_id, event)
                return out
            out.extend(
                self._hold_settle(
                    event, run_id, now, chain_start if chain_start is not None else now
                )
            )
            return out

        # Observer context: held back, unless it is one of the exceptions, and
        # printed as before on a pane that does not hold it.
        if self._show_observer or not run_id:
            out.append(event)
            self._note_printed(run_id, event)
            return out
        if self._observer_exception(event, run_id):
            out.append(event)
            self._note_printed(run_id, event, observed=True)
            return out
        self._note_printed(run_id, event)
        return out

    def flush(self, *, now: float) -> list[Mapping[str, Any]]:
        """Print every opener still held whose window has closed by ``now``."""
        out: list[Mapping[str, Any]] = []
        self._release_expired(now, out)
        return out


class PaneRowPath:
    """The follower's own row path: which of its events reach the pane.

    One instance follows one pane, and it is the whole of what a follower does
    between an event it read and a row a reader sees: the follower's own
    selection, the row policy that decides which rows print, and the memory of
    the states the pane was actually shown. The follower holds one of these, and
    a replay drives the same class, so the rows a measurement counts are the
    rows the pane prints and not a second implementation's opinion of them.

    The memory is written when a row prints, never when one is withheld: a held
    opener has not reached the pane, so a re-arm must re-evaluate it rather than
    read it as delivered. That is why the memory lives here, at the point a row
    is released, and not at the point a row is selected.
    """

    def __init__(
        self,
        *,
        selects: Callable[[Mapping[str, Any]], bool] | None = None,
        reported: Mapping[str, str] | None = None,
        show_observer: bool = False,
    ) -> None:
        self._selects = selects
        self._show_observer = bool(show_observer)
        self.reported: dict[str, str] = {
            str(run_id): str(state) for run_id, state in dict(reported or {}).items()
        }
        self._policy = RowPolicy(show_observer=self._show_observer)
        for run_id, state in self.reported.items():
            self._policy.seed(run_id, state)

    def remember(self, run_id: Any, state: Any) -> None:
        """Note a state the pane already shows, keeping any memory it has.

        A re-arm restores what the pane drew from the log when the checkpoint is
        gone, and a run the checkpoint does name must keep the checkpoint's
        word, so this never overwrites.
        """
        key, value = str(run_id or ""), str(state or "")
        if not key or not value or key in self.reported:
            return
        self.reported[key] = value
        self._policy.seed(key, value)

    def reseed(self, reported: Mapping[str, str] | None) -> None:
        """Replace the remembered states, as a continuation restores them."""
        self.reported = {
            str(run_id): str(state) for run_id, state in dict(reported or {}).items()
        }
        self._policy = RowPolicy(show_observer=self._show_observer)
        for run_id, state in self.reported.items():
            self._policy.seed(run_id, state)

    def _remember(self, rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """Record each released row's state, then hand the rows on unchanged."""
        printed = list(rows)
        for row in printed:
            run_id = str(row.get("run_id") or "")
            if run_id:
                self.reported[run_id] = str(row.get("to_state") or "")
        return printed

    def feed(
        self, event: Mapping[str, Any] | None, *, now: float
    ) -> list[Mapping[str, Any]]:
        """The rows the pane receives for one event this follower read."""
        if event is None:
            return []
        if self._selects is not None and not self._selects(event):
            return []
        return self._remember(self._policy.feed(event, now=now))

    def flush(self, *, now: float) -> list[Mapping[str, Any]]:
        """Openers whose hold window has closed, released to the pane."""
        return self._remember(self._policy.flush(now=now))


def row_moment(event: Mapping[str, Any]) -> float:
    """The epoch a row carried, which is what a replay measures windows against.

    A stamp that names no zone reads as a local wall clock here, the epoch a
    replay measures its windows against on the machine doing the replay; a
    stamp naming a zone reads as that instant. Whether a stamp names a zone is
    read from the parsed result's ``tzinfo`` rather than from the text's shape,
    so every offset spelling an ISO-8601 parser accepts is honoured. A stamp
    whose own text is not spelled strictly, or names nothing, contributes no
    moment.
    """
    text = str(event.get("observed_at") or "")
    if text != text.strip() or text.endswith("z"):
        return 0.0
    zoned = parse_iso(text)
    if zoned is None:
        return 0.0
    moment = parse_utc(text)
    if moment is None:
        return 0.0
    if zoned.tzinfo is not None:
        return moment.timestamp()
    return moment.replace(tzinfo=None).astimezone().timestamp()


def replay_row_policy(
    events: Iterable[Mapping[str, Any]],
    *,
    clock=row_moment,
    selects: Callable[[Mapping[str, Any]], bool] | None = None,
    show_observer: bool = False,
) -> list[Mapping[str, Any]]:
    """Print an ordered stream the way one follower's own row path would.

    The audit's done-when replays a recorded watch stream, so the measurement is
    taken through :class:`PaneRowPath` — the same object a live follower feeds —
    rather than through the policy alone: a row the follower would never have
    offered the policy is not a row this can count. ``selects`` is that
    follower's own selection, and a caller measuring a real follower passes the
    same selection function the follower was built with.
    """
    path = PaneRowPath(selects=selects, show_observer=show_observer)
    printed: list[Mapping[str, Any]] = []
    for event in events:
        printed.extend(path.feed(event, now=clock(event)))
    printed.extend(path.flush(now=float("inf")))
    return printed


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
        # The lane's mean over the last read of its document, by project and
        # backend: the pane draws rows in bursts and the lane republishes on its
        # own cadence, so one read serves every row inside the reuse window. A
        # None is a backend that declared no lane document, and a phrase names
        # why a declared document yielded no figure; both are held like any
        # other reading rather than re-reading the lane per row to ask again.
        self._lane_means: dict[str, tuple[datetime, float | str | None]] = {}

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

    def _lane_reading(self, project: str | None, backend: str) -> float | str | None:
        """The lane's mean, the phrase naming why none was read, or None.

        The mean is a fact about the lane rather than about the run, so the row
        takes it from the document the lane publishes — located through the
        row's own resolved configuration and read through the lane document
        module's readers — rather than from a field its watch event does not
        carry. The read is held for a short window, because a pane draws rows in
        bursts and the lane republishes on its own cadence: one resolution and
        one read serve every row drawn inside the window.

        None means the row's backend declares no lane document at all: there is
        no lane for the cell to speak about, so it prints the run's own rate
        with no lane clause rather than naming the absence of a lane the row
        never used. A clause string means a document was declared and read, and
        names why it yielded no figure.
        """
        key = f"{project or ''}\0{backend}"
        now = datetime.now(UTC)
        held = self._lane_means.get(key)
        if held is not None and (now - held[0]).total_seconds() < (
            LANE_DOCUMENT_REUSE_SECONDS
        ):
            return held[1]
        path = lane_document_path(project, backend)
        reading: float | str | None = None
        if path is not None:
            try:
                raw: str | None = Path(path).read_text(encoding="utf-8")
            except OSError:
                raw = None
            reading = lane_mean_from_text(raw, now=now)
        self._lane_means[key] = (now, reading)
        return reading

    def _lane_cells(self, event: Mapping[str, Any]) -> list[tuple[str, Any]]:
        """The lane cell: the run's own rate, and the lane's mean beside it.

        The run's own figure is rendered whether or not the lane answered: it is
        what the run did on the lane, and withholding it because the lane's
        document could not be read would cost the reader the row's own
        measurement as well. Beside it stands the lane's mean, or the phrase
        naming why no mean could be read — a rate alone is the reading this cell
        exists to replace, so the two travel together or the second says why it
        is missing. Each phrase names its cause rather than an absence that
        reads as an outage.

        A row naming no backend adds no cell at all: the document is declared
        per backend, so such a row has no lane to read and keeps the exact shape
        it rendered before. A backend that declares no lane document carries the
        run's own rate and no lane clause, because there is no lane to read and
        so no cause to name.
        """
        backend = str(event.get("backend") or "").strip()
        if not backend:
            return []
        project = str(event.get("project") or "").strip() or None
        rate = lane_rate_text(event.get("spend_generation_rate"))
        reading = self._lane_reading(project, backend)
        if reading is None:
            return [(" " * GAP, None), (f"{rate:>{LANE_RATE}}", "dim")]
        if isinstance(reading, str):
            text = f"{rate:>{LANE_RATE}} {reading:<{LANE_CLAUSE_WIDTH}}"
            return [(" " * GAP, None), (text, "dim")]
        figure = f"{LANE_LABEL} {lane_rate_text(reading):<{LANE_MEAN_WIDTH}}"
        return [(" " * GAP, None), (f"{rate:>{LANE_RATE}} {figure}", None)]

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
        """One transition as one line, at most ``width`` visible characters.

        ``with_session`` is accepted for call-site compatibility. Ownership is
        no longer a row column: the follower already scopes delivery, while an
        extra glyph between the time and model would make the lead's reading
        order conditional.

        Everything before the reason is fixed-width, in the reading order set
        by the pane: time, model, effort, role, node, transition, elapsed and
        fleet counters. A row rendered wider than the pane can spare loses
        trailing free text and nothing else. No separate attention mark is
        printed: the destination state's colour and its clause carry the
        reading order's whole answer, so the model cell follows the time
        directly with only the two-column gutter between them.
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
        # The previous state a reader is shown is the remembered one when this
        # grid has seen the run, and the event's own ``from_state`` otherwise.
        # The fallback is what lets a row survive a re-arm that starts with no
        # memory. It is also what makes the suppression below a comparison
        # against the *effective* previous state rather than the remembered one
        # alone: a run seen for the first time after it moved arrives carrying
        # the state it left, and printing ``X → X`` when that equals the state
        # it entered claims a transition that never happened. Only a row that
        # arrived on the stream is suppressed: an event carrying no kind is a
        # render probe rather than a transition claim, so it draws the row its
        # caller asked for.
        from_state = reported
        if from_state is None:
            from_state = _display_state(event.get("from_state"))
        if stream_event and from_state == to_state:
            return ""
        if stream_event:
            self._reported[run_id] = to_state

        role = _display_role(event.get("role"))
        model_cell, effort_cell = _model_and_effort(event)

        cells: list[tuple[str, Any]] = [
            (f"{local_clock(event.get('observed_at')):<{CLOCK}}", "dim"),
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
                    (f"{from_state:>{STATE_WORD}}", _state_hue(self.theme, from_state)),
                    (" " * ARROW_GAP, None),
                    (ARROW, "dim"),
                    (" " * ARROW_GAP, None),
                ]
            )
        else:
            cells.append((" " * NEW_STATE_OFFSET, None))
        cells.extend(
            [
                (f"{to_state:<{STATE_WORD}}", _state_hue(self.theme, to_state)),
                (" " * GAP, None),
            ]
        )
        cells.extend(self._spend_cells(event, to_state))
        cells.append((" " * GAP, None))
        cells.extend(self._stats(event))
        grid_width = self._grid_width(model_width)
        head = sum(len(text) for text, _ in cells)
        # The lane cell is an addition the row makes only where the pane has the
        # room for it. At its narrowest the grid holds the fixed columns and
        # nothing else, and a cell appended past that edge would wrap the row,
        # which costs a quarter of the visible history. The cell spends the
        # clause's margin and never the pane's edge.
        if head + GAP + LANE_WIDTH <= grid_width:
            cells.extend(self._lane_cells(event))
        head = sum(len(text) for text, _ in cells)

        # The clause is the row's last column and nothing follows it, so it is
        # bounded by the room the fixed columns leave after the separator that
        # introduces it, and it is not right-padded into it: a pad would buy no
        # alignment and only overrun the pane the row is read in. Every column
        # ahead of the clause keeps its fixed width, so the fixed columns'
        # offsets are unchanged; the row's visible width is its content, at most
        # the grid width. The separator travels with the clause, so a row with
        # nothing to explain ends on its last fixed glyph rather than on painted
        # blanks. A width that leaves the reason nothing renders the fixed
        # columns alone. The clause is one row and never wraps, so a bound on
        # its length is a bound on the row.
        room = max(grid_width - head - GAP, 0)
        reason = self._reason(event, entry_state, room)
        if reason:
            cells.append((" " * GAP, None))
            cells.append((reason, "dim"))
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
        printed: the destination state's colour says whether the coordinator
        must look, while this clause says what happened.
        """
        explained = CLAUSE_STATES | {
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
            # This glyph is a measurement fact: it says the declared probe
            # never ran, so the wait's condition cannot lift on its own.
            marker = UNPROBED_MARKER
        reserve = len(marker) + (1 if marker else 0)
        room_for_clause = max(0, room - reserve)
        # The label is dropped before the field cuts, not after: a clause whose
        # first word repeats the state cell is stripped to its explanation, and
        # if the cut came first the field could have already eaten everything
        # past that word — leaving an empty reason where a shorter one belonged.
        # Cutting last is what lets a clause longer than its field keep its head
        # instead of collapsing to the ellipsis alone.
        state_word = _display_state(entry_state)
        clause = _fit_clause(
            self._strip_repeated_label(_first_clause(detail), state_word),
            room_for_clause,
        )
        if not clause and entry_state == LAUNCH_FAULT_STATE:
            # The blocked bucket is where a launch failure's number arrives, so
            # the clause is the only place the row can say the stop is an
            # infrastructure fault. The classifier's cause normally fills it;
            # this names the fault itself for a record that carried the state
            # without one, rather than rendering a number with no reason.
            clause = single_clause(LAUNCH_FAULT_CLAUSE, limit=room_for_clause)
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

        Each counter is its count right-aligned to :data:`STAT_DIGITS` digits
        followed by the initial of the bucket it counts, so the letter is
        decodable from the bucket's own name rather than from a legend the
        stream does not carry, and the letter of every bucket holds one screen
        column whatever the counts are. One space separates each cell from the
        next, so two counts never touch: ``10w 12b`` is two tokens. Right
        alignment is what lets both hold at once — a single-digit count pads
        inside its own cell rather than shifting the letter beside it or
        widening the block, so the block measures the same for any counts up to
        the fixed width and the clause after it never moves. A count above the
        fixed width carries its extra digits and widens its own cell, and so its
        row's block, rather than eliding the figure a reader came for. A zero is
        dimmed rather than dropped: blanking it would leave trailing whitespace
        and take the right edge ragged, and a reader
        waiting for a drain needs to see the count reach zero, not see it
        disappear.

        The block carries no age itself: the count alone cannot separate a
        backlog being worked from one standing still — the same figure reads
        identically while a stalled item is cleared and a fresh one takes its
        place — and how long the oldest member has waited belongs to the
        surface a reader goes to for it rather than to a row.
        """
        cells: list[tuple[str, Any]] = []
        # Every bucket the fleet can show renders on every row, so the block
        # never changes shape when a run is queued and the columns after it
        # never move. A zero is dimmed rather than dropped: a reader watching a
        # drain needs to see the count reach zero rather than see the cell
        # disappear and the row's right edge go ragged. The count is right
        # aligned to a fixed digit width and one space separates each cell from
        # the next, so a single-digit fleet's spare columns sit inside its own
        # cell rather than at the block's right edge: the letters keep their
        # columns across one- and two-digit counts, two counts stay apart, and
        # the block measures the same for any counts up to the fixed width, so
        # the clause after it keeps its column. A count above the fixed width
        # carries its extra digits instead of eliding the figure.
        for label in _MAX_CELLS:
            if cells:
                cells.append((" ", None))
            count = int(event.get(_COUNT_FIELD.get(label, label)) or 0)
            cells.append(
                (
                    f"{count:>{STAT_DIGITS}}{STAT_LETTER[label]}",
                    None if count else "dim",
                )
            )
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
