"""Derive the work one coordinator session still owes.

The recovery classifier owns each live run's state and remedy.  This module
only projects those rows into coordinator duties, adds the duties whose
evidence lives outside live pointers, and reports the closure figure from the
drain view.  No obligation state is persisted here.

One of those duties is derived rather than projected: a stored review whose
dimension sits below the floor flight configuration declares for it is a
finding that must be answered before the session can close cleanly over it.
The finding is never folded into the review's total — it is reported beside
it, for the run the review is about, until a disposition in the closed set is
recorded against the dimension on the stored record itself. The disposition
vocabulary, and the read-back that decides whether a finding still stands,
live with the review store (:mod:`reckon.crew.review`); this module only
reports what is still owed.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import threading
from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon import _store, file_memo, flight, ledger, review_tiers
from reckon._timestamps import parse_utc
from reckon.crew import (
    budget_reset,
    queue_admission,
    recovery,
    recovery_classification,
    recovery_review_dispatch,
    review_dispositions,
    review_lifecycle,
    review_records,
    runs,
)
from reckon.crew import (
    repair as repair_module,
)
from reckon.crew import review as review_module
from reckon.crew.node import (
    INTERRUPTED_RUN_PHASE,
    CrewError,
    PlanReviewMissingError,
    parse_duration,
)
from reckon.crew.query import project_run_rows, run_section_index
from reckon.crew.recovery_classification import PRODUCER_GONE_STATUS
from reckon.crew.recovery_review_dispatch import supersedes_head
from reckon.crew.reports import TERMINAL_MANIFEST_STATUSES
from reckon.crew.routing import (
    WorktreeRecordIndex,
    _registered_worktrees,
    _retained_worktree,
    record_names_worktree,
)
from reckon.crew.run_paths import crew_home
from reckon.git_head import _common_git_directory, _git_directory, _head_identity
from reckon.mcp_crew_actions import CREW_WRITE_ACTIONS
from reckon.velocity import run_git

CLASSIFICATION_DUTY_KINDS = {
    "scoring": "review-missing",
    "promotable": "review-ready",
    "blocked": "blocked",
    INTERRUPTED_RUN_PHASE: "turn-ended-early",
}
# The two duty kinds whose evidence is a stored review of the run's own head.
# Each is read against the review selection — the one promotion uses — so a run
# whose head moved past the revision its stored review recorded reads
# review-missing naming both heads, whatever the classifier's own reading said.
REVIEW_CLASSIFICATION_KINDS = frozenset({"scoring", "promotable"})
#: Recovery readings that owe their session a duty under a word the lifecycle
#: classification does not carry.
#:
#: A ``launch-failed`` run whose pointer holds a pre-spawn phase past the bound
#: is one no evidence can move: the launch was cut off between composing the
#: record and spawning the worker, so there is no process to observe, no stream
#: to watch and nothing that ends on its own. It reads ``launch-failed`` rather
#: than any lifecycle word, so without an entry here it reaches no duty at all
#: and the abandoned pointer holds whatever claim it took in silence. The row
#: carries the classifier's own next action, which is the command that releases
#: it.
RECOVERY_CLASSIFICATION_DUTY_KINDS = {
    "needs-help": "needs-help",
    "launch-failed": "launch-failed",
    # Both classifications whose recovery verb is ``repair``: the run's own
    # declaration is what is wrong, so no sweep, resume or promotion moves it
    # until the session edits the thing the classifier refused. Left out, they
    # reach no duty at all and a run whose wait the reader refused sits idle
    # with the session told nothing.
    "unreadable": "unreadable",
    "exited-unfinished": "exited-unfinished",
}

#: The recovery duty kinds whose evidence is the classifier's own account of
#: what is wrong with the run, carried on the row's ``detail``. A coordinator
#: reading one of these owes a repair, so the obligation says what to repair
#: rather than only that the run is halted.
CLASSIFIER_DETAIL_DUTY_KINDS = frozenset({"unreadable", "exited-unfinished"})

# A duty kind whose evidence is a stored review rather than a live run's state:
# the row is owed while the dimension carries no disposition, and it names the
# run the review is about rather than the reviewing run.
SUB_FLOOR_DUTY_KIND = "review-dimension-sub-floor"

#: The fields a duty row can carry, beside the ones a duty of one kind adds.
#:
#: The two lists this view serves — the duties owed and the ones acknowledged —
#: are projected by their own call, and a caller may ask either for a field only
#: the other list's rows carry. The vocabulary is therefore declared here rather
#: than read from the rows in hand: an empty list says nothing about which fields
#: this view has, and ``acknowledged`` is empty in the ordinary case, so a
#: vocabulary read from it would refuse the duty's own ``kind``. A field whose
#: value is empty is left off the row by the projection; a name is accepted here
#: whether or not any duty of that kind is currently owed.
DUTY_ROW_FIELDS = (
    "kind",
    "run_id",
    "node",
    "plan",
    "age_seconds",
    "next_command",
    "next_action_form",
    # A queued duty's place in the lane's order, present only while it waits.
    "position",
    "rank",
    # A review duty's reading and the revisions it disagrees about; a refused
    # attempt's reason and record; the tier a run owes no review at all.
    "reason",
    "record",
    "head",
    "reviewed_head",
    "review_tier",
    # Set when an armed follower, rather than the coordinator, will dispatch it,
    # carrying the window that follower's sweep cadence promises, so a reader
    # deciding whether to act on the row reads the same figure the row's own
    # text quotes.
    "review_owner",
    "reflex_window_seconds",
    # An overdue remedy's time-dependent intervals; see refresh_overdue_remedy.
    "overdue_remedy",
    "refused_at",
    "queued_at",
    "worker_slots",
    # A sub-floor review dimension's own figures.
    "dimension",
    "score",
    "floor",
    "exempt_alternative",
    # How many requests one plan's review hold keeps waiting.
    "held_count",
    # The run a finished-run hold names, with its session, phase and the path
    # it fences, so a coordinator can find it without opening the waiting
    # request's pointer.
    "holder_run_id",
    "holder_session",
    "holder_phase",
    "held_path",
    # A producer-gone duty's own evidence: the probe, the value it is stuck on,
    # the terminal states it is waiting for and how long the value has stood
    # still, so a coordinator sees why nothing will lift the wait.
    "probe",
    "observed",
    "terminal",
    "still_seconds",
    # The live or queued plan review a refused queued request is waiting on, so
    # a reader sees which run ends the wait rather than the compose command that
    # would duplicate it.
    "review_run_id",
    # The paths a checkout's index stages as deleted while HEAD and the working
    # tree both still carry them.
    "paths",
)

# A review whose attempt the lane's admission gate refused with ``lane-paused``
# is not work the coordinator skipped: the reflex still owns it and retries when
# a slot frees, so the duty reads as queued rather than missing, is listed like
# any other duty, and does not hold a turn open. The reading is bounded by the
# refusal's own instant, because a refusal nobody acted on within the bound is
# evidence about the reflex rather than an explanation for the duty, and the
# duty returns to review-missing so the coordinator sees it again.
REVIEW_QUEUED_DUTY_KIND = "review-queued"
REVIEW_QUEUED_BOUND_SECONDS = 30 * 60

# The status the review reflex records on a run's pointer when the lane refused
# the attempt as paused; the refusal instant it carries is the bound's origin.
REVIEW_LANE_PAUSED_STATUS = "lane-paused"

# How long a duty may tell a coordinator the reflex will take. The review reflex
# runs on the follower's own sweep cadence, so the expectation a duty prints is
# that cadence rather than a figure of this module's own: a window the follower
# does not keep would send a coordinator to dispatch by hand exactly when the
# reflex was about to.
DEFAULT_SWEEP_SECONDS_FALLBACK = 120

# A queued request the session itself has to unblock before anything can launch
# it: the admission refusal is a condition of the session rather than of the
# lane, so the duty is the session's own. The remedy the classifier published
# for it travels as the duty's ``next_command``, so the two surfaces name the
# one action.
QUEUED_AWAITING_SESSION_DUTY_KIND = "queued-awaiting-session"

# A queued request the admission pass refused on its node contract. The
# refusal is a fact about the request and the plan section it was queued from,
# so no lane slot and no wait clears it; the session repairs the named property
# and requeues. It carries its own kind because the waiting reading it replaces
# sends a coordinator to wait for a request that would never launch, which is
# how one project's sections sat dead for the better part of an hour.
QUEUED_CONTRACT_REFUSED_DUTY_KIND = "queued-contract-refused"

# The node-contract properties ``reckon crew queue edit`` can repair, with the
# field the edit names for each. A refusal naming only these is cleared by an
# edit that keeps the request's place in the queue; any other property belongs
# to the plan section the request was queued from, so the request has to be
# dropped, the section repaired and dispatched again.
_EDITABLE_CONTRACT_PROPERTIES: Mapping[str, str] = {
    "single-goal": "steering",
    "fully-specified": "steering",
    "demonstrable": "done-when",
    "scoped": "write-paths",
    "bounded": "time-budget",
}

# Each node-contract finding is rendered as ``<property>: <detail>``, the first
# after the refusal's opening words and the rest after ``; ``. The detail may
# itself contain a colon or a semicolon, so the property is read by position
# rather than by splitting the text into pairs.
_CONTRACT_PROPERTY = re.compile(r"(?:(?<=; )|(?<=— ))([a-z][a-z_-]*): ")

# A request waiting its turn on the lane: nothing is owed for it, but it is not
# missing either, so it is listed as waiting for as long as its pointer exists.
# This is the run-level reading the review-queued kind is for a review, and the
# two stay distinct because the waits are: a refused review attempt is bounded
# by the refusal instant, while a queued request waits until the lane takes it,
# so this kind carries no bound. It is not the session-refusal kind above
# either — that names a condition only the session can clear, where this names
# a wait the lane's own admission pass ends.
QUEUED_RUN_DUTY_KIND = "run-queued"

# A queued request the admission pass recorded a stall verdict for: the lane
# could launch it and did not, so the wait is one the lane has already declared
# rather than a request simply waiting its turn. The two read alike otherwise,
# which is the stuck-in-the-queue experience this kind removes: a plain queued
# row says only that the request is waiting, while a stalled row names how long
# it has been launchable, the stage the pass was in, and the action left to a
# coordinator. It stays distinct from the plain kind because the wait is not the
# same wait — this row says admission has already failed to take it.
STALLED_RUN_DUTY_KIND = "run-stalled"

# A waiting run whose producer is provably gone: nothing alive can change the
# value its probe reports, so the declared wait will never end itself. It is
# not a queued or stalled request — admission holds no part of it — so it is
# its own kind, and unlike those two it holds a turn open: the only remedies
# are the session's, a resume that carries the wait past its producer or a
# discard of the run.
PRODUCER_GONE_DUTY_KIND = "run-producer-gone"

# A request waiting on a run no project holds: the landing that would release
# it can never be read, so no admission pass will ever end the wait. It is its
# own kind rather than a waiting reading, because a coordinator shown "waiting
# upstream" for it waits for something that will not happen. Unlike a queued or
# stalled row it holds a turn open: only the session can fix the ref or drop
# the request.
WAIT_UNRESOLVABLE_DUTY_KIND = "wait-unresolvable"

# A main checkout whose index stages a path as deleted while HEAD and the
# working tree both still carry it. An ordinary ``git commit`` in that checkout
# records the deletion, and the working tree shows nothing wrong. It is a
# notice rather than a blocking duty: no run owes it, and the repair is one
# command in a checkout the session may not be working in.
INDEX_LAG_DUTY_KIND = "index-lags-head"

#: The log one line is appended to the first time a lag is observed, and the
#: store remembering which lags that log already carries, both under the crew
#: home. A lag is keyed by HEAD and its paths, so a moved HEAD or a new path is
#: a new occurrence while the same lag read twice is not.
INDEX_LAG_LOG_NAME = "index-lag.log"
INDEX_LAG_SEEN_NAME = "index-lag-seen.json"

#: ``git diff`` refreshes the index's stat cache and writes it back unless
#: optional locks are disabled, and this reader must never change the index it
#: reports on. Every git call the detector makes carries this environment, so
#: reading the view cannot even rewrite the stat cache it reports about.
INDEX_LAG_GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0"}

#: The last reading taken per main checkout, keyed on what must move for it to
#: stop describing the checkout (the index's stat identity and HEAD), so a
#: sweep that recomputes every session's payload does not run git per tick.
_INDEX_LAG_READINGS: dict[str, tuple[tuple[Any, ...], dict[str, Any] | None]] = {}

# The duty kinds that are listed but never hold a stop open: the work behind
# them is already owned by a reflex that retries, so blocking would ask the
# coordinator to act on a wait it cannot shorten.
NON_BLOCKING_DUTY_KINDS = frozenset(
    {
        REVIEW_QUEUED_DUTY_KIND,
        QUEUED_RUN_DUTY_KIND,
        STALLED_RUN_DUTY_KIND,
        INDEX_LAG_DUTY_KIND,
    }
)


def _utc_now() -> datetime:
    """Return the observation instant through a patchable clock boundary."""
    return datetime.now(tz=UTC)


def _holder_idle_seconds(
    project: str,
    holder_session: str,
    pointers: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
) -> float | None:
    """How long the holding session has been unattended, or ``None`` if undated.

    Read through the watcher's own rule (:func:`reckon.crew.runs._unattended_seconds`),
    which takes the smaller of the follower's last renewal and the session's own
    newest stream or pointer activity, so a dispatch, a landing or a follow all
    count as attendance and this reading cannot disagree with the sweep that
    resumes unattended sessions. ``None`` means nothing dates the session, and an
    undated session is not evidence of idleness.
    """
    try:
        return runs._unattended_seconds(
            project,
            holder_session,
            now_seconds=now.timestamp(),
            pointers=pointers,
        )
    except (OSError, ValueError):
        return None


#: A queued request a finished run's claim is holding. The holder has ended —
#: its manifest records ``complete``, ``blocked`` or ``failed`` — while its
#: worktree still fences the paths, so no pass will ever launch the waiting
#: request and no timer will ever lift the wait. The duty belongs to the
#: holder's session, which is the one that can settle it; a session that has
#: been unattended past :data:`HOLDER_IDLE_SECONDS` is no longer going to, so
#: the waiting request's own session is told as well and can settle the holder
#: or ask the lead.
HELD_BY_FINISHED_RUN_DUTY_KIND = "request-held-by-finished-run"

#: How long a holding session may be unattended before the session whose
#: request it holds is told about the hold too. Six hours is the point at which
#: a session is plainly not going to come back on its own within a working day.
HOLDER_IDLE_SECONDS = 6 * 3600

#: The coordinator write verbs the crew MCP tool carries as actions, and the
#: tool parameter name each verb's own option maps onto. A duty's printed
#: command is rendered in the tool's action form so a coordinator reading the
#: obligations view can call the action directly rather than composing a shell
#: command from the printed tokens.
#:
#: Every option the eight verbs accept is listed, including the ones no duty
#: composes today: a form that silently dropped an option it did not know would
#: offer a call that answers differently from the command it was rendered from.
#: An option this map does not carry, or a bare token that names no option,
#: makes the whole command unrenderable rather than lossily rendered.
_ACTION_OPTION_PARAM: dict[str, str] = {
    # complete, and the same verb again under the landing step
    "--run": "run_id",
    "--gate": "gate",
    "--failure-classification": "failure_classification",
    "--commit": "commits",
    "--outcome": "outcome",
    "--no-commit": "no_commit",
    "--tests-added": "tests_added",
    "--completed-at": "completed_at",
    "--checkout-path": "checkout_path",
    "--gate-command": "gate_command",
    "--gate-exit-status": "gate_exit_status",
    "--gate-log-path": "gate_log_path",
    "--gate-log-digest": "gate_log_digest",
    "--waive-suite-delta": "waive_suite_delta",
    "--waive-boundary-refusal": "waive_boundary_refusal",
    "--waive-resume-path": "waive_resume_path",
    "--waive-unreviewed-promotion": "waive_unreviewed_promotion",
    "--waive-negative-control": "waive_negative_control",
    "--accept-path": "accepted_paths",
    "--no-impl-change": "no_impl_change",
    "--waive-live-run": "waive_live_run",
    "--waive-domain-check": "waive_domain_check",
    "--plan-link": "plan_link",
    "--unplanned-reason": "unplanned_reason",
    # dispose
    "--project": "project",
    "--dimension": "dimension",
    "--finding": "finding_index",
    "--kind": "kind",
    "--node": "node",
    "--reason": "reason",
    # ack
    "--until": "until",
    # widen
    "--write-path": "write_paths",
    # answer, the review verb's own answering options
    "--plan": "plan",
    "--session": "session",
    "--answer": "finding",
    "--declined": "declined",
    "--finding-id": "finding",
}

#: The options that take no value and turn their parameter on. Rendered as the
#: tool's own ``True``, which is what the action expects for the same flag. An
#: option that is a flag on the command but has no tool parameter — the landing
#: step, a lane choice — is deliberately absent: the action selects those by its
#: own name, so such a command is left to the reader rather than half-rendered.
_ACTION_FLAG_PARAM: dict[str, str] = {
    "--scope-changed": "scope_changed",
    "--dry-run": "dry_run",
    "--acted": "acted",
}

#: The options written more than once on one command — two ``--commit`` flags,
#: two ``--write-path`` flags — and the tool parameters that take the list they
#: accumulate into. Appending the parameter once per occurrence would render a
#: call no caller can make, so a repeated option renders as one list.
_ACTION_LIST_PARAM: dict[str, str] = {
    "--commit": "commits",
    "--accept-path": "accepted_paths",
    "--write-path": "write_paths",
}

_ACTION_VERBS = frozenset(CREW_WRITE_ACTIONS)


def _action_parameter(option: str) -> str | None:
    """The tool parameter one printed option writes, or ``None`` if unmapped."""
    return (
        _ACTION_OPTION_PARAM.get(option)
        or _ACTION_LIST_PARAM.get(option)
        or _ACTION_FLAG_PARAM.get(option)
    )


def action_form(command: str) -> str | None:
    """Render a printed crew command in the MCP tool's action form.

    One of the eight coordinator write verbs — ``reckon crew complete --run X
    --gate <verdict>`` — becomes ``crew(action="complete", run_id="X",
    gate="<verdict>")``, so the command a duty prints is one the coordinator
    can call on the tool surface rather than a shell invocation to assemble.
    A command naming any other verb, or none at all, has no action form and
    reads as ``None``: the reader keeps the printed command it already had.
    A command the tokeniser cannot split — one carrying an unbalanced
    quotation mark — likewise has no form, so a stored command that cannot be
    parsed leaves the read it belongs to intact rather than failing it.

    Every option on the command is rendered, and an option this form cannot
    carry — one with no tool parameter, one whose value is missing — makes the
    whole command unrenderable. Half a command is worse than a printed one: it
    reads as callable and is refused, or is silently called without the
    argument that the command it came from carried.

    Each value is written as a literal, escaped by ``json.dumps`` rather than
    wrapped in quotes, so a reason carrying a quotation mark or a path carrying
    a backslash renders a form the caller can still run.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        # A stored command carrying an unbalanced quotation mark cannot be
        # tokenised, so this form cannot carry it: leaving the whole command
        # without a form keeps the row's printed command readable rather than
        # letting the parse error take down the read that produced it.
        return None
    if len(tokens) < 3 or tokens[0] != "reckon" or tokens[1] != "crew":
        return None
    verb = tokens[2]
    if verb not in _ACTION_VERBS:
        return None
    values: dict[str, list[str]] = {}
    order: list[tuple[str, bool]] = []
    index = 3
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            return None
        option, _, inline = token.partition("=")
        parameter = _action_parameter(option)
        if parameter is None:
            return None
        if option in _ACTION_FLAG_PARAM:
            if parameter not in values:
                values[parameter] = []
                order.append((parameter, True))
            index += 1
            continue
        if inline:
            value = inline
        elif index + 1 < len(tokens) and not tokens[index + 1].startswith("-"):
            value = tokens[index + 1]
            index += 1
        else:
            return None
        if parameter not in values:
            values[parameter] = []
            order.append((parameter, False))
        values[parameter].append(value)
        index += 1
    rendered = [f"action={json.dumps(verb)}"]
    for parameter, is_flag in order:
        if is_flag:
            rendered.append(f"{parameter}=True")
            continue
        found = values[parameter]
        if parameter in _ACTION_LIST_PARAM.values():
            rendered.append(
                f"{parameter}=[" + ", ".join(json.dumps(one) for one in found) + "]"
            )
        elif len(found) == 1:
            rendered.append(f"{parameter}={json.dumps(found[0])}")
        else:
            # A scalar repeated cannot be one call: the last would win and the
            # earlier values would vanish from the offered form.
            return None
    return "crew(" + ", ".join(rendered) + ")"


