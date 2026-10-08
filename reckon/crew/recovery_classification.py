from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib
import json
import math
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timezone
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from reckon import ledger, review_tiers
from reckon._timestamps import parse_utc
from reckon.capabilities import _charged_input_from_usage
from reckon.crew import lane_document as _lane_document
from reckon.crew import metering, plan_review, quota_weight, runs
from reckon.crew import repair as repair_module
from reckon.crew import review as review_module
from reckon.crew import review_need
from reckon.crew.host_lease import LEASE_RENEW_SECONDS
from reckon.crew.node import (
    _TERMINAL_RUN_PHASES,
    DEFAULT_WATCH_STALL_WINDOW,
    INTERRUPTED_RUN_PHASE,
    LOG_STALE_AFTER_SECONDS,
    CrewError,
    parse_duration,
)
from reckon.crew.reports import (
    NON_TERMINAL_MANIFEST_STATUSES,
    TERMINAL_MANIFEST_STATUSES,
    ManifestParseError,
    manifest_status_is_template,
    parse_manifest,
)
from reckon.crew.routing import _signal_process_group
from reckon.crew.runs import (
    _manifest_freshness,
    _mutate_pointer,
    _process_start_time,
    _project_watch_claim,
    _read_watch_record,
    _stream_quiet_seconds,
    _utc_now,
    _write_watch_record,
    list_live,
    producer_lease_seconds,
    read_pointer,
    update_watch_registration,
    watch_lease_renewed_at,
    watch_lock_path,
)
from reckon.crew.ticker import NEEDS_ACTION, Ticker, _agent_label



