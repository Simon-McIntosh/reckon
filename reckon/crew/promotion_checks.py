from __future__ import annotations

# Imports below the definitions resolve sibling cycles after names are bound.
# ruff: noqa: E402
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reckon import (
    _backends,
    _store,
    flight,
    ledger,
    review_tiers,
)
from reckon._plan_html import section_record_id
from reckon._schema import is_implementable_section
from reckon._timestamps import parse_iso
from reckon.crew import plan_review, rollout
from reckon.crew import review as review_module
from reckon.crew.dispatch import (
    _backend_settings,
)
from reckon.crew.node import (
    NEGATIVE_CONTROL_FIELD,
    NEGATIVE_CONTROL_NONE,
    CrewError,
    is_test_path,
    negative_control_is_none,
    negative_control_reason,
    role_may_write_repository_paths,
)
from reckon.crew.plan_review import _is_run_comment
from reckon.crew.reports import (
    TERMINAL_MANIFEST_STATUSES,
    ManifestParseError,
    parse_manifest,
)
from reckon.crew.routing import (
    _signal_process_group,
    run_directory_of,
)
from reckon.crew.runs import (
    _manifest_freshness,
    drain,  # noqa: F401 - importable so a caller can substitute the fleet reading's drain
    process_alive,
    record_process_alive,
)
from reckon.evidence import EXECUTABLE_SECTION_ROLES


@dataclass(frozen=True)
class StreamMeasures:
    """Measurements recoverable from a run's ordered event streams."""

    completed_at: str | None
    completion_source: str | None
    worker_seconds: int | None
    budget: dict[str, Any]
    session_id: str | None
    # The rate the run generated at, read from the same terminal observation the
    # budget comes from. It has to be carried out of here because the streams it
    # is derived from are the run's, and nothing downstream re-parses them.
    throughput: dict[str, Any] = field(default_factory=dict)


def _zone_aware_stream_timestamp(timestamp: object) -> datetime | None:
    """The moment a stream event states, kept only when it names its zone.

    A stamp that carries a ``Z`` suffix or a numeric offset is a moment the
    event placed; one that names no zone is dropped, so the span this feeds is
    measured only from moments that stated where they were rather than from an
    assumption of UTC.
    """
    parsed = parse_iso(timestamp)
    if parsed is None or parsed.tzinfo is None:
        return None
    return parsed


