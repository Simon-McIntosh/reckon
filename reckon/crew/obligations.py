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

import json
import shlex
from collections.abc import Iterable, Mapping, Sequence
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import _store, file_memo, flight, ledger, review_tiers
from reckon._timestamps import parse_utc
from reckon.crew import (
    queue_admission,
    recovery,
    recovery_classification,
    review_dispositions,
    review_lifecycle,
    runs,
)
from reckon.crew import review as review_module
from reckon.crew.node import INTERRUPTED_RUN_PHASE, CrewError, parse_duration
from reckon.crew.query import project_run_rows, run_section_index
from reckon.crew.recovery_review_dispatch import supersedes_head
from reckon.crew.routing import (
    WorktreeRecordIndex,
    _registered_worktrees,
    _retained_worktree,
    record_names_worktree,
)
from reckon.mcp_crew_actions import CREW_WRITE_ACTIONS

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
RECOVERY_CLASSIFICATION_DUTY_KINDS = {"needs-help": "needs-help"}

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
    # Set when an armed follower, rather than the coordinator, will dispatch it.
    "review_owner",
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

# The duty kinds that are listed but never hold a stop open: the work behind
# them is already owned by a reflex that retries, so blocking would ask the
# coordinator to act on a wait it cannot shorten.
NON_BLOCKING_DUTY_KINDS = frozenset(
    {REVIEW_QUEUED_DUTY_KIND, QUEUED_RUN_DUTY_KIND, STALLED_RUN_DUTY_KIND}
)


def _utc_now() -> datetime:
    """Return the observation instant through a patchable clock boundary."""
    return datetime.now(tz=UTC)


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


def _reflex_review_command(project: str, pointer: Mapping[str, Any]) -> str:
    """The duty's next action while an armed reflex owns the dispatch.

    Following the dispatch a missing review prints collides with the reflex's
    own claim on the review JSON path: the race fix keeps the first registered
    review, so nothing is lost, but the loser shows as dispatched then
    withdrawn. Naming the reflex as the owner, with the window its cadence
    gives, is what leaves one actor dispatching a run's review.
    """
    session = str(pointer.get("session") or "")
    window = _reflex_review_window_seconds()
    return (
        f"the review reflex owns this dispatch: session {session!r} has an armed "
        "follower, which composes the review for this run on its next sweep — "
        f"expected within about {window} s (the follower's sweep cadence); "
        "no manual dispatch is needed"
    )


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
    review_pointer = runs.read_pointer(review_run_id)
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
    refused = None
    for declared in (reviewer.get("node") or {}).get("write_paths") or ():
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
    """Classify current project pointers without observing or launching work."""
    return [recovery.classify_pointer(pointer) for pointer in _live_pointers(project)]


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


def _live_item(row: Mapping[str, Any], *, kind: str, now: datetime) -> dict[str, Any]:
    """Project one recovery row into the stable obligation shape."""
    return {
        "kind": kind,
        "run_id": str(row.get("run_id") or ""),
        "node": str(row.get("node") or ""),
        "plan": str(row.get("plan") or ""),
        "age_seconds": _row_age(row, now=now),
        "next_command": str(row.get("next_action") or ""),
    }


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


