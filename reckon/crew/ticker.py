"""Render one fleet transition as a line of a fixed grid.

The pane this feeds is read down a column rather than across a line — which
job, what state, how many still running — and it shows roughly eight lines at
a time. Two consequences shape everything here. Every field occupies the same
screen column on every row, so a scan does not have to re-find it. And no line
ever wraps, because a wrapped row costs a quarter of the visible history; a row
wider than the pane drops its trailing cells rather than overrun it.

Colour carries two questions. *Which worker is this?* is answered by the node's
own hue, handed out in order of first appearance. *Does this need me?* is
answered by the destination state, painted by the verdict that state names.
Identity is kept perceptually clear of the four verdict hues, so a worker's
colour is never mistaken for a verdict about that worker; the reading order is
time, stage, job, transition, elapsed and fleet counters, then the lane's rate,
the model and the effort.

The row carries no free text. A reason clause and a roll-up of live job names
were both tried after the counters, and each pushed the cells after it off
their columns and repeated what the pane or the run already says; the reason a
run stopped is read from the run, where it is never cut to fit. The row prints
no separate attention mark either: the destination state's colour already
carries urgency, so a mark only repeats what the new state says.
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
from reckon.crew.recovery_vocabulary import (
    PLAN_REVIEW_NODE_PREFIX,
    REVIEW_NODE_PREFIX,
)

CLOCK = 8
# The job cell is 28 wide. A name wider than that is cut from the middle, so
# the end that distinguishes it from the rest of its wave survives the cut, and
# a name that fits keeps its whole text.
NODE = 28
# A job the pane has drawn under more than one run — a retry, a redispatch, a
# second review of one node — carries a number after its name, 1 for the first
# run the pane drew and counting up, so a reader can tell the runs apart and
# read their order. A run an admission pass launched from a queued request is
# that request continuing, so it keeps the request's number rather than taking
# the next one.
#
# The stage cell says what kind of work the job is, so a review's job cell
# drops what its node id spends saying the same thing: the review prefix and
# the reviewed head's digest. A review of ``jccm-circuits`` reads ``review``
# then ``jccm-circuits``, and a second review of it reads ``jccm-circuits 2``.
REVIEW_JOB_PREFIXES = (
    (PLAN_REVIEW_NODE_PREFIX, "plan-"),
    (REVIEW_NODE_PREFIX, ""),
    ("review-", ""),
)
REVIEWED_HEAD = re.compile(r"-[0-9a-f]{12}$")
# Model and effort are two cells at the row's right end, after the lane's rate,
# so a reader scans the effort down a column instead of parsing it out of a
# composed label. Every boundary uses the same two-column gutter. The model
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

# The hue belongs to the word the pane prints, including the left-only arrival.
DISPLAY_HUE = {
    "light": {
        "queued": 97,
        "working": 26,
        "waiting": 97,
        "halted": 124,
        "delivered": 30,
        "in review": 30,
        "landed": 22,
        "dropped": 130,
        "new": 241,
    },
    "dark": {
        "queued": 104,
        "working": 75,
        "waiting": 104,
        "halted": 203,
        "delivered": 80,
        "in review": 80,
        "landed": 71,
        "dropped": 179,
        "new": 245,
    },
}

# Every state word the producer or recovery classifier can put on either side
# of the transition. The recovery module imports this renderer, so the display
# vocabulary cannot import its tuple without a cycle; the exhaustive property
# test binds this set to that classifier vocabulary.
CLASSIFIER_STATE_WORDS = frozenset(STATE_HUE["light"]) | frozenset(
    {
        "awaiting-upstream",
        "blocked-upstream",
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

# A transition aligns its previous and destination words around one arrow.
# Every printed state fits whole in a ten-column half. The first row uses
# `new` on the left; the reason clause retains the internal distinction.
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

# The four states a broadcast row carries. Such a row is the one row this module
# keys on a name rather than a run id: the producer builds it with no run id and
# the group in ``node``. The words are spelled here rather than read from the
# lift module, which imports the recovery watch, which imports this renderer —
# reading them back would close an import cycle. The fixtures in ``lift_watch``
# bind them.
LIFT_GRANTED = "lift-granted"
LIFT_IN_FORCE = "lift-in-force"
LIFT_ENDED = "lift-ended"
LIMIT_RESET = "limit-reset"
LIFT_STATES = frozenset({LIFT_GRANTED, LIFT_IN_FORCE, LIFT_ENDED, LIMIT_RESET})
LIFT_STAGE = "lift"
LIMIT_STAGE = "limit"

# What a limit-reset row says, in place of the transition it maps to. A reset
# is a broadcast about a budget group rather than a state change of a run, and
# the pane word it lands on — ``working`` — carries none of what a reader needs:
# ``limit <group> new → working`` gives no sign that the row is a usage-limit
# reset or that it asks a stopped session to continue. The row is drawn as its
# own clause instead — what it is, the group, the clock it resets, and what it
# asks of the session — with the announcer's reason after it when the record
# carries one.
RESET_HEADLINE = f"{LIMIT_STAGE} reset"
RESET_INSTRUCTION = "orchestrator sessions may continue"

# The facts the clause reads out of the sentence the producer composes. That
# sentence names the group, the clock, the instant, who announced it and why,
# and rides the row's free-text field, which the renderer drops for every other
# row because no other row needs it. The clock is printed beside the group and
# the announcer's reason at the end, so the pane reads those two facts apart
# from the sentence. A sentence matching no pattern costs the row neither of
# them silently: the headline, the group and the instruction come from the
# row's own fields, so an unrecognised sentence narrows the new clause rather
# than blanking the row.
RESET_CLOCK_CLAUSE = re.compile(r"\([^()]*,\s*(?P<clock>[^(),]+)\)")
RESET_REASON_CLAUSE = re.compile(r"announced by [^:]*:\s*(?P<reason>[^;]+)")


def limit_reset_text(event: Mapping[str, Any]) -> str:
    """One limit-reset row as the single clause a reader acts on.

    The group is the row's own ``node``: a broadcast row carries no run, and the
    producer puts the budget group there.
    """
    clause = str(event.get("detail") or "")
    parts = [RESET_HEADLINE]
    group = str(event.get("node") or "").strip()
    if group:
        parts.append(group)
    clock = RESET_CLOCK_CLAUSE.search(clause)
    if clock:
        parts.append(clock.group("clock").strip())
    parts.append(RESET_INSTRUCTION)
    reason = RESET_REASON_CLAUSE.search(clause)
    if reason:
        parts.append(reason.group("reason").strip())
    return " · ".join(part for part in parts if part)


# The internal state the row policy writes onto a review run's launch row, so
# the pane draws the reviewed run as being in review. It is a pane word of its
# own rather than a producer state: the reviewed run's own state never changes
# while its review runs.
IN_REVIEW_STATE = "in-review"

# The sole state vocabulary printed by the pane. Recovery distinctions and
# capacity-lift causes stay in the event's own facts.
PANE_STATES = {
    "queued": frozenset({"queued"}),
    "working": frozenset(
        {
            "dispatched",
            "starting",
            "running",
            "working",
            "lift-granted",
            "lift-in-force",
            "limit-reset",
        }
    ),
    # A request held on a section it waits for is not working and not halted: it
    # sits in the waiting pane until its upstream lands, and is never released
    # by a lane slot. One whose wait can no longer land asks for the decision a
    # blocked run asks for, so it sits beside blocked.
    "waiting": frozenset({"waiting", "paused", "wait-aged", "awaiting-upstream"}),
    "halted": frozenset(
        {
            "blocked-upstream",
            "blocked",
            "needs-help",
            "held",
            "stalled",
            "stopped",
            "abandoned",
            "failed",
            "unreadable",
            "unwritten",
            "exited-unfinished",
            "ended-without-manifest",
            "launch-failed",
            "refused-at-admission",
            "ready",
            "interrupted",
            "lane-event",
            "unknown",
        }
    ),
    "delivered": frozenset(
        {
            "scoring",
            "completed_unpromoted",
            "promotable",
            "complete",
            "unpromoted",
        }
    ),
    # A delivered run whose review is running. No producer state says this:
    # the run is still ``complete`` and it is the review run's own start that
    # tells the pane the review began, so the row policy writes this word onto
    # that launch. It has to be a pane word of its own because neither
    # ``delivered`` nor ``working`` describes a run whose review is underway.
    "in review": frozenset({IN_REVIEW_STATE}),
    "landed": frozenset({"promoted", "recorded", "lift-ended"}),
    "dropped": frozenset({"withdrawn", "discarded", "departed"}),
}

# ── What the Monitor stream carries ───────────────────────────────────────────
#
# A line the follower prints reaches its coordinator through the session host as
# a notification that starts a turn, so the stream carries a row when it names
# something the coordinator oversees: a duty it must discharge, or a state
# change of the work it is running. Five pane words carry a row below, a launch
# and the in-review row carry their own facts, and a limit reset and the
# follower's own end are carried by their own facts too:
#
#   delivered      a run of this session delivered and unpromoted;
#   halted         a run halted, which is blocked, failed, stalled, or carried
#                  an unreadable manifest;
#   in review      a run delivered whose review has started, drawn under the
#                  reviewed run's node and naming the review run;
#   landed         a run that reached a written record — recorded or promoted,
#                  carried once per run whoever caused the landing;
#   dropped        a run withdrawn, discarded or departed, carried once per run
#                  whether the coordinator or a peer caused the drop;
#   queued-refused a queued run refused at launch for a reason the coordinator
#                  must clear, carrying the two words above and the remedy the
#                  refusal names;
#   review         a review finding that needs the coordinator, which is a run
#                  halted as above with a blocking finding or a dimension below
#                  the floor;
#   limit-reset    a limit reset reaching this session;
#   lift           a budget lift granted or ending that covers the session,
#                  drawn once per pane as it already is: the lead grants a lift
#                  in one session and relies on every session hearing it, so a
#                  coordinator can dispatch to the lane it opens and stop when
#                  it ends;
#   declared-wait  a wait a worker addresses to the coordinator, read from the
#                  declaration's own facts rather than from the state word;
#   follower-end   the follower ending with no session host to take over.
#
# A launch is carried by its own marker rather than by its word, because its
# word is ``working`` and every ``working``/``waiting`` churn row shares it. The
# row policy sets that marker only on the first row of an attempt, for a run the
# pane's own session sent, and only on a path that releases a launch; a
# re-announcement, a replay, a reload baseline and a peer session's start carry
# nothing.
#
# Every other row stays in ``crew view`` and the pane's history and is not
# printed: a ``working``/``waiting`` transition, a bare re-announcement or
# replay of a start, a re-arm or reload baseline, and a reload notice. The row
# policy and the summary lines read the rule from here rather than restating it,
# so a row the stream withholds is withheld by one decision.
CARRIED_PANE_WORDS = frozenset({"delivered", "halted", "in review"})

# The pane words of a run whose work is over: a written record, or a drop. A
# landing is news only when the pane itself drew the delivery before it, and a
# drop only when the pane drew the run at all: a withdrawal the coordinator
# made, a terminal echo after a reload, and a discard of a run the pane never
# showed are all its own acts and print nothing. So the words are not carried
# on the word alone; the row policy marks the one it released, the same way it
# marks a launch.
LANDING_PANE_WORDS = frozenset({"landed", "dropped"})

# The row field the row policy sets on a landing or a drop it releases as news.
LANDING_FIELD = "pane_landing"

# How old a start row may be and still be a launch the pane witnesses. A row
# stamped longer ago than this is history the pane is being handed — a replay
# of a gap in the stream, or a re-derivation after a reload — rather than work
# beginning now, so it draws no launch. Wide enough that an arrival released by
# its own expiry is still one.
LAUNCH_HORIZON = 120.0

# The row field the row policy sets on a launch it releases. It travels on the
# row rather than in a word because a launch's word is ``working``, which every
# churn row shares.
LAUNCH_FIELD = "pane_launch"


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

_CELLS = ("working", "halted", "delivered")
_WAIT_CELL = "waiting"

# The event still carries the historical count-field keys. The visible buckets
# use pane words; a waiting count includes queued and externally waiting runs.
_COUNT_FIELD = {"halted": "blocked", "delivered": "unpromoted", "waiting": "waiting"}

# Each counter is labelled with the lower-case initial of a pane word. The
# waiting bucket holds queued runs as well as runs waiting on a declared
# condition, and its label is ``q`` for the queued word, so no two labels share
# a letter and none needs a case to tell it apart.
STAT_LETTER = {
    "working": "w",
    "halted": "h",
    "delivered": "d",
    "waiting": "q",
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
# `10w 12h` is two tokens and `10w12h` is not a shape this block prints. A count
# above the fixed width carries its extra digits and widens its own cell, and so
# its row's block, rather than eliding the figure a reader came for.
# Each label takes one character. The block's width includes the three spaces
# between its four cells, leaving the reason on the same column on every row.
STAT_DIGITS = 2
_MAX_CELLS = (*_CELLS, _WAIT_CELL)
STATS = sum(STAT_DIGITS + len(STAT_LETTER[label]) for label in _MAX_CELLS) + (
    len(_MAX_CELLS) - 1
)

# The fixed head every row carries: time, stage, job, transition, elapsed and
# the four counters, each separated by the two-column gutter. A width below this
# cannot be honoured without wrapping, so it is raised to this. The lane's rate,
# the model and the effort follow the head in that order, each added only where
# the pane has the room for it, so a narrow pane loses the effort first and the
# rate last and never wraps.
MIN_WIDTH = (
    CLOCK
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
)
# The order of the row's cells, carried into the follower's layout signature so
# a reorder alone is a different layout even where every width is unchanged.
COLUMN_ORDER = (
    "clock",
    "stage",
    "job",
    "transition",
    "elapsed",
    "counters",
    "rate",
    "model",
    "effort",
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
        return "delivered"
    if word in FLEET_BLOCKED_STATES:
        return "halted"
    if word in FLEET_WAITING_STATES:
        return _WAIT_CELL
    if word in FLEET_WORKING_STATES:
        return "working"
    return None


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


# The rate cell: the mean generation rate the run's lane publishes, with its
# unit. The run's own rate is not shown beside it: it is unmeasured on nearly
# every row, so the cell would spend a column on an absence marker. The figure
# is right-aligned to a fixed width so the unit holds one column down the pane.
LANE_RATE = 5
RATE_UNIT = "tok/s"
RATE = LANE_RATE + len(" ") + len(RATE_UNIT)
# Why a declared lane document yielded no figure. The cell prints the absence
# marker for either; the cause stays with the reader of the document.
LANE_MEAN_UNPUBLISHED = "lane mean unpublished"
LANE_DOC_STALE = "lane doc stale"
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
    internal = str(state or "")
    return next(
        (word for word, states in PANE_STATES.items() if internal in states), ""
    )


def _remembered_word(state: Any) -> str:
    """The pane word for a state a grid's memory is seeded with.

    A grid remembers the state it last put on a run's row and reads the next
    row's left word straight from that memory, without mapping it. The follower
    seeds that memory on attach and reload from a remembered map merged out of a
    checkpoint and stored history, so the memory can hold a producer state
    rather than a pane word. It is mapped here, where the memory is taken in, so
    a run live across a re-arm shows its pane word on the left.

    A state that is already one of the pane's words maps to itself. Five of
    them — ``delivered``, ``halted``, ``in review``, ``landed`` and ``dropped``
    — are not also internal states, so ``_display_state`` blanks them; the
    identity is therefore taken first rather than falling through to the blank.
    """
    spelled = str(state or "")
    if spelled in PANE_STATES:
        return spelled
    return _display_state(spelled)


def _state_hue(theme: str, word: str) -> Any:
    """The hue of the pane's state word, or dim for a non-state marker."""
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