def _with_action_form(item: Mapping[str, Any]) -> dict[str, Any]:
    """Return one duty row carrying its command's action form, when it has one.

    The printed command is left as it stands, in ``next_command`` and in
    ``next_action`` alike — that spelling is the CLI's own, and some readers
    still run it. The rendered form travels under its own key, so no reader
    that already knows ``next_action`` as a command string has to tell the two
    apart.
    """
    row = dict(item)
    rendered = action_form(str(item.get("next_command") or ""))
    if rendered is not None:
        row["next_action_form"] = rendered
    return row


def _seconds_since(value: Any, *, now: datetime) -> int:
    """Return the non-negative age of one timestamp, or zero if unreadable."""
    text = str(value or "").strip()
    if not text:
        return 0
    stamp = parse_utc(text)
    if stamp is None:
        return 0
    return max(0, int((now - stamp).total_seconds()))


def _row_age(row: Mapping[str, Any], *, now: datetime) -> int:
    """Read a classifier row's best available age measurement."""
    stated_age = row.get("age_seconds")
    if isinstance(stated_age, (int, float)) and not isinstance(stated_age, bool):
        return max(0, int(stated_age))
    terminal_age = row.get("terminal_age_seconds")
    if isinstance(terminal_age, (int, float)) and not isinstance(terminal_age, bool):
        return max(0, int(terminal_age))
    exit_record = row.get("exit_record")
    if isinstance(exit_record, Mapping) and exit_record.get("exited_at"):
        return _seconds_since(exit_record["exited_at"], now=now)
    log_age = row.get("log_age_seconds")
    if isinstance(log_age, (int, float)) and not isinstance(log_age, bool):
        return max(0, int(log_age))
    return 0


_VIEW_LIVE_POINTERS: ContextVar[Mapping[str, tuple[dict[str, Any], ...]] | None] = (
    ContextVar("reckon_obligations_view_live_pointers", default=None)
)


def _live_pointers(project: str) -> list[dict[str, Any]]:
    """The live pointers this view derives from, read once for the whole view.

    Every duty kind here reads the same live pointers, so a view holding three
    of them would otherwise read the directory three times and could report
    three duties from three different snapshots of it. The view publishes its
    own read for the length of one derivation, keyed by project: a reader
    inside the view that asks about another project gets that project's
    pointers, read for itself, rather than the ones this derivation happens to
    hold. A reader called outside a view reads for itself.
    """
    held = _VIEW_LIVE_POINTERS.get()
    if held is not None:
        snapshot = held.get(project)
        if snapshot is not None:
            return list(snapshot)
    return runs.list_live(project=project)


def _reflex_review_in_flight(pointer: Mapping[str, Any]) -> bool:
    """Whether the reflex's own recorded review dispatch still holds a live run.

    The reflex records the review it launched on the run it acted for, and that
    record is its claim: while the review run it names holds a live pointer the
    reflex will not compose a second review, so a printed dispatch is one the
    coordinator can only have refused as a scope conflict. The claim is dated
    from the launch window the record carries rather than being re-judged
    against the run's manifest: a run that re-completed after that launch has
    not taken the claim away, because the live review the reflex owns is still
    the thing under way and the reflex is what re-fires once it ends.

    Whether the recorded pointer is live is decided by the reflex's own rule,
    so this reader and the reflex cannot split on a pointer that exists but
    cannot be read: both treat it as no review, and the run stays owed one.
    """
    return recovery._recorded_review_is_live(
        pointer.get(recovery.REVIEW_DISPATCH_FIELD)
    )


def _row_delivery_instant(row: Mapping[str, Any], *, now: datetime) -> datetime:
    """The instant a row's age counts from: the run's last delivery.

    ``_row_age`` measures how long a run has stood in the state the row reports,
    so that state began that many seconds before ``now``. The instant is the
    anchor an undated refusal is judged against: a refusal recorded before the
    run delivered speaks about a revision the run has since left behind.
    """
    return now - timedelta(seconds=_row_age(row, now=now))


def _refusal_speaks_for_revision(
    recorded: Mapping[str, Any],
    *,
    row: Mapping[str, Any],
    head: str,
    now: datetime,
) -> bool:
    """Whether a recorded review dispatch speaks for the revision now owed one.

    A record that names a head is counted while that head is the run's current
    one. A record that names none is counted from its instant, which is the
    only fact it carries that can be ordered against the run's own delivery:
    the refusal writer records a head only for an attempt it composed against
    one, so a refusal has to be anchored on when it was made instead.
    """
    stated = str(recorded.get("head") or "").strip()
    if stated:
        return bool(head) and recovery.same_revision(stated, head)
    at = parse_utc(recorded.get("at"))
    if at is None:
        return False
    return at >= _row_delivery_instant(row, now=now)


def _review_dispatch_refusal(
    project: str,
    pointer: Mapping[str, Any],
    *,
    row: Mapping[str, Any],
    head: str,
    now: datetime,
) -> dict[str, str] | None:
    """The refusal that makes a missing review the coordinator's to unblock.

    The reflex records every attempt it makes on a run's pointer, and a refused
    attempt is its own account of why the review did not start. While that
    refusal speaks for the revision the run owes a review of, the duty is not
    the reflex's: the reflex will keep refusing until the finding it named is
    answered, so the duty names the answer rather than a window that will pass
    without the run moving. The command is built from the same two helpers the
    refusal itself renders, so the row and the refusal cannot name different
    commands for one finding.

    None means no refusal speaks for the current revision, or that every stored
    review of it has had its blocking findings answered — in both cases the
    reflex's own next sweep is the only actor left to name.
    """
    recorded = pointer.get(recovery.REVIEW_DISPATCH_FIELD)
    if not isinstance(recorded, Mapping):
        return None
    if str(recorded.get("status") or "") != "refused":
        return None
    if not _refusal_speaks_for_revision(recorded, row=row, head=head, now=now):
        return None
    run_id = str(pointer.get("run_id") or "")
    if not run_id:
        return None
    try:
        stored = [
            record
            for _path, record in review_module.stored_records_for_run(project, run_id)
        ]
    except (OSError, ValueError):
        return None
    closed = run_id in ledger.run_ids(project)
    for record in stored:
        if (
            review_lifecycle.lifecycle(
                record, run_closed=closed, later_records=tuple(stored)
            )
            != review_lifecycle.OPEN
        ):
            continue
        undisposed = recovery_review_dispatch._undisposed_blocking_findings(record)
        if not undisposed:
            continue
        index, _finding = undisposed[0]
        fold, exempt = recovery_review_dispatch._finding_dispose_commands(
            project, run_id, index, review=record
        )
        return {
            "reason": str(recorded.get("reason") or "")
            or "the review reflex refused this review's dispatch",
            "next_command": (
                f"dispose the blocking finding with `{fold}` or `{exempt}`"
            ),
        }
    return None


def _reflex_owns_review(project: str, pointer: Mapping[str, Any]) -> bool:
    """Whether an armed follower will dispatch this run's review by itself.

    The reflex reaches a run only from inside the follower of the session that
    dispatched it: the sweep takes the session its registration names, and a run
    another session owns is that coordinator's to compose. So the question a
    printed duty answers is the follower's own liveness for the run's session,
    read from the reader the reflex itself uses (:func:`runs.follower_state`) so
    a duty cannot claim a dispatch the sweep would not make.
    """
    session = str(pointer.get("session") or "")
    if not session:
        return False
    try:
        state = runs.follower_state(project, session)
    except (OSError, ValueError, CrewError):
        return False
    return bool(state.get("live"))


def _reflex_review_window_seconds() -> int:
    """The window a duty may expect the reflex's next sweep within.

    The review pass runs on the follower's sweep cadence, so the expectation is
    read from that cadence rather than restated here.
    """
    try:
        from reckon.crew.resumption import DEFAULT_SWEEP_SECONDS
    except Exception:  # noqa: BLE001 - a printed expectation must not raise
        return DEFAULT_SWEEP_SECONDS_FALLBACK
    return int(DEFAULT_SWEEP_SECONDS)


def _station_slow_stage(
    project: str, pointer: Mapping[str, Any], *, now: datetime
) -> tuple[str, int] | None:
    """The stage and age of a station tick the reflex is waiting behind.

    A delivered run's review is armed by the station's tick, so a tick running
    past the reflex's window in one stage is what holds the dispatch. The
    reading is the one the pane's heading note prints, taken from the station's
    own papertrail, so the duty and the note cannot name different work.
    """
    session = str(pointer.get("session") or "")
    if not session:
        return None
    from reckon.crew import obligation_snapshot

    try:
        answer = obligation_snapshot.read_station_answer(project, session, now=now)
    except (OSError, ValueError):
        return None
    if not isinstance(answer, Mapping):
        return None
    stage = str(answer.get("tick_stage") or "")
    tick_age = answer.get("tick_age_seconds")
    if not stage or not isinstance(tick_age, int):
        return None
    if tick_age <= _reflex_review_window_seconds():
        return None
    return stage, tick_age


def _iso(instant: datetime) -> str:
    """One instant as the UTC stamp the snapshot store reads back."""
    return instant.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _age_of_instant(value: Any, *, now: datetime) -> int:
    """The non-negative age in seconds of one stored instant."""
    stamp = parse_utc(value)
    if stamp is None:
        return 0
    return max(0, int((now - stamp).total_seconds()))


def _overdue_figure(seconds: int) -> str:
    """How an overdue interval is printed in a duty's remedy text."""
    return f"{max(0, int(seconds)) // 60}m {max(0, int(seconds)) % 60}s"


def _overdue_remedy(
    project: str,
    pointer: Mapping[str, Any],
    *,
    age: int,
    dispatch: str,
    now: datetime,
) -> dict[str, Any] | None:
    """The time-dependent inputs of an overdue review duty's text.

    An overdue figure belongs to the reader's clock, not to the instant the
    snapshot was written. The figure is therefore kept as the interval it is
    measured from — the duty's own age, and a station stage's age as the instant
    that stage was observed — so a reader that refreshes the row's age can
    compose the text again at its own instant instead of serving the figure the
    writer printed. Returns ``None`` while the duty is still inside the reflex's
    window, where its text carries no overdue figure.
    """
    window = _reflex_review_window_seconds()
    if age <= window:
        return None
    remedy: dict[str, Any] = {
        "session": str(pointer.get("session") or ""),
        "dispatch": dispatch,
    }
    held = _station_slow_stage(project, pointer, now=now)
    if held is not None:
        stage, stage_age = held
        remedy["stage"] = stage
        remedy["stage_measured_from"] = _iso(now - timedelta(seconds=stage_age))
    return remedy