def _terminal_stream_data(
    record: Mapping[str, Any],
) -> StreamMeasures:
    """Resolve completion from events, then stream mtimes, across all turns."""
    budget = dict(record.get("budget") or {})
    if record.get("launch") != "cli":
        return StreamMeasures(None, None, None, budget, None)

    backend_name = str(record.get("backend") or "")
    backend = _backend_settings(record, None)
    path = Path(str(record.get("log_path") or ""))
    paths = _run_streams(path)
    if not paths:
        return StreamMeasures(None, None, None, budget, None)

    timestamps: list[tuple[datetime, str]] = []
    session_id = None
    throughput: dict[str, Any] = {}
    # The client-owned rollout receipt, read by the same authority
    # _harvest_lane_receipt uses, joins this observe_log call to the model span
    # it already measures. The exec stream cannot separate inference from tool
    # wait, so without the join the stored throughput block carries no span for
    # a codex run whose rollout does. Only a receipt that actually measured the
    # span is folded in: one that measured nothing must not relabel the stream's
    # own report, so an absent rollout leaves the span explicitly unmeasured
    # rather than claimed.
    record_session = str(record.get("session_id") or "").strip()
    receipt = None
    if record_session:
        candidate = rollout.read_rollout_receipt(record_session)
        if isinstance(
            getattr(candidate, "generation_seconds", None), float
        ) and isinstance(getattr(candidate, "machine_seconds", None), float):
            receipt = candidate
    for candidate in paths:
        observation = _backends.observe_log(
            backend_name=backend_name,
            backend=backend,
            log_path=candidate,
            receipt=receipt,
        )
        if observation.terminal:
            budget = dict(observation.budget)
            # The last turn that finished, not a fold across turns: the spans a
            # resume reports are its own, and adding them to an earlier turn's
            # would rate tokens against a clock that never ran for them.
            throughput = dict(observation.throughput)
        session_id = observation.session_id or session_id
        first, last = _backends.cached_stream_timestamp_bounds(
            candidate, _zone_aware_stream_timestamp
        )
        if first is not None:
            timestamps.append(first)
        if last is not None:
            timestamps.append(last)
    if timestamps:
        first = min(timestamps, key=lambda item: item[0])
        last = max(timestamps, key=lambda item: item[0])
        return StreamMeasures(
            last[1],
            "terminal_event",
            max(0, int((last[0] - first[0]).total_seconds())),
            budget,
            session_id,
            throughput,
        )

    newest = max(candidate.stat().st_mtime for candidate in paths)
    completed = (
        datetime.fromtimestamp(newest, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    return StreamMeasures(
        completed, "stream_mtime", None, budget, session_id, throughput
    )


_STREAM_SETTLE_POLL_SECONDS = 0.05


_STREAM_SETTLE_QUIESCENCE_SECONDS = 0.2


_STREAM_SETTLE_MAX_SECONDS = 2.0


def _newest_stream_mtime(paths: Iterable[Path]) -> float:
    """Return the newest modification time across a run's stream files."""
    mtimes = [candidate.stat().st_mtime for candidate in paths if candidate.is_file()]
    return max(mtimes) if mtimes else 0.0


def _wait_out_stream_tail(paths: Iterable[Path]) -> None:
    """Boundedly wait for a closed writer's stream tail, returning once quiet.

    The stream's newest mtime is polled until it has not advanced for the
    quiescence window, or the hard ceiling elapses, whichever comes first. A
    stream whose newest write already predates the window is already quiet and
    returns without any wait. The ceiling guarantees a truncated stream — a
    writer that died mid-tail, or one whose terminal record never lands —
    cannot hold a promotion past the bound.
    """
    candidates = [path for path in paths if path.is_file()]
    if not candidates:
        return
    newest = _newest_stream_mtime(candidates)
    if time.time() - newest >= _STREAM_SETTLE_QUIESCENCE_SECONDS:
        return
    deadline = time.monotonic() + _STREAM_SETTLE_MAX_SECONDS
    last_mtime = newest
    stable_since: float | None = None
    while True:
        observed = time.monotonic()
        if (
            stable_since is not None
            and observed - stable_since >= _STREAM_SETTLE_QUIESCENCE_SECONDS
        ):
            return
        if observed >= deadline:
            return
        time.sleep(_STREAM_SETTLE_POLL_SECONDS)
        current = _newest_stream_mtime(candidates)
        if current != last_mtime:
            last_mtime = current
            stable_since = None
        elif stable_since is None:
            stable_since = time.monotonic()


def _end_live_writer_for_settle(record: Mapping[str, Any]) -> bool:
    """End a still-writing run before the fold, so its terminal tail is readable.

    A prompt promotion that finds the run's process alive is about to end that
    same process in the release step after the fold; it ends it here instead,
    before the observation, so the bounded settle can fold the terminal record
    the writer flushes on shutdown rather than a file mtime. Gated exactly like
    the release's own signal — a fresh terminal manifest and a live process —
    so a promotion that would not have signalled it (a run with no manifest, or
    a launch this settle never applies to) leaves the writer untouched and the
    observation takes its pre-existing live-process shape. Returns whether the
    writer was ended and the observation should therefore settle regardless.
    """
    if str(record.get("launch") or "") != "cli":
        return False
    if not _release_terminal_manifest(record):
        return False
    pid = record.get("pid")
    if record_process_alive(record, process_alive) is not True:
        return False
    try:
        _signal_process_group(
            int(pid),
            record.get("pid_start_time"),
            run_dir=run_directory_of(record),
            reason="promotion-settle",
        )
    except (ProcessLookupError, PermissionError, OSError, CrewError):
        return False
    return True


def _promotion_terminal_observation(
    record: Mapping[str, Any], *, settle_even_if_alive: bool = False
) -> StreamMeasures:
    """Read a finished run's stream after a bounded settle for its terminal tail.

    A worker writes its manifest before the harness reaches its terminal turn
    record, so a prompt promotion can read the stream in that gap and fold a
    completion taken from a file mtime — losing the run's own timing and token
    figures from the ledger. Once the run's process has exited the writer can
    only be flushing, so promotion waits out a short quiescence of the stream
    and re-reads it. A process still alive is normally never waited on: its
    stream is legitimately mid-write, and its behaviour here is unchanged. The
    exception is a writer this promotion has already ended (settle_even_if_alive)
    — signalling it means its tail is on its way, so waiting is both safe and
    bounded. A stream that never receives a terminal record still folds the
    mtime fallback, and the bounded wait guarantees a truncated stream cannot
    hang a promotion.
    """
    if str(record.get("launch") or "") != "cli":
        return _terminal_stream_data(record)
    if record_process_alive(record, process_alive) is True and not settle_even_if_alive:
        return _terminal_stream_data(record)
    path = Path(str(record.get("log_path") or ""))
    _wait_out_stream_tail(_run_streams(path))
    return _terminal_stream_data(record)


def _recoverable_session(record: Mapping[str, Any]) -> dict[str, str] | None:
    """The session a resume could still continue, and where it was found.

    The shared resolution consults the pointer, stream and promoted ledger.
    A pointer carrying no id cannot establish that the run is unresumable.
    """
    from reckon.crew.resumption import resolve_session

    resolution = resolve_session(
        str(record.get("run_id") or ""),
        record=record,
        project=str(record.get("project") or ""),
        root=record.get("repo"),
    )
    if not resolution["resolved"]:
        return None
    return {
        "session_id": str(resolution["session_id"]),
        "source": str(resolution["source"]),
    }


def _require_resume_waiver(
    run_id: str,
    *,
    verdict: str,
    waiver_reason: str,
    classification: str,
    recoverable_session: Mapping[str, str] | None,
) -> dict[str, str] | None:
    """Refuse a promotion that would delete a resume path, unless it is stated.

    Promotion removes the pointer, and the pointer is where a resume finds the
    session it continues. A run that stopped without finishing is exactly the
    one whose session is worth keeping — a provider refusal classifies as
    blocked and leaves a session holding every turn of the worker's
    orientation, which promotion then discards while reporting success.

    So the two facts are checked together, before anything irreversible runs: a
    blocked classification and a session either source can still reach. A
    passing gate is never touched, and neither is any other terminal state. A
    caller who genuinely wants the discard states why, and the reason lands on
    the ledger row so a deliberate one is afterwards distinguishable from an
    accident.
    """
    if verdict == "passed":
        return None
    if classification != "blocked":
        return None
    found = recoverable_session
    if found is None:
        return None
    reason = str(waiver_reason).strip()
    if reason:
        return {**found, "reason": reason}
    raise CrewError(
        f"run {run_id!r} is classified blocked and its session "
        f"{found['session_id']} is still recoverable from the {found['source']}, "
        "so promoting it would delete the only record a resume needs. Continue "
        f"it with `reckon crew resume --run {run_id} --advice <answer>`, or "
        "promote anyway with --waive-resume-path REASON stating why the "
        "session is being discarded"
    )


def _fresh_manifest(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Parse a run's manifest when it is present and fresh, else None.

    The two guards below key on what the worker wrote, so both must read the
    same file. A run with no manifest, or one whose manifest postdates the
    reason the run is being judged, is left to the arms that read its absence;
    an unparseable file is a delivery defect with its own refusal.
    """
    manifest_present, fresh = _manifest_freshness(record)
    if not manifest_present or not fresh:
        return None
    try:
        parsed = parse_manifest(
            Path(str(record["manifest_path"])).read_text(encoding="utf-8")
        )
    except (OSError, KeyError, ValueError):
        return None
    return dict(parsed)


def _manifest_repository_paths(record: Mapping[str, Any]) -> tuple[str, ...]:
    """The paths a run's manifest declares inside the run's own repository.

    This is the question the review gate asks — did the run change the
    repository a reviewer would have to read? — answered from the same field
    the commit-for-changed-manifest guard reads, and by the same resolution
    rule, so the two refusals cannot disagree about what a run wrote. A
    manifest that names no path, or only paths outside the repository, is a
    run that changed nothing there.
    """
    manifest = _fresh_manifest(record)
    if manifest is None:
        return ()
    if _changed_paths_declare_no_paths(manifest, record, _manifest_text(record)):
        return ()
    return _changed_paths_inside_repository(manifest, record)


def _require_recognised_manifest_status(run_id: str, record: Mapping[str, Any]) -> None:
    """Refuse a promotion whose manifest carries no status the reader accepts.

    The status vocabulary is the reader's, not the worker's, and the review
    gate reaches a completed run only through the exact word ``complete``. So a
    worker that writes a plausible synonym — ``awaiting-orchestrator-review``,
    ``implemented-not-closed``, a bare ``done`` — exempts its own run from that
    review without any signal it has done so: the reader refuses the file, the
    classifier falls through to a reading keyed on a dead process, and the run
    then promotes as though its status had said something the reader accepts.

    The reader's own refusal already names the rejected word and the recognised
    vocabulary, so it is carried forward here rather than re-derived, and the
    run id is put in front of it so the refusal names the run it is about. A
    manifest that is absent or not fresh has no verdict to judge and is left to
    the arms that read its absence.
    """
    manifest_present, fresh = _manifest_freshness(record)
    if not manifest_present or not fresh:
        return
    try:
        text = Path(str(record["manifest_path"])).read_text(encoding="utf-8")
    except (OSError, KeyError):
        return
    try:
        parse_manifest(text)
    except ManifestParseError as refusal:
        raise ManifestParseError(
            f"run {run_id!r} cannot be promoted: {refusal}"
        ) from refusal


def _require_worker_stopped_before_promotion(
    run_id: str,
    record: Mapping[str, Any],
    *,
    waiver_reason: str,
) -> dict[str, str] | None:
    """Refuse promoting a run whose worker is still alive and not finished.

    Promotion deletes the live pointer, so a run promoted while its own process
    is still running carries on with no pointer, no follower row and no
    obligation to any coordinator — an orphaned process whose worktree cannot be
    reclaimed until it exits. The guard fires on the conjunction that makes that
    harm real: the recorded process is alive *and* this attempt has delivered no
    finished verdict of its own — it has written no manifest, its manifest
    states a status that is not terminal, or the terminal status on file belongs
    to an attempt the live one superseded. A run whose process has exited, or
    whose manifest is this attempt's own and reads complete, blocked or failed,
    promotes as before.

    A resumed attempt reuses its run directory, so the manifest beside the
    pointer may be the verdict a superseded turn left. Its own launch time is
    the fact that separates the two: a worker that started after the manifest
    was last written cannot have written it, so a terminal status still on the
    file states nothing about the attempt now running and the guard reads that
    attempt as unfinished. A worker started before the manifest keeps the
    reading its record already earns. The comparison is recovery's own, so the
    classifier that defers such a run and the gate that refuses to promote it
    cannot disagree about which attempt a manifest belongs to.

    A manifest that is absent, or older than the baseline this attempt began
    from, is no verdict on the work running now: the attempt has written
    nothing, so the guard reads it as unfinished for the same reason it reads a
    non-terminal status that way, and the live-run waiver is the only way to
    land it. A resumed attempt is the ordinary shape of that — its baseline is
    the inherited manifest's own mtime, so an inherited status is never fresh
    for it — and refusing there is the harm this guard exists for, because
    promotion would delete the live pointer under the worker the resume just
    started.

    ``waiver_reason`` is the operator's own statement of why the run may be
    promoted anyway, and it is recorded on the promoted row rather than erased.
    An unconditional waiver would stop meaning anything, so a waiver offered
    against a run with nothing to waive is itself refused.
    """
    from reckon.crew.recovery import _worker_launched_after_manifest

    manifest = _fresh_manifest(record)
    status = (
        "" if manifest is None else str(manifest.get("status") or "").strip().lower()
    )
    reason = str(waiver_reason).strip()
    superseded = (
        manifest is not None
        and status in TERMINAL_MANIFEST_STATUSES
        and _worker_launched_after_manifest(record, Path(str(record["manifest_path"])))
    )
    live = record_process_alive(record, process_alive) is True and (
        manifest is None or status not in TERMINAL_MANIFEST_STATUSES or superseded
    )
    if not live:
        if reason:
            raise CrewError(
                f"run {run_id!r} has no live, in-progress worker to waive for "
                f"--waive-live-run {reason!r}"
            )
        return None
    if reason:
        return {"reason": reason, "pid": str(record.get("pid")), "status": status}
    if manifest is None:
        reading = "no manifest written by this attempt is on file"
    elif superseded:
        reading = (
            f"its manifest's {status!r} status was written before this attempt was "
            "launched, so it reads a superseded attempt rather than the work running "
            "now"
        )
    else:
        reading = f"its manifest status is {status!r}"
    raise CrewError(
        f"run {run_id!r} cannot be promoted: its recorded worker process "
        f"{record.get('pid')} is still alive and {reading}. Promotion would "
        "delete the live pointer and orphan the worker. Wait for the process to "
        "exit, or state why it may land anyway with --waive-live-run REASON"
    )


def _capability_risk_of(capability: Any) -> str:
    """The risk a plan or section record declares, or empty when none does."""
    if not isinstance(capability, Mapping):
        return ""
    requirements = capability.get("requirements")
    if not isinstance(requirements, Mapping):
        return ""
    return str(requirements.get("risk") or "").strip()


def _run_capability_risk(
    record: Mapping[str, Any], *, root: str | Path | None
) -> str:
    """The capability risk the run's plan or its section declares.

    A section's own declaration is read first because a plan can carry a
    moderate risk overall while the one section a run lands against is where a
    guard or a fence lives, and a run's review is sized to the risk it actually
    touched. An elevated declaration at either level forces the fuller review,
    so the two are not averaged: whichever names an elevated risk wins.
    """
    state = _plan_state_for_run(record, fallback_root=root)
    if not state:
        return ""
    section_risk = ""
    wanted = section_record_id((record.get("node") or {}).get("section"))
    sections = state.get("sections")
    if isinstance(sections, (list, tuple)):
        for section in sections:
            if not isinstance(section, Mapping):
                continue
            if str(section.get("id") or "") == wanted:
                section_risk = _capability_risk_of(section.get("capability"))
                break
    plan_risk = _capability_risk_of(state.get("capability"))
    for risk in (section_risk, plan_risk):
        if review_tiers.elevated_risk(risk):
            return risk
    return section_risk or plan_risk


def _light_changed_line_ceiling(project: str, root: str | Path | None) -> int:
    """The light tier's changed-line ceiling from the resolved flight config.

    The threshold rides the ``review.tiers`` flight key, so a host or project
    layer retunes it without a code change. A config that cannot be resolved —
    a malformed host layer, an unreadable shipped default — falls back to the
    shipped ceiling rather than failing a promotion over a lookup.
    """
    config: Mapping[str, Any] | None
    try:
        config = flight.resolve(project or None, checkout_path=root).config
    except (flight.FlightConfigError, OSError, ValueError):
        config = None
    ceiling, _budget = flight.review_tier_thresholds(config)
    return ceiling


def _review_changed_scope(
    run_id: str,
    record: Mapping[str, Any],
    commit_list: Sequence[str],
) -> tuple[tuple[str, ...], int | None, bool]:
    """The paths a run changed, their changed-line count, and whether measured.

    Read the same way the ledger row's own scope is read: from each cited
    commit's own diff, so a head that merged the integration branch is not
    charged the branch's paths. A run that cites no commit — a report-only or
    review run — falls back to the repository paths its manifest declares, and
    its line count is left unmeasured, which the resolver reads as over the
    ceiling and so as the fuller review.

    The third element says whether the run's own declarations gave the tier
    anything to judge at all. A run that cites no commit and declares no path
    whatever has not said it changed nothing — it has said nothing, which is a
    different statement, and a reviewer cannot read a diff the run never named.
    Such a silent scope is reported unmeasured so the caller grants the fuller
    review rather than the lighter one. A record that names no readable tree
    measures no commit either: the diff belongs to the run's own tree, and the
    directory the promotion happens to run in cannot supply it.
    """
    tree = _record_tree(record)
    if commit_list:
        if tree is None:
            return (), None, False
        resolved = _resolve_commits(cwd=tree, revisions=commit_list, run_id=run_id)
        cumulative = _committed_scope(cwd=tree, commits=resolved, run_id=run_id)
        lines = cumulative.changed_lines
        changed_lines = (
            int(lines["added"]) + int(lines["removed"])
            if lines.get("available", True)
            else None
        )
        return cumulative.paths, changed_lines, True
    declared = _fresh_manifest(record)
    declares_paths = bool(
        declared
        and declared.get("changed_paths")
        and not _changed_paths_declare_no_paths(
            declared, record, _manifest_text(record)
        )
    )
    return _manifest_repository_paths(record), None, declares_paths


def _run_review_tier(
    run_id: str,
    record: Mapping[str, Any],
    *,
    commit_list: Sequence[str],
    root: str | Path | None,
) -> str:
    """Resolve this run's review tier from what it actually changed.

    The four inputs the tier is decided from are read here rather than passed
    in: the run's changed paths and their changed-line count at the promoted
    head, the specification level its node declares, and the capability risk
    its plan or section declares. The light ceiling is the resolved flight
    value, so the threshold is not a literal in this module.

    A run whose own declarations measure nothing is granted the fuller review
    rather than the lighter one: silence about what changed is not evidence that
    what changed was safe, and the tier resolver would otherwise read an empty
    path list as a run that touched no runtime source.
    """
    changed_paths, changed_lines, measured = _review_changed_scope(
        run_id, record, commit_list
    )
    if not measured:
        return review_tiers.FULL
    node = record.get("node") or {}
    return review_tiers.review_tier(
        changed_paths,
        changed_lines,
        str(node.get("spec_level") or ""),
        _run_capability_risk(record, root=root),
        light_changed_lines=_light_changed_line_ceiling(
            str(record.get("project") or ""), root
        ),
    )


def _require_review_waiver(
    run_id: str,
    record: Mapping[str, Any],
    *,
    verdict: str,
    classification: str,
    review: Mapping[str, Any] | None,
    review_action: str,
    waiver_reason: str,
    review_tier: str = "",
    promoted_head: str = "",
    stale_head: str = "",
    manifest_commits: Sequence[str] = (),
) -> dict[str, str] | None:
    """Refuse an unreviewed promotion of a run that owes a review.

    The gate follows what the run changed, not the role name: a passing run
    whose changes include runtime source has produced work a reviewer must
    read, whatever role carried it. The tier is computed from the run's own
    changed paths, changed-line count, declared spec level and declared
    capability risk, so a node that changes no runtime source — a test, plan,
    evidence, research-data or figure node — promotes unreviewed with its tier
    recorded on the row as the reason no review exists, while a runtime-source
    node is refused until a review is stored or a waiver states why it may land.
    The implement role is no longer singled out: a source-touching test or
    documentation node earns the same review the implement role does, and the
    tier is what separates them. The review role is exempt, because the review
    it wrote for another run is its own deliverable; requiring another review
    would recurse without a stopping point.

    The obligation is read from the delivery this promotion is proceeding on —
    a terminal manifest written for this attempt — and not from the live-pointer
    classification. The classifier's ``running`` arm means only that a live
    process could still supersede the manifest, so it defers the run's outcome;
    a delivered run whose worker has not stopped yet therefore classifies as
    neither ``scoring`` nor ``promotable``, and a gate that read the obligation
    off that classification disarmed itself for exactly those runs: they
    promoted unreviewed with no waiver, and a waiver offered for one was refused
    as a waiver of nothing.

    ``review`` is the record whose own comment says it read ``promoted_head``; a
    record of a different revision does not satisfy the gate. When such a record
    exists, its head arrives as ``stale_head`` so the refusal can name both
    revisions: an operator told only that no review is stored looks for a record
    that is already on disk, and one told which two revisions disagree knows the
    review must be recomposed against the new head.

    The classification alone cannot carry the decision, because the classifier
    reads the store without naming a revision and so counts a review of an
    earlier head as a complete review of the run. A parsed record at a different
    head is therefore promotable and unreviewed at once, and the head comparison
    — which only this gate makes — is what separates them.
    """
    from reckon.crew.recovery import (
        REVIEW_ROLE,
        _pointer_role,
        _review_dispatch_action,
    )

    role = _pointer_role(record)
    reason = str(waiver_reason).strip()
    delivered = _release_terminal_manifest(record)
    review_required = (
        classification == "scoring"
        or (classification == "promotable" and bool(stale_head))
        or delivered
    )
    # The tier, not the role, decides whether the run changed work a reviewer
    # owes. An unmeasured or unknown tier is treated as the fuller review, so a
    # caller that could not resolve one never opens a lighter path by silence.
    tier = str(review_tier or review_tiers.FULL)
    unreviewed = (
        verdict == "passed"
        and review_required
        and role != REVIEW_ROLE
        and tier != review_tiers.NONE
        and not (review and review.get("status") == "parsed")
    )
    if unreviewed:
        if reason:
            return {"reason": reason}
        if delivered and classification not in ("scoring", "promotable"):
            # A deferred delivery's classification names an action for the run's
            # own lifecycle — observe it, answer its blocker — rather than one
            # that produces a review. The refusal asks for a review, so it names
            # the review dispatch rather than sending the operator to watch a
            # run that has already delivered.
            review_action = _review_dispatch_action(record)
        # The operator who cites the revisions their manifest names is refused
        # because the stored review read a revision the list does not carry. The
        # refusal names that reviewed head as the value to cite, so the way to a
        # promotion is one flag rather than a search for which of two revisions
        # the store meant.
        cite_reviewed_head = ""
        if stale_head and promoted_head and not any(
            str(candidate).strip()
            and (
                str(candidate).strip() == stale_head
                or stale_head.startswith(str(candidate).strip())
                or str(candidate).strip().startswith(stale_head)
            )
            for candidate in manifest_commits
        ):
            cite_reviewed_head = stale_head
        raise CrewError(
            _unreviewed_refusal(
                run_id,
                review_action,
                promoted_head,
                stale_head,
                classification=classification,
                cite_reviewed_head=cite_reviewed_head,
            )
        )
    if reason:
        raise CrewError(
            f"run {run_id!r} has no unreviewed promotion for "
            f"--waive-unreviewed-promotion {reason!r} to waive"
        )
    return None


def _require_standing_suite(
    project: str,
    review_tier: str,
    root: str | Path | None,
) -> None:
    """Refuse a lighter promotion while the project's declared suite is held.

    A per-node gate runs only the tests a node's brief names, so a project whose
    default command stopped at collection can keep promoting unseen; the
    project's own suite is the check that sees the whole tree, and the lighter
    tiers wait on it. The tier is the one promotion has already resolved from
    what the run changed, so a ``full`` review -- which reads the run for
    itself -- is never held, and the tier is neither re-derived nor copied here.

    The reason comes from the project's recorded suite runs: the latest one
    failed to collect or overran its budget and no later waiver has lifted it.
    The refusal names that reason together with both ways out, so an operator is
    not left to guess which command answers the node. A run whose record names
    no project owns no declared suite and is not held.
    """
    if not project:
        return
    from reckon.crew import standing_suite

    reason = standing_suite.hold_reason(root, review_tier, project)
    if reason is None:
        return
    raise CrewError(
        f"standing suite holds this promotion: {reason}; record a passing run "
        f"with `reckon crew suite run --project {project}` or record a lead "
        f"waiver with `reckon crew suite waive --project {project} --reason TEXT`"
    )


class _PromotionRefusalError(CrewError):
    """One refusal carrying several failed preconditions.

    A sweep inside a nested helper raises this so an enclosing sweep can flatten
    the parts into its own ordered list: the message a caller reads is composed
    once, from every failure in the order the promotion checks them.
    """

    def __init__(self, refusals: Sequence[BaseException]) -> None:
        super().__init__(_combined_refusal_text(refusals))
        self.refusals = list(refusals)


def _combined_refusal_text(refusals: Sequence[BaseException]) -> str:
    """Render every failure, keeping the first one's wording verbatim first.

    A caller that matches on the leading refusal keeps matching, and the rest
    follow in the order the promotion checks them so the operator reads them in
    the sequence the code runs them.
    """
    first, *rest = refusals
    if not rest:
        return str(first)
    listed = "\n".join(f"  - {refusal}" for refusal in rest)
    return (
        f"{first}\n\nThis promotion also fails {len(rest)} further "
        f"precondition(s), in the order the promotion checks them:\n{listed}"
    )


def _require_independently(checks: Sequence[Callable[[], Any]]) -> list[Any]:
    """Run independent preconditions together, refusing once with every failure.

    Each check returns its product; every check runs even after an earlier one
    has refused, because the facts behind them are independent, and then one
    refusal carries all of them in the order given. A check whose inputs come
    from an earlier check's success is not listed as independent — the caller
    keeps it after this sweep, where it is judged only once its inputs hold.
    """
    refusals: list[BaseException] = []
    products: list[Any] = []
    for check in checks:
        try:
            products.append(check())
        except (CrewError, ledger.LedgerError) as refusal:
            products.append(None)
            if isinstance(refusal, _PromotionRefusalError):
                refusals.extend(refusal.refusals)
            else:
                refusals.append(refusal)
    if refusals:
        if len(refusals) == 1:
            raise refusals[0]
        raise _PromotionRefusalError(refusals)
    return products


def _require_gate_check_precondition(
    gate_check: Mapping[str, Any] | None,
    *,
    gate: str,
    require_gate_check: bool,
) -> None:
    """Refuse a passing gate with no check before the landing path runs.

    ``ledger.build_record`` enforces the same requirement as the backstop for
    every caller that assembles a record, so the wording is taken from that
    check and raised here as the same error: a promotion that fails only this
    precondition reports exactly what it reported before.
    """
    if not require_gate_check or str(gate).strip().lower() != "passed":
        return
    missing = ledger.gate_check_missing_fields(gate_check)
    if missing:
        raise ledger.LedgerError(
            "a passing gate requires the check that produced it; missing "
            + ", ".join(missing)
        )


def _unreviewed_refusal(
    run_id: str,
    review_action: str,
    promoted_head: str,
    stale_head: str,
    *,
    classification: str = "scoring",
    cite_reviewed_head: str = "",
) -> str:
    """State why an unreviewed promotion is refused, naming both revisions.

    A run promoted on the strength of a review of an earlier revision is the
    failure this gate exists for, and an operator who reads only "no review is
    stored" goes looking for a record that is already on disk. Naming the
    revision the promotion asserts beside the one the stored record read makes
    the repair obvious: the review must be recomposed against the new head.

    The classification is the one the refusal was reached under, so a delivery
    that owes a review while its process is still running is reported as the
    deferred run it is, rather than under the scoring word the gate's other arm
    usually reaches. It is stated beside the absent review rather than as its
    cause: a run is classified from its own record, so "classified running
    because no review is stored" would read as a claim that producing a review
    changes the classification the run already holds.
    """
    revision = (
        f"the stored review read revision {stale_head[:12]} and this promotion "
        f"asserts {promoted_head[:12]}: no review of the promoted revision is "
        "stored"
        if stale_head and promoted_head
        else "no complete independent review is stored"
    )
    citation = (
        " The manifest's own commit list predates the revision the review read: "
        f"pass --commit {cite_reviewed_head} to cite {cite_reviewed_head[:12]} "
        f"as the promoted revision, or recompose the review against "
        f"{promoted_head[:12]} and cite that"
        if cite_reviewed_head
        else ""
    )
    return (
        f"run {run_id!r} is classified {classification}; {revision}.{citation} "
        f"Produce it with `{review_action}`, or promote anyway with "
        "--waive-unreviewed-promotion REASON stating why this run may land "
        "without review"
    )


def _review_outcome_summary(stored: Mapping[str, Any]) -> str:
    """Summarise a stored review as the outcome a promotion records.

    The two figures are the ones a review run's own deliverable carries — the
    total all dimensions sum to and the count of findings — so the summary
    is derived from the record rather than restated by hand. A review whose
    score is withheld (an unparsed or dimension-incomplete record) has no total
    to name, and a summary that invented one would read as a measured score.
    """
    total = stored.get("total")
    findings = stored.get("findings")
    count = len(findings) if isinstance(findings, Sequence) else 0
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        return f"review stored with no total score; {count} finding(s)"
    return f"review scored {int(total)}, {count} finding(s)"


def _resolve_promotion_outcome(
    run_id: str,
    record: Mapping[str, Any],
    *,
    verdict: str,
    outcome: str = "",
) -> str:
    """Return the outcome text a promotion records, defaulting a review run's.

    A non-passing gate must land with a summary of what failed or why the
    evidence could not be produced, so an empty outcome is refused. A review
    run's summary already exists in its deliverable, so the demand is met from
    the stored review's total score and finding count instead of requiring the
    operator to restate a figure the review store holds. Every other run still
    refuses, and a review run with no readable stored review refuses too,
    because a summary composed from nothing would read as evidence.
    """
    supplied = str(outcome).strip()
    if verdict == "passed" or supplied:
        return supplied
    from reckon.crew import recovery

    if not recovery._is_review_run(record):
        raise CrewError(
            "a non-passing gate requires --outcome; write what failed or why "
            "the evidence could not be produced"
        )
    project = str(record.get("project") or "")
    delivered = recovery._delivered_review_record(record, project)
    if delivered is None:
        raise CrewError(
            f"run {run_id!r} is a review run whose stored review cannot be "
            "read, so --outcome has no default to take; store the review or "
            "write what failed or why the evidence could not be produced"
        )
    _, path = delivered
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        stored = None
    if not isinstance(stored, Mapping):
        raise CrewError(
            f"run {run_id!r} is a review run whose stored review at {path} "
            "does not parse, so --outcome has no default to take; store the "
            "review or write what failed or why the evidence could not be "
            "produced"
        )
    return _review_outcome_summary(stored)


def plan_impl_at(
    project: str,
    plan: str,
    root: str | Path | None,
) -> float | None:
    """Return a plan's persisted impl, or None when unset or unreadable.

    A plan that has never carried a ``plan-impl`` scalar reads as unset rather
    than as zero, so a promotion can tell "the plan never moved" from "the plan
    has no impl to compare".
    """
    if not project or not plan:
        return None
    try:
        state, _version = _store.read_plan(project, plan, root, artifact_type="plan")
    except (OSError, ValueError, _store.OpError):
        return None
    return _plan_impl_from_state(state)


def _plan_impl_from_state(state: Any) -> float | None:
    if not isinstance(state, Mapping) or state.get("type") != "plan":
        return None
    value = state.get("impl")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _plan_state_for_run(
    record: Mapping[str, Any], *, fallback_root: str | Path | None
) -> dict[str, Any]:
    """Read the run's plan from the repository its dispatch authority names.

    The impl comparison must read the plan the run was dispatched against, so
    the authority's plan repository is preferred over the ledger checkout — a
    caller may point ``--checkout-path`` at another tree.
    """
    project = str(record.get("project") or "")
    plan = str((record.get("node") or {}).get("plan") or "")
    if not project or not plan:
        return {}
    root: str | Path | None = fallback_root
    authority = record.get("authority")
    if isinstance(authority, Mapping):
        plan_authority = authority.get("plan")
        if isinstance(plan_authority, Mapping) and plan_authority.get("repository"):
            root = str(plan_authority["repository"])
    try:
        state, _version = _store.read_plan(project, plan, root, artifact_type="plan")
    except (OSError, ValueError, _store.OpError):
        return {}
    return state if isinstance(state, Mapping) else {}


def _landed_sections(state: Mapping[str, Any]) -> set[str]:
    """Return the sections a landing comment already records.

    A promoted run appends one section comment under a run-derived id, so the
    presence of that id is the plan's own record that work landed against the
    section. A comment written for any other reason carries another id and
    says nothing about a landing. This records run outcomes, not document-card
    shape or section closure.
    """
    comments = state.get("comments")
    if not isinstance(comments, Mapping):
        return set()
    landed: set[str] = set()
    for raw_section, entries in comments.items():
        if not isinstance(entries, (list, tuple)):
            continue
        for entry in entries:
            if isinstance(entry, Mapping) and _is_run_comment(entry.get("id")):
                landed.add(str(raw_section).strip())
                break
    return landed


def _plan_remaining_sections(state: Mapping[str, Any]) -> list[str]:
    """Return the plan's sections that still have work to land.

    A landing already recorded on a section is subtracted, so the refusal
    names work a reader can still pick up rather than a section that has been
    delivered. The schema predicate first selects declared work, which remains
    outstanding until reclassification regardless of landings; subtraction
    applies only to this pickup list. A plan that has not persisted a
    classification falls back to the section identities its gates and comment
    anchors name.
    """
    landed = _landed_sections(state)
    declarations = state.get("section_declarations")
    if isinstance(declarations, Mapping):
        return sorted(
            section
            for section, classification in declarations.items()
            if is_implementable_section(classification)
            and str(section).strip() not in landed
        )
    from reckon._schema import declared_section_identities

    return sorted(declared_section_identities(state) - landed)


_IMPL_MOVE_EXEMPT_CLASSIFICATIONS = frozenset({"negative-result", "correct-refusal"})


_IMPL_MOVE_CORRECTIVE_ATTEMPT_KINDS = frozenset({"resume", "redispatch"})


def _require_brief_owner(
    run_id: str,
    record: Mapping[str, Any],
    *,
    plan_link: str,
    unplanned_reason: str,
) -> dict[str, Any] | None:
    """Refuse an implement-role brief run that names no owner for its change.

    A brief run has no plan section to move, so nothing joins a product change
    to the plan it belongs to unless the promotion says so. An implement-role
    brief run may land only with one of two discharges: ``--plan-link <slug>``
    naming the plan whose product it changed, or ``--unplanned-reason <text>``
    stating why it changed no plan. A non-implementing role needs neither, and a
    plan run records its owner through the plan itself. Both discharges name the
    flag a reader would pass, so the refusal is answered in one word of work.
    """
    node = record.get("node") or {}
    if not str(node.get("brief") or "").strip():
        return None
    if str(node.get("plan") or "").strip():
        # A brief beside a plan section is briefed plan work: the plan it
        # names is its owner, so no discharge is needed.
        return None
    role = str(record.get("role") or "")
    if role not in EXECUTABLE_SECTION_ROLES:
        return None
    link = str(plan_link).strip()
    reason = str(unplanned_reason).strip()
    if link or reason:
        return {"plan_link": link, "unplanned_reason": reason}
    raise CrewError(
        f"implement-role brief run {run_id!r} names neither a plan link nor an "
        "unplanned reason; pass --plan-link <slug> naming the plan whose "
        "product it changed, or --unplanned-reason <text> stating why it "
        "changed no plan"
    )


def _require_impl_moved(
    run_id: str,
    record: Mapping[str, Any],
    *,
    gate: str,
    failure_classification: str,
    no_impl_change: str,
    plan_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare the plan's impl at promotion against the value at dispatch.

    Landing work is supposed to advance the plan it lands against, and nothing
    in the landing path made the plan move: plans sat at zero percent while
    nodes landed against them one after another, so this converts the habit
    into a check. Every exemption is named in the returned record, and the
    refusal names the flag that waives it so recording a reason is one word of
    work.
    """

    role = str(record.get("role") or "")
    node = record.get("node") or {}
    plan = str(node.get("plan") or "")
    recorded = record.get("plan_impl_at_dispatch")
    if isinstance(recorded, (int, float)) and not isinstance(recorded, bool):
        recorded_value = float(recorded)
    else:
        recorded_value = None
    check: dict[str, Any] = {
        "plan": plan,
        "at_dispatch": recorded_value,
        "at_complete": None,
    }
    # A brief-only run names no plan section, so there is no plan impl to move
    # and nothing for this guard to read. The skip is named here rather than
    # left to the empty-plan branch so a reader of the row sees the run's
    # carrier as the reason, not an absent plan that might read as a defect. A
    # brief beside a plan section is plan work and its impl is expected to move.
    if str(node.get("brief") or "").strip() and not plan:
        check["verdict"] = "exempt"
        check["reason"] = "brief-names-no-plan"
        return check
    if role not in EXECUTABLE_SECTION_ROLES:
        check["verdict"] = "exempt"
        check["reason"] = f"role-not-enforced:{role or 'unknown'}"
        return check
    if str(failure_classification).strip().lower() in _IMPL_MOVE_EXEMPT_CLASSIFICATIONS:
        check["verdict"] = "exempt"
        check["reason"] = f"failure-classification:{failure_classification}"
        return check
    attempt_kind = str(record.get("attempt_kind") or "").strip().lower()
    if attempt_kind in _IMPL_MOVE_CORRECTIVE_ATTEMPT_KINDS:
        check["verdict"] = "exempt"
        check["reason"] = f"corrective-run:{attempt_kind}"
        return check
    # A dispatch may name the promoted run it repairs with ``--repairs``. The
    # movement that run produced belongs to the run being repaired, so the
    # same exemption the attempt-kind corrective forms carry applies, and the
    # repaired run is named so a reader can follow the chain.
    repaired_run = str(record.get("repairs") or "").strip()
    if repaired_run:
        check["verdict"] = "exempt"
        check["reason"] = f"corrective-run:repairs:{repaired_run}"
        return check

    if str(gate).strip().lower() != "passed":
        check["verdict"] = "exempt"
        check["reason"] = "gate-not-passing"
        return check
    if not plan:
        check["verdict"] = "exempt"
        check["reason"] = "node-names-no-plan"
        return check
    if not plan_state:
        check["verdict"] = "exempt"
        check["reason"] = "plan-unreadable"
        return check
    current = _plan_impl_from_state(plan_state)
    check["at_complete"] = current
    if recorded_value is None:
        # A run dispatched before this check existed carries no value to
        # compare; it is exempt rather than treated as a plan that never moved.
        check["verdict"] = "exempt"
        check["reason"] = "no-impl-recorded-at-dispatch"
        return check
    if current is None:
        check["verdict"] = "exempt"
        check["reason"] = "plan-records-no-impl"
        return check
    if current != recorded_value:
        check["verdict"] = "moved"
        return check
    if str(no_impl_change).strip():
        check["verdict"] = "waived"
        check["reason"] = str(no_impl_change).strip()
        return check
    remaining = _plan_remaining_sections(plan_state)
    listed = ", ".join(remaining) if remaining else "(none declared)"
    raise CrewError(
        f"run {run_id!r} promotes a passing {role} run, but plan {plan!r} impl "
        f"did not move: {recorded_value:g} at dispatch and {current:g} at "
        f"completion. Sections still to land: {listed}. Advance the plan's impl "
        f"as the work lands, or record why it did not move with "
        f"`reckon crew complete --run {run_id} --gate passed "
        f"--no-impl-change REASON` (the reason lands on the ledger row); if the "
        f"plan's impl is not this run's to move, state that reason"
    )


def _negative_control_log_text(
    log_path: str, *, manifest_path: str
) -> tuple[str | None, str]:
    """Read the red log a declaration names, resolving it against the manifest.

    A worker writes a path relative to the manifest it delivered, so a relative
    value is resolved there rather than against the promoting process's working
    directory, which would read as a missing file for every manifest on disk.
    """
    raw = str(log_path or "").strip()
    if not raw:
        return None, ""
    path = Path(raw).expanduser()
    if not path.is_absolute() and manifest_path:
        path = Path(manifest_path).expanduser().parent / path
    try:
        return path.read_text(encoding="utf-8"), str(path)
    except (OSError, UnicodeError):
        return None, str(path)


def _control_failure_ids(log_text: str) -> set[str]:
    """The failing tests a control log names, as canonical node ids.

    Only the ids the log enumerates are a fact: a runner's summary saying how
    many tests failed states that something did fail, not which test, and the
    comparison needs the identity rather than the tally. The log's own short
    summary marks them FAILED or ERROR, read through the review module's reader
    so both sides of the comparison are canonical.
    """
    return review_module._pytest_failure_ids(log_text)


def _baseline_suite_failure_ids(manifest: Mapping[str, Any] | None) -> set[str] | None:
    """The failing tests the manifest records for the baseline, or ``None``.

    The baseline is the fallback arm a control is compared against when the
    manifest records no readable head arm. A manifest that recorded no
    ``baseline_suite`` at all observed nothing, so every failure its control
    names is one the baseline does not.

    An arm that is recorded but does not declare its run complete is a
    different fact: the run may have been interrupted, so the ids it lists are
    not the set it failed and the arm says nothing about what it passed.
    Completion is asked the way the head arm asks it — a literal ``True``,
    never a truthy stand-in — so an arm that omits ``completed`` or carries
    null is unreadable here, and ``None`` says so. Reading such an arm as
    complete would either trust the ids of a half-run or, read as an empty set,
    admit every control it was meant to refuse. ``None`` therefore decides no
    control: the caller falls through to the head arm when one is readable, and
    refuses when neither is.

    The ``failure_ids`` the arm lists are read the way the head reader reads
    them: a list of non-empty strings. An unreadable shape is unreadable here
    too, for the same reason the completion key is — a value read as a set of
    ids but that is not one says nothing. A ``failure_ids`` recorded as a JSON
    string would otherwise iterate as its characters and admit a control the
    same manifest in list form refuses, and a non-iterable value would raise
    out of the gate; both return ``None``.
    """
    observation = None if manifest is None else manifest.get("baseline_suite")
    if not isinstance(observation, Mapping):
        return set()
    if observation.get("completed") is not True:
        return None
    failure_ids = observation.get("failure_ids")
    if not isinstance(failure_ids, list) or any(
        not isinstance(test_id, str) or not test_id.strip() for test_id in failure_ids
    ):
        return None
    return {review_module.canonical_node_id(test_id.strip()) for test_id in failure_ids}


def _head_suite_failure_ids(manifest: Mapping[str, Any] | None) -> set[str] | None:
    """The failing tests the manifest records for the head arm, or ``None``.

    The head arm is the run's own after measurement: the same suite over the
    tree the change landed in. It is the arm a control has to be compared
    against, because it answers the question the control exists to ask — does
    the mutation redden a test the changed code passes? The baseline answers a
    different one. A node whose tests were written before the repair has every
    new case failing at the base by design, so a baseline comparison refuses
    exactly the sound controls that redden those cases.

    ``None`` means no readable head arm is recorded: an absent ``after_suite``,
    an observation that does not declare its run complete, or one whose
    ``failure_ids`` cannot be read as a list of ids says nothing about what the
    head arm passed. Reading such an arm as failing nothing would admit every
    control, so the caller falls back to the baseline comparison instead.

    Completion is asked the way the strict arm validator and the manifest report
    ask it — a literal ``True``, never a truthy stand-in — so an arm that omits
    the key or carries null is unreadable here and decides nothing.
    """
    observation = None if manifest is None else manifest.get("after_suite")
    if not isinstance(observation, Mapping):
        return None
    if observation.get("completed") is not True:
        return None
    failure_ids = observation.get("failure_ids")
    if not isinstance(failure_ids, list) or any(
        not isinstance(test_id, str) or not test_id.strip() for test_id in failure_ids
    ):
        return None
    return {review_module.canonical_node_id(test_id.strip()) for test_id in failure_ids}


def _require_declared_negative_control(
    run_id: str,
    record: Mapping[str, Any],
    *,
    gate: str,
    manifest: Mapping[str, Any] | None,
    manifest_path: str,
    waiver_reason: str = "",
) -> dict[str, Any]:
    """Refuse a passing gate on a check whose red log shows nothing break.

    A node whose write paths include a test file declares the mutation that
    check must fail against. The declaration is discharged at promotion by a
    manifest that carries the path to the log that mutation produced: the pair
    of logs is the positive and negative control of one measurement.

    The log is judged on two facts about the run it captured, not on how it is
    worded: the run's terminal record exited non-zero, and the log names at
    least one failing test id the head arm does not fail. The head arm is the
    run's own after measurement, so a control that reddens a case written
    before the repair is admitted even though the baseline, taken over the
    unfixed tree, fails that case too; the baseline comparison decides only
    when the manifest records no readable head arm. An arm is readable only when
    it declares its run complete — a literal ``True``, never a truthy stand-in —
    so neither an ``after_suite`` nor a ``baseline_suite`` that omits the key or
    carries null decides anything, and when both are unreadable the control is
    refused rather than admitted against a comparison that was never made.
    Neither fact is inferred —
    a log with no exit record and a log naming no failing test id each state
    too little to admit the control, and a bare failing count is not
    evidence that either fact holds. Wording cannot carry either fact, so a
    declaration pasted into a log whose run exited zero is refused, and neither
    can a log whose run merely repeated the failures the compared arm already had.
    The declaration stays on the node record and on the verdict row beside the
    log path, so a person compares the two — the instrument for *is this the
    right mutation*, which no comparison of text can be. A declaration of
    ``none`` with its reason is an explicit escape rather than a silent one, so
    it is recorded on the row rather than refused.
    """

    check: dict[str, Any] = {"verdict": "exempt"}
    node = record.get("node") or {}
    if not isinstance(node, Mapping):
        check["reason"] = "node-writes-no-test-path"
        return check
    test_paths = sorted(
        str(path) for path in node.get("write_paths") or () if is_test_path(str(path))
    )
    if not test_paths:
        check["reason"] = "node-writes-no-test-path"
        return check
    check["test_paths"] = test_paths
    declaration = str(node.get(NEGATIVE_CONTROL_FIELD) or "").strip()
    if not declaration:
        # A run dispatched before this check existed carries no field to read;
        # it is exempt rather than treated as a node that declared nothing.
        check["verdict"] = "exempt"
        check["reason"] = "no-negative-control-declared"
        return check
    if str(gate).strip().lower() != "passed":
        check["verdict"] = "exempt"
        check["reason"] = "gate-not-passing"
        return check
    if negative_control_is_none(declaration):
        reason = negative_control_reason(declaration)
        if not reason:
            raise CrewError(
                f"run {run_id!r} declares its negative control as "
                f"{NEGATIVE_CONTROL_NONE!r} in the {NEGATIVE_CONTROL_FIELD} field "
                "without the reason it applies. A check that admits no applicable "
                f"mutation states so as `{NEGATIVE_CONTROL_NONE}: <reason>`, and "
                "the reason is what a later reader has to judge"
            )
        check["verdict"] = "none-recorded"
        check["declaration"] = declaration
        check["reason"] = reason
        return check

    check["declaration"] = declaration
    delivered = (
        "" if manifest is None else str(manifest.get("negative_control_log") or "")
    )
    check["log"] = delivered
    if not delivered:
        raise CrewError(
            f"run {run_id!r} writes a check ({', '.join(test_paths)}) and declares "
            f"the mutation {declaration!r} in its {NEGATIVE_CONTROL_FIELD} field, "
            "but its manifest carries no negative_control_log path. Promotion "
            "refuses a passing gate whose negative control was never run: keep the "
            "log that mutation produced beside the passing one and name its path "
            "in the manifest as `negative_control_log: <path>`"
        )
    text, resolved = _negative_control_log_text(delivered, manifest_path=manifest_path)
    check["resolved_log"] = resolved
    if text is None:
        raise CrewError(
            f"run {run_id!r} names negative_control_log {delivered!r}, which cannot "
            "be read, so the mutation it was to evidence was never shown to fail. "
            "Write the red log where the manifest can be read alongside it and "
            "name that path"
        )
    control_ids = _control_failure_ids(text)
    head_ids = _head_suite_failure_ids(manifest)
    baseline_ids = _baseline_suite_failure_ids(manifest)
    # The head arm decides the control whenever it is readable; the baseline is
    # the fallback only when it is not. ``None`` from either reader means the arm
    # is unreadable, so when both are unreadable nothing is left to compare
    # against and the control cannot be admitted on a comparison that was never
    # made: ``reference_ids`` stays ``None`` and the refusal below carries it.
    if head_ids is not None:
        reference_ids: set[str] | None = head_ids
    else:
        reference_ids = baseline_ids
    added = [] if reference_ids is None else sorted(control_ids - reference_ids)
    recorded_exit = _recorded_exit_status(text)
    check["control_exit_status"] = recorded_exit
    check["control_failure_ids"] = sorted(control_ids)
    if baseline_ids is not None:
        check["baseline_failure_ids"] = sorted(baseline_ids)
    if head_ids is not None:
        check["head_failure_ids"] = sorted(head_ids)
    if reference_ids is None:
        check["comparison_arm"] = "none"
    elif head_ids is None:
        check["comparison_arm"] = "baseline_suite"
    else:
        check["comparison_arm"] = "after_suite"
    check["added_failure_ids"] = added
    # Both facts have to come from something only the run could have written: a
    # log with no EXIT record says nothing about whether its command failed,
    # and one naming no failing test id says nothing about what broke. Neither
    # is inferred — a failing count and a repeated declaration together are
    # exactly the shape the removed wording rule could not tell from a
    # measurement — so an unrecorded fact refuses the declaration rather than
    # admitting it.
    if recorded_exit is None:
        unexplained = "it records no EXIT status, so whether its run failed is unknown"
    elif recorded_exit == 0:
        unexplained = "it records EXIT=0, so its run did not fail"
    elif not control_ids:
        unexplained = "it names no failing test id, so what broke is unknown"
    elif reference_ids is None:
        unexplained = (
            "no readable comparison arm is recorded — neither after_suite nor "
            "baseline_suite declares the run complete — so it adds no failure "
            "against an arm the gate can read, and the failure it names "
            f"({', '.join(sorted(control_ids))}) is one nothing compares"
        )
    elif head_ids is not None and not added:
        unexplained = (
            "it adds no failure to the head arm's: every test it names "
            f"({', '.join(sorted(control_ids))}) is one the head arm also fails"
        )
    elif not added:
        unexplained = (
            "it adds no failure to the baseline's: every test it names "
            f"({', '.join(sorted(control_ids))}) is one the baseline already fails"
        )
    else:
        unexplained = ""
    if unexplained:
        refusal = (
            f"run {run_id!r} declares the mutation {declaration!r} but the log at "
            f"{resolved!r} shows no failed control run: {unexplained}. A control "
            "is admitted on its facts alone — a non-zero exit record and at least "
            "one failing test id the head arm does not fail, falling back to the "
            "baseline's failing ids only when the manifest records no readable "
            "head arm, an arm being readable only when it declares its run "
            "complete — and neither fact is "
            "inferred from the log's wording or from a bare failing count. Re-run "
            "the declared mutation and keep the log it produced, with the "
            "capture's EXIT=<n> record and the runner's own list of which tests "
            "failed, or use --waive-negative-control REASON to record why the "
            "control may be accepted without it"
        )
        if waiver_reason:
            check["verdict"] = "waived"
            check["reason"] = waiver_reason
            return check
        raise CrewError(refusal)
    check["verdict"] = "matched"
    return check


def _refuse_commits_for_a_shadow(
    run_id: str, record: Mapping[str, Any], commits: Sequence[str]
) -> None:
    """Refuse a shadow run presenting commits: its evidence is a patch, not code."""
    if _is_shadow(record) and any(str(sha).strip() for sha in commits):
        raise CrewError(
            f"shadow run {run_id!r} is commitless evidence; --commit is refused"
        )


def _landing_scope_products(
    run_id: str,
    record: Mapping[str, Any],
    *,
    shadow: bool,
    node: Mapping[str, Any],
    commits: Sequence[str],
    accepted_paths: Mapping[str, str] | None,
) -> dict[str, Any]:
    """The scope facts a landing row records, refusing an out-of-role commit.

    A verifier may read the repository it grades but writes only its manifest,
    report and logs, so a cited commit that changes repository paths under a
    non-writing role is refused here rather than recorded as the verifier's
    work. A shadow asserts no code, so its scope is its patch. Everything the
    row carries is returned rather than recomputed, so the refusal and the row
    it would have written cannot disagree. The tree the scope is measured in is
    the run's own, resolved through :func:`_tree_for_measurement`: a patch or a
    citation belongs to the run's tree, never to the directory the promotion
    happens to run in.
    """
    if shadow:
        artifact = _write_shadow_patch(record)
        return {
            "shadow_patch": str(artifact),
            "changed_lines": _shadow_patch_stat(
                artifact, cwd=_tree_for_measurement(record, run_id)
            ),
            "scope_acceptances": [],
        }
    if not commits:
        return {"shadow_patch": "", "changed_lines": None, "scope_acceptances": []}
    tree = _tree_for_measurement(record, run_id)
    cumulative = _committed_scope(cwd=tree, commits=commits, run_id=run_id)
    acceptances: list[dict[str, str]] = []
    if cumulative.changed_lines.get("available", True):
        if (
            not role_may_write_repository_paths(str(record.get("role") or ""))
            and cumulative.paths
        ):
            raise CrewError(
                f"run {run_id!r} has role 'test', but its cited commit "
                "changes repository paths: "
                + ", ".join(cumulative.paths)
                + ". A verifier may read the repository it grades, but "
                "writes only its manifest, report, and logs outside the "
                "repository; dispatch an implement node for source edits"
            )
        outside = _outside_declared_scope(
            cumulative.paths,
            node.get("write_paths") or (),
            record=record,
            tree=tree,
        )
        if outside:
            acceptances = _accepted_scope_exceptions(
                run_id,
                outside,
                accepted_paths,
                record=record,
                tree=tree,
                commits=commits,
            )
    return {
        "shadow_patch": "",
        "changed_lines": cumulative.changed_lines,
        "scope_acceptances": acceptances,
    }


def _landing_preconditions(
    run_id: str,
    record: Mapping[str, Any],
    *,
    checkout: Path | None,
    ledger_root: str | Path | None,
    commits: Sequence[str],
    gate: str,
    failure_classification: str,
    no_impl_change: str,
    plan_link: str,
    unplanned_reason: str,
    boundary_waiver: str,
    negative_control_waiver: str | None,
    accepted_paths: Mapping[str, str] | None,
    gate_check: Mapping[str, Any] | None,
    require_gate_check: bool,
) -> dict[str, Any]:
    """Judge every precondition the landing checks, refusing once with all.

    The checks are independent of one another — each reads the record, the
    run's tree or its plan — so a promotion failing several reports them
    together rather than one per call, in the order the landing path checks
    them today. The one dependency is the citations: the changed-scope and
    role checks diff the commits a promotion presents, so a citation that does
    not resolve leaves them unjudged and they run only once it does. A run
    whose row is already in the ledger re-promotes through the already-promoted
    path, which checks none of this, so the probe below returns before any of
    them run.
    """
    project = str(record.get("project") or "")
    node = record.get("node") or {}
    shadow = _is_shadow(record)
    existing = next(
        (
            item
            for item in ledger.load(project, root=ledger_root)[0]["runs"]
            if str(item.get("run_id") or "") == run_id
        ),
        None,
    )
    if existing is not None:
        return {"already_landed": True}

    refusals: list[BaseException] = []

    def attempt(check: Callable[[], Any]) -> tuple[bool, Any]:
        try:
            return True, check()
        except (CrewError, ledger.LedgerError) as refusal:
            refusals.append(refusal)
            return False, None

    attempt(lambda: _require_committable_checkout(checkout, run_id))

    commit_list = list(_presented_commits_without_a_declaration(record, commits))
    shadow_ok, _ = attempt(
        lambda: _refuse_commits_for_a_shadow(run_id, record, commit_list)
    )
    resolved_ok = True
    resolved: Sequence[str] = []
    if commit_list:
        resolved_ok, resolved = attempt(
            lambda: _resolve_commits(
                cwd=_tree_for_measurement(record, run_id),
                revisions=commit_list,
                run_id=run_id,
            )
        )
    if not resolved_ok:
        # The changed-scope and role checks diff the cited commits, so an
        # unresolvable citation leaves them unjudged: this pair stays
        # sequential, after the citation itself is judged.
        commit_list = []
        resolved = []
    elif resolved:
        # The row records the canonical revisions the citations resolved to —
        # an abbreviated sha or a tag is admitted as a citation, never as the
        # value a later reader resolves again.
        commit_list = [str(canonical) for canonical in resolved]

    scope_products: dict[str, Any] = {
        "shadow_patch": "",
        "changed_lines": None,
        "scope_acceptances": [],
    }
    if shadow_ok and resolved_ok:
        _ok, measured = attempt(
            lambda: _landing_scope_products(
                run_id,
                record,
                shadow=shadow,
                node=node,
                commits=tuple(resolved),
                accepted_paths=accepted_paths,
            )
        )
        if measured:
            scope_products = measured

    _ok, boundary_waived = attempt(
        lambda: _require_repository_tree_boundary(
            run_id, record, waiver_reason=boundary_waiver
        )
    )
    plan_state = _plan_state_for_run(record, fallback_root=ledger_root)
    _ok, brief_owner = attempt(
        lambda: _require_brief_owner(
            run_id, record, plan_link=plan_link, unplanned_reason=unplanned_reason
        )
    )
    _ok, impl_move = attempt(
        lambda: _require_impl_moved(
            run_id,
            record,
            gate=gate,
            failure_classification=failure_classification,
            no_impl_change=no_impl_change,
            plan_state=plan_state,
        )
    )

    manifest_path = str(record.get("manifest_path") or "")
    manifest_text: str | None = None
    manifest: Mapping[str, Any] | None = None
    if manifest_path:
        try:
            manifest_path_text = Path(manifest_path).read_text(encoding="utf-8")
        except OSError:
            manifest_text = None
        else:
            manifest_text = manifest_path_text
    if manifest_text is not None:
        try:
            manifest = parse_manifest(manifest_text)
        except (KeyError, ValueError, OSError):
            manifest = None
    waiver_reason = (
        "" if negative_control_waiver is None else str(negative_control_waiver).strip()
    )

    def _negative_control_check() -> dict[str, Any]:
        if negative_control_waiver is not None and not waiver_reason:
            raise CrewError("--waive-negative-control requires a non-empty reason")
        control = _require_declared_negative_control(
            run_id,
            record,
            gate=gate,
            manifest=manifest,
            manifest_path=manifest_path,
            waiver_reason=waiver_reason,
        )
        if negative_control_waiver is not None and control["verdict"] != "waived":
            raise CrewError(
                f"run {run_id!r} has no negative-control match refusal for "
                f"--waive-negative-control {waiver_reason!r} to waive"
            )
        return control

    _ok, negative_control = attempt(_negative_control_check)
    attempt(
        lambda: _require_gate_check_precondition(
            gate_check, gate=gate, require_gate_check=require_gate_check
        )
    )

    if refusals:
        if len(refusals) == 1:
            raise refusals[0]
        raise _PromotionRefusalError(refusals)

    return {
        "already_landed": False,
        "commits": commit_list,
        "shadow_patch": scope_products["shadow_patch"],
        "changed_lines": scope_products["changed_lines"],
        "scope_acceptances": scope_products["scope_acceptances"],
        "boundary_waived": boundary_waived,
        "brief_owner": brief_owner,
        "impl_move": impl_move,
        "manifest": manifest,
        "manifest_text": manifest_text,
        "negative_control": negative_control,
    }


def _staging_review_record_by_run(
    project: str, run_id: str, record: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """The staged review record a review run delivered, found by its own id.

    A plan review is stored by the plan-review store rather than as a scored run
    review, so the run-store lookup the run-review path uses does not find it.
    Both kinds name the review run that produced them, which is the one stable
    key they share, so the lookup is served by the review store's own index for
    that key rather than by walking the project directory — a whole-store pass
    per promotion is the cost the index removes.

    A plan review's record is keyed by the composed review run id its report
    directory carries, which is not the id of the crew run that produced it, so
    the index lookup by the promoting run's id finds nothing for a plan review.
    Its delivered report names the promoting run, and is the join between them,
    so the run's own record resolves the report and the record it delivered.
    """
    found = review_module.record_for_review_run(project, run_id)
    if found is not None:
        return found[1]
    if record is None:
        return None
    return _plan_review_record_for_promoting_run(project, run_id, record)


def _plan_review_record_for_promoting_run(
    project: str, run_id: str, record: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The plan-review record whose delivered report names the promoting run.

    A plan review's record is keyed by the composed review run id its report
    directory carries, which differs from the crew run that produced it, so a
    lookup by the promoting run's own id finds nothing. The report's
    ``dispatch.json`` names that crew run, so the delivered report is the join
    between the promoting run and the record it delivered. Its report directory
    is resolved from the plan the run's node names, and its report is stored
    first when no stored record carries its id yet — the same step a plan build
    runs, so the record is reachable from the staging store as well as from the
    commit. A run whose node names no plan, or a plan with no delivered report
    naming the run, yields nothing.
    """
    from reckon.crew import recovery

    node_id = str((record.get("node") or {}).get("id") or "")
    prefix = recovery.PLAN_REVIEW_NODE_PREFIX
    if not node_id.startswith(prefix):
        return None
    plan_slug = node_id[len(prefix) :]
    if not plan_slug:
        return None
    matched: Mapping[str, Any] | None = None
    for sidecar in plan_review.delivered_reports(project, plan_slug):
        composed = str(sidecar.get("review_run_id") or "")
        if (
            review_module._plan_review_crew_run_id(project, plan_slug, composed)
            != run_id
        ):
            continue
        matched = sidecar
        break
    if matched is None:
        return None
    if not matched.get("stored"):
        # Store through the refusal-recording reader, so a delivery the store
        # refuses — a composed-but-undelivered report carrying no RUBRIC or
        # FINDING line — records its reason on the sidecar and leaves promotion
        # running with nothing committed for that round, rather than aborting
        # the landing with an unhandled exception.
        plan_review.store_delivered_reviews(project, plan_slug)
    found = review_module.record_for_review_run(
        project, str(matched.get("review_run_id") or "")
    )
    return found[1] if found is not None else None


def _misdelivered_plan_review_directories(
    project: str, run_id: str, record: Mapping[str, Any]
) -> tuple[Path, Path] | None:
    """The assigned and misdelivered report directories of an off-path delivery.

    A plan-review run is composed a report directory whose ``dispatch.json``
    names the crew run that runs it, and the run is told to write ``report.md``
    there. A run that writes it into a directory named for its own id instead
    leaves the composed directory holding its dispatch and sidecar but no
    report, so the delivery sits beside no sidecar and is read by neither
    promotion nor the store — the review is lost silently.

    Returns ``(assigned, misdelivered)`` when both conditions hold: the composed
    directory naming the run exists and carries no report, and a sibling
    directory under the same report root, named for the run's own id, carries
    one. Every other case yields ``None`` — a delivered report, a run that
    delivered no report anywhere, a run whose composed directory cannot be
    found, and any run that is not a plan review — so those promote as before.
    The composed directory is found by the same ``dispatch.json`` join the
    record lookup uses, applied across the plan's whole report root rather than
    to the delivered subset.
    """
    from reckon.crew import recovery

    node_id = str((record.get("node") or {}).get("id") or "")
    prefix = recovery.PLAN_REVIEW_NODE_PREFIX
    if not node_id.startswith(prefix):
        return None
    plan_slug = node_id[len(prefix) :]
    if not plan_slug:
        return None
    root = plan_review._plan_review_report_root(project, plan_slug)
    if not root.is_dir():
        return None
    assigned: Path | None = None
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        if (
            review_module._plan_review_crew_run_id(project, plan_slug, directory.name)
            == run_id
        ):
            assigned = directory
            break
    if assigned is None:
        return None
    if (assigned / plan_review._REVIEW_REPORT_NAME).is_file():
        return None
    misdelivered = root / run_id
    if not (misdelivered / plan_review._REVIEW_REPORT_NAME).is_file():
        return None
    return assigned, misdelivered


def _require_plan_review_delivered_in_its_directory(
    project: str, run_id: str, record: Mapping[str, Any]
) -> None:
    """Refuse a plan review that wrote its report outside its assigned directory.

    The composed directory is where a plan-review run is told to write
    ``report.md``; a report delivered anywhere else is lost, because the store
    and promotion both join the delivery through the composed directory's
    sidecar. The refusal names both directories so the coordinator moves the
    report into place and promotes again.
    """
    directories = _misdelivered_plan_review_directories(project, run_id, record)
    if directories is None:
        return
    assigned, misdelivered = directories
    raise CrewError(
        f"plan-review run {run_id} was assigned the report directory {assigned} "
        f"but delivered its report.md into {misdelivered}, a directory named for "
        "the run itself, so the review sits beside no sidecar and cannot be "
        f"stored; move {misdelivered / plan_review._REVIEW_REPORT_NAME} into "
        f"{assigned} and promote again"
    )


def _delivered_review_payloads_for_commit(
    record: Mapping[str, Any], project: str, run_id: str
) -> list[dict[str, Any]]:
    """Every stored review record a promoting review run should commit.

    A review run's deliverable is the record it stored beside the subject it
    read, and that record is one round of that subject's review: the run makes
    no repository commit of its own, so landing it is the moment the round can
    be landed. A subject reviewed more than once — a first review, then a
    repair round and its re-review at a new head — leaves one round per head in
    the host staging store, and every one of them is the subject's review. So
    the promotion commits every stored round of the subject, across all heads
    and all review run ids, not only the round this run selected for the head
    it promotes; the earlier rounds would otherwise survive only in the staging
    store and nowhere that travels with the plan and the ledger.

    The subject is resolved from the run's node id (a plan review reviews a
    plan and names no reviewed run), and its rounds are enumerated from the
    staging store. The round this run delivered is included whether or not the
    store index already lists it, so a record filed off the run's own path is
    still committed. A plan review, whose subject is a plan, contributes the
    single round this run produced. A run that is not a review contributes
    nothing and lands as before. Rounds are deduplicated by review run id, the
    key the committed file is filed under, so the delivered round and the same
    round read from the index commit once.

    The round this run delivered is filed under the promoting run's id. A
    record a reviewer wrote by hand can lose its own ``review_run_id``, and
    since the promoting run is exactly the run that produced that round its id
    is the correct key: refusing would abandon the round the run was minted to
    deliver, and filing it under the reviewed run would collide across two
    review runs of one subject. A round the index returns that is neither this
    run's own delivered round nor carries a review run id of its own is keyed by
    the derived legacy id of its own bytes, the same derivation the host importer
    uses, and marked ``review_run_id_source: derived`` — so every round of the
    subject lands rather than surviving only in the staging store.

    The store refuses a record it cannot key or time; this function does not
    filter such a record out, because the refusal must reach the caller as a
    rolled-back landing rather than as a silently dropped round.
    """
    from reckon.crew import recovery

    if not recovery._is_review_run(record):
        return []
    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()

    def include(payload: Mapping[str, Any] | None) -> None:
        if not payload:
            return
        key = str(payload.get("review_run_id") or "").strip()
        if key:
            if key in seen:
                return
            seen.add(key)
        payloads.append(dict(payload))

    delivered = recovery._delivered_review_record(
        record, str(record.get("project") or "")
    )
    delivered_path = delivered[1] if delivered is not None else None
    if delivered_path is not None:
        delivered_payload = _read_json_object(delivered_path)
        if delivered_payload and not str(
            delivered_payload.get("review_run_id") or ""
        ).strip():
            delivered_payload = {**delivered_payload, "review_run_id": run_id}
        include(delivered_payload)
    reviewed_run_id = recovery._resolved_reviewed_run_id(record, project)
    if reviewed_run_id:
        for path, stored in review_module.stored_records_for_run(
            project, reviewed_run_id
        ):
            if delivered_path is not None and Path(path) == Path(delivered_path):
                continue
            round_payload = stored
            if not str(stored.get("review_run_id") or "").strip():
                # The round carries no review run id of its own, so it cannot be
                # keyed by one. Its bytes key it stably instead: file it under
                # the derived legacy id — the same derivation the host importer
                # uses — so the round is committed rather than surviving only in
                # the staging store.
                round_payload = {
                    **stored,
                    "review_run_id": review_module.derived_legacy_review_run_id(
                        Path(path).read_bytes()
                    ),
                    "review_run_id_source": "derived",
                }
            include(round_payload)
    if not payloads:
        # A plan review names no reviewed run and its own round was not found
        # through the delivered lookup, so the store index for the review run
        # id is the remaining way to reach it.
        include(_staging_review_record_by_run(project, run_id, record))
    return payloads



from reckon.crew.promotion_evidence import (
    _committed_scope,
    _manifest_text,
    _presented_commits_without_a_declaration,
    _recorded_exit_status,
)
from reckon.crew.promotion_records import (
    _read_json_object,
    _release_terminal_manifest,
)
from reckon.crew.promotion_release import (
    _record_tree,
    _tree_for_measurement,
)
from reckon.crew.promotion_scope import (
    _accepted_scope_exceptions,
    _changed_paths_declare_no_paths,
    _changed_paths_inside_repository,
    _is_shadow,
    _outside_declared_scope,
    _require_committable_checkout,
    _require_repository_tree_boundary,
    _resolve_commits,
    _run_streams,
    _shadow_patch_stat,
    _write_shadow_patch,
)