# ── The start row ─────────────────────────────────────────────────────────────
#
# The transition that opens a run's work: the row saying it left ``dispatched``
# and began. The audit classes it observer context — it asks the coordinator for
# nothing — so the pane held it back entirely, and a session with work in flight
# showed nothing at all between a run's launch and its end. The pane has two
# readers, and the person watching wants to see the work while it runs, so this
# one observer kind is delivered for the pane's own runs. It is delivered for the
# own session only: a peer session's start, a bare re-announcement of an
# already-running run, an echo and a replay are still held, so nothing arrives
# for work that is not this pane's.
#
# A run reaches ``working`` from ``dispatched`` on a fresh launch, from
# ``starting`` on a relaunched attempt, and from any stop state the pane drew as
# ``halted`` when an attempt is resumed. All announce the same news — the run
# began doing the work — so all are starts. ``START_KIND`` is the fresh-launch
# pair a reader names when it means the canonical start; ``RowPolicy`` reads the
# set when it decides whether a row is a start. The stop states are read from
# the pane's own vocabulary rather than listed here, so every state the pane
# draws ``halted`` — ``blocked``, ``exited-unfinished``, ``stalled``,
# ``abandoned`` and the rest — pairs with ``working`` and a new one is a start
# the moment it is drawn. The states the pane draws ``delivered`` or ``landed``
# are excluded, because a producer re-reading a rewritten manifest as working
# (`delivered -> working`) is not resumed work and stays held.
START_KIND: tuple[Any, Any] = ("dispatched", "working")
START_KINDS: frozenset[tuple[Any, Any]] = frozenset(
    {START_KIND, ("starting", "working")}
) | frozenset((state, "working") for state in PANE_STATES["halted"])

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