def classify_pointer(
    record: Mapping[str, Any],
    *,
    stale_after_seconds: int = LOG_STALE_AFTER_SECONDS,
    now_seconds: float | None = None,
    condition_test: Callable[[Mapping[str, Any], Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Classify one live pointer, without touching it.

    Pure and read-only, so the same judgement serves an MCP read and
    :func:`recover`. Liveness is established at the moment of use: when the
    record's launching host is this host the process table is asked now, and
    otherwise the stored answer is carried and marked unproven. The recorded
    launching host is the pointer's ``launcher_host`` field, spelled with
    ``socket.gethostname()`` on the machine that launched the run. Delivery
    comes from the manifest's status, because a terminal stream event only says
    the worker's turn ended. It does not say the node completed successfully.
    """
    run_id = str(record.get("run_id") or "")
    phase = str(record.get("phase") or "")
    # Read once here because the manifest read records when it happened, and
    # every reading below is ordered against the same instant.
    moment = _utc_seconds() if now_seconds is None else float(now_seconds)
    manifest = Path(str(record.get("manifest_path") or ""))
    manifest_file_present, manifest_present = _run_chain_manifest_freshness(record)
    # The memo is keyed on the files this classification reads, so it is
    # resolved from the same paths the reads below use: a key taken from a
    # differently resolved path would describe a read nobody made.
    memo = _read_classification_memo(record)
    memo_inputs = _classification_inputs(
        record, Path(str(record.get("log_path") or ""))
    )
    memo_key = _classification_key(memo_inputs)
    memo_fresh = memo.get("key") == memo_key
    manifest_data: dict[str, Any] = {}
    manifest_error = ""
    manifest_digest: str | None = None
    manifest_text = ""
    if manifest_present and memo_fresh:
        served_manifest = memo.get("manifest")
        if isinstance(served_manifest, Mapping):
            manifest_text = str(served_manifest.get("text") or "")
            manifest_data = dict(served_manifest.get("data") or {})
            manifest_digest = served_manifest.get("digest")
            manifest_error = str(served_manifest.get("error") or "")
            _remember_manifest_size(
                _manifest_size_key(record, manifest),
                int(served_manifest.get("size") or 0),
                moment,
            )
            manifest_present = bool(served_manifest.get("present"))
        else:
            manifest_present = False
    elif manifest_present:
        try:
            manifest_text = manifest.read_text()
            manifest_data = parse_manifest(manifest_text)
            # A content digest lets a watcher tell a rewrite that changed
            # something from a touch that did not. Computed from the same read
            # that parsed the status, so the digest and the verdict can never
            # describe different versions of the file.
            manifest_digest = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
            # The last size a readable manifest had, with the moment of this
            # read, so a later read that finds the file smaller recognises a
            # truncating rewrite earlier than the mtime signature would.
            _remember_manifest_size(
                _manifest_size_key(record, manifest),
                len(manifest_text.encode("utf-8")),
                moment,
            )
        except (OSError, ManifestParseError) as exc:
            # The file exists but no reader can judge it: an unreadable file is
            # a condition of the delivery, not an exception in the classifier.
            # Collecting it here keeps the refusal text (the parse error) in a
            # channel the classification branches read, so a manifest that
            # declares a format and is not readable degrades to its own outcome
            # rather than escaping this function and failing every ticker
            # refresh for every session.
            manifest_error = str(exc)
        memo["manifest"] = {
            "text": manifest_text,
            "data": manifest_data,
            "digest": manifest_digest,
            "error": manifest_error,
            "size": len(manifest_text.encode("utf-8")),
            "present": True,
        }
    manifest_reported_status = str(manifest_data.get("status") or "").strip().lower()
    # The orientation write is the first thing every dispatch writes: the tree,
    # the base revision and the write paths, before any status exists. A body
    # carrying those keys and no readable status is a run in progress, not a
    # delivery a reader has to repair, so it reaches the unwritten handling
    # beside the template. The body is read whole because the status is required
    # before any field reaches the classifier, so a reader that refused it may
    # have refused a file whose only sin was being one minute old.
    manifest_unwritten = manifest_status_is_template(
        manifest_reported_status
    ) or _carries_orientation_write(manifest_text, manifest_data)
    # The dispatch contract prints all terminal choices as a placeholder. It
    # is evidence that the worker never wrote a verdict, not a fourth spelling
    # of one, so no terminal predicate may see it as delivered state.
    manifest_status = "" if manifest_unwritten else manifest_reported_status
    manifest_derived = str(manifest_data.get("derived") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if manifest_derived:
        # A recovery artifact preserves evidence; it is not delivery by the
        # worker and therefore cannot satisfy the promotion precondition.
        manifest_present = False
        manifest_digest = None
    manifest_commits = list(manifest_data.get("commits") or [])
    manifest_blockers = list(manifest_data.get("blockers") or [])
    needs_help = manifest_data.get("needs_help")
    # Populated only while a dead unfinished run is being distinguished as an
    # interruption with retained work or as an abandonment with nothing left:
    # asking git costs a subprocess, so live and settled delivery paths never pay.
    commits_beyond_base = 0
    # Liveness is read at the moment it is used, not carried from the fleet
    # read that loaded the pointer, through the one host-gated reading every
    # consumer shares.
    alive, liveness_proven = local_liveness(record)
    local_reading = liveness_proven
    # The worker record is read again for the descendant check below, which
    # asks whether anything runs under the worker: that answer is about the
    # pid's children rather than about the run's liveness.
    worker_alive = _worker_record_liveness(record)
    # Whether anything runs under the worker is the second half of the same
    # question, so it is read here rather than by each consumer: the pid asked
    # is the process the work happens in, which for a supervised launch is the
    # worker the supervisor spawned and not the pointer's own pid. A supervisor
    # holds its worker as a child for as long as it lives, so asking the
    # pointer's pid whether anything runs below it would answer yes for every
    # supervised run and tell a reader nothing. A pid is worth asking about
    # only on the host that issued it: a run launched elsewhere asks nothing,
    # and the row then carries no descendant reading rather than a foreign
    # process table's opinion of some other machine's pid.
    worker_pid: int | None = None
    if local_reading and alive is True:
        worker_pid = _worker_record_pid(record) if worker_alive is True else None
        if worker_pid is None:
            worker_pid = _int_or_none(record.get("pid"))
    descendant_alive = _live_descendant(worker_pid) if worker_pid is not None else None
    # The run's own supervisor records the worker's exit in the run directory,
    # and that account survives a pointer nobody updates and a pid no machine
    # but the launching one can look up. It is consulted only where the process
    # table has not answered that the worker is still there, because a resumed
    # attempt reuses the run directory and the record an earlier attempt left
    # behind must not call the new worker dead. Where the pid cannot answer, the
    # record is the proof of the end that a bare pid never was, so the run stops
    # being inferred dead from a missing process and is read from its record.
    exit_record = _run_exit_record(record)
    ended_exit = exit_record if exit_record is not None and alive is not True else None
    if ended_exit is not None:
        alive = False
    # The liveliest stream the run has, taken through the shared reader, so a
    # resumed or lane-changed run is aged against what it is writing now rather
    # than the first file the pointer named. Absent a non-empty stream the
    # pointer's own log path is still used, so an empty stream file keeps
    # reporting its own age rather than none at all.
    stream_reading = _record_newest_stream(record)
    log = Path(str(record.get("log_path") or ""))
    if stream_reading is not None:
        log = stream_reading[0]
    age = None
    if log.is_file():
        age = max(0, int(_utc_seconds() - log.stat().st_mtime))
    # Superseded-by-newer-activity applies to an ordinary non-terminal report
    # that is not yet a verdict. A declared wait is different: the manifest is
    # the authority for what the worker is parked on, and its process may stay
    # alive briefly or exit immediately without changing that condition.
    # Terminal-looking reports are handled below: the live process outranks
    # every worker-reported outcome regardless of file recency, and the
    # manifest becomes authoritative when that process exits.
    if (
        manifest_status
        and manifest_status not in TERMINAL_MANIFEST_STATUSES
        and manifest_status != WAITING_STATUS
        and alive is True
        and log.is_file()
        and manifest.is_file()
        and log.stat().st_mtime_ns > manifest.stat().st_mtime_ns
    ):
        manifest_present = False
        manifest_data = {}
        manifest_digest = None
        manifest_status = ""
        manifest_commits = []
        manifest_blockers = []
        needs_help = None
    # A provider refusal makes an otherwise-abandoned run a block: the process
    # is gone but the stop is triageable (a named backend, limit and reset) and
    # resumable once the limit lifts. Detected from the same stream observe
    # reads, so the two paths agree.
    with _memo_published(record, memo):
        budget = _stream_budget(record)
    refusal_block = (
        _refusal_block(record, budget)
        if budget is not None and budget.get("refusal")
        else None
    )
    # A spent lane writes retries, not a refusal event; its mid-flight shape is
    # read alongside the refusal and only when no refusal already explains the
    # stop, so the two dead-lane readings never compete for the same run. The
    # budget is resolved once above, so both gates share a single stream read. A
    # background wait is checked only when neither already explains the stop:
    # all three name a process that is gone but resumable, and the lane reason
    # is the most triageable of the three when more than one is present.
    retry_block = (
        _stream_retry_block(record, budget)
        if budget is not None and not budget.get("refusal")
        else None
    )
    # Terminal retry exhaustion on an unmetered lane: the budget block carries
    # lane_backpressure and a retry count where the metered exhaustion carries
    # a refusal, so the two dead-lane readings stay on their own gates and a
    # live or recovered run never reaches a block through either.
    exhaustion_block = (
        _stream_exhaustion_block(record, budget)
        if budget is not None and not budget.get("refusal")
        else None
    )
    # A rejected rate-limit window is a hold time lifts, not a refusal a person
    # resolves: the event names the window and its reset, so the run pauses
    # until the window turns over rather than blocking for a coordinator.
    budget_hold = _budget_hold_block(record, budget)
    with _memo_published(record, memo):
        background_wait = (
            None
            if (refusal_block or retry_block or budget_hold)
            else _background_wait_signal(record)
        )
    # A refusal at admission is read from the stream's own marks, not from the
    # budget block: it is not a spend refusal — nothing was requested — and the
    # block carries no budget to refuse from. It is resolved here so the
    # dead-process chain consults the stream once for the shape. Only a run whose
    # process is gone can reach that arm, so the read is taken only when it can
    # be used: a live run never pays for a scan whose verdict the chain discards.
    admission_refusal = (
        None
        if (
            refusal_block
            or retry_block
            or exhaustion_block
            or budget_hold
            or alive is not False
        )
        else _admission_refusal(record, memo=memo)
    )
    terminal = phase in ("complete", "failed")
    wait = _manifest_wait(
        manifest_data,
        manifest,
        now_seconds=moment,
        stale_after_seconds=stale_after_seconds,
        stream_mtime=_run_stream_mtime(record),
        previous_lift=record.get("auto_resume"),
    )
    if wait is not None and not wait["valid"]:
        # An incomplete wait declaration is a reading failure carried on the
        # row whatever the process state: a gone run reads unreadable from it,
        # a live run reads running from liveness with the same text beside it,
        # so both readings share one refusal instead of each arm re-deriving it.
        manifest_error = str(wait["error"])
    wait_observation: dict[str, str] | None = None
    if wait is not None and wait["valid"]:
        observe_condition = (
            _run_wait_condition_probe if condition_test is None else condition_test
        )
        try:
            wait_observation = _wait_condition_observation(
                observe_condition(record, wait),
                terminal_values=list(wait["terminal"]),
            )
        # A probe is untrusted external input. Any ordinary fault says nothing
        # about either the condition or the worker, so it becomes unknown and
        # the run stays waiting; abandoned remains reserved for proof of death.
        except Exception as exc:  # noqa: BLE001
            wait_observation = {
                "state": "unknown",
                "observed": "unavailable",
                "detail": f"condition probe could not answer: {exc}",
            }
    # A live worker may hold no written verdict yet, because the reader caught
    # its manifest between a rewrite's truncate and write. That is not a
    # delivery a reader must repair, but naming it unwritten in that instant
    # makes a working run flicker, so the reading is held back while the file is
    # clearly mid-rewrite. Past that window a live run whose manifest still
    # carries no readable verdict is genuinely unwritten, which keeps the
    # word's purpose. An absent manifest is deliberately left alone: a live run
    # with no manifest at all is already classified by the stall and liveness
    # arms, and re-labelling it here would take that reading away from it.
    if alive is True:
        if _absence_of_a_verdict_is_transient(record, manifest, manifest_error, moment):
            manifest_unwritten = False
        elif manifest_present and not manifest_reported_status and bool(manifest_error):
            manifest_unwritten = True
            manifest_status = ""
    terminal_at = None
    terminal_age_seconds = None
    # A terminal manifest is provisional while a worker that could have
    # superseded it is alive. The pointer's own pid proves that on the host that
    # launched the run, but a resumed attempt often carries no such proof: the
    # run directory and its manifest are reused, and the pointer's process
    # reading can be left unproven, so the stale verdict would otherwise be read
    # as delivery — a working run counted as unpromoted and offered a promotion
    # that would delete its live pointer. The worker's own record is the second
    # proof: a worker launched after the manifest was last written cannot have
    # written it, so a terminal status still on the file belongs to the
    # superseded attempt. Only the launch time is compared; a worker started
    # before the manifest keeps the classification its record already earns.
    superseded_manifest = (
        alive is not True
        and worker_alive is True
        and _worker_launched_after_manifest(record, manifest)
    )
    deferred_outcome = manifest_status in TERMINAL_MANIFEST_STATUSES and (
        alive is True or superseded_manifest
    )
    interruption = None
    interruption_commits = 0
    if manifest_status not in TERMINAL_MANIFEST_STATUSES:
        interruption, interruption_commits = _interruption_evidence(
            record,
            phase=phase,
            process_alive=alive,
            liveness_proven=liveness_proven,
            exit_record=ended_exit,
        )
        commits_beyond_base = interruption_commits
    # A worker whose end the run itself recorded — the supervisor's exit
    # record, not an inference from a vanished pid — and whose worktree carries
    # commits past its base while the manifest still reads a working status
    # delivered work whose verdict word was never written. The end is a
    # receipt, the worktree holds the work, and nothing about the stop is
    # ambiguous, so the run reads as its own state rather than as a block: the
    # coordinator replaces the missing verdict and the work lands through the
    # ordinary gate. An interruption claims a death nothing recorded and stays
    # its own reading, because a killed worker's remedy is its surviving
    # session rather than a verdict word.
    exited_unfinished = False
    if (
        alive is False
        and interruption is None
        and ended_exit is not None
        and manifest_status in NON_TERMINAL_MANIFEST_STATUSES
    ):
        commits_beyond_base = _commits_beyond_base(record)
        exited_unfinished = commits_beyond_base > 0
    review: dict[str, Any] | None = None
    review_error = ""
    if manifest_status == "complete" and not deferred_outcome:
        served_review = memo.get("review") if memo_fresh else None
        if isinstance(served_review, Mapping):
            stored_review = served_review.get("record")
            review = dict(stored_review) if isinstance(stored_review, Mapping) else None
            review_error = str(served_review.get("error") or "")
        else:
            review, review_error = _stored_review(record)
            memo["review"] = {"record": review, "error": review_error}
    review_complete = _review_is_complete(review)
    if manifest_status in TERMINAL_MANIFEST_STATUSES and not deferred_outcome:
        terminal_seconds = manifest.stat().st_mtime
        terminal_at = (
            datetime.fromtimestamp(terminal_seconds, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        terminal_age_seconds = max(0, int(moment - terminal_seconds))

    # Classification order: the process is consulted before the manifest
    # reading. A worker whose process is alive is classified from that life and
    # never as unreadable, so a strictness added for manifests at rest cannot
    # misreport work in progress; the manifest and its status become
    # authoritative only once the process is gone.
    #
    # The liveness test itself is hoisted above the chain as one verdict, and
    # every reading that could call a run unreadable — a present-but-unparseable
    # manifest or an incomplete wait declaration — consults it here rather than
    # testing liveness for itself, so the guarantee cannot decay into per-arm
    # guards as manifest readings are added.
    process_gone = alive is not True
    # The same three-way reading the stalled detail states, taken here from the
    # same evidence so the two surfaces cannot call one run alive and gone at
    # once. The offer of a resume is gated on it rather than on ``process_gone``,
    # which is true of an unproven reading as well as of an observed end:
    # resuming on an unproven reading is the reading's most expensive misread,
    # because nobody observed the process the resume is predicated on.
    process_reading = _process_reading(
        alive, liveness_proven=liveness_proven, exit_record=ended_exit
    )
    marker = None
    needs_help_complete_value = None
    if phase == "queued":
        classification = "queued"
        detail = str(record.get("reason") or "waiting for a local lane slot")
        action = "wait for a local lane slot"
    elif interruption is not None:
        classification = INTERRUPTED_RUN_PHASE
        signal_name = interruption.get("signal_name")
        if signal_name:
            detail = (
                f"the worker process ended by {signal_name} "
                f"(signal {interruption['signal']}) before the run completed"
            )
        elif interruption["reason"] == "dead-pid-with-retained-work":
            detail = (
                "the worker process is gone with no recorded exit and the "
                f"worktree carries {interruption_commits} commit"
                f"{'s' if interruption_commits != 1 else ''} beyond the dispatch base"
            )
        else:
            detail = (
                "the worker process is gone with no recorded exit; its pointer "
                "had already recorded that no terminal event arrived"
            )
        action = "resolve the surviving session before choosing a recovery"
    elif exited_unfinished:
        classification = "exited-unfinished"
        detail = (
            f"the worker {_exit_record_end_phrase(ended_exit)} with "
            f"{commits_beyond_base} commit"
            f"{'s' if commits_beyond_base != 1 else ''} beyond its recorded "
            f"base, but the manifest at {manifest} still reads "
            f"{manifest_status!r}; the work is committed and no verdict word "
            "says so"
        )
        action = (
            f"reckon crew repair-status --run {run_id} --status complete "
            "--reason <the verdict the worker reached>"
        )
    elif manifest_unwritten:
        classification = "running"
        if _carries_orientation_write(manifest_text, manifest_data):
            # The first write of every dispatch, read while the worker is still
            # filling in the rest of the manifest. There is no verdict to repair
            # and no placeholder to replace; the run is simply early.
            detail = (
                f"the manifest at {manifest} carries the run's orientation write "
                "and no status yet; the worker is early in its turn"
            )
            action = f"reckon crew observe --run {run_id}"
        else:
            detail = (
                f"the manifest template at {manifest} is present but its status "
                "placeholder was never replaced"
            )
            action = (
                f"reckon crew resume --run {run_id} --advice "
                "write the manifest's current status before continuing"
            )
        if not manifest_present or manifest_error:
            # A live run whose manifest is absent or unreadable carries no
            # verdict to repair, so the reader is pointed at the run rather
            # than at a status line that does not exist.
            detail = (
                f"the run is live and has no written verdict at {manifest}; "
                "the worker has not delivered a status yet"
            )
            action = f"reckon crew observe --run {run_id}"
    elif deferred_outcome:
        classification = "running"
        detail = "the process is alive"
        action = f"reckon crew observe --run {run_id}"
    elif manifest_status == "complete":
        if _pointer_role(record) == REVIEW_ROLE:
            # A review run's deliverable is the review it wrote for another
            # run, so it is not itself awaiting review. The exemption is the
            # role the run carried, not the presence of a stored review: a run
            # that never had a review attached still reads as scoring when its
            # role could have had one, and the reflex keeps dispatching for it.
            # Without this arm the scoring branch composes a dispatch whose
            # source node is this run, whose review run completes and scores in
            # turn — an unbounded chain of reviews reviewing reviews, each one
            # a real dispatch against a real member.
            classification = "promotable"
            detail = (
                "the worker manifest reports completion; the run is the "
                f"{REVIEW_ROLE} it dispatched with, so the review it wrote is "
                "its deliverable and no review of this run is required"
            )
            action = (
                f"promote the completed {REVIEW_ROLE} run once its verdict is "
                "read; the run is not itself reviewed"
            )
        elif review_complete:
            classification = "promotable"
            detail = (
                "the worker manifest reports completion and an independent "
                "parsed review is attached; the run is ready for promotion"
            )
            commits = _canonical_commits(_review_tree(record), manifest_commits)
            base = str(record.get("base_sha") or "").strip()
            if len(commits) == 1 and base and same_revision(commits[0], base):
                # A run that changed nothing records its dispatch base as its
                # only commit. Promotion refuses a citation of the base — the
                # base predates the run — so the offer names the declaration
                # a commitless run promotes under instead of the citation
                # that reproduces the refusal.
                action = (
                    f"reckon crew complete --run {run_id} --gate not-run "
                    "--no-commit '<why the run produced no commit>' "
                    "--outcome '<what the run produced>'"
                )
            else:
                action = f"reckon crew complete --run {run_id} --gate <verdict>"
                for commit in commits:
                    action += f" --commit {commit}"
        else:
            classification = "scoring"
            if review_error:
                review_detail = f"the stored review could not be read: {review_error}"
            elif review is None:
                review_detail = "no independent review is attached"
            else:
                review_detail = (
                    f"the attached review is {review.get('status') or 'incomplete'}"
                )
            detail = (
                "the worker manifest reports completion, but "
                f"{review_detail}; an independent review must be produced before promotion"
            )
            action = _review_dispatch_action(record)
    elif manifest_status == "blocked":
        classification = "blocked"
        # A blocked transition explains itself from the best source available,
        # in order: the worker's own escape-hatch question (already parsed and
        # complete — the sentence a coordinator can answer in one turn), then
        # the manifest's blockers, then a generic fallback. A bare-punctuation
        # result (a block-scalar indicator misread as its value, upstream)
        # explains nothing, so it is treated as absent too.
        needs_help_complete = isinstance(needs_help, Mapping) and bool(
            needs_help.get("complete")
        )
        headline = str(needs_help.get("headline") or "") if needs_help_complete else ""
        blocker = "; ".join(manifest_blockers)
        reason_text = headline or blocker or "the manifest reports a blocker"
        if not re.search(r"[A-Za-z0-9]", reason_text):
            reason_text = "the manifest reports a blocker"
        needs_help_complete_value = needs_help_complete
        detail = f"the worker manifest reports blocked: {reason_text}"
        # The manifest says what the worker was doing when it stopped; a
        # provider refusal says when anything can be attempted at all. When
        # both are present the manifest arm must not crowd the refusal out:
        # the refusal names the condition that gates recovery, so it is added
        # with its reset and the reader is told which must clear first. The
        # classification and the manifest reason both stay — a NEEDS-HELP
        # question on a spent lane still needs its answer, and an operator
        # simply cannot act on it until the lane clears.
        if refusal_block:
            lane = (
                f"backend {refusal_block['backend']!r} refused the turn on a "
                f"{refusal_block['limit_kind']}; reset {refusal_block['resets_at']}"
            )
            detail += (
                f"; the provider refusal must clear first — {lane} — no resume "
                "may be attempted before it does"
            )
        if needs_help_complete:
            marker = "?"
            action = f"reckon crew resume --run {run_id} --advice <answer>"
        else:
            marker = "!"
            action = f"read {manifest}; resolve the blocker before resuming the run"
        if refusal_block:
            # The lane, not the worker, owns the stop: a resume attempted before
            # the reset is refused on budget, so the offered resume is gated on
            # the lane clearing rather than proposed as work the operator can do
            # today. The recovery sweep resumes blocked runs, so the same command
            # stays the correct next action under that gate.
            action += " once the lane clears"
    elif manifest_status == "failed":
        classification = "failed"
        failure = "; ".join(manifest_blockers) or "the worker manifest reports failure"
        detail = f"the worker manifest reports failed: {failure}"
        action = (
            f"read {manifest} and launch log {record.get('stderr_path')}; "
            "repair or redispatch the run"
        )
    elif wait is not None and wait["valid"]:
        classification = WAITING_STATUS
        if (
            wait_observation
            and wait_observation["state"] == "met"
            and process_reading == "process gone"
        ):
            detail = (
                f"ready to resume: {wait['condition']} reported "
                f"{wait_observation['observed']!r}, a declared terminal state"
            )
            action = f"reckon crew resume --run {run_id} --advice continue"
        elif wait_observation and wait_observation["state"] == "met":
            # The condition is met, but nothing observed the worker's process
            # end. A live worker is still writing the run and a second worker on
            # it would collide with the first; a worker whose liveness nothing
            # established is not a death, so the offer would rest on a reading
            # nobody took. Both wait on the process rather than offering the
            # resume, and the row names which reading it is holding.
            detail = (
                f"waiting on {wait['condition']}: the probe reported "
                f"{wait_observation['observed']!r}, a declared terminal state, "
                f"but the process reading is {process_reading!r}, so no lift is "
                "offered until the process is observed to have ended"
            )
            action = (
                f"the recovery sweep lifts run {run_id} once its process is "
                "observed to have ended"
            )
        else:
            observation_detail = (
                wait_observation["detail"]
                if wait_observation is not None
                else "the condition probe did not answer"
            )
            detail = (
                f"waiting {wait['age_seconds']}s on {wait['condition']}; "
                f"{observation_detail}; terminal when the probe reports "
                f"{', '.join(wait['terminal'])}"
            )
            action = (
                f"the recovery sweep will resume run {run_id} when the condition "
                "test reports a terminal state"
            )
        if wait.get("wait_key_defect"):
            # The declaration lifted this run once already and came back
            # unchanged, so its terminal state is not ending anything. The
            # sweep already refuses a second lift for the same declaration; the
            # row says why rather than reading as an ordinary pending wait.
            detail = (
                f"{wait['wait_key_defect']} (waiting {wait['age_seconds']}s on "
                f"{wait['condition']})"
            )
            action = (
                f"edit the wait declaration in {manifest} so its terminal list "
                "names a state the probe cannot report while the job is live"
            )
    elif wait is not None and process_gone:
        classification = "unreadable"
        detail = (
            f"the manifest at {manifest} declares an external wait but is "
            f"incomplete: {manifest_error}"
        )
        action = f"repair the waiting declaration in {manifest} before resuming"
    elif phase == "stopped":
        classification = "stopped"
        detail = "the run was intentionally stopped"
        action = (
            f"inspect the worktree at {record.get('worktree')} and discard when safe"
        )
    elif budget_hold and alive is not True:
        # A rate-limit window that rejected the turn is the clearest case of
        # the who-lifts-it rule: the request was refused on a window the
        # provider resets on its own cadence, so time lifts the hold and no
        # person is needed. The paused verdict names the reset as its wake.
        # Deliberately not routed through the sweep claim the refusal arm makes:
        # a rejected window is not a formal refusal, so the sweep is not the
        # mechanism that lifts it — the reset is, and that is what is named.
        # A live process is never classified paused on this signal: it is still
        # running, and if it goes quiet the stall gate names the rejected
        # window as a wait rather than a hang. This arm owns the dead-process
        # reading, where a vanished run's last word was the rejection.
        classification = "paused"
        hold = (
            f"{budget_hold['limit_kind']} window refusals on backend "
            f"{budget_hold['backend']!r} reset {budget_hold['resets_at']}"
        )
        detail = (
            f"paused: {hold}; the hold ages out when the window resets and "
            "the run proceeds from there"
        )
        action = (
            f"resume run {run_id} once the window reset at "
            f"{budget_hold['resets_at']} lifts the hold"
        )
    elif refusal_block:
        # A refusal stays blocked rather than paused even when the limit has a
        # reset: the recovery sweep auto-resumes only runs classified blocked
        # (resumption gates on it), so a paused refusal would wait for a reset
        # nothing acts on. The who-lifts-it rule is therefore applied to the
        # window hold that carries its own expiry — the budget_hold arm above —
        # while a prose or retry exhaustion refusal remains a decision the
        # coordinator must make: it is not a wait that lifts itself.
        classification = "blocked"
        block = (
            f"backend {refusal_block['backend']!r} refused the turn on a "
            f"{refusal_block['limit_kind']}; reset {refusal_block['resets_at']}"
        )
        # The block states what was delivered so a reader does not conclude
        # nothing happened. A run killed with no manifest has nothing to show;
        # one whose manifest never reached a verdict still names its delivery
        # in the file, and pointing at it is the difference between a blocked
        # run and a vanished one.
        if not manifest_file_present:
            delivery = "no manifest was delivered and nothing has landed yet"
        else:
            delivery = (
                "the in-progress manifest at "
                f"{manifest} records what was already delivered"
            )
        detail = f"blocked: {block}; {delivery}"
        action = f"reckon crew resume --run {run_id} once the limit lifts"
    elif alive is False and exhaustion_block:
        # A dead run whose unmetered lane ended its retries in refusal is the
        # same stop as a metered budget refusal: the lane owns it, so the row
        # names the lane and offers resume rather than reading as abandonment.
        # The terminal-error shape sets it apart from the mid-flight retry arm
        # below, which names the retry count of a run still in flight when it
        # died — the exhausted run's own lane already refused, so the reading
        # keeps the refusal phrasing instead.
        classification = "blocked"
        block = (
            f"backend {exhaustion_block['backend']!r} refused the turn on a "
            f"{exhaustion_block['limit_kind']} (consumer queue backpressure) "
            f"after {exhaustion_block['retries']} retries"
        )
        if not manifest_file_present:
            delivery = "no manifest was delivered and nothing has landed yet"
        else:
            delivery = (
                "the in-progress manifest at "
                f"{manifest} records what was already delivered"
            )
        detail = f"blocked: {block}; {delivery}"
        action = f"reckon crew resume --run {run_id} once the lane recovers"
    elif alive is False and retry_block:
        # A dead process whose stream ended mid-retry is a lane kill, not a
        # vanished worker: the budget block names the retry count, the process
        # table says the worker is gone, and the lane that refused is the most
        # triageable stop a fleet can suffer. Liveness is the verdict, never the
        # count alone — a live worker mid-retry-burst is exactly the
        # two-runs-that-succeeded case and reads running, not blocked — and
        # phase is not consulted, because a finished or killed run can still
        # carry a starting phase in its pointer. The next action offers resume
        # rather than discard because the lane, not the worker, owns the stop.
        classification = "blocked"
        block = (
            f"backend {retry_block['backend']!r} rate-limited the run "
            f"{retry_block['retries']} times and its process died mid-retry "
            f"({retry_block['limit_kind']})"
        )
        if not manifest_file_present:
            delivery = "no manifest was delivered and nothing has landed yet"
        else:
            delivery = (
                "the in-progress manifest at "
                f"{manifest} records what was already delivered"
            )
        detail = f"blocked: {block}; {delivery}"
        action = f"reckon crew resume --run {run_id} once the lane recovers"
    elif background_wait:
        # A vanished process is not the same fact as a crashed one: the run
        # directory itself says it was waiting on background work when it
        # ended, so it resumes rather than reading as abandoned and inviting a
        # redispatch that throws away an intact session. Whether it blocks or
        # pauses is the who-lifts-it rule: a run whose in-progress manifest
        # names committed work is parked on its own job and resumes when that
        # job ends, so it pauses; one with nothing committed needs a reader to
        # decide, so it stays blocked.
        if not manifest_file_present:
            delivery = "no manifest was delivered and nothing has landed yet"
        else:
            delivery = (
                "the in-progress manifest at "
                f"{manifest} records what was already delivered"
            )
        if manifest_commits:
            # The who-lifts-it rule, on the parked case: the run is waiting on
            # its own background job, its committed work is safe in the tree,
            # and the job ends on its own — so it pauses and names the end of
            # that work as the wake. The resume action is the follow-through
            # once the job ends, not the reason it paused.
            classification = "paused"
            detail = (
                f"paused: {background_wait}; the committed work is safe and the "
                "run resumes when the background work it was waiting on ends"
            )
            action = (
                f"resume run {run_id} when the background work it was waiting on ends"
            )
        else:
            classification = "blocked"
            detail = f"blocked: {background_wait}; {delivery}"
            action = f"reckon crew resume --run {run_id}"
    elif manifest_error and manifest_present and process_gone:
        # The third manifest outcome next to absent and readable-and-terminal:
        # a file that is present but that no supported reader can parse is
        # neither a delivered record nor an absence. The name states what the
        # reader is to do, and the refusal text (the parse error, naming the
        # format the file declared and why it was rejected) travels in the same
        # manifest_error channel the abandoned arm used so the operator's next
        # question is answerable one turn before the run can be judged.
        # A positively live process outranks this reading: the worker is still
        # in flight and its half-written or mid-write manifest is a condition of
        # that work, not an unreadable delivery, so the run reads running and a
        # reader answers where it is rather than reporting it unreadable.
        classification = "unreadable"
        detail = (
            f"the manifest at {manifest} is present but could not be read: "
            f"{manifest_error}"
        )
        # The named object is the manifest: the file is what needs repair, and
        # the abandoned instruction (which points at the launch log and offers
        # redispatch) must never read as the remedy for a file that exists.
        action = (
            f"the manifest at {manifest} cannot be read — repair or replace "
            "it before judging the run"
        )
    elif manifest_status in NON_TERMINAL_MANIFEST_STATUSES:
        # A worker-reported working status is evidence of life, not death. What
        # the process table says now happened after the worker's last word, so
        # the row reads working — the status stays on it rather than being
        # dropped — and never abandoned, whatever state the process is in.
        classification = "running"
        if alive is True:
            detail = (
                f"the worker manifest reports it is still working: {manifest_status}"
            )
        else:
            detail = (
                f"the worker manifest reports it was still working "
                f"({manifest_status}) when the process ended; the run is not "
                "reported dead"
            )
        action = f"reckon crew observe --run {run_id}"
    elif alive is False and _commits_beyond_base(record):
        # Committed work is proof the worker delivered, and the fact lives in
        # git rather than in any manifest format, so it survives a missing or
        # unreported manifest. A dead process with commits past its base to
        # show is not a vanished worker; it reads running and names the
        # committed work as what survived.
        commits_beyond_base = _commits_beyond_base(record)
        classification = "running"
        detail = (
            f"the worktree at {record.get('worktree')} carries "
            f"{commits_beyond_base} commit"
            f"{'s' if commits_beyond_base != 1 else ''} beyond its recorded "
            "base; the delivered work survives in git"
        )
        action = (
            f"inspect the worktree at {record.get('worktree')}; the committed "
            "work is safe and can be promoted or resumed once a manifest "
            "documents it"
        )
    elif alive is False and admission_refusal is not None:
        # A run the backend refused at admission is named for what it is rather
        # than folded into the abandoned bucket. The process is gone and no
        # manifest was delivered in both cases, but here the stop is that no
        # model ever served a turn — a fact the stream states and the generic
        # bucket cannot, so a reader is spared diagnosing a vanish as the lane
        # fault they already know about. The narrow arm sits beside the
        # abandoned reading and takes nothing from it: a genuine vanish carries
        # none of these marks and still reads abandoned.
        classification = "refused-at-admission"
        detail = (
            "refused at admission: the backend returned no turn "
            f"({admission_refusal['reason']!r}, terminal reason "
            f"{admission_refusal['terminal_reason']!r}) with every token "
            "counter zero; no model was reached"
        )
        action = (
            f"read the refusing stream {record.get('log_path')} and launch log "
            f"{record.get('stderr_path')}; the run never reached a model, and a "
            "resume replaces the pointer, so keep the stream as the durable "
            "record of the refusal"
        )
    elif terminal and alive is False:
        # Abandoned requires positive proof of death: the process table says
        # the worker is gone AND nothing eligible for promotion was delivered.
        # The stored phase alone is the last writer's label, not evidence, so
        # it only participates when the process verdict confirms it.
        classification = "abandoned"
        if manifest_derived:
            delivery = "only a recovery-derived manifest exists"
        elif not manifest_present:
            delivery = "no manifest was delivered"
        else:
            # A present-but-unreadable manifest never reaches this arm: it is
            # intercepted above as its own outcome before the terminal reading
            # can fold it into abandoned.
            delivery = f"the manifest status {manifest_status!r} is not usable"
        detail = (
            f"the stored phase is terminal but {delivery}; nothing is eligible "
            "for promotion"
        )
        action = (
            f"reckon crew resume --run {run_id} --advice "
            f"{shlex.quote(f'review {manifest} and replace it with a worker-written manifest')}"
            if manifest_derived
            else (
                f"read launch log {record.get('stderr_path')}; inspect the worktree at "
                f"{record.get('worktree')} and redispatch if needed"
            )
        )
    elif terminal:
        # A terminal stored phase is not a dead run while the process table
        # has not confirmed death. An alive process outranks the stored phase,
        # and a pid whose liveness cannot be checked is no proof of death
        # either, so the run is never called abandoned here and its action
        # never advises redispatch — duplicating a live worker is the cost
        # this arm exists to stop.
        if alive is True:
            classification = "running"
            detail = "the process is alive despite the terminal stored phase"
        else:
            classification = "running"
            detail = (
                "the stored phase is terminal but process liveness could not be "
                "proven; the pointer is left in place pending a manifest or "
                "evidence of death"
            )
        action = f"reckon crew observe --run {run_id}"
    elif phase == "launch-failed" or (
        ended_exit is not None and _exit_record_is_launch_failure(ended_exit)
    ):
        # A launch that never wrote a stream record reached no model, so this
        # is an infrastructure fault rather than a worker turn. It sits on its
        # own state so a reader sees it apart from a working run, and the lift
        # refuses it until a person acts. The exit record decides this from the
        # run directory, so a run whose pointer never reached the launch-failed
        # phase is read the same way as one the launcher labelled.
        failures = list(record.get("launch_failures") or ())
        latest = failures[-1] if failures else {}
        tail = str(latest.get("stderr_tail") or "").strip().splitlines()
        cause = tail[-1] if tail else "the process exited before any turn"
        classification = "launch-failed"
        if failures:
            detail = (
                f"the launch for backend "
                f"{latest.get('backend') or record.get('backend')!r} "
                f"exited with status {latest.get('exit_status')} before writing any "
                f"stream record ({cause}); {len(failures)} launch failure"
                f"{'s' if len(failures) != 1 else ''} recorded; no model was reached"
            )
        else:
            # The phase and the failure list are written by the launcher and the
            # exit record by the supervisor, so a pointer can hold the phase with
            # neither of the others behind it. Nothing was recorded to name then,
            # and naming an end anyway would be an invention.
            recorded_end = (
                _exit_record_end_phrase(ended_exit)
                if ended_exit is not None
                else "ended without a recorded exit"
            )
            detail = (
                f"the launch for backend {record.get('backend')!r} "
                f"{recorded_end} before writing any "
                f"stream record ({cause}); no model was reached"
            )
        action = (
            f"fix the command and PATH for backend "
            f"{latest.get('backend') or record.get('backend')!r}, then resume "
            f"{run_id} by hand — the lift loop stays stopped until then"
        )
    elif _stranded_launch(record, now_seconds=moment):
        # A launch cut off between composing its record and spawning a worker
        # leaves a pointer holding a pre-spawn phase and nothing else: no pid,
        # no worker record, no stream, no launch log. Nothing about it is in
        # flight and nothing about it ends, so without this reading it holds
        # whatever claim it took for as long as the pointer lives. The
        # launch-failed word is the one that already says no model was reached;
        # the clause adds what that arm cannot, that not even an end was
        # recorded, because here there was nothing to record one from.
        classification = "launch-failed"
        detail = (
            "a stranded launch: the pointer has held the pre-spawn phase "
            f"{str(record.get('phase') or '')!r} for more than "
            f"{STRANDED_LAUNCH_BOUND_SECONDS}s with no worker record, stream or "
            "launch log in its run directory, so the launch was cut off before "
            "it spawned and nothing recorded an end"
        )
        action = (
            f"compose run {run_id} again; the pointer holds a claim no run "
            "backs, and a review it claimed is released by the reflex"
        )
    elif alive is True:
        classification = "running"
        detail = "the process is alive"
        action = f"reckon crew observe --run {run_id}"
    elif alive is False:
        # The deliverable is read before the process, so this arm reaches only
        # a run with nothing to show. Every manifest reading that a killed
        # worker can leave behind — a complete status behind a parsed review, a
        # non-terminal status, an unreadable file, committed work past base,
        # a refusal or a lane stop in the stream — is arbitrated above this
        # point and never falls here, because a dead process says nothing about
        # what the run delivered before it died.
        classification = "abandoned"
        if ended_exit is not None:
            # The end is recorded rather than inferred from a vanished pid, so
            # the row states how the process ended and when, and the reader is
            # not left to reconstruct it from a launch log.
            detail = (
                f"the worker process {_exit_record_end_phrase(ended_exit)} "
                f"(recorded at {ended_exit.get('exited_at') or 'an unrecorded moment'}) "
                "without a complete manifest; nothing is eligible for promotion"
            )
        else:
            detail = (
                "the process is gone without a complete manifest; nothing is eligible "
                "for promotion"
            )
        action = (
            f"read launch log {record.get('stderr_path')}; the worktree at "
            f"{record.get('worktree')} is left in place for review and is never "
            "force-removed"
        )
    else:
        classification = "running"
        detail = (
            "an in-harness run: liveness belongs to the calling harness, so it "
            "is reported as running until a manifest appears"
        )
        action = f"reckon crew observe --run {run_id}"

    session_resolution = None
    resume_remedy = None
    if classification in RESUMPTION_READING_CLASSIFICATIONS:
        session_resolution = _blocked_session_resolution(record, run_id)
    if classification == INTERRUPTED_RUN_PHASE and session_resolution is not None:
        resume_remedy = _resume_remedy(session_resolution, run_id)
        if resume_remedy is not None:
            action = resume_remedy["command"]
            detail = (
                f"{detail}; session {resume_remedy['session_id']!r} survives in "
                f"the {resume_remedy['source']} record"
            )
        else:
            absent_evidence = str(
                session_resolution.get("detail")
                or "no session id was found in the available run evidence"
            )
            action = (
                f"inspect the worktree at {record.get('worktree')}, then redispatch "
                "the unfinished work"
            )
            detail = f"{detail}; redispatch is required because {absent_evidence}"
    if session_resolution is not None and (
        refusal_block is not None
        or retry_block is not None
        or exhaustion_block is not None
    ):
        resume_remedy = _resume_remedy(session_resolution, run_id)
        if resume_remedy is None:
            absent_evidence = str(
                session_resolution.get("detail")
                or "no session id was found in the available run evidence"
            )
            detail = f"{detail}; no resume remedy: {absent_evidence}"
            if action.startswith("reckon crew resume"):
                action = (
                    f"inspect the worktree at {record.get('worktree')} and launch "
                    "log; no session id is available to resume"
                )
    if classification in {"stopped", "abandoned"} and session_resolution is not None:
        resume_remedy = _resume_remedy(session_resolution, run_id)
        if resume_remedy is not None:
            # The run is over as a process but its session still holds every
            # turn, so the remedy is to continue it rather than discard or
            # redispatch the work it had already done. An arm whose own advice
            # is already a resume keeps it: the run holding only a
            # recovery-derived manifest must still be told to replace that
            # artifact, which is part of resuming rather than an alternative to
            # it, and the surviving session is named beside that advice.
            if not action.startswith("reckon crew resume"):
                action = resume_remedy["command"]
            detail = (
                f"{detail}; session {resume_remedy['session_id']!r} survives in "
                f"the {resume_remedy['source']} record"
            )

    hold = refusal_block or exhaustion_block or retry_block or budget_hold
    if classification == INTERRUPTED_RUN_PHASE:
        recovery_classification = INTERRUPTED_RUN_PHASE
    elif manifest_unwritten:
        # A live worker between its orientation write and its first status has
        # not failed to deliver, and the phase is already read from its stream;
        # the recovery word is read from the same evidence, or a run whose
        # stream is growing renders unwritten and a reader is sent to resume
        # work in flight. Motion is what buys the reading — see
        # :func:`_orientation_write_of_a_run_in_motion`.
        if _orientation_write_of_a_run_in_motion(
            record,
            alive=alive,
            worker_alive=worker_alive,
            manifest_text=manifest_text,
            manifest_data=manifest_data,
        ):
            recovery_classification = classification
        else:
            recovery_classification = "unwritten"
    elif classification in {"blocked", "paused"} and hold is not None:
        recovery_classification = "held"
    elif classification == "blocked" and needs_help_complete_value:
        recovery_classification = "needs-help"
    elif (
        classification == WAITING_STATUS
        and wait_observation is not None
        and wait_observation.get("state") == "met"
        and process_reading == "process gone"
    ):
        # Ready is the one classification that reads as an offer, and the
        # recovery a reader acts on is chosen from it. A met condition whose
        # worker's end nothing observed is therefore a wait: the run is held by
        # a process no reading has seen end, so it classifies as waiting and
        # the remedy stays the wait's own.
        recovery_classification = "ready"
    elif classification == WAITING_STATUS and wait and wait.get("overdue"):
        recovery_classification = "wait-aged"
    else:
        recovery_classification = classification
    recovery_verb = RECOVERY_VERBS[recovery_classification]
    if resume_remedy is not None and recovery_classification in {
        INTERRUPTED_RUN_PHASE,
        "stopped",
        "abandoned",
    }:
        # These arms otherwise advise disposing of the run — discard it, or
        # redispatch its work. A session that survives means the turns come
        # back with it, so continuing is the remedy.
        recovery_verb = "resume"

    lifting_condition = None
    if classification == WAITING_STATUS and wait is not None:
        lifting_condition = (
            f"{wait['condition']} reports one of {', '.join(wait['terminal'])}"
        )
    elif classification == "paused":
        if budget_hold is not None:
            lifting_condition = (
                f"the {budget_hold['limit_kind']} window resets at "
                f"{budget_hold['resets_at']}"
            )
        elif background_wait:
            lifting_condition = "the background work named by the row ends"
        else:
            lifting_condition = DEFAULT_LIFTING_CONDITIONS["paused"]

    timing = _budget_timing(record, now_seconds=now_seconds)
    timing.update(_budget_overrun_cause(record, timing, now_seconds=now_seconds))
    observed_phase = _observed_phase(
        phase,
        alive=alive,
        worker_alive=worker_alive,
        worker_record_names_pid=_worker_record_pid(record) is not None,
        ended_exit=ended_exit,
        manifest_status=manifest_status,
        commits_beyond_base=commits_beyond_base,
        # Read only where the label is still pre-spawn, which is the one case
        # the answer can change: every other phase the classifier reaches
        # already reads as the work it names, and the run's streams are not
        # read to confirm a label that says working.
        stream_shows_work=(
            phase in _PRE_SPAWN_PHASES and _newest_stream_shows_work(record)
        ),
    )
    lane_cause, stream_ended_at = (
        _terminal_lane_signal(record) if alive is False else (None, None)
    )
    exit_ended_at = (
        str(ended_exit.get("exited_at") or "") if ended_exit is not None else ""
    )
    lane_ended_at = next(
        (
            stamp
            for stamp in (exit_ended_at, stream_ended_at, terminal_at)
            if parse_utc(stamp)
        ),
        None,
    )
    identity = ledger.normalize_identity(record)
    classified = {
        "lane": identity.get("lane"),
        "model_key": identity.get("model_key"),
        "run_id": run_id,
        "backend": str(record.get("backend") or ""),
        "lane_cause": lane_cause,
        "lane_ended_at": lane_ended_at,
        "lane_event": None,
        "project": record.get("project"),
        # Several coordinator sessions share one project, so every read of a
        # run has to say whose it is. Without it a session reading the live
        # view cannot tell its own fleet from a peer's, and acting on a peer's
        # row is worse than not seeing it.
        "session": record.get("session"),
        "plan": (record.get("node") or {}).get("plan"),
        "node": (record.get("node") or {}).get("id"),
        "classification": classification,
        # Cause and remedy are separate from the compatibility lifecycle
        # grouping above. This pair is the authoritative instruction surface:
        # readers act on the verb and use the classification to understand why.
        "recovery_classification": recovery_classification,
        "recovery": recovery_verb,
        "lifting_condition": lifting_condition,
        "resets_at": (
            str(hold.get("resets_at") or "unknown")
            if recovery_classification == "held" and hold is not None
            else None
        ),
        # The phase the run's own evidence supports. The launcher's label is
        # kept beside it under ``stored_phase``: it is what the pointer last
        # recorded, and a reader comparing the two sees why a run left the
        # pre-spawn bucket without anything having run ``observe``.
        "phase": observed_phase,
        "stored_phase": phase,
        # The stored phase is the last launcher's label; the effective phase is
        # what the run's own evidence supports, so a run whose pointer never
        # advanced past starting reads from its stream and its process; it does
        # not sit in a pre-spawn label for its whole life.
        "effective_phase": (
            INTERRUPTED_RUN_PHASE
            if classification == INTERRUPTED_RUN_PHASE
            else observed_phase
        ),
        "interruption": interruption,
        # The run directory's own account of the worker's exit, when it was
        # consulted: carried whole so a reader sees the receipt — signal, exit
        # code, whether a model was reached, and when the supervisor wrote it —
        # rather than a verdict with no record behind it. None when no record
        # exists or a live process outranked it.
        "exit_record": ended_exit,
        "process_alive": alive,
        # Whether a line runs under the worker, carried beside its own liveness
        # because the two are one reading taken at one seam. None is "nothing
        # was asked": no pid this host may look at, or no live worker to look
        # under, so a reader never sees an absence where a check never ran.
        "process_descendant_alive": descendant_alive,
        # False when the stored answer was carried because the launching host
        # could not be shown to be this host, or there is no pid to ask about.
        # An unproven answer is not death, so a reader needing certainty reads
        # this field rather than treating a stale stored value as a verdict.
        "liveness_proven": liveness_proven,
        "manifest_present": manifest_present,
        "manifest_file_present": manifest_file_present,
        "manifest_fresh": manifest_present,
        "manifest_path": str(manifest) if str(manifest) != "." else "",
        # A living worker's complete or failed report remains on disk but is
        # not exposed as an outcome. The single-event watcher consumes this
        # field, so returning the raw report here would call the run terminal
        # while the classification and ticker correctly call it live.
        "manifest_status": None if deferred_outcome else manifest_status or None,
        # Keep the worker's raw spelling alongside the effective status. A
        # live process defers terminal-looking placeholders, while the one-shot
        # watcher still needs to recognise a fresh completion written by the
        # resumed attempt it is waiting for.
        "manifest_reported_status": manifest_reported_status or None,
        "manifest_derived": manifest_derived,
        "manifest_commits": manifest_commits,
        # Committed work past the recorded base, read from git on the abandoned
        # tail only. A surface that would otherwise read the same run dead
        # consults this field, so classification and the pane never disagree.
        "commits_beyond_base": commits_beyond_base,
        # The refusal text when a present manifest could not be read, carried on
        # the row so a surface that discards nothing has it one field away.
        "manifest_error": manifest_error or None,
        # A content digest of the manifest as read, so a watcher can tell a
        # rewrite that changed something from a touch that did not. None when
        # no manifest was present or readable, so absence never looks like a
        # digest to compare against.
        "manifest_digest": manifest_digest,
        # Review presence and readability are separate facts. An emitted review
        # that did not parse is evidence to repair, never an absent review that
        # can be silently regenerated without showing what the reviewer wrote.
        "review_present": review is not None or bool(review_error),
        "review_status": (
            "unreadable"
            if review_error
            else str(review.get("status") or "") or None
            if review is not None
            else None
        ),
        "review_error": review_error or None,
        "terminal_at": terminal_at,
        "terminal_age_seconds": terminal_age_seconds,
        "log_age_seconds": age,
        "log_fresh": None if age is None else age <= stale_after_seconds,
        **timing,
        "worktree": record.get("worktree"),
        "detail": detail,
        "next_action": action,
        # Set only for a blocked run: "?" when the escape-hatch question is
        # complete enough that `reckon crew resume --advice` can answer it,
        # "!" when the reader has to read the manifest itself. The fact behind
        # it travels too so the renderer derives the glyph instead of persisting it.
        "marker": marker,
        "needs_help_complete": needs_help_complete_value,
        "external_wait": wait,
        "wait_age_seconds": wait.get("age_seconds") if wait else None,
        "wait_overdue": wait.get("overdue") if wait else None,
        "wait_condition_state": (
            wait_observation.get("state") if wait_observation is not None else None
        ),
        "wait_observed": (
            wait_observation.get("observed") if wait_observation is not None else None
        ),
        # A declaration that lifted its run and came back unchanged is the
        # defect the lift loop's own stop cannot name; carried on the row so a
        # reader sees why the run is still parked rather than inferring it.
        "wait_key_defect": wait.get("wait_key_defect") or None if wait else None,
    }
    if session_resolution is not None:
        classified["session_resolution"] = session_resolution
    if resume_remedy is not None:
        classified["resume_remedy"] = resume_remedy
    # Lifecycle and fleet attention are distinct vocabularies. Publish both
    # from this observation so consumers never reread a stream or process to
    # derive the fleet's verdict, while lifecycle callers retain their contract.
    classified["fleet_verdict"] = _watch_verdict(
        record, classified, moment=moment, stall_seconds=stale_after_seconds
    )
    settled = classified["fleet_verdict"]["state"]
    if settled in FLEET_SETTLED_STATES:
        # The ledger's word also names the lifecycle reading, so a committed
        # completion cannot read as two different outcomes on one row.
        classified["classification"] = settled
    # The memo is written from the key the reads were made under, so a reader
    # that finds this file again serves it only while every input still holds
    # the identity it had here. The stream's own state travels whatever the key
    # says: it is where the next read resumes from, and the records it has
    # already folded are the same records whether or not anything else moved.
    memo["key"] = memo_key
    memo["inputs"] = memo_inputs
    _write_classification_memo(record, memo)
    return classified


from .recovery_liveness import (  # noqa: E402
    STRANDED_LAUNCH_BOUND_SECONDS,
    _PRE_SPAWN_PHASES,
    _absence_of_a_verdict_is_transient,
    _carries_orientation_write,
    _exit_record_end_phrase,
    _exit_record_is_launch_failure,
    _int_or_none,
    _interruption_evidence,
    _live_descendant,
    _manifest_size_key,
    _manifest_wait,
    _observed_phase,
    _orientation_write_of_a_run_in_motion,
    _process_reading,
    _record_newest_stream,
    _remember_manifest_size,
    _run_chain_manifest_freshness,
    _run_exit_record,
    _run_stream_mtime,
    _stranded_launch,
    _worker_launched_after_manifest,
    _worker_record_liveness,
    _worker_record_pid,
    local_liveness,
)
from .recovery_memo import (  # noqa: E402
    _classification_inputs,
    _classification_key,
    _memo_published,
    _read_classification_memo,
    _terminal_lane_signal,
    _write_classification_memo,
)
from .recovery_review_subject import (  # noqa: E402
    _canonical_commits,
    _review_dispatch_action,
    _review_is_complete,
    _review_tree,
    _stored_review,
    same_revision,
)
from .recovery_stream import (  # noqa: E402
    DEFAULT_LIFTING_CONDITIONS,
    _admission_refusal,
    _blocked_session_resolution,
    _budget_hold_block,
    _budget_overrun_cause,
    _budget_timing,
    _refusal_block,
    _resume_remedy,
    _stream_budget,
    _stream_exhaustion_block,
    _stream_retry_block,
)
from .recovery_vocabulary import (  # noqa: E402
    RECOVERY_VERBS,
    RESUMPTION_READING_CLASSIFICATIONS,
    REVIEW_ROLE,
    WAITING_STATUS,
)
from .recovery_wait import (  # noqa: E402
    _background_wait_signal,
    _newest_stream_shows_work,
    _run_wait_condition_probe,
    _wait_condition_observation,
)
from .recovery_watch import (  # noqa: E402
    FLEET_SETTLED_STATES,
    _commits_beyond_base,
    _pointer_role,
    _utc_seconds,
    _watch_verdict,
)