def _queued_item(
    row: Mapping[str, Any],
    *,
    placements: Mapping[str, Mapping[str, int]],
    now: datetime,
    verdicts: Mapping[str, Mapping[str, Any]] | None = None,
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
    """
    run_id = str(row.get("run_id") or "")
    stall = (verdicts or {}).get(run_id)
    if (
        isinstance(stall, Mapping)
        and str(stall.get("verdict") or "") == queue_admission.STALL_VERDICT
        and str(row.get("phase") or "") == queue_admission.QUEUED_PHASE
    ):
        return _stalled_item(row, stall=stall, now=now)
    item = _live_item(row, kind=QUEUED_RUN_DUTY_KIND, now=now)
    placement = placements.get(str(row.get("run_id") or ""))
    if placement is not None:
        item.update(placement)
        item["next_command"] = (
            f"waiting for a lane slot: position {placement['position']} "
            f"of the queued requests, rank {placement['rank']} among "
            f"this session's"
        )
    return item


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


def _review_tier_for_duty(row: Mapping[str, Any], pointer: Mapping[str, Any]) -> str:
    """The run's review tier, read through the resolver the sweep itself uses.

    The tier rule belongs to promotion, so it is read through
    :func:`reckon.crew.recovery._sweep_review_tier`, which calls the same
    resolver promotion's gate calls on the same inputs — changed paths, changed
    lines, declared spec level and declared capability risk. Re-deriving those
    inputs here is how a duty and a sweep come to disagree about which run owes
    a review, so they are never re-derived. A scope the resolver cannot measure
    already reads there as the fuller tier, so a run whose change is unreadable
    still owes its review.
    """
    try:
        return str(
            recovery._sweep_review_tier(
                pointer, list(row.get("manifest_commits") or [])
            )
        )
    except Exception:  # noqa: BLE001 - an unreadable tier owes the fuller review
        return review_tiers.FULL


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
    ready, because the safe direction is to ask for evidence.

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
        return item

    if _review_tier_for_duty(row, pointer) == review_tiers.NONE:
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
    head, tree = resumed_head, _tree
    try:
        review, reviewed_head = recovery.select_review_for_head(
            project, str(row.get("run_id") or ""), head, tree=tree
        )
    except (OSError, ValueError):
        review, reviewed_head = None, ""
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
    if (
        item["kind"] == "review-missing"
        and classification == "scoring"
        and _reflex_owns_review(project, pointer)
    ):
        # One actor owns review dispatch. While an armed follower will compose
        # this review, the duty names that reflex and its window instead of
        # printing a command whose run collides with the reflex's own claim.
        item["next_command"] = _reflex_review_command(project, pointer)
        item["review_owner"] = "reflex"
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
    """Return the ledger record holding each registered tree that is still held.

    A tree is held only when Git's registry lists it, its directory is still on
    disk, a promotion record names it, and no promotion record shows it
    released. Git keeps a path in its registry until the registry is pruned, so
    a directory deleted out from under a registration is still listed; naming
    that registration would raise a duty whose remedy, a project-wide sweep,
    would take peers' trees to clear a tree entry that holds nothing.

    A registered path a live run now occupies is skipped: the promoted run's
    ledger record still names it under the same node id, but the tree belongs
    to the live run and the collector will not take it, so naming the promoted
    run would send a coordinator at the wrong run for a tree it cannot free.

    The rows are taken from the ledger reader that refreshes them: it stats
    every per-run document once and re-decodes one only when it has moved since
    the published index, so a record is never older than its file. A screen
    that acted on the published index unvalidated would answer from a run's old
    row, and a run that starts retaining a tree after publication would be
    dropped. A run names a tree by either route :func:`record_names_worktree`
    applies — its node id, or the path its retention carries — and the screen
    admits on both, so a run dispatched for one node and retaining a tree named
    for another still answers. The same rows supply the candidates, so no
    document is opened a second time and only a row that can name an inspected
    tree is filed. The screen is dropped, not the answer, when that reader
    refuses or fails: every run stays a candidate and the per-run documents are
    read for themselves, so a reader that cannot be reached costs a read rather
    than a duty.
    The whole call runs inside :func:`reckon.file_memo.read_only_region`, so a
    document is stat'ed and decoded once for the call and handed on unshared.
    Records are filed into one index of the trees they name, so an inspected
    tree is one lookup rather than a comparison against every record, and the
    row the merged ledger's own order puts last holds the tree. A tree no row
    names falls back to the whole ledger, which reads the merged history once
    for the session however many trees need it.
    """
    docs_dir = _store._docs_dir_for_project(project)
    if docs_dir is None:
        return {}
    repository = docs_dir.parent.resolve()
    with file_memo.read_only_region():
        return _held_worktree_records_read(project, repository)


def _held_worktree_records_read(
    project: str, repository: Path
) -> dict[Path, Mapping[str, Any]]:
    """The held-tree records, inside the read-only region the caller opened."""
    registered = {
        path
        for path in _registered_worktrees(repository)
        if path != repository and path.is_dir()
    }
    occupied = _live_worktrees(project)
    inspected = sorted(registered - occupied)
    names = {worktree.name for worktree in inspected}
    trees = set(inspected)
    # One fresh read of the merged ledger supplies both the screen and the
    # records: every per-run document is stat'ed once to detect a write, and one
    # that has moved is re-decoded, so no candidate is ever older than its file.
    fresh = _run_namings(project, repository) if names else ({}, [])
    namings: Mapping[str, tuple[str, Path | None]]
    rows: Sequence[Mapping[str, Any]]
    if fresh is None:
        # The reader that supplies the namings refused or failed, so the screen
        # cannot be built. Every run stays a candidate and the documents are
        # read for themselves: a screen that drops the runs it cannot name turns
        # a refused reader into a lost duty, which is the one outcome a screen
        # must never produce.
        namings, rows = {}, _run_file_rows(project, repository)
    else:
        namings, rows = fresh
    index = WorktreeRecordIndex()
    for row in rows:
        # A row that can name no inspected tree on either route the naming rule
        # applies is left out of the index: the match is then bounded by the
        # trees that are held rather than by the corpus, and the answer is
        # unchanged because a row the screen excludes names none of them.
        if _row_may_name_a_tree(row, names, trees, namings):
            index.add(row)
    matched: dict[Path, Mapping[str, Any]] = {}
    unresolved: list[Path] = []
    for worktree in inspected:
        record = _holding_record(worktree, index)
        if record is None:
            unresolved.append(worktree)
        else:
            matched[worktree] = record
    if unresolved:
        # A tree the screened rows do not name is left to the whole-ledger scan,
        # which matches on every recorded row and refuses a damaged one exactly
        # where the whole-ledger reader always has.
        for record in rows:
            for worktree in unresolved:
                if _names_worktree(record, worktree):
                    matched[worktree] = record

    held: dict[Path, Mapping[str, Any]] = {}
    for worktree, record in matched.items():
        release = record.get("release")
        if isinstance(release, Mapping) and release.get("worktree_released") is True:
            # The promotion's own record answers for this tree: it reported the
            # tree released, so the path is not held whatever the registry or
            # the filesystem now shows.
            continue
        held[worktree] = record
    return held


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
        return (
            recovery.newest_review_for_headless_run(project, run_id, reclaimed=False),
            "",
        )
    return recovery.select_review_for_head(project, run_id, head, tree=tree)


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
    later = [
        other for _path, other in review_module.stored_records_for_run(project, run_id)
    ]
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
        if review_module.read_review(project, run_id) is None:
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
    for row in _classified_rows(project):
        if str(row.get("session") or "") != session:
            continue
        if (
            pointers.get(str(row.get("run_id") or ""), {}).get("phase")
            == queue_admission.HELD_PHASE
        ):
            continue
        classification = str(row.get("classification") or "")
        recovery_classification = str(row.get("recovery_classification") or "")
        if classification == "queued":
            # A queued request is the lane's to launch, but it is not missing:
            # for as long as its pointer exists it is listed as waiting, beside
            # its place in the queue, so a coordinator reading this view sees
            # the request it dispatched rather than an absence. The place is
            # the admission pass's own, read from the ordering that pass
            # admits by, so this view cannot disagree with the lane about
            # which request goes first. A refusal naming the session is
            # listed under its own kind instead: nothing on the lane clears
            # that one, so the session owes the fix.
            if row.get("session_refusal"):
                items.append(
                    _live_item(row, kind=QUEUED_AWAITING_SESSION_DUTY_KIND, now=now)
                )
                continue
            items.append(
                _queued_item(row, placements=placements, now=now, verdicts=verdicts)
            )
            continue
        kind = ""
        if classification in REVIEW_CLASSIFICATION_KINDS:
            if (
                classification == "scoring"
                and str(row.get("run_id") or "") in reviews_in_flight
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
        if recovery_classification in RECOVERY_CLASSIFICATION_DUTY_KINDS:
            kind = RECOVERY_CLASSIFICATION_DUTY_KINDS[recovery_classification]
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
    return {
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