# The pane words of a run that is still being worked on. A later row that moves
# a run into one of these words resolves the run's held settle row on that row
# instead of leaving it to its window: a return to the word the pane shows drops
# the held row with the return, and a move to another live word prints the held
# row at once, ahead of the move. A held row is never released stale after the
# run has moved on. A settled destination, such as the terminal echo
# ``complete -> recorded``, is not one of these words.
LIVE_PANE_WORDS = frozenset({"working", "waiting", "queued"})

# The pane words of a run whose work has finished. A run the pane draws in one
# of these has stopped producing, so a later row that moves it into a live word
# is a producer re-reading a rewritten record rather than work resuming. The
# departure hold measures a round trip out of these words as it measures one out
# of the queued word: a run that leaves the word the pane shows and returns to
# it inside the settle window is the rewrite alone, and the pane draws neither
# half.
FINISHED_PANE_WORDS = frozenset({"delivered", "landed"})


def _stamped_moment(event: Mapping[str, Any]) -> float:
    """The epoch a row's own ``observed_at`` names, or zero when it names none.

    A stamp that carries no zone is read as UTC here rather than as a local
    wall clock: this measures how old a row is against the reader's own clock,
    and reading a zoneless stamp as local would shift it by the host's offset.
    """
    parsed = parse_iso(str(event.get("observed_at") or ""))
    if parsed is None:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


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


# A departure held for the round trip: the row that took a run away from the
# word the pane last showed, the word it left, and the moment its window closes.
# Only a row the pane would otherwise draw is ever held here, so the hold can
# subtract a row and never add one. A run that returns to that word before the
# deadline drops the departure with the return; one that does not is delivered
# when the window passes, exactly as it would have been delivered unheld.
class _Departure(NamedTuple):
    deadline: float
    event: Mapping[str, Any]
    word: str


def _pane_word(event: Mapping[str, Any]) -> str:
    """The pane word a row's destination draws, as the renderer reads it.

    A cause-specific recovery word the producer supplied for a stop or an
    unproducible wait stands in for the raw destination exactly as the renderer
    substitutes it, so a caller judges a row by the word the pane would show
    rather than by the event's own field.
    """
    typed = str(event.get("recovery_classification") or "")
    entry = (
        typed
        if typed in {"held", "needs-help", "unwritten"}
        else str(event.get("to_state") or "")
    )
    return _display_state(entry)