def _render_overdue_remedy(
    remedy: Mapping[str, Any], *, age: int, now: datetime
) -> str:
    """Compose an overdue duty's text from its intervals at one clock reading."""
    figure = _overdue_figure(int(age))
    stage = str(remedy.get("stage") or "")
    if stage:
        stage_age = _age_of_instant(remedy.get("stage_measured_from"), now=now)
        text = (
            f"review overdue by {figure}: the station's tick has run "
            f"{_overdue_figure(stage_age)} in its {stage} stage, which holds "
            "the reflex's sweep"
        )
        dispatch = str(remedy.get("dispatch") or "")
        if dispatch:
            return f"{text}; dispatch the review now with: {dispatch}"
        return f"{text}; dispatch the review for session {remedy.get('session')!r} now"
    return (
        f"review overdue by {figure}: session {remedy.get('session')!r} "
        "has an armed follower, but its reflex has not dispatched this "
        "run's review; inspect the follower's review pass"
    )


def refresh_overdue_remedy(row: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    """One duty row whose time-dependent remedy text is composed at ``now``.

    A published snapshot stores a duty's age as the instant it is measured
    from, and the reader turns it back into seconds against its own clock. The
    remedy text beside it was composed once, at the writer's instant, so without
    this the figure it prints stays at the value the snapshot was written with
    while ``age_seconds`` grows — a stuck clock beside a moving one. The row
    carries the intervals the figure is built from, so the text is composed
    here at the reading instant, from the same refreshed age the row shows.
    """
    refreshed = dict(row)
    remedy = refreshed.get("overdue_remedy")
    if not isinstance(remedy, Mapping):
        return refreshed
    age = refreshed.get("age_seconds")
    if isinstance(age, bool) or not isinstance(age, int):
        return refreshed
    refreshed["next_command"] = _render_overdue_remedy(remedy, age=age, now=now)
    return refreshed


def _reflex_review_command(
    project: str,
    pointer: Mapping[str, Any],
    *,
    age: int,
    dispatch: str = "",
    now: datetime | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """The duty's next action while an armed reflex owns the dispatch.

    Following the dispatch a missing review prints collides with the reflex's
    own claim on the review JSON path: the race fix keeps the first registered
    review, so nothing is lost, but the loser shows as dispatched then
    withdrawn. Naming the reflex as the owner, with the window its cadence
    gives, is what leaves one actor dispatching a run's review.

    Past the window the reflex has not made good on that promise, so the duty
    names what held it. A station tick running long in one stage is the case a
    coordinator can act on, because the arm that starts each sweep can wait
    behind that stage: the duty carries the stage, its age and the dispatch
    that starts the review now, rather than sending the coordinator to inspect
    a follower whose pass has nothing wrong with it.

    The second element is the overdue remedy's time-dependent inputs, carried
    on the duty so the text can be composed again at a later reading; it is
    ``None`` while the duty is still inside the reflex's window.
    """
    instant = now if now is not None else _utc_now()
    remedy = _overdue_remedy(project, pointer, age=age, dispatch=dispatch, now=instant)
    if remedy is not None:
        return _render_overdue_remedy(remedy, age=age, now=instant), remedy
    session = str(pointer.get("session") or "")
    window = _reflex_review_window_seconds()
    return (
        (
            "the review reflex owns this dispatch: "
            f"session {session!r} has an armed follower, which composes the "
            "review for this run on its next sweep — expected within about "
            f"{window} s (the follower's sweep cadence); no manual dispatch is "
            "needed"
        ),
        None,
    )


def _reflex_open_refusal(
    project: str, pointer: Mapping[str, Any], *, age: int
) -> dict[str, str] | None:
    """The refusal the reflex's own dispatch rule would return, once overdue.

    Inside the window the reflex has not yet been given its chance, so the duty
    still promises its next sweep. Past it, the dispatch predicate decides
    whether any sweep can start the review at all: a run whose newest review
    still owes an answer is refused on every sweep, and the duty must name that
    refusal and the dispose command that lifts it rather than send the
    coordinator to inspect a follower with nothing wrong with it.

    The question is asked of :func:`review_dispatch_refusal`, the read-only form
    of the predicate the dispatch applies, so the duty and the dispatch cannot
    name different reasons for one held review.
    """
    if age <= _reflex_review_window_seconds():
        return None
    report = recovery_review_dispatch.review_dispatch_refusal(pointer)
    if not isinstance(report, Mapping) or not report.get("refused"):
        return None
    reason = str(report.get("reason") or "")
    indices = [
        index
        for index in report.get("undisposed_findings") or ()
        if isinstance(index, int)
    ]
    run_id = str(pointer.get("run_id") or "")
    if not indices or not run_id or not reason:
        return {"reason": reason, "next_command": reason}
    fold, exempt = recovery_review_dispatch._finding_dispose_commands(
        project,
        run_id,
        indices[0],
        # The report names the record the findings were raised on, so a figure
        # review's finding is answered on its own record here too.
        review={
            "rubric": str(report.get("review_rubric") or ""),
            "review_run_id": str(report.get("review_run_id") or ""),
        },
    )
    return {
        "reason": reason,
        "next_command": f"dispose the blocking finding with `{fold}` or `{exempt}`",
    }


def _in_flight_review(
    pointer: Mapping[str, Any], pointers: Sequence[Mapping[str, Any]]
) -> str:
    """The review standing for this run's head, read by the reflex's own rule.

    The rule is :func:`reckon.crew.recovery._review_in_flight`, so a review's
    subject and head are recognised in one place. A caller holding the view's
    snapshot hands it over rather than have the sweep read the live directory
    again. The rule's original one-argument form is still honoured: a reader
    registered in its place without the snapshot keyword is asked for its own
    read, and the answer is the same one the rule would give. Nothing is hidden
    by the fallback — a failure raised inside the rule's body raises again on
    the retry and surfaces from here.
    """
    try:
        return recovery._review_in_flight(pointer, pointers=pointers)
    except TypeError:
        # A reader registered against the rule's one-argument form.
        return recovery._review_in_flight(pointer)


def _current_review_in_flight(pointer: Mapping[str, Any]) -> bool:
    """Whether a live review is working on this run's current revision.

    The reflex's own claim is honoured whatever revision the run now carries.
    A hand-launched review carries no such record, so it is recognised by the
    head-keyed record its reviewer was granted, which keeps a review of an
    older revision from reading as a review of the current one. The record is
    matched by its file name rather than by its whole path: the name spells the
    reviewed run and the head that review read wherever the record is
    delivered, so a review dispatched before the record moved out of the store
    still suppresses the duty for the head it stands at.

    A reviewer the lane accepted into its queue is not working on the revision
    yet — it has not launched — so it does not suppress the duty, and it is
    kept out of the recognition below. Excluding it matters when both stand for
    the same head: a queued request beside a launched review would otherwise be
    able to answer for the head, and the duty would read missing while a review
    is working on it.
    """
    if _reflex_review_in_flight(pointer):
        return True
    launched = [
        candidate
        for candidate in _live_pointers(str(pointer.get("project") or ""))
        if str(candidate.get("phase") or "") != queue_admission.QUEUED_PHASE
    ]
    review_run_id = _in_flight_review(pointer, launched)
    if not review_run_id:
        return False
    try:
        review_pointer = runs.read_pointer(review_run_id)
    except CrewError:
        # The view listed this review from its own snapshot, and the run left
        # before this read: a review promoted or discarded during a slow tick
        # is not working on anything and does not suppress the duty.
        return False
    if not review_pointer:
        return False
    if str(review_pointer.get("phase") or "") == queue_admission.QUEUED_PHASE:
        # Accepted by the lane and waiting to launch: no review is working on
        # the revision yet, so the duty is not suppressed as in flight — it is
        # listed as queued while the run waits for its slot.
        return False
    expected = recovery._review_dispatch_fields(pointer)["write_paths"]
    head_keyed = expected[1:]
    if not head_keyed:
        return True
    node = review_pointer.get("node") or {}
    granted = {Path(str(path)).name for path in node.get("write_paths") or ()}
    return any(Path(str(path)).name in granted for path in head_keyed)


def _refused_delivered_review(pointer: Mapping[str, Any]) -> dict[str, str] | None:
    """Read a live review's refused delivery before asking for another review."""
    reviewed_run_id = str(pointer.get("run_id") or "")
    review_run_id = _in_flight_review(
        pointer, _live_pointers(str(pointer.get("project") or ""))
    )
    if not review_run_id:
        return None
    try:
        reviewer = runs.read_pointer(review_run_id)
    except CrewError:
        return None
    if not reviewer:
        return None
    directory = review_module.run_review_record_directory(runs.run_dir(reviewed_run_id))
    declared = tuple((reviewer.get("node") or {}).get("write_paths") or ())
    record_signatures = []
    for name in declared:
        path = Path(str(name))
        if path.parent != directory:
            continue
        try:
            signature = file_memo.file_signature(path)
        except OSError:
            signature = None
        record_signatures.append((str(path), signature))
    return file_memo.memoized_key(
        "obligation_delivered_review",
        (reviewed_run_id, review_run_id, declared, tuple(record_signatures)),
        lambda: _refused_delivered_review_read(reviewed_run_id, directory, declared),
    )


def _refused_delivered_review_read(
    reviewed_run_id: str, directory: Path, declared_paths: tuple[Any, ...]
) -> dict[str, str] | None:
    """Validate the review files a reviewer declared for this run."""
    refused = None
    for declared in declared_paths:
        path = Path(str(declared))
        if path.parent != directory or not path.is_file():
            continue
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            reasons = (
                review_module.store_review_refusals(body)
                if isinstance(body, Mapping)
                else ["record is not an object"]
            )
        except (OSError, ValueError) as exc:
            reasons = [f"unreadable JSON: {exc}"]
        if not reasons:
            return None
        if refused is None:
            change = (
                "subject"
                if any(
                    "missing project" in reason
                    or "missing reviewed_run_id" in reason
                    or "missing review_run_id" in reason
                    for reason in reasons
                )
                else "status=parsed"
            )
            command = (
                f"reckon crew repair --review --run {shlex.quote(reviewed_run_id)} "
                f"--record {shlex.quote(str(path))} --set {change} "
                "--reason '<why this repair is correct>'"
            )
            refused = {
                "reason": "; ".join(reasons),
                "record": str(path),
                "next_command": command,
            }
    return refused


def _live_review_runs(project: str, session: str) -> set[str]:
    """Snapshot session runs whose current revision has a review in any session."""
    in_flight: set[str] = set()
    for pointer in _live_pointers(project):
        if str(pointer.get("session") or "") != session:
            continue
        run_id = str(pointer.get("run_id") or "")
        if run_id and _current_review_in_flight(pointer):
            in_flight.add(run_id)
    return in_flight


def _classified_rows(project: str) -> list[dict[str, Any]]:
    """Classify current project pointers without observing or launching work.

    A queued request whose refusal the classifier does not read as a session
    duty carries the refusal's remedy here, on the row, because both producers
    of a session's duty list build a queued row through this one helper: the
    derivation and the slice a sweep publishes. Attaching it in one place is
    what keeps the two surfaces from disagreeing about a refused request.
    """
    rows: list[dict[str, Any]] = []
    for pointer in _live_pointers(project):
        row = recovery.classify_pointer(pointer)
        refusal = _queued_contract_refusal(pointer, row)
        if refusal is not None:
            row["contract_refusal"] = refusal
        rows.append(row)
    return rows


def _queued_contract_refusal(
    pointer: Mapping[str, Any], row: Mapping[str, Any]
) -> dict[str, str] | None:
    """The remedy for a queued request admission refused on its node contract.

    A lane hold, a queue-full refusal and a scope conflict all clear by
    themselves, and the classifier already leaves them on the waiting reading.
    A contract refusal does not: the request fails a property a dispatch checks
    before any worker exists, so the admission pass re-tests it on every pass
    and refuses it every time until the property is repaired. Leaving it on the
    waiting reading is what let a project's sections sit unlaunched while the
    obligation surface said they were merely waiting their turn.

    The property names are the refusal's own, so the row's reason tells a
    coordinator what to repair. The remedy follows from which properties
    failed: an editable one is cleared in place, keeping the request's place in
    the queue, while a plan-owned one leaves nothing to edit and the request is
    dropped after the section is repaired.
    """
    if str(pointer.get("phase") or "") != queue_admission.QUEUED_PHASE:
        return None
    if row.get("session_refusal"):
        # The classifier has already read this refusal as a condition of the
        # session and published its own remedy; it outranks this reading.
        return None
    failure = pointer.get("failure")
    if not failure:
        return None
    text = queue_admission._failure_text(failure)
    if queue_admission._NODE_CONTRACT_REFUSAL not in text:
        return None
    if queue_admission._CAPACITY_REFUSAL_REMEDY in text:
        # A full lane names the same condition in its opening words, but an
        # occupying run finishing clears it: that one is a wait, not a repair.
        return None
    run_id = str(pointer.get("run_id") or "")
    properties = _CONTRACT_PROPERTY.findall(text)
    editable = bool(properties) and all(
        name in _EDITABLE_CONTRACT_PROPERTIES for name in properties
    )
    if editable:
        fields = sorted({_EDITABLE_CONTRACT_PROPERTIES[name] for name in properties})
        detail = (
            "the last admission attempt refused this queued request on its node "
            f"contract, which no lane slot clears; the queue edit sets the "
            f"field behind each named property ({', '.join(fields)}): {text}"
        )
        return {
            "detail": detail,
            "next_command": f"reckon crew queue edit --run {run_id}",
        }
    detail = (
        "the last admission attempt refused this queued request on its node "
        "contract, which no lane slot clears; the named properties belong to the "
        "plan section the request was queued from, so repair the section and "
        f"dispatch it again: {text}"
    )
    return {"detail": detail, "next_command": f"reckon crew queue drop --run {run_id}"}


def _acknowledgements_in_force(
    project: str, *, now: datetime
) -> dict[str, dict[str, Any]]:
    """Return each run's recorded deferral that has not yet expired.

    A deferral is read from whichever store the run can own one in: the live
    pointer of a run still in flight, and the run's own file under the crew
    home once it has been promoted and no pointer is left. The promoted file is
    transient state, so reading it costs the acknowledgement and nothing of any
    project's committed history — a deliberate remainder of a promoted run is
    withheld exactly as a live run's is, and expires the same way.

    A deferral whose ``until`` has passed is not returned, so the run it named
    re-enters the list on the next read rather than lingering in the
    acknowledged block. A run whose deferral cannot be parsed as an instant is
    treated as undeferred: an unreadable deadline is no excuse to withhold an
    obligation.
    """
    in_force: dict[str, dict[str, Any]] = {}
    for pointer in _live_pointers(project):
        run_id = str(pointer.get("run_id") or "")
        record = runs.run_acknowledgement(pointer)
        if not run_id or not record:
            continue
        until = parse_utc(record.get("until"))
        if until is not None and until > now:
            in_force[run_id] = record
    for record in runs.recorded_promoted_acknowledgements(project):
        run_id = str(record.get("run_id") or "")
        if not run_id or run_id in in_force:
            continue
        until = parse_utc(record.get("until"))
        if until is not None and until > now:
            in_force[run_id] = {
                "reason": record.get("reason"),
                "until": record.get("until"),
                "recorded_at": record.get("recorded_at"),
            }
    return in_force


def _partition_acknowledged(
    items: list[dict[str, Any]],
    acknowledgements: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split duties into those still owed and those deliberately deferred."""
    owed: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for item in items:
        record = acknowledgements.get(str(item.get("run_id") or ""))
        if record is None:
            owed.append(item)
            continue
        deferred.append(
            {
                **item,
                "reason": str(record.get("reason") or ""),
                "until": str(record.get("until") or ""),
            }
        )
    return owed, deferred


def _live_item(
    row: Mapping[str, Any],
    *,
    kind: str,
    now: datetime,
    pointer: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project one recovery row into the stable obligation shape.

    A kind whose remedy is a repair carries the classifier's own account of the
    refusal as its ``reason``, because "repair the waiting declaration" names
    nothing a session can act on without the text that says what the reader
    refused.
    """
    item = {
        "kind": kind,
        "run_id": str(row.get("run_id") or ""),
        "node": str(row.get("node") or ""),
        "plan": str(row.get("plan") or ""),
        "age_seconds": _row_age(row, now=now),
        "next_command": str(row.get("next_action") or ""),
    }
    if kind in CLASSIFIER_DETAIL_DUTY_KINDS:
        item["reason"] = str(row.get("detail") or "")
    if kind == QUEUED_RUN_DUTY_KIND and pointer is not None:
        hold = queue_admission.recorded_pass_hold(pointer)
        verdict = str(hold.get("verdict") or "")
        if verdict:
            reason = str(hold.get("reason") or "")
            item["reason"] = f"{verdict}: {reason}" if reason else verdict
            item["next_command"] = f"wait: {item['reason']}"
    return item


def admission_verdicts() -> dict[str, dict[str, Any]]:
    """The most recent admission verdict recorded for each queued request.

    Read once per obligations build and handed to every queued row that build
    makes, so a request's stall is decided from one reading of the log rather
    than one read per request, and two rows in one build cannot disagree about
    what the pass last decided. The last line naming a request wins, which is
    how a stall clears: the pass that launches the request records a later
    verdict against it. A log that cannot be read yields no verdicts, because
    the absence of a record is not a record of a stall.
    """
    path = runs.crew_home() / queue_admission.ADMISSION_VERDICT_LOG
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    verdicts: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        for row in record.get("pointers") or ():
            if not isinstance(row, Mapping):
                continue
            run_id = str(row.get("run_id") or "")
            if run_id:
                verdicts[run_id] = dict(row)
    return verdicts


def _stalled_item(
    row: Mapping[str, Any], *, stall: Mapping[str, Any], now: datetime
) -> dict[str, Any]:
    """The duty a request admission has stalled owes the session that queued it.

    Every field is the pass's own record rather than a second derivation: the
    age is how long the request has been admissible, not how long it has
    existed, because a request queued before its section was ready has waited
    long without ever having been launchable, and only the pass measures the
    difference. The stage is the one the request is owed, with that stage's
    duration on the last pass, so a reader learns which of release, launch and
    land to look at rather than only that the wait is overdue. The action names
    the two things a coordinator can do with a request the lane owns: move it
    up the queue, or withdraw it.
    """
    item = _live_item(row, kind=STALLED_RUN_DUTY_KIND, now=now)
    age = stall.get("age_seconds")
    if isinstance(age, (int, float)) and not isinstance(age, bool):
        item["age_seconds"] = max(0, int(age))
    stage = str(stall.get("stage") or "")
    if stage:
        item["stage"] = stage
    stage_seconds = stall.get("stage_seconds")
    if isinstance(stage_seconds, (int, float)) and not isinstance(stage_seconds, bool):
        item["stage_seconds"] = round(float(stage_seconds), 3)
    run_id = str(item.get("run_id") or "")
    item["next_command"] = (
        f"stalled at the {stage or 'admission'} stage: admission could launch it "
        f"and did not — reprioritise it with `reckon crew queue reprioritise "
        f"--run {run_id} --priority 0`, or drop it"
    )
    return item


def _producer_gone_item(row: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    """The duty a waiting run owes when nothing alive can end its wait.

    Every field is the classifier's own evidence rather than a second
    derivation: the probe vector, the value it last reported, the terminal
    states it was waiting for and how long that value has stood still. The
    action names the two remedies left — carry the wait past its gone producer,
    or discard the run — because no sweep will lift it and the session is the
    only thing that can decide.
    """
    item = _live_item(row, kind=PRODUCER_GONE_DUTY_KIND, now=now)
    item["reason"] = str(row.get("detail") or "")
    wait = row.get("external_wait")
    wait = wait if isinstance(wait, Mapping) else {}
    probe = wait.get("probe")
    if probe:
        item["probe"] = " ".join(str(part) for part in probe)
    observed = row.get("wait_observed")
    if observed is not None:
        item["observed"] = str(observed)
    terminal = wait.get("terminal")
    if terminal:
        item["terminal"] = list(terminal)
    verdict = wait.get("producer_gone")
    verdict = verdict if isinstance(verdict, Mapping) else {}
    still = verdict.get("still_seconds")
    if isinstance(still, (int, float)) and not isinstance(still, bool):
        item["still_seconds"] = int(still)
    run_id = str(item.get("run_id") or "")
    item["next_command"] = (
        f"reckon crew resume --run {run_id} --advice continue, or "
        f"reckon crew discard --run {run_id}"
    )
    return item


def _queued_item(
    row: Mapping[str, Any],
    *,
    placements: Mapping[str, Mapping[str, int]],
    now: datetime,
    verdicts: Mapping[str, Mapping[str, Any]] | None = None,
    pointer: Mapping[str, Any] | None = None,
    awaited_review: str = "",
) -> dict[str, Any]:
    """The waiting duty one queued request owes the session that dispatched it.

    It carries the place the admission pass would launch it at when the
    ordering resolved one. Both producers of a session's payload — the
    derivation and the slice a sweep publishes — build a queued row through
    here, so neither lists a queued request the other omits. A refusal naming a
    condition of the session is not built here: that one is the session's own
    duty and is listed under its own kind by each caller before the ordering
    is read, which is what keeps a refusal from costing an ordering that
    cannot be resolved.

    ``verdicts`` is the build's single reading of the admission verdict log. A
    request the pass recorded as stalled is built as a stalled row instead of a
    plain queued one, and only while the request still reads as queued: a
    request that has moved on to another phase is owed its own duty, not one
    left behind by a verdict written before the move.

    ``awaited_review`` names a plan review of the request's plan that is already
    live or queued for the same session. That sentence is the whole wait: the
    refusal's remedy — composing the review — would only queue a duplicate, so
    the row reads as a wait the session cannot shorten and states which review
    ends it. A stall verdict is set aside while the review is in flight for the
    same reason, because the pass having turned the request over is not what the
    session is waiting on.
    """
    run_id = str(row.get("run_id") or "")
    refusal = row.get("contract_refusal")
    if isinstance(refusal, Mapping):
        # The refusal outranks the queue place: a request the admission pass
        # refused on its contract has no launch to wait its turn for.
        item = _live_item(row, kind=QUEUED_CONTRACT_REFUSED_DUTY_KIND, now=now)
        item["reason"] = str(refusal.get("detail") or "")
        item["next_command"] = str(refusal.get("next_command") or "")
        return item
    stall = None if awaited_review else (verdicts or {}).get(run_id)
    if (
        isinstance(stall, Mapping)
        and str(stall.get("verdict") or "") == queue_admission.STALL_VERDICT
        and str(row.get("phase") or "") == queue_admission.QUEUED_PHASE
    ):
        return _stalled_item(row, stall=stall, now=now)
    item = _live_item(row, kind=QUEUED_RUN_DUTY_KIND, now=now, pointer=pointer)
    placement = placements.get(str(row.get("run_id") or ""))
    if placement is not None:
        hold_reason = item.get("reason")
        item.update(placement)
        if hold_reason:
            item["reason"] = hold_reason
        else:
            item["next_command"] = (
                f"waiting for a lane slot: position {placement['position']} "
                f"of the queued requests, rank {placement['rank']} among "
                f"this session's"
            )
    if awaited_review:
        plan = str(row.get("plan") or "")
        named = (
            f"this request is refused for a plan review of {plan} that is "
            f"already live or queued as run {awaited_review}; composing it "
            "again would only queue a duplicate"
        )
        previous = str(item.get("reason") or "")
        item["reason"] = f"{previous}; {named}" if previous else named
        item["review_run_id"] = awaited_review
        item["next_command"] = (
            f"wait: the live or queued plan review {awaited_review} of {plan} "
            "ends this request's refusal"
        )
    return item


def _plan_review_this_request_awaits(
    pointer: Mapping[str, Any] | None,
    live_pointers: Iterable[Mapping[str, Any]],
    session: str,
) -> str:
    """The live or queued plan review a refused request is waiting on, or ``""``.

    A ``plan-review-missing`` refusal is not a defect the session has to repair:
    the remedy it publishes is the command that composes the review, so running
    it while one is already composed only queues a duplicate. When this session
    already has a review of the same plan live or queued, that review is what
    ends the refusal and the request is a wait rather than a duty.

    The refusal is read from the class the admission pass stamps at the head of
    the stored failure, through the reader that owns that stamp, rather than
    from the sentence a remedy composes around it. The review itself is looked
    up by the reader that owns that question, the one the dispatch lock and the
    preview already consult, so a preview, a lock and this view cannot answer
    differently about which review stands for a plan. A review another session
    composed does not clear this one's refusal, because the recovery reading of
    a run is its own session's business.
    """
    pointer = pointer or {}
    node = pointer.get("node")
    failure = pointer.get("failure")
    if not failure or not isinstance(node, Mapping):
        return ""
    stamped = recovery_classification._stamped_refusal_class(
        queue_admission._failure_text(failure)
    )
    if stamped != PlanReviewMissingError.__name__:
        return ""
    plan = str(node.get("plan") or "")
    if not plan:
        return ""
    return recovery_review_dispatch._standing_plan_review(
        str(pointer.get("project") or ""),
        plan,
        session=session,
        pointers=list(live_pointers),
    )


def _queued_row_item(
    row: Mapping[str, Any],
    *,
    placements: Mapping[str, Mapping[str, int]]
    | Callable[[], Mapping[str, Mapping[str, int]]],
    verdicts: Mapping[str, Mapping[str, Any]] | None,
    pointer: Mapping[str, Any] | None,
    live_pointers: Iterable[Mapping[str, Any]],
    session: str,
    now: datetime,
) -> dict[str, Any]:
    """The one reading of a queued row that both payload producers list.

    A queued request is normally a wait on the lane and is built by
    :func:`_queued_item`. Two refusals change that reading: one the session
    must repair, which is its own duty, and a missing plan review that another
    run is already composing, which is still a wait. Which of the three a row
    is, is decided here and nowhere else, so the derivation and the published
    slice cannot disagree about the same row — the snapshot is a slice of this
    view, and a second spelling of the decision is how a slice comes to list a
    request the view does not.

    ``placements`` may be a mapping or a callable returning one, so a producer
    that holds the ordering for a whole pass can hand it over and one that has
    to read it per row can resolve it only for a row that needs it.
    """
    awaited = _plan_review_this_request_awaits(pointer, live_pointers, session)
    if awaited:
        ordered = placements() if callable(placements) else placements
        return _queued_item(
            row,
            placements=ordered,
            now=now,
            verdicts=verdicts,
            pointer=pointer,
            awaited_review=awaited,
        )
    if row.get("session_refusal"):
        return _live_item(row, kind=QUEUED_AWAITING_SESSION_DUTY_KIND, now=now)
    ordered = placements() if callable(placements) else placements
    return _queued_item(
        row, placements=ordered, now=now, verdicts=verdicts, pointer=pointer
    )


def _published_worker_slots(session: str) -> Any:
    """The session's share of the local lane's published admission block.

    The lane's router publishes each session's fair share as ``worker_slots``;
    the session's own entry is the figure the refusal was about, and the
    new-session and lane-wide shares stand in when the map lists no entry for
    it. An unreadable document, or a block publishing no figure, reads as
    ``"unknown"``: absence of a published figure is not a published zero.
    """
    from reckon.crew import lane_document, paid_lanes

    try:
        document = json.loads(paid_lanes.local_lane_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown"
    admission = lane_document.read_lane_admission(document)
    listed = (
        admission.get("sessions", {}).get(session)
        if admission.get("sessions_present")
        else None
    )
    candidates = [
        listed.get("worker_slots") if isinstance(listed, Mapping) else None,
        (
            admission.get(lane_document.ADMISSION_NEW_SESSION_WORKER_SLOTS_KEY)
            if admission.get("present")
            else None
        ),
        admission.get(lane_document.ADMISSION_WORKER_SLOTS_KEY),
    ]
    for value in candidates:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return "unknown"


def _queued_review_fields(
    pointer: Mapping[str, Any],
    *,
    head: str,
    now: datetime,
    live_pointers: Sequence[Mapping[str, Any]],
    placements: Mapping[str, Mapping[str, int]],
) -> dict[str, Any] | None:
    """The queued reading of this run's review, or None.

    Two things mean the review is waiting rather than missing, and both are
    read here so the two carry one kind: an attempt the lane refused as paused,
    which the reflex still owns and retries inside its bound, and a review run
    the lane accepted into its queue and has not launched yet. The second has
    no stored record, so it is the wait the absence of a review would otherwise
    misreport as work the coordinator skipped.
    """
    refused = _refused_review_fields(pointer, head=head, now=now)
    if refused is not None:
        return refused
    return _queued_review_run_fields(
        pointer,
        head=head,
        live_pointers=live_pointers,
        placements=placements,
    )


def _refused_review_fields(
    pointer: Mapping[str, Any], *, head: str, now: datetime
) -> dict[str, Any] | None:
    """The queued reading of a review the lane refused, or None.

    The reflex records every attempt on the run it acted for, dated and naming
    the head it composed for, so a refusal is read from that record rather than
    inferred from an absent review. Only a paused refusal is queued: any other
    refusal is a defect the coordinator must see. A refusal about an earlier
    revision no longer speaks for the head the run carries, and one older than
    the bound has had its chance to be retried, so both fall back to the
    missing reading.
    """
    record = pointer.get(recovery.REVIEW_DISPATCH_FIELD)
    if not isinstance(record, Mapping):
        return None
    if str(record.get("status") or "") != REVIEW_LANE_PAUSED_STATUS:
        return None
    refused_at = parse_utc(record.get("at"))
    if refused_at is None:
        return None
    age = (now - refused_at).total_seconds()
    if age < 0 or age > REVIEW_QUEUED_BOUND_SECONDS:
        return None
    if not recovery._review_head_covers(str(record.get("head") or ""), head):
        return None
    session = str(pointer.get("session") or "")
    slots = _published_worker_slots(session)
    stamp = str(record.get("at") or "")
    return {
        "refused_at": stamp,
        "worker_slots": slots,
        "next_command": (
            f"the lane refused this review dispatch at {stamp} "
            f"(session worker slots: {slots}); the reflex retries for up to "
            f"{REVIEW_QUEUED_BOUND_SECONDS // 60}m"
        ),
    }


def _queued_review_run_fields(
    pointer: Mapping[str, Any],
    *,
    head: str,
    live_pointers: Sequence[Mapping[str, Any]],
    placements: Mapping[str, Mapping[str, int]],
) -> dict[str, Any] | None:
    """The queued review run waiting to launch for this run, or None.

    A review dispatched into the lane's queue is a live pointer carrying the
    queue phase and naming the run it was composed for; it has written no
    record, so nothing in the review store answers for it. It is the same wait
    a refused attempt describes, so it reads as the same kind rather than as a
    queued request of its own: the coordinator sees one duty, not two.

    Which review stands for this run's head is read from
    :func:`reckon.crew.recovery._review_in_flight`, the one recognition the
    reflex itself uses, so a review's subject and head are decided in one place
    and this view cannot recognise a review the reflex does not. That reader is
    handed the view's own pointer snapshot rather than reading the directory
    again, narrowed to the pointers still waiting in the queue. The narrowing
    is what keeps this wait distinct from a review that has launched: that one
    is in flight and suppresses the duty upstream, and it must not be able to
    answer here in place of the queued request beside it, nor make the run read
    as though only the launched review existed.

    The place travels from the admission pass's own ordering, for the same
    reason a queued request's does — this view must not be able to disagree
    with the lane about which request goes first. A pointer whose ``queued_at``
    is unreadable publishes no ``queued_at`` of its own, and no place either:
    the ordering cannot place it, so the reading withholds both rather than
    substituting the read time, which would report a dispatch instant that
    never happened.
    """
    queued = [
        candidate
        for candidate in live_pointers
        if str(candidate.get("phase") or "") == queue_admission.QUEUED_PHASE
    ]
    review_run_id = _in_flight_review(pointer, queued)
    if not review_run_id:
        return None
    review_pointer = next(
        (
            candidate
            for candidate in queued
            if str(candidate.get("run_id") or "") == review_run_id
        ),
        None,
    )
    if review_pointer is None:
        return None
    fields: dict[str, Any] = {}
    queued_at = parse_utc(review_pointer.get("queued_at"))
    if queued_at is not None:
        fields["queued_at"] = queued_at.isoformat()
    placement = placements.get(review_run_id)
    if placement is not None:
        fields.update(placement)
        fields["next_command"] = (
            f"a review is queued for this run at position {placement['position']} "
            f"of the queued requests, rank {placement['rank']} among this "
            "session's"
        )
    else:
        fields["next_command"] = "a review is queued for this run, awaiting a lane slot"
    return fields


def _selected_review(
    project: str, run_id: str, head: str, *, tree: Path | None
) -> tuple[dict[str, Any] | None, str]:
    """Select one run's review again only when its indexed entries move."""
    key = (
        project,
        run_id,
        head,
        str(tree or ""),
        review_records.review_entries_signature(project, run_id),
    )
    return file_memo.memoized_key(
        "obligation_review_selection",
        key,
        lambda: recovery.select_review_for_head(project, run_id, head, tree=tree),
    )


def _plan_state_signature(record: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """A cheap change token for the plan the run's review tier reads.

    The tier's capability risk is declared on the plan or its section, so a
    plan write can move the tier without any run input moving. The token is the
    plan document's stat identity — the same resolution a store read makes, one
    ``stat`` rather than a parse — falling back to the project's published
    write generation when no document is named. ``None`` means no token could
    be built, so the caller computes the tier rather than trusting a cache it
    cannot invalidate.
    """
    project = str(record.get("project") or "")
    plan = str((record.get("node") or {}).get("plan") or "")
    if not project or not plan:
        return None
    root: str | Path | None = str(record.get("repo") or "") or None
    authority = record.get("authority")
    if isinstance(authority, Mapping):
        plan_authority = authority.get("plan")
        if isinstance(plan_authority, Mapping) and plan_authority.get("repository"):
            root = str(plan_authority["repository"])
    try:
        path = _store._resolve_html_file(project, plan, root, "plan")
        if path is not None:
            return ("file", file_memo.file_signature(path))
    except OSError:
        pass
    try:
        return ("generation", _store.read_write_generation(project))
    except OSError:
        return None


def _review_tier_key(
    row: Mapping[str, Any],
    pointer: Mapping[str, Any],
    *,
    head: str,
    tree: Path | None,
) -> tuple[Any, ...] | None:
    """The inputs one run's review tier is decided from, as a cache key.

    The tier reads the run's changed paths and lines at its head (git objects
    beneath the tree), its declared spec level and section, the manifest it
    cites when it cites no commit, and the plan's declared risk. So the key
    carries the resolved head and tree, the cited commits, the manifest's stat
    identity and the plan's change token. A run whose head, manifest or plan
    moves gets a new key and its tier is recomputed; every other run's key is
    the one the previous tick stored.
    """
    run_id = str(row.get("run_id") or "")
    if not run_id:
        return None
    plan_signature = _plan_state_signature(pointer)
    if plan_signature is None:
        return None
    node = pointer.get("node")
    node = node if isinstance(node, Mapping) else {}
    manifest_path = str(pointer.get("manifest_path") or "")
    manifest_signature: tuple[int, ...] | None = None
    if manifest_path:
        try:
            manifest_signature = file_memo.file_signature(manifest_path)
        except OSError:
            manifest_signature = None
    return (
        run_id,
        head,
        str(tree or ""),
        tuple(str(commit) for commit in (row.get("manifest_commits") or [])),
        manifest_signature,
        str(node.get("spec_level") or ""),
        str(node.get("section") or ""),
        plan_signature,
    )


def _clear_cached_obligation_inputs() -> None:
    """Drop every memoised per-run derivation, so the next read recomputes."""
    _INDEX_LAG_READINGS.clear()
    with _HELD_READ_LOCK:
        _HELD_RECORD_READS.clear()
        _HELD_REGISTRY_READS.clear()
    file_memo.clear()


def _review_tier_for_duty(
    row: Mapping[str, Any],
    pointer: Mapping[str, Any],
    *,
    head: str,
    tree: Path | None,
) -> str:
    """The run's review tier, read through the resolver the sweep itself uses.

    The tier rule belongs to promotion, so it is read through
    :func:`reckon.crew.recovery._sweep_review_tier`, which calls the same
    resolver promotion's gate calls on the same inputs — changed paths, changed
    lines, declared spec level and declared capability risk. Re-deriving those
    inputs here is how a duty and a sweep come to disagree about which run owes
    a review, so they are never re-derived. A scope the resolver cannot measure
    already reads there as the fuller tier, so a run whose change is unreadable
    still owes its review.

    The answer is memoised on those same inputs, so a steady-state publish
    reads one run's tier once and reuses it until that run's head, manifest or
    plan moves. The resolver runs git and reads the plan, and both are what a
    tick was spending most of its time on.
    """
    key = _review_tier_key(row, pointer, head=head, tree=tree)
    if key is None:
        return _resolve_review_tier(row, pointer)
    return file_memo.memoized_key(
        "obligation_review_tier", key, lambda: _resolve_review_tier(row, pointer)
    )


def _resolve_review_tier(row: Mapping[str, Any], pointer: Mapping[str, Any]) -> str:
    """Read one run's review tier from its own change, uncached."""
    try:
        return str(
            recovery._sweep_review_tier(
                pointer, list(row.get("manifest_commits") or [])
            )
        )
    except Exception:  # noqa: BLE001 - an unreadable tier owes the fuller review
        return review_tiers.FULL


def _coordinator_resume_in_flight(
    pointer: Mapping[str, Any],
    repair: Mapping[str, Any],
    review: Mapping[str, Any],
) -> bool:
    """Whether a resume a person started is answering this round right now.

    A coordinator who answers a repair by handing the run advice and resuming
    it works the round inside the reviewed run, because the finding is about
    that run's own worktree and its commit claim. While the resumed attempt
    runs, the reflex cannot open a second repair for the same run: it is
    refused for the live worker. So a ``repair-owed`` row names a duty nobody
    can act on, and the stop hook holds every turn end on the refusal the
    reflex already recorded.

    The reflex's own resume is not this case. It records the round it opened as
    an opening status on the pointer, and that attempt keeps the in-flight
    reading. A live resume whose round record carries no opening status is the
    one a person started, and it stands in for this duty while it runs.

    The round is matched on the reviewed head: a live resume of a later round
    than the review's says nothing about the findings this review raised, so
    the duty is left standing.
    """
    if str(repair.get("status") or "") in recovery.REPAIR_ROUND_OPENING_STATUSES:
        return False
    recorded_round = str(repair.get("round_id") or "")
    if recorded_round and recorded_round != repair_module.repair_round_id(review):
        return False
    from reckon.crew.resumption import _attempt_kind, _live_attempt_number

    attempt = _live_attempt_number(pointer)
    if attempt is None:
        return False
    return _attempt_kind(pointer, attempt) == "resume"


def _review_duty_item(
    project: str,
    row: Mapping[str, Any],
    pointer: Mapping[str, Any] | None,
    *,
    now: datetime,
    grace: int,
    live_pointers: Sequence[Mapping[str, Any]] | None = None,
    placements: Mapping[str, Mapping[str, int]] | None = None,
) -> dict[str, Any] | None:
    """One review duty, keyed to the review of the run's current worktree head.

    The kind is decided by the selection promotion uses
    (:func:`reckon.crew.recovery.select_review_for_head`), read for the
    revision the run's tree carries now: a complete review stored at that head
    reads review-ready, and a stored record describing any other revision reads
    review-missing, naming both heads — the revision the review read and the
    head the run now carries — so the reader sees which two disagree rather
    than an absence. With no stored record at all the classifier's own reading
    stands, there being no other head to name.

    A run the tier rule owes no review is the exception the review selection
    cannot see. The tier is read through the resolver the sweep uses, and a
    tier-none run reads the kind a reviewed run with a passing review would
    get, carrying its tier on the duty: the sweep dispatches no review for it
    and the merged-head gate checks it instead, so a duty left review-missing
    would name a dispatch nothing will ever run. Its command is the promotion
    for the same reason: the row's own action is the review dispatch, and a
    duty that read ready to promote while printing that dispatch would name
    the very work the tier rule decided against.

    A reviewer run owes no review of itself: the record it wrote is its own
    deliverable, so its kind stays the classifier's. A row whose live pointer
    is gone keeps the classifier's reading too, there being no tree left to
    ask, and a review the store cannot answer for reads missing rather than
    ready, because the safe direction is to ask for evidence. That reading is
    what a snapshot keeps for a run that has already departed: a ready or stale
    promotion duty it still names is settled at read time, on a ``stat`` of the
    run's pointer, by
    :func:`reckon.crew.obligation_snapshot.without_departed_run_duties`.

    A missing review whose most recent attempt the lane refused as paused reads
    review-queued while the refusal is inside its bound, naming when the lane
    refused it and the session's published worker slots, so the coordinator
    sees a wait the reflex owns rather than a dispatch nobody ran.

    A landing refusal for this head names its guard on the session's duty.
    It takes precedence over a ready review, since the stored review alone
    cannot explain why the automatic landing stopped.

    A run that was resumed owes nothing against the head the resume replaced.
    Whatever this run's delivered revision earned — a missing review, a stale one,
    a tier the merged-head gate checks instead — the evidence speaks for work the
    run has moved past, and a coordinator acting on it spends a member, a lane or
    a promotion on a revision that is no longer the run's. So the duty is
    withdrawn while the run still carries the revision its resume superseded, and
    it returns naming the run's new head as soon as the run delivers one. Returns
    None for a withdrawn duty, which its caller lists as no duty at all.

    ``live_pointers`` and ``placements`` are the caller's own read of the live
    directory and its admission order. A derivation passes them so every duty
    in one view answers from one snapshot; a caller holding neither — a slice
    built from a fleet state the sweep already read, a reader outside any view —
    may leave them out, and the queued reading reads the pointers and the
    ordering for itself when it needs them.
    """
    age = _row_age(row, now=now)
    classification = str(row.get("classification") or "")

    def _classification_kind() -> str:
        return (
            "promotable-stale"
            if classification == "promotable" and age > grace
            else CLASSIFICATION_DUTY_KINDS[classification]
        )

    if pointer is None or recovery._is_review_run(pointer):
        # No live tree to ask, or a reviewer whose own record is its
        # deliverable: the classifier's own reading stands.
        return _live_item(row, kind=_classification_kind(), now=now)

    if (
        classification == "scoring"
        and row.get("review_run_id")
        and row.get("review_run_phase") == "working"
    ):
        item = _live_item(row, kind="review-in-flight", now=now)
        item["reason"] = str(row.get("detail") or "")
        return item

    resumed_head, _tree = recovery._review_head_and_tree(pointer)
    if supersedes_head(pointer, resumed_head):
        return None
    landing = pointer.get("admission_landing")
    if (
        isinstance(landing, Mapping)
        and landing.get("status") == "refused"
        and recovery.same_revision(str(landing.get("head") or ""), resumed_head)
        and str(landing.get("guard") or "").strip()
    ):
        item = _live_item(row, kind="landing-refused", now=now)
        item["head"] = resumed_head
        item["reason"] = f"automatic landing refused: {landing['guard']}"
        # The guard names the rule that refused; the remedy names the one edit
        # that satisfies it. A coordinator reading only the guard goes looking
        # at the command it ran, when the defect is usually a missing record in
        # the evidence behind it. The remedy is carried only when the lander
        # recorded one, so a guard it cannot repair reads exactly as recorded.
        remedy = str(landing.get("remedy") or "").strip()
        if remedy:
            item["reason"] += f"; remedy: {remedy}"
        return item

    head, tree = resumed_head, _tree
    try:
        review, reviewed_head = _selected_review(
            project, str(row.get("run_id") or ""), head, tree=tree
        )
    except (OSError, ValueError):
        review, reviewed_head = None, ""
    tier = _review_tier_for_duty(row, pointer, head=head, tree=tree)
    if review is not None:
        blocking = recovery_review_dispatch._undisposed_blocking_findings(review)
        if blocking:
            repair = pointer.get(recovery_review_dispatch.REPAIR_DISPATCH_FIELD)
            repair = repair if isinstance(repair, Mapping) else {}
            if _coordinator_resume_in_flight(pointer, repair, review):
                return None
            item = _live_item(row, kind="repair-owed", now=now)
            if head:
                item["head"] = head
            if tier == review_tiers.NONE:
                item["review_tier"] = tier
            status = str(repair.get("status") or "")
            item["reason"] = (
                f"the newest review has {len(blocking)} open blocking finding(s); "
                + (
                    f"repair {status}: {repair.get('reason') or 'awaiting progress'}"
                    if status
                    else "repair not opened"
                )
            )
            run_id = str(row.get("run_id") or "")
            if status == "advice-hold":
                item["next_command"] = (
                    f"repair held for coordinator advice; reckon crew resume --run "
                    f"{run_id} --advice '<advice for this repair>'"
                )
            elif status in {"resumed", "dispatched"}:
                repair_run = str(repair.get("run_id") or run_id)
                item["next_command"] = (
                    f"repair in flight; reckon crew observe --run {repair_run}"
                )
            else:
                item["next_command"] = (
                    "the repair reflex owes this run a repair; "
                    f"reckon crew observe --run {run_id}"
                )
            return item
    if tier == review_tiers.NONE:
        item = _live_item(
            row,
            kind="promotable-stale" if age > grace else "review-ready",
            now=now,
        )
        item["review_tier"] = review_tiers.NONE
        item["next_command"] = recovery_classification._promotion_command(
            str(row.get("run_id") or ""),
            pointer,
            list(row.get("manifest_commits") or []),
        )
        item["reason"] = (
            "the run changes no runtime source, so its review tier is none; no "
            "review is owed and the merged-head gate checks it instead"
        )
        return item
    item = _live_item(row, kind=_classification_kind(), now=now)
    refused = _refused_delivered_review(pointer)
    if refused:
        item["kind"] = "review-refused"
        item["reason"] = refused["reason"]
        item["record"] = refused["record"]
        item["next_command"] = refused["next_command"]
        return item
    if head:
        item["head"] = head
    if review is not None and recovery._review_is_complete(review):
        item["kind"] = "promotable-stale" if age > grace else "review-ready"
    else:
        superseded = bool(reviewed_head) and not recovery.same_revision(
            reviewed_head, head
        )
        if superseded:
            # A stored record describes a revision the run has moved past, so
            # both revisions travel on the duty so the reader sees which two
            # disagree.
            item["reviewed_head"] = reviewed_head
        # No stored review covers the head, so the duty would read
        # review-missing. A review the coordinator has already dispatched is
        # the one exception: one the lane refused, or one it queued and has not
        # launched, reads as queued inside its wait and the duty does not hold a
        # turn open over a wait the coordinator cannot shorten. That holds
        # whether or not an older stored record exists, because a review
        # composed for the head after a test-driven repair is still a dispatch
        # already made.
        if live_pointers is None:
            live_pointers = _live_pointers(project)
        if placements is None:
            placements = queue_admission.queued_placements(live_pointers, now=now)
        queued = _queued_review_fields(
            pointer,
            head=head,
            now=now,
            live_pointers=live_pointers,
            placements=placements,
        )
        if queued is not None:
            item["kind"] = REVIEW_QUEUED_DUTY_KIND
            item.update(queued)
        elif superseded:
            item["kind"] = "review-missing"
    if item["kind"] == "review-missing" and classification == "scoring":
        refusal = _review_dispatch_refusal(
            project, pointer, row=row, head=head, now=now
        )
        if refusal is not None:
            # The reflex has already tried and been refused, so naming it as
            # the owner would promise a dispatch its own record says it cannot
            # make. The duty carries the refusal's own account and the answer
            # that lifts it, so the coordinator acts instead of waiting.
            item["reason"] = refusal["reason"]
            item["next_command"] = refusal["next_command"]
        elif _reflex_owns_review(project, pointer):
            # One actor owns review dispatch. While an armed follower will
            # compose this review, the duty names that reflex and the window
            # its cadence promises, as a field beside the words so a reader
            # deciding whether to act reads the same figure the text quotes.
            # A reflex already refused is not a wait, so once its window has
            # passed the duty carries the refusal's own reason and the dispose
            # command that lifts it instead of a follower to inspect.
            held = _reflex_open_refusal(project, pointer, age=age)
            if held is not None:
                item["reason"] = held["reason"]
                item["next_command"] = held["next_command"]
            else:
                # The row's own action is the dispatch this duty replaces only
                # while the reflex still owns it inside the window; past the
                # window it travels into the replacement so the coordinator is
                # handed the command that starts the review rather than a
                # follower to read.
                item["next_command"], overdue_remedy = _reflex_review_command(
                    project,
                    pointer,
                    age=age,
                    dispatch=str(item.get("next_command") or ""),
                    now=now,
                )
                if overdue_remedy is not None:
                    # Keep the intervals the overdue figure is built from, so
                    # a reader of a published snapshot composes the text at its
                    # own clock reading instead of serving a frozen figure.
                    item["overdue_remedy"] = overdue_remedy
                item["review_owner"] = "reflex"
                item["reflex_window_seconds"] = _reflex_review_window_seconds()
    return item


def _live_worktrees(project: str) -> set[Path]:
    """Registered-tree paths a live run currently occupies.

    A retained path reused by a live run is not held by the promoted run whose
    ledger record still names it. The collector sees the path live-referenced
    and leaves it alone, so hinting a collection for it would name work that
    cannot be done and a run that no longer owns the tree.
    """
    trees: set[Path] = set()
    for pointer in _live_pointers(project):
        value = str(pointer.get("worktree") or "").strip()
        if value:
            trees.add(Path(value).expanduser().resolve())
    return trees


def _names_worktree(record: Mapping[str, Any], worktree: Path) -> bool:
    """Whether a run's record names this retained tree.

    Delegates to the shared rule ``crew gc --run`` also resolves a run's tree
    from, so a tree this view names is one that command can reach.
    """
    return record_names_worktree(record, worktree)


def _ledger_read_root(project: str, repository: Path) -> Path | None:
    """The root a ledger read should pass so one derivation builds one index.

    ``repository`` is the checkout that owns the project, resolved from the mount
    registry. A read without a root reaches the same ledger through the
    config-home state directory, which is symlinked into that checkout, and the
    other ledger readers inside a derivation — the utilisation reader among them
    — take that default route. Passing the checkout's own root here would name
    the same file under a second key, and the derivation's run index would then
    be built once for each spelling. So the default root is used whenever it
    names the ledger ``repository`` names; a checkout whose state is not the
    registered one still gets its own root and its own index.
    """
    if (
        ledger.ledger_path(project, None).resolve()
        == ledger.ledger_path(project, repository).resolve()
    ):
        return None
    return repository


def _run_namings(
    project: str, repository: Path
) -> tuple[dict[str, tuple[str, Path | None]], list[dict[str, Any]]] | None:
    """Each run's node id and retained tree, with the rows, from one fresh read.

    A per-run document can name an inspected tree by either route
    :func:`record_names_worktree` applies: the node id it carries, or the path
    its retention carries. Both are taken here from the ledger's own reader,
    which stats every per-run document once and re-decodes one only when it has
    moved since the index it holds. A screen that acted on the published index
    unvalidated would answer from a record older than its file — a run that
    starts retaining a tree after publication keeps its old row, and dropping
    that run is the wrong answer this read exists to avoid. The retained path is
    taken through the naming rule's own helper, so this screen cannot drift from
    the rule it screens for.

    The same rows are returned so the caller files its candidates from the read
    the screen already made, rather than opening each screened-in document a
    second time.

    ``None`` means the reader refused or failed. The caller answers that by
    dropping the screen rather than the derivation: a screen is a bound on the
    work, never the source of the answer, so one that cannot be built costs a
    read and never a duty.
    """

    namings: dict[str, tuple[str, Path | None]] = {}
    try:
        rows = ledger.runs(project, root=_ledger_read_root(project, repository))
    except Exception:  # noqa: BLE001 — a screen must never fail the derivation
        return None
    for row in rows:
        run_id = str(row.get("run_id") or "")
        if not run_id:
            continue
        node = row.get("node")
        node_id = (
            str(node.get("id") or "") if isinstance(node, Mapping) else str(node or "")
        )
        namings[run_id] = (node_id, _retained_worktree(row))
    return namings, rows


def _run_file_rows(project: str, repository: Path) -> list[Mapping[str, Any]]:
    """Every row the run documents carry, read without the merged reader.

    This is the path taken when the reader that supplies the namings refuses or
    fails. The records it would have merged are each still on disk, in the
    document a run's own writer put there, so the derivation asks the ledger's
    own document reader for them rather than answering with nothing: the merged
    reader is how the answer is reached early, not what the answer is.

    The union, its order and the check that a document holds the run it is
    named for all belong to ``reckon.ledger``; this asks for them in the mode
    that skips a document it cannot use instead of refusing the read, so a
    damaged copy costs one row rather than the derivation's answer.
    """

    return ledger.run_rows_from_documents(project, repository)


def _may_name_a_tree(
    run_id: str,
    names: set[str],
    trees: set[Path],
    namings: Mapping[str, tuple[str, Path | None]],
) -> bool:
    """Whether the run ``run_id`` could name one of the inspected trees.

    The routes are exactly the ones :func:`record_names_worktree` applies, so
    the screen never narrows the rule it screens for: a run whose node id
    matches no inspected tree's directory name is still read when the path its
    retention carries is one of those trees, which is how a run dispatched for
    another node comes to hold a tree named for a node it never had. A run the
    index does not carry is read, so the screen never drops a duty on an index
    that lags.
    """

    if not names:
        return True
    naming = namings.get(run_id)
    if naming is None:
        return True
    node_id, retained = naming
    return node_id in names or (retained is not None and retained in trees)


def _row_may_name_a_tree(
    row: Mapping[str, Any],
    names: set[str],
    trees: set[Path],
    namings: Mapping[str, tuple[str, Path | None]],
) -> bool:
    """:func:`_may_name_a_tree` for a row, keyed by the run id it carries."""
    run_id = str(row.get("run_id") or "")
    return bool(run_id) and _may_name_a_tree(run_id, names, trees, namings)


def _holding_record(
    worktree: Path,
    index: WorktreeRecordIndex,
) -> Mapping[str, Any] | None:
    """The run the whole-ledger reader attributes a tree to, or None.

    The tree is looked up in an index built from the records' own naming keys,
    so each record is filed once and the work is proportional to records plus
    trees rather than to their product. Which records name a tree is decided by
    the record's own two namings, exactly as the whole-ledger reader decides it.
    The rows arrive in the merged reader's own order, so the candidate filed
    last is the one that reader puts last: a tree two records name goes to that
    one, not to whichever document happened to be read last.
    """
    candidates = index.records_naming(worktree)
    if not candidates:
        return None
    return candidates[-1]


def _held_worktree_records(project: str) -> dict[Path, Mapping[str, Any]]:
    """Return the record holding each registered, present and unoccupied tree.

    Source identities validate per-run files, live claims, the common Git
    registry and registered-tree presence. Unchanged inputs reuse the answer;
    changed inputs update the retained naming index and only the affected
    trees. New registrations read their own gitdir entries, and new or edited
    run documents read their own ledger rows, without loading the whole ledger.
    An aggregate rewrite rebuilds the index because its envelope has no per-row
    identity. Reader failures retain the unrefused document-reader fallback.
    The read-only region shares parsed document views without copying them.
    """
    docs_dir = _store._docs_dir_for_project(project)
    if docs_dir is None:
        return {}
    repository = docs_dir.parent.resolve()
    with file_memo.read_only_region():
        key = (project, str(repository), _held_records_signature(project, repository))
        if key[2] is None:
            return _held_worktree_records_read(project, repository)
        # Source identity is stable when unrelated checkout files are created.
        return file_memo.memoized(
            "obligation_held_records",
            Path(__file__),
            lambda: _held_records_read_uncached(project, repository, key[2]),
            variant=key,
        )


def _held_records_read_uncached(
    project: str, repository: Path, signature: tuple[Any, ...]
) -> dict[Path, Mapping[str, Any]]:
    """Read the held-tree records once, inside the region a fresh read opens."""
    token = _HELD_SOURCE_SIGNATURE.set(signature)
    try:
        with file_memo.read_only_region():
            return _held_worktree_records_read(project, repository)
    finally:
        _HELD_SOURCE_SIGNATURE.reset(token)


def _held_input_signature(path: Path) -> tuple[int, ...] | None:
    """A source identity; absence is stable, other read errors forfeit reuse."""
    try:
        return file_memo.file_signature(path)
    except FileNotFoundError:
        return None


def _held_directory_signature(directory: Path) -> tuple[Any, ...]:
    """Reuse a directory's listing, checking its files for in-place writes."""
    stamp = _held_input_signature(directory)
    paths = (
        file_memo.memoized(
            "obligation_held_directory",
            directory,
            lambda: tuple(sorted(directory.glob("*.json"))),
        )
        if stamp is not None
        else ()
    )
    return (
        str(directory),
        tuple((str(path), _held_input_signature(path)) for path in paths),
    )


def _held_live_worktrees(project: str) -> tuple[Path, ...]:
    """Resolve live claims only when their pointer inputs change.

    File identities validate the pointer snapshot; its semantic value is the
    set of claimed paths. Unrelated pointer fields and pointers without a
    worktree therefore cannot invalidate the held-record answer.
    """
    held = _VIEW_LIVE_POINTERS.get()
    snapshot = held.get(project) if held is not None else None
    inputs = (
        tuple(sorted({str(row.get("worktree") or "") for row in snapshot}))
        if snapshot is not None
        else _held_directory_signature(runs.live_dir())
    )
    return file_memo.memoized(
        "obligation_held_live_worktrees",
        Path(__file__),
        lambda: tuple(sorted(_live_worktrees(project))),
        variant=(project, inputs),
    )


def _held_present_worktrees(registered: Sequence[Path]) -> tuple[Path, ...]:
    """Validate tree presence through parent listings shared by unchanged calls."""
    grouped: dict[Path, set[str]] = {}
    for tree in registered:
        grouped.setdefault(tree.parent, set()).add(tree.name)
    present: list[Path] = []
    for parent, names in sorted(grouped.items()):
        if _held_input_signature(parent) is None:
            continue

        def read(parent: Path = parent, names: set[str] = names) -> tuple[Path, ...]:
            with os.scandir(parent) as entries:
                return tuple(
                    sorted(
                        parent / entry.name
                        for entry in entries
                        if entry.name in names and entry.is_dir()
                    )
                )

        present.extend(
            file_memo.memoized(
                "obligation_held_present",
                parent,
                read,
                variant=tuple(sorted(names)),
            )
        )
    return tuple(sorted(present))


def _held_records_signature(project: str, repository: Path) -> tuple[Any, ...] | None:
    """The ledger, registered paths and live claims that determine held trees.

    Source identities validate cached listings and live claims. Per-file
    identities detect in-place record or pointer writes, while parent listings
    detect vanished trees. The answer key uses tree presence and occupied
    registered paths, so unrelated directory or pointer churn costs validation
    rather than a new ledger derivation. Unreadable inputs forfeit reuse.
    """
    root = _ledger_read_root(project, repository)
    aggregate = ledger.ledger_path(project, root).resolve()
    try:
        registry_signature = _worktree_registry_signature(repository)
        registered = _held_registered_worktrees(
            repository, signature=registry_signature
        )
        present = _held_present_worktrees(registered)
        occupied = set(_held_live_worktrees(project))
        live_signature = tuple(tree for tree in present if tree in occupied)
        return (
            str(aggregate),
            _held_input_signature(aggregate),
            _held_directory_signature(aggregate.parent / "runs"),
            registry_signature,
            present,
            live_signature,
        )
    except OSError:
        return None


def _worktree_registry_signature(repository: Path) -> tuple[Any, ...]:
    """The common Git registry, including a linked checkout's shared registry."""
    git_dir = _git_directory(repository)
    if git_dir is None:
        raise OSError(f"cannot resolve the worktree registry for {repository}")
    registry = _common_git_directory(git_dir) / "worktrees"
    stamp = _held_input_signature(registry)
    entries = (
        file_memo.memoized(
            "obligation_held_registry_entries",
            registry,
            lambda: tuple(sorted(registry.glob("*/gitdir"))),
        )
        if stamp is not None
        else ()
    )
    return (
        str(registry),
        stamp,
        tuple((str(path), _held_input_signature(path)) for path in entries),
    )


def _held_registered_worktrees(
    repository: Path, *, signature: tuple[Any, ...] | None = None
) -> list[Path]:
    """List Git's worktrees once per registry change, using the shared memo."""
    if signature is None:
        try:
            signature = _worktree_registry_signature(repository)
        except OSError:
            return _registered_worktrees(repository)
    try:
        return file_memo.memoized(
            "obligation_held_registered",
            Path(__file__),
            lambda: _held_registry_changes(repository, signature),
            variant=(str(repository), signature),
        )
    except OSError:
        # A registration can disappear between validation and reading gitdir.
        return _registered_worktrees(repository)


# Retain mutable indexes separately from parsed document views. A refresh updates
# these under one lock; callers receive the file memo's read-only answer view.
_HELD_SOURCE_SIGNATURE: ContextVar[tuple[Any, ...] | None] = ContextVar(
    "held_source_signature", default=None
)
_HELD_READ_LOCK = threading.RLock()
_HELD_RECORD_READS: OrderedDict[tuple[str, str], Any] = OrderedDict()
_HELD_REGISTRY_READS: OrderedDict[str, Any] = OrderedDict()
_HELD_READ_LIMIT = 64


def _held_remember(cache: OrderedDict, key: Any, value: Any) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _HELD_READ_LIMIT:
        cache.popitem(last=False)


def _held_registry_changes(repository: Path, signature: tuple[Any, ...]) -> list[Path]:
    """Read only added or replaced Git registry entries after the first listing."""
    if len(signature) != 3:
        return _registered_worktrees(repository)
    registry, _stamp, identities = signature
    current = dict(identities)
    with _HELD_READ_LOCK:
        previous = _HELD_REGISTRY_READS.get(registry)
        old, paths, base = previous if previous is not None else ({}, {}, set())
        paths = dict(paths)
        for name in old.keys() - current.keys():
            paths.pop(name, None)
        for name, identity in current.items():
            if previous is None or old.get(name) != identity:
                gitfile = Path(Path(name).read_text().strip())
                if gitfile.name != ".git":
                    raise OSError(f"invalid worktree registration: {name}")
                paths[name] = gitfile.parent.resolve()
        if previous is None:
            base = set(_registered_worktrees(repository)) - set(paths.values())
        _held_remember(_HELD_REGISTRY_READS, registry, (current, paths, base))
        return sorted(base | set(paths.values()))


class _HeldWorktreeState:
    """Track affected trees around the shared record naming index."""

    def __init__(self, aggregate_rows: Sequence[Mapping[str, Any]]) -> None:
        self.records = WorktreeRecordIndex()
        self.aggregate = {
            str(row.get("run_id")) for row in aggregate_rows if row.get("run_id")
        }
        self.extra: set[str] = set()
        self.trees: dict[Any, set[Path]] = {}
        self.inspected: set[Path] = set()
        self.held: dict[Path, Mapping[str, Any]] = {}

    def replace(
        self, key: Any, row: Mapping[str, Any] | None, retained: Path | None = None
    ) -> set[Path]:
        names = (
            self.records.remove(key)
            if row is None
            else self.records.replace(key, row, retained=retained, position=key)
        )
        affected: set[Path] = set()
        for name in names:
            affected.update(self.trees.get(name, ()))
        return affected

    def refresh_trees(self, inspected: set[Path], affected: set[Path]) -> None:
        for tree in self.inspected - inspected:
            self.held.pop(tree, None)
            for name in self.records.worktree_keys(tree):
                self.trees[name].discard(tree)
                if not self.trees[name]:
                    del self.trees[name]
        for tree in inspected - self.inspected:
            for name in self.records.worktree_keys(tree):
                self.trees.setdefault(name, set()).add(tree)
            affected.add(tree)
        self.inspected = inspected
        for tree in affected & inspected:
            record = _holding_record(tree, self.records)
            release = record.get("release") if record is not None else None
            if record is None or (
                isinstance(release, Mapping)
                and release.get("worktree_released") is True
            ):
                self.held.pop(tree, None)
            else:
                self.held[tree] = record


def _held_worktree_records_read(
    project: str, repository: Path, *, signature: tuple[Any, ...] | None = None
) -> dict[Path, Mapping[str, Any]]:
    """Apply changed run documents and tree registrations to the previous answer.

    File identities are still validated, including in-place document edits.
    Only changed documents are decoded and filed, and only trees those rows
    name or whose registration/presence/occupancy changed are matched again.
    An aggregate rewrite rebuilds the index because its JSON envelope carries
    no per-row change identities; a refused merged read keeps the unrefused
    document-reader fallback rather than losing duties from healthy records.
    """
    signature = (
        signature
        or _HELD_SOURCE_SIGNATURE.get()
        or _held_records_signature(project, repository)
    )
    if signature is None:
        # The source cannot be validated: make a fresh read, never reuse it.
        registered = _held_registered_worktrees(repository)
        present = tuple(
            tree for tree in registered if tree != repository and tree.is_dir()
        )
        signature = ("", None, ("", ()), None, present, tuple(_live_worktrees(project)))
        reusable = False
    else:
        reusable = True
    _aggregate, aggregate_stamp, documents, _registry, present, occupied = signature
    identities = dict(documents[1])
    inspected = set(present) - set(occupied) - {repository}
    cache_key = (project, str(repository))
    with _HELD_READ_LOCK:
        previous = _HELD_RECORD_READS.get(cache_key) if reusable else None
        if previous is None and not inspected:
            return {}
        if previous is None or previous[0] != aggregate_stamp:
            fresh = _run_namings(project, repository)
            rows = (
                fresh[1] if fresh is not None else _run_file_rows(project, repository)
            )
            try:
                aggregate_rows = ledger.load_aggregate(
                    project, _ledger_read_root(project, repository)
                )[0]["runs"]
            except Exception:  # noqa: BLE001 - unreadable aggregates contribute no rows
                aggregate_rows = []
            index = _HeldWorktreeState(aggregate_rows)
            aggregate_order: dict[Any, deque[int]] = defaultdict(deque)
            for ordinal, row in enumerate(aggregate_rows):
                aggregate_order[
                    (str(row.get("run_id") or ""), ledger.promotion_order_key(row))
                ].append(ordinal)
            for position, row in enumerate(rows):
                run_id = str(row.get("run_id") or "")
                retained = (
                    fresh[0][run_id][1]
                    if fresh is not None and run_id in fresh[0]
                    else _retained_worktree(row)
                )
                if run_id in index.aggregate or not run_id:
                    ordinals = aggregate_order[
                        (run_id, ledger.promotion_order_key(row))
                    ]
                    key = (0, ordinals.popleft() if ordinals else position)
                else:
                    key = (1, run_id)
                    index.extra.add(run_id)
                index.replace(key, row, retained)
            affected = set(inspected)
        else:
            _stamp, old_identities, index = previous
            had_extra = bool(index.extra)
            affected = set()
            for name in old_identities.keys() | identities.keys():
                if old_identities.get(name) == identities.get(name):
                    continue
                source = Path(name)
                run_id = source.stem
                row = None
                if name in identities:
                    try:
                        row = ledger.normalize_identity(ledger._read_run(source))
                    except ledger.LedgerError:
                        # A damaged document cannot erase the aggregate's copy.
                        row = None
                if run_id in index.aggregate:
                    # The aggregate remains authoritative for duplicate copies,
                    # including the unrefused reader's conflict fallback.
                    continue
                if row is None:
                    index.extra.discard(run_id)
                else:
                    index.extra.add(run_id)
                affected.update(
                    index.replace(
                        (1, run_id),
                        row,
                        _retained_worktree(row) if row is not None else None,
                    )
                )
            if had_extra != bool(index.extra):
                affected.update(inspected)
        index.records.order_key = ledger.promotion_order_key if index.extra else None
        index.refresh_trees(inspected, affected)
        if reusable:
            _held_remember(
                _HELD_RECORD_READS, cache_key, (aggregate_stamp, identities, index)
            )
        return dict(index.held)


def _held_worktrees_by_session(
    project: str, *, now: datetime
) -> dict[str, list[dict[str, Any]]]:
    """One session's held-worktree duties, from the trees that are still held.

    The session is the directory the tree was created under, which is the
    session that dispatched the run whose record still names it.
    """
    docs_dir = _store._docs_dir_for_project(project)
    if docs_dir is None:
        return {}
    repository = docs_dir.parent.resolve()
    by_session: dict[str, list[dict[str, Any]]] = {}
    for worktree, record in _held_worktree_records(project).items():
        run_id = str(record.get("run_id") or "")
        if not run_id:
            # No run record names this tree, so there is no run a sweep could
            # be confined to; the ledger itself is the remedy, and the duty
            # must not widen to a repository-wide sweep.
            continue
        command = " ".join(
            shlex.quote(part)
            for part in (
                "reckon",
                "crew",
                "gc",
                "--repo",
                str(repository),
                "--project",
                project,
                "--run",
                run_id,
                "--apply",
            )
        )
        retention = record.get("worktree_retention")
        retained_at = (
            retention.get("retained_at") if isinstance(retention, Mapping) else None
        )
        by_session.setdefault(worktree.parent.name, []).append(
            {
                "kind": "worktree-held",
                "run_id": run_id,
                "node": str(record.get("node") or ""),
                "plan": str(record.get("plan") or ""),
                "age_seconds": _seconds_since(
                    retained_at or record.get("completed_at"),
                    now=now,
                ),
                "next_command": command,
            }
        )
    return by_session


def _held_worktrees(
    project: str, session: str, *, now: datetime
) -> list[dict[str, Any]]:
    """One session's held worktrees, sliced from the fleet-wide derivation."""
    return _held_worktrees_by_session(project, now=now).get(session, [])


def stored_review_for_run(
    project: str,
    run_id: str,
    pointer: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """The one reading of a run's stored review, shared by its two readers.

    A dimension duty follows from a stored review that exists, whether or not
    the run's worktree survives, and the command that disposes a duty has to
    reach the same record the duty was built from: a row the coordinator is
    shown and no command can retire is worse than no row. Both readers take
    this function with these arguments, so the selection cannot drift between
    them. The head comes from the resolver every other reader uses
    (:func:`reckon.crew.recovery._review_head_and_tree`), so a run whose
    worktree has been reclaimed is keyed to the head its own record carries
    rather than to the shared checkout's HEAD, and the tree alongside it is
    the one the head belongs to. A record naming no resolvable head is read
    through the store's newest record for the run, the evidence there is: a
    low dimension is a finding about a review that exists, and a reclaimed
    worktree neither supplies nor retires one. The second element is the head
    a non-matching record named, empty when none, so a writer can name the
    disagreement rather than report an absence.
    """
    head, tree = recovery._review_head_and_tree(pointer)
    if not head:
        return file_memo.memoized_key(
            "obligation_headless_review",
            (project, run_id, review_records.review_entries_signature(project, run_id)),
            lambda: (
                recovery.newest_review_for_headless_run(
                    project, run_id, reclaimed=False
                ),
                "",
            ),
        )
    return _selected_review(project, run_id, head, tree=tree)


def _review_is_hot(
    project: str,
    run_id: str,
    record: Mapping[str, Any],
    *,
    closed: set[str],
) -> bool:
    """Whether a run's stored review still owes a live duty.

    The lifecycle is derived once, in :mod:`reckon.crew.review_lifecycle`, and
    consulted here through its hot-set predicate so the states a live reader
    acts on are not restated. A review lands once its run has a ledger row —
    promoted, or closed without promotion — and is superseded once a later
    review of the same run exists; either way it describes work in the past and
    raises no duty. The run's other stored reviews are the candidates for
    superseding this one, read from the store's own rows so a by-head selection
    cannot hide a later round.
    """
    later = file_memo.memoized_key(
        "obligation_review_rounds",
        (project, run_id, review_records.review_entries_signature(project, run_id)),
        lambda: [
            other
            for _path, other in review_module.stored_records_for_run(project, run_id)
        ],
    )
    return review_lifecycle.is_hot(
        record,
        run_closed=run_id in closed,
        later_records=later,
    )


def _sub_floor_items_by_session(
    project: str,
    floors: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, list[dict[str, Any]]]:
    """Return one duty per undisposed review dimension below its declared floor.

    The review is selected through :func:`stored_review_for_run`, the reading
    the disposition command takes too, so a duty row is always one the writer
    can retire and a record describing a superseded revision cannot stand in
    for the current one. A run whose worktree has been reclaimed is keyed to
    the head its own record carries rather than to the shared checkout's HEAD.
    Each row names the run the review is about — the run whose work carries
    the low dimension — and carries the dimension, the score it was given and
    the floor it fell below, so a reader can see the finding without opening
    the record.

    A review with no floor declared for the dimension, and a dimension already
    answered by a disposition in the closed set, produce nothing. The total is
    not consulted: a high total over four strong dimensions does not retire a
    fifth one that sits below its floor, which is the case this duty exists
    for.
    """
    if not floors:
        return {}
    closed = recovery._promoted_run_ids(project)
    by_session: dict[str, list[dict[str, Any]]] = {}
    for pointer in _live_pointers(project):
        session = str(pointer.get("session") or "")
        run_id = str(pointer.get("run_id") or "")
        if not run_id:
            continue
        signature = review_records.review_entries_signature(project, run_id)
        newest = file_memo.memoized_key(
            "obligation_review_presence",
            (project, run_id, signature),
            lambda run_id=run_id: review_module.read_review(project, run_id),
        )
        if newest is None:
            # A run with no stored review owes no dimension duty, and the head
            # is consulted only to choose between reviews that exist, so
            # resolving it here would launch one git process per live run per
            # sweep for an answer that cannot name a duty.
            continue
        record, _described = stored_review_for_run(project, run_id, pointer)
        if not record:
            continue
        if not _review_is_hot(project, run_id, record, closed=closed):
            # A review is a live duty only while it is hot. A run review lands
            # once its run has a ledger row and is superseded once a later
            # review of the same run exists, and a landed or superseded review
            # is the archive: it describes work in the past, so a dimension
            # below its floor is kept but no longer owed.
            continue
        node = pointer.get("node") or {}
        for finding in review_dispositions.sub_floor_dimensions(record, floors):
            # The fold and the exemption are the two shapes one disposition
            # takes, rendered once by the writer's own module so a duty row and
            # a refusal cannot spell the same command two ways. They travel as
            # two fields because next_command is read as one runnable command,
            # and a second --kind in it would name two dispositions at once.
            fold, exempt = review_dispositions.dispose_commands(
                project,
                run_id,
                f"--dimension {shlex.quote(str(finding['dimension']))}",
            )
            by_session.setdefault(session, []).append(
                {
                    "kind": SUB_FLOOR_DUTY_KIND,
                    "run_id": run_id,
                    "node": str(node.get("id") or run_id),
                    "plan": str(node.get("plan") or ""),
                    "age_seconds": _seconds_since(record.get("timestamp"), now=now),
                    "dimension": finding["dimension"],
                    "score": finding["score"],
                    "floor": finding["floor"],
                    "next_command": fold,
                    "exempt_alternative": exempt,
                }
            )
    return by_session


def _sub_floor_items(
    project: str,
    session: str,
    floors: Mapping[str, Any],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """One session's sub-floor duties, sliced from the fleet-wide derivation."""
    return _sub_floor_items_by_session(project, floors, now=now).get(session, [])


def recent_limit_resets(session: str) -> list[dict[str, Any]]:
    """The recent limit resets that apply to one session, newest first.

    Read through the store's own reader with the horizon a re-armed producer
    announces by, so a session's limits are the ones its pane would still draw
    and the coordinator hook's prompt line cannot disagree with this view. The
    horizon is imported here rather than at module level: the watch module that
    owns it reaches back into this one.
    """
    from reckon.crew.lift_watch import RESET_HORIZON

    return budget_reset.recent_resets(session=session, horizon=RESET_HORIZON)


def _index_lag_paths(repository: Path) -> tuple[tuple[str, ...], str]:
    """The paths HEAD and the working tree hold while the index stages a delete.

    ``git diff --cached --diff-filter=D`` lists what the index records as
    deleted against HEAD. A path counts only when the file is still on disk and
    the checkout does not ignore it: a staged deletion whose file is gone is an
    ordinary intended removal, and a path untracked with ``git rm --cached``
    and then ignored is a deliberate one. The two are not distinguishable with
    certainty, which is why the finding is a notice naming the restore rather
    than a repair applied on the reader's behalf.
    """
    staged = run_git(
        repository,
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=D",
        "-z",
        "HEAD",
        env=INDEX_LAG_GIT_ENV,
    )
    names = (
        [os.fsdecode(name) for name in staged.stdout.split(b"\0") if name]
        if staged.returncode == 0
        else []
    )
    present = [name for name in names if (repository / name).exists()]
    if not present:
        return (), ""
    ignored_read = run_git(
        repository,
        "check-ignore",
        "-z",
        "--stdin",
        input=b"\0".join(os.fsencode(name) for name in present) + b"\0",
        env=INDEX_LAG_GIT_ENV,
    )
    if ignored_read.returncode not in (0, 1):
        return (), ""
    ignored = {os.fsdecode(name) for name in ignored_read.stdout.split(b"\0") if name}
    paths = tuple(sorted(set(present) - ignored))
    if not paths:
        return (), ""
    head_read = run_git(repository, "rev-parse", "HEAD", env=INDEX_LAG_GIT_ENV)
    head = (
        head_read.stdout.decode("utf-8", "surrogateescape").strip()
        if head_read.returncode == 0
        else ""
    )
    return paths, head


def _record_index_lag(paths: Sequence[str], head: str, *, now: datetime) -> None:
    """Append one line to the crew home's lag log unless this lag is recorded.

    The log is the only durable trace of an index that drifted, and its purpose
    is matching a later occurrence to its writer, so it names the instant, HEAD
    and the paths once per distinct lag. Best effort throughout: an unwritable
    crew home must not take the view down with it.
    """
    home = crew_home()
    key = hashlib.sha256(
        (head + "\0" + "\0".join(paths)).encode("utf-8", "surrogateescape")
    ).hexdigest()
    try:
        home.mkdir(parents=True, exist_ok=True)
        with (home / (INDEX_LAG_SEEN_NAME + ".lock")).open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                seen_path = home / INDEX_LAG_SEEN_NAME
                try:
                    seen = json.loads(seen_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    seen = {}
                if not isinstance(seen, dict):
                    seen = {}
                if key in seen:
                    return
                stamp = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
                line = json.dumps({"at": stamp, "head": head, "paths": list(paths)})
                with (home / INDEX_LAG_LOG_NAME).open("a", encoding="utf-8") as log:
                    log.write(line + "\n")
                seen[key] = stamp
                pending = seen_path.with_name(seen_path.name + ".tmp")
                pending.write_text(
                    json.dumps(seen, sort_keys=True) + "\n", encoding="utf-8"
                )
                os.replace(pending, seen_path)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return


def _index_lag_reading_key(repository: Path) -> tuple[Any, ...] | None:
    """What must move for a cached lag reading to stop describing this checkout.

    The index's stat identity moves on any write to it, and the head identity
    moves whenever the resolved commit does, so the key changes on both of the
    events that can make a cached notice wrong. The head identity is the one
    the follower's per-second freshness read uses: it resolves a linked
    worktree's pointer and packed refs as files, never by spawning git. None
    means the head cannot be read as files, and the caller then caches
    nothing — an unreadable identity is not a stable one.
    """
    head_identity = _head_identity(repository)
    if head_identity is None:
        return None
    git_dir = _git_directory(repository)
    try:
        index_signature: Any = file_memo.file_signature(
            (git_dir if git_dir is not None else repository / ".git") / "index"
        )
    except OSError:
        index_signature = None
    return (str(repository), index_signature, head_identity)


def _index_lag_notice(project: str, *, now: datetime) -> dict[str, Any] | None:
    """The notice one project's main checkout raises when its index lags HEAD.

    Read from the project's registered mount, which is the checkout every
    producer and follower of that project writes to, rather than from the
    caller's enclosing worktree. The notice is not run-scoped: it belongs to
    the project, so every session working it sees the same row.

    The reading is cached against the checkout's index stat identity and HEAD,
    and dropped when a named path stops existing: a sweep recomputes every
    session's payload on each tick, and a git subprocess per tick to learn that
    nothing moved is the cost that cache avoids. A write to the index or a
    moved HEAD changes the key, so the next read re-derives.
    """
    from reckon.crew.dispatch_sections import project_mount_repository

    repository = project_mount_repository(project)
    if repository is None:
        return None
    key = _index_lag_reading_key(repository)
    store_key = str(repository)
    cached = _INDEX_LAG_READINGS.get(store_key) if key is not None else None
    if cached is not None and cached[0] == key:
        notice = cached[1]
        if notice is None:
            return None
        if all((repository / path).exists() for path in notice["paths"]):
            return dict(notice)
    paths, head = _index_lag_paths(repository)
    if not paths:
        if key is not None:
            _INDEX_LAG_READINGS[store_key] = (key, None)
        return None
    _record_index_lag(paths, head, now=now)
    restore = "git -C {} reset -q -- {}".format(
        shlex.quote(str(repository)),
        " ".join(shlex.quote(path) for path in paths),
    )
    notice = {
        "kind": INDEX_LAG_DUTY_KIND,
        "run_id": "",
        "age_seconds": 0,
        "head": head,
        "paths": list(paths),
        "reason": (
            "the checkout's index stages these paths as deleted while HEAD and "
            "the working tree both still carry them, so a plain `git commit` "
            "there would record them as deleted"
        ),
        "next_command": restore,
    }
    if key is not None:
        _INDEX_LAG_READINGS[store_key] = (key, notice)
    return dict(notice)


def obligations(
    project: str, session: str, *, fields: Iterable[str] | None = None
) -> dict[str, Any]:
    """Return every current duty owed by one coordinator session.

    Live duty kinds are a projection of :func:`recovery.recover`; the
    promotable grace comes from the same resolved flight configuration used by
    dispatch.  The result is ordered oldest first so the first row is also the
    most urgent age signal.

    Every duty row is built here and projected here, through the one projection
    :func:`reckon.crew.query.project_run_rows`, so this view carries the section
    the run was dispatched for and refuses a field it does not have.

    The live pointers are read once and published for the length of the
    derivation, so every duty kind answers from one snapshot of the directory
    and a run in flight cannot appear in one duty and be absent from another
    read in the same view.
    """
    token = _VIEW_LIVE_POINTERS.set({project: tuple(runs.list_live(project=project))})
    try:
        return _obligations_view(project, session, fields=fields)
    finally:
        _VIEW_LIVE_POINTERS.reset(token)


def _obligations_view(
    project: str, session: str, *, fields: Iterable[str] | None = None
) -> dict[str, Any]:
    """The duties of one session, read from the view's published pointers."""
    now = _utc_now()
    resolved = flight.resolve(project)
    config = resolved.config
    grace = parse_duration(
        str((config.get("fences") or {}).get("unreconciled_run_grace") or "15m")
    )
    reviews_in_flight = _live_review_runs(project, session)
    items: list[dict[str, Any]] = []
    pointers = {
        str(pointer.get("run_id") or ""): pointer for pointer in _live_pointers(project)
    }
    live_pointers = list(pointers.values())
    # Read once for the whole view, from the ordering the admission pass admits
    # by: a queued row and a queued review duty both report their place from
    # it, so every pointer is read once and no two duties can disagree about
    # which request goes first. A project with nothing queued orders nothing.
    placements = queue_admission.queued_placements(live_pointers, now=now)
    # Read once for the whole view, for the same reason: a queued row is told
    # from a stalled one by the verdict the admission pass last recorded for it,
    # so one reading serves every row this build makes.
    verdicts = admission_verdicts()
    for pointer in live_pointers:
        if pointer.get("phase") != queue_admission.HELD_PHASE:
            continue
        if str(pointer.get("session") or "") != session:
            continue
        hold = pointer.get("queue_hold") or {}
        started = parse_utc(hold.get("started_at"))
        if started is None or (now - started).total_seconds() <= 24 * 3600:
            continue
        run_id = str(pointer.get("run_id") or "")
        items.append(
            {
                "kind": "run-held",
                "run_id": run_id,
                "node": str((pointer.get("node") or {}).get("id") or ""),
                "plan": str(
                    pointer.get("plan") or (pointer.get("node") or {}).get("plan") or ""
                ),
                "age_seconds": int((now - started).total_seconds()),
                "reason": str(hold.get("reason") or ""),
                "next_command": f"reckon crew queue release --run {run_id}",
            }
        )
    # A request waiting on a run no project holds is listed under its own kind
    # rather than as a waiting row: the admission pass records the reason on
    # the pointer, and no pass will ever clear it. The wait itself is read from
    # the pass's own obligation record, so this view cannot disagree with the
    # reading that failed to resolve it.
    for pointer in live_pointers:
        details = queue_admission.unresolvable_wait_obligations(pointer)
        if not details:
            continue
        if str(pointer.get("session") or "") != session:
            continue
        run_id = str(pointer.get("run_id") or "")
        started = parse_utc(pointer.get("waits_at") or pointer.get("created_at"))
        items.append(
            {
                "kind": WAIT_UNRESOLVABLE_DUTY_KIND,
                "run_id": run_id,
                "node": str((pointer.get("node") or {}).get("id") or ""),
                "plan": str(
                    pointer.get("plan") or (pointer.get("node") or {}).get("plan") or ""
                ),
                "age_seconds": (int((now - started).total_seconds()) if started else 0),
                "reason": details[0],
                "next_command": (
                    f"fix the wait or drop the request: no project holds the run "
                    f"this request waits on, so it can never be released while "
                    f"the ref stands (reckon crew discard --run {run_id})"
                ),
            }
        )
    # Requests an admission pass held because a plan unit they read carries no
    # stored review are one duty per plan, not one per request: one composed
    # review answers every one of them, so the row names how many wait and the
    # command that releases them.
    review_holds: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for pointer in live_pointers:
        if pointer.get("phase") != queue_admission.HELD_PHASE:
            continue
        if str(pointer.get("session") or "") != session:
            continue
        hold = pointer.get("queue_hold")
        if not isinstance(hold, dict) or not hold.get("automatic"):
            continue
        if str(hold.get("reason") or "") != queue_admission.PLAN_REVIEW_HELD_REASON:
            continue
        plan = str(pointer.get("plan") or (pointer.get("node") or {}).get("plan") or "")
        owner = str(pointer.get("project") or project)
        review_holds.setdefault((owner, plan), []).append(pointer)
    if review_holds:
        from reckon.crew import plan_review

        for (owner, plan), held in review_holds.items():
            started = [
                parse_utc((pointer.get("queue_hold") or {}).get("started_at"))
                for pointer in held
            ]
            ages = [int((now - moment).total_seconds()) for moment in started if moment]
            items.append(
                {
                    "kind": "plan-review-owed",
                    "run_id": str(held[0].get("run_id") or ""),
                    "plan": plan,
                    "held_count": len(held),
                    "age_seconds": min(ages) if ages else 0,
                    "reason": queue_admission.PLAN_REVIEW_HELD_REASON,
                    "next_command": plan_review.review_invocation(
                        owner, plan, session=session
                    ),
                }
            )
    # A request the most recent admission pass found held by a finished run:
    # the holder's own session is the one that can settle it, so the duty is
    # raised there rather than left to read as an ordinary wait. A session
    # unattended past the idle bound is not coming back on its own, so the
    # waiting request's session is told as well and can settle the holder or
    # ask the lead. Every reader takes the hold from the pointer, which the
    # pass wrote, rather than re-deriving the claim.
    classified = _classified_rows(project)
    classified_by_run = {str(row.get("run_id") or ""): row for row in classified}
    for pointer in live_pointers:
        if str(pointer.get("phase") or "") != queue_admission.QUEUED_PHASE:
            continue
        hold = queue_admission.recorded_pass_hold(pointer)
        holder = hold.get("holder")
        if not isinstance(holder, Mapping):
            continue
        holder_run = str(holder.get("run_id") or "")
        holder_pointer = pointers.get(holder_run)
        if not holder_run or holder_pointer is None:
            continue
        holder_row = classified_by_run.get(holder_run) or {}
        status = str(holder_row.get("manifest_status") or "").strip().lower()
        if status not in TERMINAL_MANIFEST_STATUSES:
            continue
        waiting_session = str(pointer.get("session") or "")
        holder_session = str(
            holder.get("session") or holder_pointer.get("session") or ""
        )
        if session == holder_session:
            pass
        elif session == waiting_session and holder_session:
            idle = _holder_idle_seconds(
                project,
                holder_session,
                [
                    candidate
                    for candidate in live_pointers
                    if str(candidate.get("session") or "") == holder_session
                ],
                now=now,
            )
            if idle is None or idle < HOLDER_IDLE_SECONDS:
                continue
        else:
            continue
        queued_at = parse_utc(pointer.get("queued_at"))
        age = int((now - queued_at).total_seconds()) if queued_at else 0
        waiting_run = str(pointer.get("run_id") or "")
        node = pointer.get("node") or {}
        holder_phase = str(holder.get("phase") or holder_pointer.get("phase") or "")
        item: dict[str, Any] = {
            "kind": HELD_BY_FINISHED_RUN_DUTY_KIND,
            "run_id": waiting_run,
            "node": str(node.get("id") or waiting_run),
            "plan": str(pointer.get("plan") or node.get("plan") or ""),
            "age_seconds": age,
            "holder_run_id": holder_run,
            "holder_session": holder_session,
            "holder_phase": holder_phase,
            "reason": (
                f"run {holder_run!r} (session {holder_session!r}, phase "
                f"{holder_phase!r}) has ended with the manifest status {status!r} "
                f"and its claim still fences a write path this request names, so "
                "nothing will launch it until that run is settled"
            ),
            "next_command": recovery_classification._promotion_command(
                holder_run,
                holder_pointer,
                list(holder_row.get("manifest_commits") or []),
            ),
        }
        held_path = str(holder.get("path") or "")
        if held_path:
            item["held_path"] = held_path
        items.append(item)
    for row in classified:
        if str(row.get("session") or "") != session:
            continue
        if (
            pointers.get(str(row.get("run_id") or ""), {}).get("phase")
            == queue_admission.HELD_PHASE
        ):
            continue
        classification = str(row.get("classification") or "")
        recovery_reading = str(row.get("recovery_classification") or "")
        if classification == "queued":
            # A queued request is the lane's to launch, but it is not missing:
            # for as long as its pointer exists it is listed as waiting, beside
            # its place in the queue, so a coordinator reading this view sees
            # the request it dispatched rather than an absence. The place is
            # the admission pass's own, read from the ordering that pass
            # admits by, so this view cannot disagree with the lane about
            # which request goes first. A refusal naming the session is
            # listed under its own kind instead: nothing on the lane clears
            # that one, so the session owes the fix. The one refusal that is
            # not the session's to fix is a missing plan review already being
            # composed: that request waits on the review, so it keeps the
            # waiting reading and names the run that ends it.
            items.append(
                _queued_row_item(
                    row,
                    placements=placements,
                    verdicts=verdicts,
                    pointer=pointers.get(str(row.get("run_id") or "")),
                    live_pointers=live_pointers,
                    session=session,
                    now=now,
                )
            )
            continue
        kind = ""
        if classification == PRODUCER_GONE_STATUS:
            # Nothing alive can change what the probe reports, so this wait is
            # a decision rather than a wait: the duty carries the evidence and
            # the two remedies, and holds the turn open until one is taken.
            items.append(_producer_gone_item(row, now=now))
            continue
        if classification in REVIEW_CLASSIFICATION_KINDS:
            if (
                classification == "scoring"
                and str(row.get("run_id") or "") in reviews_in_flight
                and row.get("review_run_phase") != "working"
                and not (
                    pointers.get(str(row.get("run_id") or ""))
                    and _refused_delivered_review(
                        pointers[str(row.get("run_id") or "")]
                    )
                )
            ):
                continue
            item = _review_duty_item(
                project,
                row,
                pointers.get(str(row.get("run_id") or "")),
                now=now,
                grace=grace,
                live_pointers=live_pointers,
                placements=placements,
            )
            if item is not None:
                items.append(item)
            continue
        if recovery_reading in RECOVERY_CLASSIFICATION_DUTY_KINDS:
            kind = RECOVERY_CLASSIFICATION_DUTY_KINDS[recovery_reading]
        else:
            kind = CLASSIFICATION_DUTY_KINDS.get(classification, "")
        if kind:
            items.append(_live_item(row, kind=kind, now=now))

    items.extend(
        _sub_floor_items(
            project,
            session,
            review_dispositions.declared_dimension_floors(config),
            now=now,
        )
    )
    items.extend(_held_worktrees(project, session, now=now))
    # A main checkout's index can stage a path as deleted while HEAD and the
    # working tree both hold the file, and the next plain commit there records
    # the deletion. The condition belongs to the project rather than to a run,
    # so every session working it is shown the same notice.
    index_lag = _index_lag_notice(project, now=now)
    if index_lag is not None:
        items.append(index_lag)
    acknowledgements = _acknowledgements_in_force(project, now=now)
    items, acknowledged = _partition_acknowledged(items, acknowledgements)
    items.sort(
        key=lambda item: (
            -int(item["age_seconds"]),
            str(item["run_id"]),
            str(item["kind"]),
        )
    )
    acknowledged.sort(
        key=lambda item: (
            str(item["until"]),
            str(item["run_id"]),
            str(item["kind"]),
        )
    )
    closure = runs.drain(project, session=session)
    items = [_with_action_form(item) for item in items]
    acknowledged = [_with_action_form(item) for item in acknowledged]
    sections = run_section_index([pointers.values()])
    summary = {
        "count": len(items),
        "oldest_age_seconds": max(
            (int(item["age_seconds"]) for item in items), default=0
        ),
        "unreconciled_runs": int(closure["unreconciled_runs"]),
    }
    view = {
        "project": project,
        "session": session,
        "obligations": project_run_rows(
            items,
            fields=fields,
            view="obligations",
            vocabulary=DUTY_ROW_FIELDS,
            sections=sections,
        ),
        "acknowledged": project_run_rows(
            acknowledged,
            fields=fields,
            view="obligations",
            vocabulary=DUTY_ROW_FIELDS,
            sections=sections,
        ),
        "summary": summary,
    }
    # Carried only when the session has one. The published snapshot is sliced
    # from fleet state and compared with this view field for field, and a
    # prompt reads the reset store itself rather than the payload, so a key
    # present on one surface and absent from the other would read as a
    # divergence where there is no difference.
    resets = recent_limit_resets(session)
    if resets:
        view["limit_resets"] = resets
    return view