def stream_carries(event: Mapping[str, Any]) -> bool:
    """Whether this row is a line the Monitor stream carries.

    A carried line names something the coordinator oversees: a duty it must
    discharge, or a state change of the work it is running. One of the pane
    words below, a launch the row policy marked, a limit reset, a budget lift
    granted or ending that covers the session, a declared wait a worker
    addresses to the coordinator, or the follower's own end. A row about
    anything else — a ``working`` or ``waiting`` transition that asks nothing
    and opens no attempt, a bare re-announcement or replay of a start, a re-arm
    baseline, a reload notice — carries no such news, so it is left to ``crew
    view`` and the pane's history and is not printed.

    This is the rule the row policy applies to every row it would release, and
    the rule the follower's summary lines read before naming anything, stated
    once here so the stream and the summaries cannot disagree about what is news.
    """
    if event.get("status_repair") and _pane_word(event) == "halted":
        # A transition into a halted word that a coordinator's own ``crew
        # repair --status`` caused: the coordinator wrote the word itself, so
        # the row carries nothing it does not already know — the same reading a
        # ``landed`` or ``dropped`` row from its own merge or discard is given.
        # The repair is a duty the coordinator discharged, not one it owes, so
        # it is left to ``crew view`` and the pane history and prints nothing.
        # A repair not followed by a resume still leaves the run in the
        # needs-you summary, so nothing actionable is lost with the row.
        #
        # Only the halted word is withheld. This rule removes the row a halt
        # produces, not every row a repair causes: a repair that lands a run on
        # the other carried word, a delivery, is judged as any other row is, so
        # the rule cannot silence a delivery it did not itself complete.
        return False
    to_state = str(event.get("to_state") or "")
    if to_state == LIMIT_RESET or to_state in LIFT_STATES:
        return True
    if declares_a_wait(event):
        return True
    if event.get(LAUNCH_FIELD):
        # The first row of an attempt the pane's own session sent. The policy
        # set this only where it released a launch, so the session limit and
        # the once-only judgement are already made and this stays agnostic.
        return True
    if event.get(LANDING_FIELD):
        # A landing or a drop the policy judged news: the delivery it follows
        # was printed by this pane, or the run was drawn before it ended. The
        # mark carries that judgement so this rule stays a function of the row.
        return True
    if _pane_word(event) in CARRIED_PANE_WORDS:
        return True
    return (
        transition_class(event.get("from_state"), event.get("to_state"))
        == ROW_COORDINATOR
    )


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

    def __init__(
        self,
        *,
        show_observer: bool = False,
        own_session: str | None = None,
        observed_sessions: Iterable[str] = (),
        lift_groups: Iterable[str] = (),
        lift_claim: Callable[[str, str], bool] | None = None,
    ) -> None:
        self._show_observer = bool(show_observer)
        # The session this pane follows. A start row is delivered for this
        # session's own runs alone; a pane with no named session — a fleet-wide
        # reader — holds every start as it held every observer row.
        self._own_session = str(own_session) if own_session else None
        # The sessions this pane was asked to observe for oversight. Observing a
        # session is a request to see its rows, so an observed session's rows are
        # exempt from the observer-context hold. The launch-flicker and settle
        # holds still apply to them, because those decide when a row prints, not
        # whether it is this pane's to print.
        self._observed_sessions = frozenset(
            str(name) for name in observed_sessions if name
        )
        self._reported: dict[str, str] = {}
        # run_id -> the word the pane last actually showed for the round trip's
        # judgement, written only when a row prints or the pane is seeded. A
        # withheld row never sets it, so a departure is measured against "the
        # word the pane last showed for it". Not the state a held row left.
        self._shown: dict[str, str] = {}
        # run_id -> the wait facts of the last row this pane showed for it, or
        # None where that row declared no wait (:func:`wait_facts`). The pane's
        # word cannot separate a wait aging from a wait recovering, because
        # both draw ``waiting``, so the same-word rule reads this beside the
        # word: a row that repeats the word and only clears the overdue flag
        # with the declaration otherwise unchanged is a re-derivation rather
        # than news.
        self._shown_wait: dict[str, tuple[Any, ...] | None] = {}
        # run_id -> the word of the last row this pane actually carried. A
        # landing is news only where that word is the delivery it follows, and
        # a drop only where there is one at all, so a withdrawal, a terminal
        # echo after a reload and a discard of a run this pane never drew all
        # print nothing. Seeded memory does not write it: the pane was told the
        # word before this image existed, not by it.
        self._printed: dict[str, str] = {}
        # run_id -> a departure held for the round trip. A run that leaves the
        # shown word and returns to it inside the settle window drops the
        # departure with the return, so the pane draws neither half.
        self._departures: dict[str, _Departure] = {}
        # run_id -> the withheld row and its hold. A run carries one hold at
        # most: a second hold for the same run supersedes the first where the
        # settle rule says so, and is otherwise handled on its own account.
        self._held: dict[str, _Hold] = {}
        # run_id -> whether the pane has been told something actionable for the
        # run since its arrival. A recovery is news only once a duty was shown,
        # and an unexplained end is news only while none has been.
        self._told: dict[str, bool] = {}
        # The lift groups this pane has drawn as in force. A lift row carries no
        # run id, so the run-keyed memory cannot hold it; the group in ``node``
        # is the key. A grant records a group, an end forgets it, and a baseline
        # in-force row draws only for a group not yet drawn — so a reloaded lift
        # watcher re-announcing a standing lift adds no row. The groups a
        # previous image drew are handed in here, so the memory survives the
        # reload as well as the watcher's own restart.
        # The groups a previous image drew, or none when the carrier named no
        # lift; the set is built from whatever was handed in.
        self._lift_groups: set[str] = {
            str(group) for group in (lift_groups or ()) if group
        }
        # One grant is one event, but the followers of a pane each write the same
        # pane, and every project's producer sweeps the whole lift document into
        # its own stream, so the same grant reaches this pane once per project
        # followed. ``lift_claim`` is the shared, on-disk claim (per pane, lift
        # id and transition) that lets whichever follower reaches a lift row
        # first take it and the rest draw nothing. The pane's key is the host
        # that spawned the followers when they have one, and the follower's own
        # session otherwise, so a host following two sessions draws each lift
        # once between them. ``None`` draws every lift row on this pane's own
        # memory alone, the behaviour before the claim.
        self._lift_claim = lift_claim

    @property
    def lift_groups(self) -> frozenset[str]:
        """The lift groups this pane has drawn as in force."""
        return frozenset(self._lift_groups)

    def seed_lift_group(self, group: str) -> None:
        """Remember a lift group the pane already shows as in force.

        The restore path for a memory read from elsewhere — a reload's carried
        checkpoint, or the stored history a re-arm reads — so the producer's
        re-arm baseline row for a group already drawn adds no row.
        """
        name = str(group or "")
        if name:
            self._lift_groups.add(name)

    def seed(self, run_id: str, state: str) -> None:
        """Remember a state the pane already shows, before this pane's first row.

        A re-arm restores the states the previous pane drew, so the policy must
        start from that memory: without it a re-derived baseline for a run the
        reader has already seen would print as a fresh transition, which is the
        noise the counter-only rule exists to keep off the pane.
        """
        if run_id and state:
            self._reported[str(run_id)] = str(state)
            self._shown[str(run_id)] = _remembered_word(state)
            if self._lands_in_an_action_state({"to_state": str(state)}):
                self._told[str(run_id)] = True

    def _note_shown(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Record what the pane showed for each run a printed row names.

        The round-trip rule measures a departure against the word the pane
        actually shows, and the same-word wait rule measures a return against
        the declaration that was shown, so both are written where rows are
        released rather than where they are noted — a withheld row has reached
        nobody, and reading it as shown would suppress a real departure later
        and make a withheld wait look like the one the reader saw.
        """
        for event in rows:
            run_id = str(event.get("run_id") or "")
            if run_id:
                self._shown[run_id] = _pane_word(event)
                self._shown_wait[run_id] = wait_facts(event)

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

    def _is_own_start(self, event: Mapping[str, Any], kind: tuple[Any, Any]) -> bool:
        """Whether this row is the start of a run the pane's own session sent.

        The pane's own session is the only fleet whose starts it delivers: a
        peer session's start is work this reader does not own, and the pane's
        silence about it is what keeps another session's history off this one's
        line. A pane with no named session owns no run, so no start qualifies.
        Every start pair qualifies — a fresh launch's ``dispatched -> working``,
        a relaunched attempt's ``starting -> working`` and a resume's move out of
        any stop state into ``working`` — because each announces the same news,
        that the run began.
        """
        if self._own_session is None:
            return False
        if str(event.get("session") or "") != self._own_session:
            return False
        return kind in START_KINDS

    def _is_own_launch(self, event: Mapping[str, Any], kind: tuple[Any, Any]) -> bool:
        """Whether this row opens an attempt of a run the pane's session sent.

        A launch is the first row of an attempt that reaches the ``working``
        pane word: the arrival a fresh dispatch opens with, and the start pair
        a relaunch or a resume writes. The arrival counts because a run still
        sitting in ``dispatched`` when the arrival's window closes is the
        launch the pane must show. Inventory is never a launch: a reload
        baseline describes a run already on screen, so a re-arm prints none.
        """
        if is_baseline(event):
            return False
        if self._own_session is None:
            return False
        if str(event.get("session") or "") != self._own_session:
            return False
        return kind in START_KINDS or kind == ARRIVAL_KIND

    def _launch_row(
        self,
        event: Mapping[str, Any],
        kind: tuple[Any, Any],
        run_id: str,
        now: float,
    ) -> Mapping[str, Any] | None:
        """The row a launch prints, or None when this row is not one.

        The attempt's own memory decides once-only: a run the pane already
        shows ``working`` has had its launch drawn, so a replayed or
        re-announced start adds nothing and a resume, which leaves a word the
        pane draws ``halted``, prints its own.

        A review run's launch is drawn under the reviewed run's node as the
        reviewed run's ``in review`` row, naming the review run in its own
        field. The launch is still the review run's, and the memory that stops
        it printing twice stays keyed on it.

        """
        if not self._is_own_launch(event, kind):
            return None
        # The fresh-launch pair belongs to a run this pane has not drawn. A run
        # it already shows did not begin under it: the row is a re-derivation
        # after a reload, or a producer rewriting the state it had recorded, and
        # neither opens an attempt the reader is watching start. A resume, whose
        # pair leaves a word the pane draws ``halted``, is a launch either way.
        if (
            kind == START_KIND
            and run_id
            and (
                run_id in self._shown
                or _remembered_word(self._reported.get(run_id)) == "working"
            )
        ):
            return None
        stamp = _stamped_moment(event)
        if stamp and now - stamp > LAUNCH_HORIZON:
            return None
        if run_id and self._shown.get(run_id) == "working":
            return None
        reviewed = str(event.get("reviewed_run_id") or "")
        if reviewed:
            return {
                **event,
                "run_id": reviewed,
                "node": str(event.get("reviewed_node") or ""),
                "to_state": IN_REVIEW_STATE,
                # The row replaces the reviewed run's first working row, so it
                # leaves the word the pane last showed for that run rather than
                # the review's own: a reader drawing the row alone reads the
                # same transition the grid draws from its memory.
                "from_state": self._shown.get(reviewed) or event.get("from_state"),
                "review_run_id": run_id,
                LAUNCH_FIELD: True,
            }
        return {**event, LAUNCH_FIELD: True}

    def _release_launch(
        self,
        out: list[Mapping[str, Any]],
        event: Mapping[str, Any],
        kind: tuple[Any, Any],
        run_id: str,
        now: float,
    ) -> bool:
        """Append the launch row for this event when it is one. True if it did."""
        row = self._launch_row(event, kind, run_id, now)
        if row is None:
            return False
        out.append(row)
        self._note_launch(run_id, row)
        return True

    def _note_launch(self, run_id: str, row: Mapping[str, Any]) -> None:
        """Record a released launch against the attempt that opened.

        The word is ``working`` because that is what the attempt began. The key
        is the run that launched, so a review run's launch — drawn as the
        reviewed run's row — is remembered on the review run, and a second
        review of the same run is a different launch that still prints.
        """
        key = str(row.get("review_run_id") or run_id or "")
        if key:
            self._reported[key] = "working"
            self._shown[key] = "working"

    def _is_observed(self, event: Mapping[str, Any]) -> bool:
        """Whether this row belongs to a session the pane was asked to observe.

        Observing a session is a request to see its rows, so its rows are this
        pane's to print however the classifier classes the kind — a bare
        ``working`` row with no left side included. A pane observing nothing
        answers False for every row, so the exemption is inert until a caller
        names a session.
        """
        if not self._observed_sessions:
            return False
        return str(event.get("session") or "") in self._observed_sessions

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
        return _actionable_recovery(event)

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

    def _hold_departure(
        self, event: Mapping[str, Any], run_id: str, now: float
    ) -> bool:
        """Hold a row that takes a run away from the word the pane last showed.

        The round trip is the whole of the news: a run that leaves the word the
        pane drew for it and comes back to that word inside the settle window
        shows the reader nothing they did not already have. This is called only
        where the policy has already decided the row would print — after the
        observer hold has judged it — because the hold may subtract a row the
        pane would draw and must never make a hidden row visible. A row that
        asks the coordinator for something is never held: that news is the row's
        own, whatever word the run left.

        The hold covers a departure into a settled state or the dropped word,
        the withdrawal it was written for, and a departure out of a finished
        word. A run the pane drew as delivered or landed is one whose worker
        holds a written record, and a producer that re-reads it as working is
        reading a manifest the worker rewrote mid-turn — not resumed work. Both
        halves of that pair cancel, so neither reaches the pane.
        """
        shown = self._shown.get(run_id) if run_id else None
        word = _pane_word(event)
        if not run_id or shown in (None, word) or self._actionable(event):
            return False
        if (
            str(event.get("to_state") or "") not in SETTLED_STATES
            and word != "dropped"
            and shown not in FINISHED_PANE_WORDS
        ):
            return False
        self._departures[run_id] = _Departure(now + SETTLE_WINDOW, event, shown)
        return True

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
        """Print every held row whose window has passed uncompleted.

        A held arrival that expires is a launch, so it passes the same
        own-session test every other release path applies: a peer session's
        arrival, and a reload baseline, leave here unmarked and are withheld by
        :func:`stream_carries` exactly as they would have been on the row's own
        path.
        """
        for run_id in [r for r, hold in self._held.items() if hold.deadline <= now]:
            hold = self._held.pop(run_id)
            opener = hold.opener
            kind = (opener.get("from_state"), opener.get("to_state"))
            if self._release_launch(out, opener, kind, run_id, now):
                continue
            out.append(opener)
            self._note_printed(run_id, opener)
        # A departure that never returns is a real one, so it is delivered once
        # its window passes, exactly as it would have been delivered unheld.
        for run_id in [
            r for r in list(self._departures) if self._departures[r].deadline <= now
        ]:
            departure = self._departures.pop(run_id)
            out.append(departure.event)
            self._note_printed(run_id, departure.event)

    def _observer_exception(self, event: Mapping[str, Any], run_id: str) -> bool:
        """Whether an observer row prints despite the hold.

        Two kinds do. A recovery — the run leaves a state into a live one after
        the pane printed a duty for it — is the news a reader waiting on a
        repair looks for. An unexplained end — the run reaches a settled
        state without the pane ever having shown a duty for it — is the only
        notice that a run which never asked for anything has stopped. And a
        landing or a drop is the run's own state change, carried once where
        this pane drew the delivery before it: the coordinator oversees the
        work whether or not it was owed something when the work finished, but a
        landing after a reloaded pane, a withdrawal and a discard of a run this
        pane never drew are all its own acts and stay held.
        """
        if _pane_word(event) in LANDING_PANE_WORDS:
            return self._landing_is_news(event)
        to_state = str(event.get("to_state") or "")
        told = bool(self._told.get(run_id))
        if told and to_state not in SETTLED_STATES:
            return True
        return to_state in SETTLED_STATES and not told

    def _landing_is_news(self, event: Mapping[str, Any]) -> bool:
        """Whether this landing or drop is a state change this pane oversaw.

        A landing follows a delivery: it is news where the last word this pane
        carried for the run was that delivery, and silence otherwise — a
        reloaded pane already showed the completion, so the echo is inventory.
        A drop is news where the pane carried the run at all: a run this pane
        never drew is the coordinator's own discard, and a ``withdrawn``
        departure is its own withdrawal rather than a drop.
        """
        run_id = str(event.get("run_id") or "")
        if not run_id:
            return False
        if str(event.get("to_state") or "") == "withdrawn":
            return False
        if _pane_word(event) == "landed":
            return self._printed.get(run_id) == "delivered"
        return bool(self._printed.get(run_id))

    def _mark_carried(
        self, rows: Iterable[Mapping[str, Any]]
    ) -> list[Mapping[str, Any]]:
        """Mark and keep the rows this pane carries, recording what it showed.

        The landing mark is applied here rather than where the row is built,
        because the judgement is about what the pane already printed and only
        this pass knows it. Every row carried writes the memory a later landing
        or drop is judged against.
        """
        out: list[Mapping[str, Any]] = []
        for row in rows:
            marked = row
            if _pane_word(row) in LANDING_PANE_WORDS and self._landing_is_news(row):
                marked = {**row, LANDING_FIELD: True}
            if not stream_carries(marked):
                continue
            out.append(marked)
            # An in-review row is drawn under the reviewed run's id but is the
            # review's own state change, so it must not overwrite the word the
            # pane holds for the run it reads: that word decides whether the
            # reviewed run's landing is news.
            run_id = str(marked.get("review_run_id") or marked.get("run_id") or "")
            if run_id:
                self._printed[run_id] = _pane_word(marked)
        return out

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
        if self._is_own_start(event, kind):
            # The pane's own run left dispatched into working: the launch's hold
            # collapses into this one row, worn with the arrival's left side, so
            # the reader sees the work begin rather than the run's end alone.
            resolved = _arrival_resolution(event)
            self._release_launch(out, resolved, kind, run_id, now)
            return out
        if self._show_observer:
            out.append(event)
            self._note_printed(run_id, event)
            return out
        if run_id:
            self._reported[run_id] = str(event.get("to_state") or "")
        return out

    def feed(self, event: Mapping[str, Any], *, now: float) -> list[Mapping[str, Any]]:
        """Take one delivered row and return the rows that should print for it.

        The pass decides how a row is held, and the last word is
        :func:`stream_carries`': a row the holds released that carries no duty
        the coordinator must act on is dropped here, so the stream shows a launch
        or a ``working``/``waiting`` churn to nobody while ``crew view`` still
        draws it. The memory of what printed is written from the carried rows
        alone, so a withheld row is never read as shown. ``_feed`` keeps its own
        ``_reported`` bookkeeping, which is what makes a later re-derivation of a
        withheld row silent.
        """
        out = self._mark_carried(self._feed(event, now=now))
        self._note_shown(out)
        return out

    def _feed(self, event: Mapping[str, Any], *, now: float) -> list[Mapping[str, Any]]:
        """The policy's own pass over one row; ``feed`` records what printed."""
        out: list[Mapping[str, Any]] = []
        self._release_expired(now, out)
        # The rows this pass has already released are what the pane shows for
        # their runs, so the judgement a later row in the same pass makes reads
        # them: a resume released behind its own halt is not a replay.
        self._note_shown(out)

        run_id = str(event.get("run_id") or "")
        from_state = event.get("from_state")
        to_state = str(event.get("to_state") or "")
        kind = (from_state, to_state)

        # A departure is held while the run is away. A row that lands the run
        # back in the word the pane last showed cancels the hold and drops both
        # halves; a row that asks for the coordinator while the run is away is
        # real news, so the departure prints ahead of it and the row is handled
        # below; anything else the run does while away is drawn as the round
        # trip's middle, which the pane is not shown.
        departure = self._departures.pop(run_id, None) if run_id else None
        if departure is not None:
            if _pane_word(event) == departure.word:
                return out
            if self._row_class(event, kind) != ROW_COORDINATOR and not self._actionable(
                event
            ):
                self._departures[run_id] = departure
                return out
            out.append(departure.event)
            self._note_printed(run_id, departure.event)

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
            elif _pane_word(event) in LIVE_PANE_WORDS:
                # The run has moved on to a live pane word, so the held settle
                # row is stale: releasing it when its window closed would draw
                # the pane a halt for a run a later row has already shown
                # working, waiting or queued. The hold is resolved here, on the
                # row that moved the run on, rather than left to expire.
                self._held.pop(run_id, None)
                opener = held.opener
                held = None
                if _pane_word(event) == self._shown.get(run_id):
                    # The run came back to the word the pane last showed for it,
                    # so the held row and this return are one round trip and
                    # neither is drawn — exactly as the departure rule drops a
                    # run that leaves its shown word and returns to it.
                    if run_id:
                        self._reported[run_id] = to_state
                    return out
                # Live somewhere else, the held row prints at once, ahead of
                # this row, and this row is judged with the run already told.
                out.append(opener)
                self._note_printed(run_id, opener)
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
                # stale, so it prints late and this row is handled below. It is
                # still a launch, so it takes the same own-session test the
                # resolving path applies; a peer's stale arrival is nobody's
                # launch and leaves unmarked.
                self._release_launch(out, opener, opener_kind, run_id, now)
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

        # A row whose destination draws the word the pane already shows for its
        # run is a re-derivation rather than news, so it is withheld — unless
        # :func:`same_word_row_is_news` names it news, because it carries a duty
        # whose condition the coordinator must be able to read even when its
        # state word repeats. The policy and the renderer read one predicate for
        # this, so a row the policy releases is never drawn as the blank line
        # the reader never saw. A suppressed duty is the failure this policy
        # exists to prevent, so the exemption is on the duty's own evidence
        # rather than on the kind alone.
        # A launch is news even when its word repeats the one the pane last
        # recorded for the run: a producer writes ``starting`` and then
        # ``working`` seconds later, both of which the pane draws ``working``,
        # and only the second is the attempt beginning. The attempt's own memory
        # — the word the pane actually showed — decides once-only.
        launch = bool(
            run_id
            and self._is_own_launch(event, kind)
            and self._shown.get(run_id) != "working"
        )
        word = _pane_word(event)
        if (
            run_id
            and word
            and word == _remembered_word(self._reported.get(run_id))
            and not same_word_row_is_news(event, self._shown_wait.get(run_id))
            and not launch
        ):
            self._reported[run_id] = to_state
            return out

        if self._is_own_start(event, kind):
            # A start whose arrival hold has already gone: the pane's own run
            # left dispatched into working with no arrival left standing, so the
            # row prints directly rather than being held as observer context.
            if not self._release_launch(out, event, kind, run_id, now) and run_id:
                self._reported[run_id] = to_state
            return out

        if run_id and kind == ARRIVAL_KIND:
            if self._reported.get(run_id) not in (None, ARRIVAL_STATE):
                # A re-announced arrival for a run the pane already shows past
                # dispatched is inventory about a move already drawn; holding it
                # would print a stale arrival when its window closed, so a
                # re-announcement or replay of a start delivers nothing.
                return out
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

        if to_state in LIFT_STATES:
            # A lift row is keyed on its group, not a run: the producer builds
            # it with no run id and the group in ``node``. Holding a baseline
            # in-force row for a group already drawn is what stops a reloaded
            # lift watcher re-announcing a standing lift the pane already shows.
            return self._feed_lift(out, event, to_state=to_state)

        cls = self._row_class(event, kind)

        # A row that takes a run away from the word the pane last showed for it
        # is a departure held for the round trip — but only where the policy has
        # already decided the row would print. The observer hold below is
        # consulted first, so this rule can subtract a row the pane would draw
        # and can never add one the contract hides: a terminal echo that
        # supersedes nothing is withheld whether or not it is a departure.

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
        if self._is_observed(event):
            # A session the pane was asked to observe is one whose rows it
            # prints, so the observer-context hold does not apply to it. What is
            # delivered is the row as it stands: a replayed history row (such as
            # ``working`` with no left side) arrives without being held, because
            # seeing the observed session's shape is the point of observing it.
            if self._hold_departure(event, run_id, now):
                return out
            out.append(event)
            self._note_printed(run_id, event, observed=True)
            return out
        if self._observer_exception(event, run_id):
            if self._hold_departure(event, run_id, now):
                return out
            out.append(event)
            self._note_printed(run_id, event, observed=True)
            return out
        self._note_printed(run_id, event)
        return out

    def _feed_lift(
        self,
        out: list[Mapping[str, Any]],
        event: Mapping[str, Any],
        *,
        to_state: str,
    ) -> list[Mapping[str, Any]]:
        """Decide a lift row by its group, so a standing lift is drawn once.

        The pane keeps one memory about lifts — the groups it has drawn as in
        force. A grant records its group and prints; an end forgets it and
        prints, so the next grant is drawn again. A baseline in-force row is
        inventory of what already exists, so it draws only for a group the pane
        has not drawn: a first attach shows the standing lift once, and the
        baseline a reloaded watcher re-announces for a group already drawn adds
        no row. A row with no group cannot be keyed, so it prints as before.

        A row the pane keeps is then offered to the pane's shared claim on
        (pane, its key, transition): the followers of one pane write it, and
        every project's producer carries the same row, so whichever follower
        reaches it first takes the claim and draws it, and the others draw
        nothing. The claim is taken here, at the point the row would print,
        after every hold has decided — a follower that claimed and then held the
        row would leave no pane drawing it. A follower that loses the claim
        keeps no memory of a lift it did not show, so its grant's end is a peer
        follower's to draw: the group memory holds only what this follower itself
        drew. A row carrying no key cannot be claimed and prints on this pane's
        own memory alone.

        A limit reset is its own case. It is not a lift in any of the three
        senses above, so it neither reads nor writes the group memory: the pane
        draws one reset per event, and the grants and ends of the
        lift family are left as the lift rows set them. Its claim is the same
        exclusive create the grant takes, keyed on the event's own id.
        """
        group = str(event.get("node") or "")
        lift_id = str(event.get("lift_id") or "")
        claim = self._lift_claim if lift_id else None

        if to_state == LIMIT_RESET:
            if claim is not None and not claim(lift_id, to_state):
                return out
            out.append(event)
            return out

        if to_state == LIFT_GRANTED:
            if claim is not None and not claim(lift_id, to_state):
                return out
            if group:
                self._lift_groups.add(group)
            out.append(event)
            return out

        if to_state == LIFT_ENDED:
            if claim is not None and group and group not in self._lift_groups:
                # This pane never showed the grant — a peer follower did — so
                # its end is not this follower's to draw.
                return out
            if claim is not None and not claim(lift_id, to_state):
                if group:
                    self._lift_groups.discard(group)
                return out
            if group:
                self._lift_groups.discard(group)
            out.append(event)
            return out

        # A baseline in-force row: inventory of a standing lift. A group this
        # follower already drew draws nothing, and the claim picks the one
        # follower that shows a group it has not.
        if group and group in self._lift_groups:
            return out
        if claim is not None and not claim(lift_id, to_state):
            return out
        if group:
            self._lift_groups.add(group)
        out.append(event)
        return out

    def flush(self, *, now: float) -> list[Mapping[str, Any]]:
        """Print every opener still held whose window has closed by ``now``.

        The released rows are filtered by :func:`stream_carries` exactly as
        ``feed`` filters its own, so a hold that expires into a row carrying no
        duty leaves the stream as silently as it would have entered it.
        """
        out: list[Mapping[str, Any]] = []
        self._release_expired(now, out)
        out = self._mark_carried(out)
        self._note_shown(out)
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
        own_session: str | None = None,
        observed_sessions: Iterable[str] = (),
        lift_groups: Iterable[str] = (),
        lift_claim: Callable[[str, str], bool] | None = None,
    ) -> None:
        self._selects = selects
        self._show_observer = bool(show_observer)
        self._own_session = str(own_session) if own_session else None
        self._observed_sessions = frozenset(
            str(name) for name in observed_sessions if name
        )
        self._lift_claim = lift_claim
        self.reported: dict[str, str] = {
            str(run_id): str(state) for run_id, state in dict(reported or {}).items()
        }
        self._policy = RowPolicy(
            show_observer=self._show_observer,
            own_session=self._own_session,
            observed_sessions=self._observed_sessions,
            lift_groups=lift_groups,
            lift_claim=lift_claim,
        )
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

    def remember_lift(self, group: str) -> None:
        """Note a lift group the pane already shows as in force."""
        self._policy.seed_lift_group(group)

    @property
    def lift_groups(self) -> frozenset[str]:
        """The lift groups this pane has drawn as in force."""
        return self._policy.lift_groups

    def reseed(
        self,
        reported: Mapping[str, str] | None,
        *,
        lift_groups: Iterable[str] = (),
    ) -> None:
        """Replace the remembered states, as a continuation restores them.

        The lift groups a continuation carries are restored in the same step,
        because a fresh policy would otherwise start with no memory of the
        stands the previous image already drew and re-announce them.
        """
        self.reported = {
            str(run_id): str(state) for run_id, state in dict(reported or {}).items()
        }
        self._policy = RowPolicy(
            show_observer=self._show_observer,
            own_session=self._own_session,
            observed_sessions=self._observed_sessions,
            lift_groups=lift_groups,
            lift_claim=self._lift_claim,
        )
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
    own_session: str | None = None,
    observed_sessions: Iterable[str] = (),
) -> list[Mapping[str, Any]]:
    """Print an ordered stream the way one follower's own row path would.

    The audit's done-when replays a recorded watch stream, so the measurement is
    taken through :class:`PaneRowPath` — the same object a live follower feeds —
    rather than through the policy alone: a row the follower would never have
    offered the policy is not a row this can count. ``selects`` is that
    follower's own selection, and a caller measuring a real follower passes the
    same selection function the follower was built with. ``own_session`` names
    the session the pane follows, so its own runs' starts are delivered here the
    same way the live follower delivers them, and ``observed_sessions`` names the
    sessions it was asked to observe, whose rows the pane prints as it prints its
    own.
    """
    path = PaneRowPath(
        selects=selects,
        show_observer=show_observer,
        own_session=own_session,
        observed_sessions=observed_sessions,
    )
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


# A wait whose condition no probe has read is not the same situation as one whose
# probe answered and is still pending: nothing is testing the first, so its
# condition cannot lift on its own and a reader waiting for it waits for nothing.
# The record separates them — the probe's verdict (``wait_condition_state``) is
# absent when no probe has run, while a manifest asking to wait still emits its
# horizon and overdue flag — so a row marks the unprobed wait with this glyph in
# the state cell's spare column, beside the word rather than in a cell of its own.
UNPROBED_WAIT_MARKER = "?"


def unprobed_wait(event: Mapping[str, Any]) -> bool:
    """Whether the row declares a wait no probe has been run for.

    The probe's verdict is written only when a probe answered, so the field's
    presence is the record of execution and its absence the record of none. A
    manifest that asks to wait emits its horizon and its overdue flag with the
    verdict absent — the wait nobody is checking — which is the row this marks,
    and it is distinguished from an ordinary row, which carries none of the
    declaration's facts at all.
    """
    return declares_a_wait(event) and event.get("wait_condition_state") is None


def _actionable_recovery(event: Mapping[str, Any]) -> bool:
    """Whether the row's own classification names a coordinator duty.

    The classifier owns "needs the coordinator", so its actionable verdict is
    read from the vocabulary rather than restated as a list of kinds here.
    """
    from reckon.crew.recovery import ACTIONABLE_RECOVERY_CLASSIFICATIONS

    return (
        str(event.get("recovery_classification") or "")
        in ACTIONABLE_RECOVERY_CLASSIFICATIONS
    )


def wait_facts(event: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """The facts a declared wait is judged by, or None for a row declaring none.

    The set is the wait's own identity as the event carries it: the overdue flag
    compared on its own, then the probe's verdict, the declaration's last
    observation, the horizon it was declared against and the brief a resumed
    worker reads. A row that declares no wait carries none of them, so None is
    the explicit unmeasured state rather than a tuple of Nones — it tells a
    caller the row has no wait to compare at all.
    """
    if not declares_a_wait(event):
        return None
    return (
        bool(event.get("wait_overdue")),
        event.get("wait_condition_state"),
        event.get("wait_observed"),
        event.get("expected_horizon_seconds"),
        event.get("resume_brief"),
    )


def same_word_row_is_news(
    event: Mapping[str, Any],
    remembered: tuple[Any, ...] | None = None,
) -> bool:
    """Whether a row repeating the pane's word for its run is still news.

    The pane draws a transition only where its two halves differ, so a row
    whose destination word equals the word the pane already shows for the run
    would draw an empty line — and a released row that draws nothing is a duty
    the reader never saw. Such rows are news all the same for three reasons,
    because each carries something a repeated word would hide: a recovery
    classification the fleet calls actionable, a launch the row policy released
    for an attempt the pane's own session began, and a declared external wait
    whose condition the coordinator must be able to read.

    A wait is judged against the facts the pane's own last shown row carried
    for that run, handed in as ``remembered`` (from :func:`wait_facts`). The
    wait's word covers a departure the pane draws and a return it does not —
    a run that ages out of its wait and one that recovers from the age both
    read ``waiting`` — so the state word alone cannot tell the return from the
    departure. A same-word wait row is news where it newly sets ``wait_overdue``
    (the remembered row had it false or absent) or where the probe's verdict,
    the observation, the horizon or the brief differ from that row's; a row
    that only clears the overdue flag while the rest of its declaration stands
    unchanged tells the reader nothing the aging row did not, so it is not
    news. With no memory for the run the row is taken as news, because a wait
    the pane has no shown declaration for is a duty rather than a repetition.

    ``RowPolicy`` and ``Ticker.render`` read this one predicate, each handing
    its own per-run memory of the shown row's wait facts. The policy withholds
    every same-word row it is not told is news, so a row it releases is never
    drawn blank, and the renderer draws the ones this names news rather than
    dropping them as the empty line the pane never put up. A row whose two
    halves name different words is judged by them and is news wherever they
    differ.
    """
    if _actionable_recovery(event) or bool(event.get(LAUNCH_FIELD)):
        return True
    facts = wait_facts(event)
    if facts is None:
        return False
    if remembered is None:
        return True
    overdue, *declaration = facts
    if overdue and not remembered[0]:
        return True
    return tuple(declaration) != tuple(remembered[1:])


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


def _job_name(node: str, role: Any) -> str:
    """The node as the job cell shows it beside its stage.

    A review's node id repeats what the stage cell already says — the review
    prefix — and ends in the reviewed head's digest, which identifies nothing a
    reader can use. Both are dropped for a review, so the cell names the job
    under review. Every other stage shows its node id whole.
    """
    if str(role or "") != "review":
        return node
    for prefix, replacement in REVIEW_JOB_PREFIXES:
        if node.startswith(prefix):
            rest = node[len(prefix) :]
            if prefix == REVIEW_NODE_PREFIX:
                rest = REVIEWED_HEAD.sub("", rest)
            return f"{replacement}{rest}" if rest else node
    return node


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
        self.width = self._grid_width()
        # NO_COLOR is the caller's environment overriding the caller's flag, per
        # the convention; any non-empty value disables.
        self.color = bool(color) and not os.environ.get("NO_COLOR")
        self._hues: dict[str, int] = {}
        # The runs this pane has drawn under each job cell, keyed by stage and
        # the cell's text, each with the number the pane gave it. Kept as long
        # as the pane: a number a reader has seen must not change, and a later
        # attach replays its baselines through this same grid.
        self._job_runs: dict[str, dict[str, int]] = {}
        # The state this pane last put on screen for each run. A producer may
        # skip an intermediate observation in its own ``from_state``; the pane
        # must not rewrite the story it already showed, so every later left side
        # comes from here. Synthetic unit events that carry no event kind are
        # independent render probes rather than stream rows and do not enter the
        # chain.
        self._reported: dict[str, str] = {}
        # run_id -> the wait facts of the last row this pane drew for the run,
        # or None where that row declared none. Read by the same-word rule
        # beside the word, so a wait returning from aged with an unchanged
        # declaration is not drawn as a fresh transition.
        self._shown_wait: dict[str, tuple[Any, ...] | None] = {}
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

    def _grid_width(self) -> int:
        """The whole grid's width: the request, raised to fit the fixed head."""
        return max(self._requested_width, MIN_WIDTH)

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
        no lane for the cell to speak about, so it stays blank rather than
        naming the absence of a lane the row never used. A string means a
        document was declared and read, and names why it yielded no figure.
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

    def _rate_cell(self, event: Mapping[str, Any]) -> tuple[str, Any]:
        """The lane's mean generation rate with its unit, at a fixed width.

        The cell has one width in every state, so the model and effort after it
        hold their columns. A row naming no backend, or a backend that declares
        no lane document, has no lane to read and leaves the cell blank. A
        declared document that yields no figure prints the absence marker,
        never a zero, because a lane that published nothing has not measured
        nothing.
        """
        backend = str(event.get("backend") or "").strip()
        if not backend:
            return (" " * RATE, None)
        project = str(event.get("project") or "").strip() or None
        reading = self._lane_reading(project, backend)
        if reading is None:
            return (" " * RATE, None)
        if isinstance(reading, str):
            return (f"{DIM_MARKER:>{LANE_RATE}} {RATE_UNIT}", "dim")
        return (f"{lane_rate_text(reading):>{LANE_RATE}} {RATE_UNIT}", None)

    def hue(self, node: str) -> int:
        """The node's colour, claimed on first sighting and kept thereafter."""
        if node not in self._hues:
            palette = PALETTE[self.theme]
            self._hues[node] = palette[len(self._hues) % len(palette)]
        return self._hues[node]

    def _job_cell(self, job: str, stage: str, run_id: str, predecessor: str) -> str:
        """The job name fitted to its cell, numbered once the job has two runs.

        The name is cut from the middle so its end survives, because the job
        column answers *which worker is this?* and the peers of a wave share a
        long head. What is compared is the cell a reader sees under its stage:
        two runs of one job, and two names that cut to one text, land on one
        key. Each run on a key takes the next number in the order the pane drew
        it, and every row on that key carries its number once the key has two,
        so the runs read apart and in order. A run launched from a queued
        request (``predecessor``) is the request continuing and takes its
        number.
        """
        text = elide(job, NODE, keep_end=True)
        numbered = self._job_runs.setdefault(f"{stage}\0{text}", {})
        if run_id not in numbered:
            if predecessor and predecessor in numbered:
                numbered[run_id] = numbered[predecessor]
            else:
                numbered[run_id] = max(numbered.values(), default=0) + 1
        if len(set(numbered.values())) < 2:
            return text
        number = str(numbered[run_id])
        return f"{elide(job, NODE - len(number) - 1, keep_end=True)} {number}"

    def render(self, event: Mapping[str, Any], *, with_session: bool = False) -> str:
        """One transition as one line, at most ``width`` visible characters.

        ``with_session`` is accepted for call-site compatibility. Ownership is
        no longer a row column: the follower already scopes delivery.

        The row reads in the pane's order: time, stage, job, transition,
        elapsed and the fleet counters, which every row carries at fixed
        widths, then the lane's rate, the model and the effort. Those three
        are added while the pane has room for them, so a narrow pane drops the
        effort first and never wraps. Nothing follows the effort: the row
        carries no free text, and ends on its last cell with any trailing pad
        removed.
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
        # The row spells the cause-specific type when the producer supplied one.
        entry_state = (
            typed_state
            if typed_state in {"held", "needs-help", "unwritten"}
            else raw_to_state
        )
        to_state = _display_state(entry_state)
        run_id = str(event.get("run_id") or node)
        stream_event = bool(str(event.get("event") or "").strip())
        # A run an admission pass launched from a queued request continues the
        # story the pane told about that request, so it starts from the word the
        # pane last showed for the request rather than from ``new``.
        predecessor = str(event.get("admitted_from") or "")
        if (
            stream_event
            and predecessor
            and run_id not in self._reported
            and predecessor in self._reported
        ):
            self._reported[run_id] = self._reported[predecessor]
        reported = self._reported.get(run_id) if stream_event else None
        # The first streamed row begins at `new`, even when its event names an
        # earlier internal state. Later rows use the word this painter showed.
        # A direct render probe can supply its own left state. Either way, two
        # internal states collapsing to one pane word produce no transition.
        from_state = reported
        if from_state is None:
            from_state = (
                "new"
                if stream_event or event.get("from_state") is None
                else _display_state(event.get("from_state"))
            )
        if from_state == to_state:
            # The row's destination repeats the word the pane already shows for
            # the run. That draws no transition — unless the row carries a duty
            # (:func:`same_word_row_is_news`), which the pane owes the reader
            # even when the word repeats. Such a row prints the destination word
            # twice, or its own left word where the row names a different one,
            # so a block or an unreadable the pane already shows is never
            # released as an empty line the reader never saw.
            if not same_word_row_is_news(event, self._shown_wait.get(run_id)):
                return ""
            own_left = _display_state(event.get("from_state"))
            from_state = own_left if own_left and own_left != to_state else to_state
        if stream_event:
            self._reported[run_id] = to_state
            # The wait facts the pane has now shown, so a later row repeating
            # the word is judged against the declaration the reader can see
            # rather than against its own alone.
            self._shown_wait[run_id] = wait_facts(event)

        # A reset is drawn as a clause of its own rather than as the transition
        # its pane word would make: what it is, the group, the clock, and what
        # it asks. Its pane word is still recorded above, so the pane's memory
        # of the group reads as it does for every other row.
        if raw_to_state == LIMIT_RESET:
            return self._reset_line(event)

        # A broadcast row carries no run and so no role: its stage names the
        # broadcast, and its job cell names the budget group it concerns. A
        # lift keeps its own stage word here; a reset is drawn above, as the
        # clause it is, so the two families stay readable apart.
        stage = (
            LIFT_STAGE
            if raw_to_state in LIFT_STATES
            else _display_role(event.get("role"))
        )
        model_cell, effort_cell = _model_and_effort(event)
        job = _job_name(node, event.get("role"))
        # A wait no probe has run for is marked beside its destination word, in
        # the state cell's own spare column: the cell is a fixed ten columns and
        # the longest pane word is nine, so the glyph rides inside the word's
        # field and a marked row aligns with an unmarked one rather than
        # widening the row. The en dash is not the marker — the row already
        # spends it on an absent value.
        to_cell = to_state + (UNPROBED_WAIT_MARKER if unprobed_wait(event) else "")

        cells: list[tuple[str, Any]] = [
            (f"{local_clock(event.get('observed_at')):<{CLOCK}}", "dim"),
            (" " * GAP, None),
            (f"{stage:<{ROLE}}", "dim"),
            (" " * GAP, None),
            (
                f"{self._job_cell(job, stage, run_id, predecessor):<{NODE}}",
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
                (f"{to_cell:<{STATE_WORD}}", _state_hue(self.theme, to_state)),
                (" " * GAP, None),
            ]
        )
        cells.extend(self._spend_cells(event, to_state))
        cells.append((" " * GAP, None))
        cells.extend(self._stats(event))
        # The trailing cells, in reading order. Each is added only where the
        # pane has the room for it, because a cell appended past the pane's edge
        # would wrap the row, which costs a quarter of the visible history.
        trailing: list[list[tuple[str, Any]]] = [
            [(" " * GAP, None), self._rate_cell(event)],
            [
                (" " * GAP, None),
                (f"{elide(model_cell, model_width):<{model_width}}", "dim"),
            ],
            [(" " * GAP, None), (f"{effort_cell:<{EFFORT}}", "dim")],
        ]
        room = self._grid_width() - sum(len(text) for text, _ in cells)
        for group in trailing:
            span = sum(len(text) for text, _ in group)
            if span > room:
                break
            cells.extend(group)
            room -= span
        # The row ends on its last glyph: a blank trailing cell, such as the
        # model of a request that never launched, is dropped and the last
        # cell's pad is trimmed, so no row carries painted blanks at its end.
        while cells and not cells[-1][0].strip():
            cells.pop()
        if cells:
            text, style = cells[-1]
            cells[-1] = (text.rstrip(), style)
        # A shadow will never merge, so the row says so about itself end to end
        # rather than spending a column on an identifier a reader cannot use.
        shadow = is_shadow(event)
        return "".join(
            self._paint(text, "dim" if shadow and style is not None else style)
            for text, style in cells
        )

    def _reset_line(self, event: Mapping[str, Any]) -> str:
        """One limit reset as a clause, in place of the transition's two words.

        The clause takes the room the stage, job and transition columns give it,
        and the elapsed cell and the counters keep their places after it, so a
        reset row lines up with the fleet rows beneath it. It is the one row
        carrying free text, so it is cut to the pane like every other cell: the
        cut is what keeps a long reason from wrapping the row, which would cost
        a quarter of the visible history.
        """
        spend = self._spend_cells(event, LIMIT_RESET)
        stats = self._stats(event)
        tail = (
            GAP
            + sum(len(text) for text, _ in spend)
            + GAP
            + sum(len(text) for text, _ in stats)
        )
        room = self._grid_width() - CLOCK - GAP - tail
        cells: list[tuple[str, Any]] = [
            (f"{local_clock(event.get('observed_at')):<{CLOCK}}", "dim"),
            (" " * GAP, None),
            (elide(limit_reset_text(event), room), None),
            (" " * GAP, None),
            *spend,
            (" " * GAP, None),
            *stats,
        ]
        shadow = is_shadow(event)
        return "".join(
            self._paint(text, "dim" if shadow and style is not None else style)
            for text, style in cells
        )

    def _spend_cells(
        self, event: Mapping[str, Any], to_state: str
    ) -> list[tuple[str, Any]]:
        """Elapsed time, fed by the transition record's fact.

        The figure is right-aligned to its fixed width so the column a reader
        scans stays put as the span grows. An unmeasured span renders the dim
        absence marker, never a zero; a measured zero stays a zero. Elapsed time
        on a row is what the run has spent, so an internal dispatch event
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
        if str(event.get("to_state") or "") == "dispatched":
            wall, wall_style = "", None
        return [
            (" " * SPEND_GAP, None),
            (f"{wall:>{WALL}}", wall_style),
        ]

    def _stats(self, event: Mapping[str, Any]) -> list[tuple[str, Any]]:
        """The fleet after this transition, as a grid whose digits line up.

        Each counter is its count right-aligned to :data:`STAT_DIGITS` digits
        followed by its compact label, so each label holds one screen column
        whatever the counts are. One space separates each cell from the
        next, so two counts never touch: ``10w 12h`` is two tokens. Right
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
        return cells

    def _paint(self, text: str, style: Any) -> str:
        if not self.color or style is None or not text:
            return text
        prefix = _DIM if style == "dim" else f"\x1b[38;5;{int(style)}m"
        return f"{prefix}{text}{_RESET}"
