from __future__ import annotations

import argparse
import ast
import contextlib
import ctypes
import dataclasses
import errno
import fcntl
import hashlib
import inspect
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from reckon import _backends, _store, capability, flight, ledger
from reckon._plan_html import (
    _strip_tags,
    plan_headings,
    section_id_candidates,
    section_prose,
)
from reckon._timestamps import parse_iso, parse_utc
from reckon.crew import bar as bar_module
from reckon.crew import lane_document as _lane_document
from reckon.crew import prescription as prescription_module
from reckon.crew import summary
from reckon.crew.node import (
    _SAFE_ID,
    _TERMINAL_RUN_PHASES,
    DEFAULT_MEMBER_IDLE_WINDOW,
    NEEDS_HELP_MARKER,
    BudgetHold,
    CompetenceLimit,
    CrewError,
    NodeValidation,
    PlanVisibilityError,
    ScopeConflict,
    TaskNode,
    UnreconciledRuns,
    WatcherRequired,
    claim_disposition,
    claim_repository,
    done_when_warnings,
    gate_population_finding,
    member_in_flight_verdict,
    negative_control_finding,
    normalize_section,
    parse_duration,
    placement_query_undeclared,
    placement_requirement_node_local,
    placement_requirement_unmet,
    refuse_member_in_flight,
    repository_identity,
    role_may_write_repository_paths,
    validate_node,
)
from reckon.crew.prompts import compose_prompt, time_fence_statement
from reckon.crew.recovery import (
    REVIEW_NODE_PREFIX,
    _resolve_commit,
    resume_window_refusal,
    stream_paths_newest_first,
)
from reckon.crew.refusals import format_refusal
from reckon.crew.reserve import admit_windows as reserve_admit_windows
from reckon.crew.review import review_store_root
from reckon.crew.routing import (
    _agent_configuration,
    _boundary_tree_roots,
    _budget_verdict,
    _competence_verdict,
    _create_worktree,
    _disposable_member_id,
    _fleet_script,
    _remove_worktree,
    _repository_tree_snapshot,
    _signal_process_group,
    _workspace_roots,
    mounted_repository_projects,
    reap_idle_session_members,
    require_plan_reviewed,
    require_plan_section_visible,
    resolve_budget_fallback,
    resolve_dispatch_authority,
    resolve_dispatch_ledger_root,
    resolve_role,
    resolve_role_override,
    resolve_scope_repository,
    resolve_section_routing,
    resolved_time_budget,
    resolved_time_ceiling,
    shadow_worktree_session,
    shared_verdict_inputs,
    signal_worker,
)
from reckon.crew.runs import (
    WATCH_LOG_ENV,
    _expanded_scope_paths,
    _manifest_freshness,
    _manifest_mtime_ns,
    _merge_peer_scopes,
    _mutate_pointer,
    _pointer_lock,
    _process_start_time,
    _project_derivations,
    _repository_relative_scope,
    _scopes_overlap,
    _shared_write_paths,
    _utc_now,
    _watch_arming_line,
    _watch_attach_line,
    _write_json,
    capture_run_session,
    crew_home,
    delivery_roots,
    list_live,
    new_run_id,
    placement_job_alive,
    pointer_path,
    process_alive,
    read_pointer,
    record_process_alive,
    reports_dir,
    run_dir,
    scheduler_job_reason,
    scheduler_job_state,
    scheduler_kill_class,
    watch_lock_path,
    watch_log_path,
    watch_observer_alive,
    watch_state,
    watch_stream_path,
)

_INOTIFY_EVENTS = 0x00000100 | 0x00000008 | 0x00000080


class DirectoryClaimConflict(ScopeConflict):
    """A directory write claim overlaps a live run's exact path.

    A directory claim is deliberately coarser than an exact file claim: it can
    sweep up files a peer already holds. So it refuses by default and names the
    exact paths that collide, letting the caller either narrow the claim to the
    files its brief names or pass ``--accept-directory-claim`` to keep the whole
    tree. The message states the claim, its owner, the exact alternative and the
    flag, because a refusal that only says no costs a diagnosis.
    """

    def __init__(
        self,
        *,
        run_id: str,
        node_id: str,
        candidate_path: str,
        claimed_path: str,
        alternatives: Iterable[str] = (),
    ) -> None:
        super().__init__(
            run_id=run_id,
            node_id=node_id,
            candidate_path=candidate_path,
            claimed_path=claimed_path,
        )
        self.alternatives = tuple(alternatives)
        listing = ", ".join(repr(path) for path in self.alternatives) or "none"
        self.args = (
            format_refusal(
                "D12",
                f"write path {candidate_path!r} claims a directory overlapping "
                f"the live claim {claimed_path!r} held by run {run_id!r} "
                f"(node {node_id!r}); declare the files the brief names as the "
                f"exact alternative ({listing}) or pass "
                "--accept-directory-claim to claim the whole directory",
            ),
        )


# Process startup and registration may receive only one scheduler slice in six
# while two CPU-bound jobs share a loaded host. Keep every watcher condition
# wait on this one six-times-unloaded bound so a red test reports a producer
# defect rather than which process won the scheduler.
WATCHER_LOAD_BOUND_SECONDS = 30.0

# Workers launch inside the fence. Every worker launch — a fresh dispatch, an
# in-place resume and a lane-change redispatch alike — sits behind a read-only
# overlay of the operator's dot directories, so a worker cannot write the
# coordinator's home, project state or plan store; the run's own write roots are
# re-bound writable over that overlay, including a linked worktree's git
# directory and its repository's shared object store, so a fenced worker can
# still commit the work it was dispatched to do; each run then adopts its own
# harness home, seeded from the operator's settings and instruction files so the
# run's hooks and guidance are still read.
#
# One launch is deliberately outside the overlay: the lane-availability probe in
# reckon/crew/resumption.py (_request_lane_availability) composes its launch with
# fence=False. It issues the smallest supported model request to classify whether
# a lane serves, so it runs no worker and writes nowhere the overlay protects; it
# exists to decide routing rather than to do a run's work, and the fence binds a
# run's worktree and run directory, neither of which a probe has.
FENCE_WORKERS = True


# Arming spawns a detached supervisor on purpose: a coordinator's producer has
# to outlive the process that armed it. Under a test the same act is a leak —
# the test ends, its configuration home is deleted, and the producer keeps
# polling a directory nothing will ever write to again. Measured before a
# manual reap: 25 live producers for one fixture project, the oldest 204 hours
# old, 14 of them polling an already-deleted temporary home. So arming refuses
# when the resolved configuration home lies under a pytest temporary
# directory, and the refusal is raised at the caller rather than skipped
# quietly. A pytest-named directory above the home is one signal; a pytest
# session's own declared --basetemp is the other, so a custom base temp whose
# directory carries no pytest name is still recognised. A test whose own
# subject is the producer lifecycle, and which reaps what it starts, says so
# through this variable.
WATCH_ARMING_ENV = "RECKON_WATCH_ARMING"
_PYTEST_TEMPORARY_ROOT = re.compile(r"^(pytest-of-.+|pytest-\d+)$")

# Twenty-two words can still be one compact evidence pointer naming a test
# path, symbol, unit, and numeric threshold. The twenty-third word is where the
# text stops being that pointer and becomes a reproduced passage. Report rather
# than refuse: short shared facts are the desired way to point back to a plan,
# and making an advisory overlap check block dispatch would invite disabling it.
DONE_WHEN_PLAN_TEXT_SPAN_WORDS = 23


def project_mount_repository(project: str) -> Path | None:
    """Return the repository root registered for one project's docs mount.

    The answer comes from the same mounted-project map every other scope
    resolution consults, so a project's mount has one definition rather than a
    second spelling here.
    """
    for repository, projects in mounted_repository_projects().items():
        if project in projects:
            return repository
    return None


def resolve_project_repository(
    project: str, repo: str | Path | None, *, flag: str = "--repo"
) -> Path:
    """Return the repository root one project's work is written in.

    The project's registered mount decides it, never the repository enclosing
    the caller's working directory. A caller that names no repository is given
    the mount, because a dispatch run from another checkout otherwise cuts its
    worktree from the wrong repository and the worker then finds none of its
    declared write paths. A named repository is admitted when it resolves to
    the same repository as the mount — a linked worktree shares the mount's git
    common directory, so it is one repository under two paths, which is what
    lets a coordinator dispatch from inside a worktree. Anything else is
    refused before a worktree, pointer or ledger row exists, naming both
    resolved roots and the flag so the caller can correct one of them.
    """
    mount = project_mount_repository(project)
    if repo is None:
        if mount is None:
            raise CrewError(
                f"project {project!r} has no registered mount, so {flag} must "
                "name the repository its work is written in"
            )
        return mount
    named = Path(repo).expanduser().resolve()
    if mount is None:
        return named
    if repository_identity(named) == repository_identity(mount):
        # One repository under two paths: the mount is the canonical root, and
        # the caller's worktree names the same repository rather than a second
        # one, so the work is still cut from the mount.
        return mount
    raise CrewError(
        f"{flag} {named} is not the repository registered for project "
        f"{project!r} ({mount}); the project's mount decides where its work is "
        f"written, so name {mount} or omit {flag}"
    )


def _normalised_words(text: str) -> tuple[list[str], list[str]]:
    """Return comparison words and their readable spellings from HTML or prose."""
    from bs4 import BeautifulSoup

    plain = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    displayed = re.sub(r"\s+", " ", plain).strip().split()
    return [word.casefold() for word in displayed], displayed


def _section_heading_matches(heading, requested: str, ids: set[str]) -> bool:
    """Return whether one heading identifies the requested authored section."""
    if heading.identity in ids or str(heading.raw_id or "").casefold() in ids:
        return True
    text = re.sub(r"\s+", " ", heading.text).casefold()
    return text == requested or bool(
        re.match(rf"^{re.escape(requested)}(?:\s|[-—:])", text)
    )


def _plan_section_text(html_text: str, section: str) -> str | None:
    """Extract one section's visible text without including its successors.

    The requested spelling resolves to a heading record. An authored plan
    section — a level-two heading the section-prose reader serves — takes its
    text from ``section_prose``, the one reader the review digests and the
    section view share. A heading that reader does not serve as a unit, such as
    a nested subsection or the document title, keeps its own extent's prose, so
    the level bound is unchanged. The branch for an identified element that is
    not a heading is unchanged.
    """
    from bs4 import BeautifulSoup

    requested = re.sub(r"\s+", " ", section.strip()).casefold()
    if not requested:
        return None
    ids = section_id_candidates(requested)
    headings = plan_headings(html_text)
    soup = BeautifulSoup(html_text, "html.parser")
    identified = next(
        (
            tag
            for tag in soup.find_all(id=True)
            if str(tag.get("id") or "").casefold() in ids
        ),
        None,
    )
    if identified is not None:
        heading = next(
            (item for item in headings if item.raw_id == identified.get("id")), None
        )
        if heading is None:
            return identified.get_text(" ", strip=True)
    else:
        heading = next(
            (
                item
                for item in headings
                if _section_heading_matches(item, requested, ids)
            ),
            None,
        )
    if heading is None:
        return None
    parts = [
        prose
        for identity, prose in section_prose(html_text)
        if identity == heading.identity
    ]
    if parts:
        return " ".join(parts)
    return _strip_tags(html_text[slice(*heading.span)])


def _resolved_plan_section_text(
    *,
    node: TaskNode,
    project: str,
    authority: Mapping[str, Any],
    plan_commit: str,
) -> str | None:
    """Read the named section from the resolved plan's committed blob."""
    from reckon.resources import resolve_resource

    plan_data = authority["plan"]
    plan_repo = Path(str(plan_data["repository"])).resolve()
    docs_dir = Path(str(plan_data["docs"])).resolve()
    resource = resolve_resource(
        docs_dir, project, node.plan, "plan", include_archived=False
    )
    if resource is None:
        return None
    relative_path = resource.path.resolve().relative_to(plan_repo)
    blob = subprocess.run(
        ["git", "show", f"{plan_commit}:{relative_path.as_posix()}"],
        cwd=str(plan_repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if blob.returncode:
        return None
    return _plan_section_text(blob.stdout, node.section)


def _dispatch_section_routing(
    config: Mapping[str, Any],
    *,
    node: TaskNode,
    project: str,
    repo: str | Path | None,
    authority: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Resolve a node's routing from its plan section's own typed record.

    The record declares capability and executable run history supplies the
    attempt count. A section at the threshold resolves on the raised class's
    lane, and the payload's summary names the count that caused it.

    A node whose plan or section cannot be read here resolves through role
    routing alone: the visibility gates downstream remain the authority for
    refusals, so a lane lookup that cannot see the plan — no repository, no
    mount, a record the parser rejects — must not become a new refusal point,
    nor a reason a node that dispatches today stops dispatching. A rule that
    was read and could not be resolved is the one failure that fallback would
    otherwise hide, so it comes back as a routing failure record rather than as
    nothing: the node still dispatches on role routing, and the record says the
    raise was attempted and lost.
    """
    from reckon.resources import ResourceCollision, resolve_resource

    if not node.section.strip() or not node.plan.strip():
        return None
    if repo is None and authority is None:
        return None
    try:
        resolved_authority = dict(
            authority
            or resolve_dispatch_authority(project, Path(str(repo)).resolve())
        )
        docs_dir = Path(str(resolved_authority["plan"]["docs"])).resolve()
        resource = resolve_resource(
            docs_dir, project, node.plan, "plan", include_archived=False
        )
        if resource is None:
            return None
        return resolve_section_routing(config, node=node, plan_path=resource.path)
    except (
        CrewError,
        ledger.LedgerError,
        PlanVisibilityError,
        ResourceCollision,
        OSError,
        ValueError,
    ) as exc:
        return _section_routing_failure(node.section, exc)


def _section_routing_failure(section: str, exc: BaseException) -> dict[str, Any]:
    """Return the record a dispatch carries when a section's raise cannot resolve.

    The failure names the exception class and the section, because the two cases
    it separates — a rule that raised and a section that carries no rule — both
    end on role routing and would otherwise leave identical evidence. The detail
    is the exception's own message, so a reader sees what the parser or the
    lookup actually said rather than a paraphrase of it.
    """
    label = str(section or "section")
    exception = type(exc).__name__
    detail = str(exc).strip()
    summary = f"{label}: the raise could not be resolved ({exception})"
    if detail:
        summary += f": {detail}"
    summary += "; dispatch fell back to role routing"
    return {
        "failure": {
            "section": str(section),
            "exception": exception,
            "detail": detail,
        },
        "summary": summary,
    }


def _section_routing_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Trim a routing payload to the section facts a dispatch record carries.

    The ``failure`` key is written on every outcome rather than left off when
    the rule resolved cleanly: absent would read as a section whose rule was
    never read, which is the state ``section_routing: None`` already names.
    """
    failure = payload.get("failure")
    if failure is not None:
        return {"failure": dict(failure), "summary": payload["summary"]}
    return {
        "attempts": payload["attempts"],
        "capability": payload["capability"],
        "raise": payload["raise"],
        "summary": payload["summary"],
        "failure": None,
    }


def _longest_contiguous_word_span(left: str, right: str) -> tuple[int, str]:
    """Return the length and readable text of the longest shared word run."""
    left_words, left_display = _normalised_words(left)
    right_words, _right_display = _normalised_words(right)
    previous = [0] * (len(right_words) + 1)
    best_length = 0
    best_end = 0
    for left_index, left_word in enumerate(left_words, start=1):
        current = [0] * (len(right_words) + 1)
        for right_index, right_word in enumerate(right_words, start=1):
            if left_word != right_word:
                continue
            current[right_index] = previous[right_index - 1] + 1
            if current[right_index] > best_length:
                best_length = current[right_index]
                best_end = left_index
        previous = current
    start = best_end - best_length
    return best_length, " ".join(left_display[start:best_end])


def _done_when_plan_overlap_warning(
    *,
    node: TaskNode,
    project: str,
    authority: Mapping[str, Any],
    plan_commit: str,
) -> str | None:
    """Quote copied plan prose while remaining unable to break a dispatch."""
    try:
        section_text = _resolved_plan_section_text(
            node=node,
            project=project,
            authority=authority,
            plan_commit=plan_commit,
        )
        if section_text is None:
            return None
        length, span = _longest_contiguous_word_span(section_text, node.done_when)
    except Exception:  # noqa: BLE001 - an advisory report cannot stop dispatch
        # This report is advisory. The existing visibility guard remains the
        # authority for dispatchability; failure to produce an extra warning
        # must not create a new refusal or alter an otherwise valid launch.
        return None
    if length < DONE_WHEN_PLAN_TEXT_SPAN_WORDS:
        return None
    return (
        f"done-when reproduces a {length}-word contiguous span from plan "
        f"{node.plan!r} section {node.section!r}: \u201c{span}\u201d"
    )


def _actionable_budget_hold(
    verdict: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None,
) -> BudgetHold:
    """Name when one backend's hold lifts and what refreshes its evidence."""
    from reckon import budget as budget_module

    hold = dict(verdict)
    backend = str(hold.get("backend") or "unknown")
    state = hold.get("state")
    state = state if isinstance(state, Mapping) else {}
    resets_at = state.get("resets_at")
    if resets_at:
        timing = f"the stated reset at {resets_at} lifts this hold"
    else:
        bound = float(
            budget_module.policy(config).get(
                "evidence_shelf_life_minutes",
                budget_module.DEFAULT_SHELF_LIFE_MINUTES,
            )
        )
        stamp = state.get("observed_at")
        observed = parse_utc(str(stamp))
        if observed is None:
            timing = (
                f"the evidence age is unknown against the {bound:g} minute "
                "shelf-life bound because its refusal carries no readable time"
            )
        elif bound <= 0:
            timing = (
                f"the evidence is dated {stamp}, but the {bound:g} minute "
                "shelf-life bound disables ageing"
            )
        else:
            moment = parse_utc(_utc_now())
            assert moment is not None, "the repository clock is not ISO-8601"
            age_minutes = max(0.0, (moment - observed).total_seconds() / 60.0)
            lifts_at = (observed + timedelta(minutes=bound)).astimezone(UTC)
            lift_stamp = lifts_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            timing = (
                f"the evidence is {age_minutes:.1f} minutes old against the "
                f"{bound:g} minute shelf-life bound, and ageing lifts this hold "
                f"at {lift_stamp}"
            )
    refresh = f"a served turn on backend {backend!r} refreshes this evidence"
    reason = str(hold.get("reason") or "budget evidence holds the lane")
    hold["reason"] = f"{reason}; {timing}; {refresh}"
    return BudgetHold(hold)


def _live_runs_on_backend(
    backend_name: str, *, exclude_run_ids: Iterable[str] = ()
) -> list[dict[str, Any]]:
    """Return non-terminal live pointers claiming a backend, newest run id last.

    ``exclude_run_ids`` drops runs by identity. A dispatch that has already
    published its own claim counts the lane's other occupants, never itself:
    its reservation carries no worker yet, so counting it would refuse a
    dispatch that fits under the ceiling by exactly one.
    """
    excluded = set(exclude_run_ids)
    return [
        pointer
        for pointer in list_live()
        if str(pointer.get("backend") or "") == backend_name
        and str(pointer.get("phase") or "") not in _TERMINAL_RUN_PHASES
        and str(pointer.get("run_id") or "") not in excluded
    ]


def _live_runs_across_backends(
    *, exclude_run_ids: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    """Non-terminal live pointers on every backend, newest run id last.

    The reservation roster is not a backend's: one allocation admits every
    placed worker of the host as a step, whichever backend dispatched it, so the
    population the roster is counted over is taken across backends. The lane
    bound beside it stays one backend's, because a served lane is consumed by
    every caller that sends it a request, placed or not.
    """
    excluded = set(exclude_run_ids or ())
    return [
        pointer
        for pointer in list_live()
        if str(pointer.get("phase") or "") not in _TERMINAL_RUN_PHASES
        and str(pointer.get("run_id") or "") not in excluded
    ]


def _refuse_over_reservation_roster(
    backend: Mapping[str, Any],
    occupying: list[dict[str, Any]],
    project: str | None = None,
) -> None:
    """Refuse a dispatch past the placement reservation's roster cap.

    The cap is the roster's alone: under ``--overlap`` the scheduler enforces
    nothing inside the allocation, so the only thing standing between the
    reservation and an oversubscribed node is this check. It applies to a
    backend whose workers are placed into the reservation and only while a
    reservation is actually held — an unplaced backend has no roster of ours,
    and a host with no reservation has nothing to oversubscribe.

    The record is one shared allocation's, and the count is the fleet's. One
    reservation admits every project's and every backend's placed workers, so
    every run actually placed inside it occupies the same roster whichever
    project or backend dispatched it, and a run whose record names no placement
    is unbounded by it. The population handed in is the host-wide live set and
    the seat selection applied here is the same one the hold's reach statement
    counts with, so the projects the reach names are exactly the runs this guard
    counts. A placed run holds its seat only while its worker can still hold
    memory — so a finished-but-unpromoted run, a blocked run awaiting resume,
    and a run waiting on an external condition with no live process all hold
    none. The lane bound above this counts one backend's population and should,
    because a served lane is consumed by every caller that sends it a request,
    placed or not; an allocation is consumed only by the workers running inside
    it as steps, wherever they came from.
    """
    from reckon import flight
    from reckon.crew import placement as placement_module

    if flight.placement_for(backend) is None:
        return
    if not placement_module.read_reservation(project):
        return
    occupants = placement_module.roster_occupants(occupying)
    refusal = placement_module.reservation_roster_refusal(len(occupants))
    if refusal is None:
        return
    # The seats, not the population handed in: with the population host-wide a
    # reader would otherwise be shown every live run on the machine, most of
    # which are not inside the allocation at all.
    occupying_ids = [str(pointer.get("run_id") or "unknown") for pointer in occupants]
    raise CrewError(f"{refusal} Occupying runs: {', '.join(occupying_ids) or 'none'}.")


def _refuse_over_concurrency_ceiling(
    backend_name: str,
    backend: Mapping[str, Any],
    project: str | None = None,
    *,
    exclude_run_ids: Iterable[str] = (),
) -> None:
    """Refuse a dispatch that would exceed whichever resource bound is binding.

    A physical resource that is already spent must not be asked to carry one
    more worker: the harness retry budget is fixed and reckon passes no retry
    configuration, so once an overcommitted resource refuses long enough a 429
    turns from a pause at the protocol into a dead print-mode worker — measured,
    a sixth concurrent worker on the local lane killed two already-running runs
    after ten 429 retries. Adding work destroyed work, so the only reliable
    remedy is not to create the overload.

    Which resource bounds the lane is read rather than assumed. The cores a
    placement's reservation admits and the login memory slice the coordinator
    still lives inside are the candidates, and the refusal names the one that
    ran out with its measured value. The retired roster key is still carried by
    the bound model so a reader can find it, but it never binds. The check
    happens before any worktree or worker exists and never touches a run
    already in flight — a finished run holds no slot (its phase is terminal),
    and terminating one to admit a new one would reproduce the harm this exists
    to prevent.

    Every bound is user data or a host reading. A bound that cannot be read
    admits: an unstated reservation and an unreadable cgroup can neither of
    them justify refusing work.
    """
    occupying = _live_runs_on_backend(backend_name, exclude_run_ids=exclude_run_ids)
    # The cores bound and the roster refusal are consumed by the workers placed
    # inside the shared reservation, not by every run on one backend: an
    # unplaced run runs outside the allocation and holds no core of it, and a
    # placed run of another backend is still a step inside the same allocation.
    # So both are measured against the reservation's own roster population,
    # taken across backends with the one selection the hold's reach statement
    # counts with, while the lane bound above stays this backend's own.
    reservation_occupancy: int | None = None
    roster_pointers: list[dict[str, Any]] = []
    if flight.placement_for(backend) is not None:
        from reckon.crew import placement as placement_module

        roster_pointers = _live_runs_across_backends(exclude_run_ids=exclude_run_ids)
        reservation_occupancy = len(placement_module.roster_occupants(roster_pointers))
    bounds = summary.concurrency_bounds(
        backend,
        occupancy=len(occupying),
        login_slice=summary.read_login_slice(),
        reservation_occupancy=reservation_occupancy,
    )
    binding = summary.binding_bound(bounds)
    if binding is not None and not binding.admits_one_more:
        run_ids = [str(pointer.get("run_id") or "unknown") for pointer in occupying]
        raise CrewError(
            format_refusal(
                "D09",
                summary.bound_refusal_text(
                    binding, backend_name=backend_name, occupying=run_ids
                ),
            )
        )
    # A placed backend's workers run inside the one shared reservation, and
    # under --overlap the scheduler admits whatever is asked, so the
    # reservation's own roster is a real limit rather than a formality: it is
    # the only bound nothing else enforces on the fleet's behalf. It is counted
    # over the host-wide population, because a placed worker of any backend is
    # a step inside the same allocation.
    _refuse_over_reservation_roster(backend, roster_pointers, project)


def _refuse_against_the_bookend_reserve(
    *,
    config: Mapping[str, Any] | None,
    role: str | None,
    pace_record: Mapping[str, Any],
) -> None:
    """Refuse a dispatch the bookend reserve withholds the window's fraction from.

    The figure is the dispatch's own pace row — the one composed before this
    refusal and carried on the run record — so the reading a caller is refused
    against and the reading a later replay judges the decision from are one
    reading rather than two that could disagree.

    Only a lane declaring a wallet is judged. A lane carrying no wallet has no
    window to reserve a share of, and its row reports no reading by
    construction rather than a reading that failed; refusing there would read
    an absent wallet as an unreadable window and would bar every unmetered
    lane from implementation work.

    The row says which periods the source published. An unpublished period has
    no reserve to judge; a published period whose reading failed is unreadable
    and still refuses work outside the bookend roles.
    """
    if pace_record.get("group") is None:
        return
    verdict = reserve_admit_windows(
        (config or {}).get("budget") or {},
        role=role,
        clocks=pace_record.get("clocks") or {},
        lane=str(pace_record.get("lane") or ""),
    )
    if verdict["admitted"]:
        return
    raise CrewError(format_refusal("D10", verdict["reason"]))


def _jsonl_events(path: Path) -> Iterable[Mapping[str, Any]]:
    """Yield readable objects from an append-only harness transcript."""
    try:
        lines = path.open(encoding="utf-8")
    except OSError:
        return
    with lines:
        for line in lines:
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(event, Mapping):
                yield event


def _charged_claude_tokens(usage: Mapping[str, Any]) -> dict[str, int] | None:
    """Normalize one assistant request to the input total the meter charges."""

    def count(name: str) -> int:
        value = usage.get(name)
        measured = isinstance(value, (int, float)) and not isinstance(value, bool)
        return int(value) if measured else 0

    uncached = count("input_tokens")
    created = count("cache_creation_input_tokens")
    cached = count("cache_read_input_tokens")
    output = count("output_tokens")
    reasoning_detail = usage.get("output_tokens_details")
    reasoning = (
        int(reasoning_detail.get("thinking_tokens") or 0)
        if isinstance(reasoning_detail, Mapping)
        else 0
    )
    charged_input = uncached + created + cached
    if not charged_input and not output:
        return None
    return {
        "input_tokens": charged_input,
        "uncached_input_tokens": uncached,
        "cache_creation_input_tokens": created,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": charged_input + output,
    }


def _claude_authoring_turn(path: Path) -> dict[str, int] | None:
    """Return the latest complete assistant request from a Claude transcript."""
    latest = None
    for event in _jsonl_events(path):
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        usage = message.get("usage") if isinstance(message, Mapping) else None
        if isinstance(usage, Mapping):
            latest = _charged_claude_tokens(usage) or latest
    return latest


def _codex_authoring_turn(path: Path) -> dict[str, int] | None:
    """Return cumulative usage for the current Codex turn through dispatch."""
    latest = None
    for event in _jsonl_events(path):
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        usage = info.get("total_token_usage") if isinstance(info, Mapping) else None
        if not isinstance(usage, Mapping):
            continue
        measured = {
            str(key): int(value)
            for key, value in usage.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        latest = measured or latest
    return latest


def _ancestor_processes() -> tuple[set[int], list[str]]:
    """Return the bounded process ancestry used to identify the active harness."""
    pids: set[int] = set()
    names: list[str] = []
    pid = os.getppid()
    for _ in range(12):
        if pid <= 1 or pid in pids:
            break
        pids.add(pid)
        try:
            names.append((Path("/proc") / str(pid) / "comm").read_text().strip())
            fields = (Path("/proc") / str(pid) / "stat").read_text().split()
            pid = int(fields[3])
        except (OSError, IndexError, TypeError, ValueError):
            break
    return pids, names


def _latest_transcript(paths: Iterable[Path]) -> Path | None:
    candidates = []
    for path in paths:
        try:
            candidates.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def _coordinator_runtime() -> tuple[str | None, str | None, Path | None]:
    """Resolve the calling harness, its session id, and current transcript."""
    ancestry, names = _ancestor_processes()
    claude_session = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    codex_session = (
        os.environ.get("CODEX_SESSION_ID", "").strip()
        or os.environ.get("CODEX_THREAD_ID", "").strip()
    )
    try:
        claude_pid = int(os.environ.get("CLAUDE_PID", ""))
    except ValueError:
        claude_pid = -1
    harness = None
    runtime_session = None
    if claude_session and claude_pid in ancestry:
        harness, runtime_session = "claude-code", claude_session
    elif codex_session and any(name == "codex" for name in names):
        harness, runtime_session = "codex", codex_session
    elif claude_session and not codex_session:
        harness, runtime_session = "claude-code", claude_session
    elif codex_session:
        harness, runtime_session = "codex", codex_session
    elif claude_session:
        harness, runtime_session = "claude-code", claude_session
    if harness is None or runtime_session is None:
        return None, None, None

    user_home = Path.home()
    if harness == "claude-code":
        project_key = str(Path.cwd().resolve()).replace("/", "-")
        direct = (
            user_home
            / ".claude"
            / "projects"
            / project_key
            / f"{runtime_session}.jsonl"
        )
        search_root = user_home / ".claude" / "projects"
        transcript = (
            direct
            if direct.is_file()
            else _latest_transcript(search_root.glob(f"*/{runtime_session}.jsonl"))
        )
    else:
        search_root = user_home / ".codex" / "sessions"
        transcript = _latest_transcript(
            search_root.glob(f"*/*/*/*{runtime_session}.jsonl")
        )
    return harness, runtime_session, transcript


def _coordinator_accounting(session: str) -> dict[str, Any]:
    """Identify the dispatcher and measure the request that authored this run."""
    harness, runtime_session, transcript = _coordinator_runtime()
    tokens = None
    if transcript is not None and harness == "claude-code":
        tokens = _claude_authoring_turn(transcript)
    elif transcript is not None and harness == "codex":
        tokens = _codex_authoring_turn(transcript)
    authoring_turn = (
        {
            "status": "measured",
            "tokens": tokens,
            "source": f"{harness}-session-transcript",
        }
        if tokens is not None
        else {
            "status": "unknown",
            "tokens": None,
            "detail": "the dispatching harness exposed no authoring-turn token usage",
        }
    )
    return {
        "session_id": str(session),
        "runtime_session_id": runtime_session,
        "harness": harness,
        "authoring_turn": authoring_turn,
    }


def _watch_arming_intent() -> str:
    """Return the environment's stated arming intent: ``on``, ``off`` or ``""``."""
    return os.environ.get(WATCH_ARMING_ENV, "").strip().lower()


def watch_arming_suppressed() -> bool:
    """True when the environment forbids arming, so callers waive the watch.

    The suppression is expressed through the same waiver a `--no-watch`
    dispatch records, so a suppressed run is visible on its own record rather
    than being a producer that silently never existed.
    """
    return _watch_arming_intent() == "off"


def _running_under_pytest() -> bool:
    """True when this process is a pytest session or one of its workers."""
    return bool(
        os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("PYTEST_VERSION")
    )


def _current_and_ancestor_argvs(limit: int = 12) -> list[list[str]]:
    """The argv of this process and its ancestors, nearest first, bounded."""
    argvs: list[list[str]] = []
    pid = os.getpid()
    for _ in range(limit):
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            break
        argvs.append([os.fsdecode(item) for item in raw.split(b"\0") if item])
        rest = stat.rsplit(")", 1)[-1].split()
        parent = int(rest[1]) if len(rest) > 1 else 2
        if parent in (0, 1, pid):
            break
        pid = parent
    return argvs


def _declared_basetemp() -> Path | None:
    """The temporary root the enclosing pytest session was told to use.

    A session given a custom ``--basetemp`` names it on the command line, which
    this process carries itself in a serial run and inherits from the session
    master through its ancestors under ``pytest-xdist``. The default base temp
    is discovered instead by the ``pytest-of-*`` ancestor name, so only a
    declared one is read here, and only when a pytest session is running.
    """
    if not _running_under_pytest():
        return None
    for argv in _current_and_ancestor_argvs():
        for index, arg in enumerate(argv):
            if arg == "--basetemp" and index + 1 < len(argv):
                return Path(argv[index + 1])
            if arg.startswith("--basetemp="):
                return Path(arg.split("=", 1)[1])
    return None


def _temporary_home_root(home: Path) -> Path | None:
    """Return the throwaway test root containing ``home``, if there is one.

    Two signals name a throwaway home: a pytest-named directory above it, and
    the temporary root a running pytest session declared on its own command
    line. The second is what carries a custom ``--basetemp`` such as
    ``/tmp/anything``, whose directory carries no pytest name for the first to
    match, and it is read from the running session so an ordinary home outside
    that root is still armed.
    """
    for candidate in (home, *home.parents, *home.resolve().parents):
        if _PYTEST_TEMPORARY_ROOT.match(candidate.name):
            return candidate
    declared = _declared_basetemp()
    if declared is not None:
        root = declared.resolve()
        resolved = home.resolve()
        if resolved == root or root in resolved.parents:
            return declared
    return None


def _refuse_arming_under_a_throwaway_home(project: str) -> None:
    """Refuse to arm a producer that would outlive the home it reports into."""
    if _watch_arming_intent() == "on":
        return
    home = _store._config_home()
    root = _temporary_home_root(home)
    if root is None:
        return
    raise CrewError(
        format_refusal(
            "D18",
            f"refusing to arm the watch producer for {project}: the resolved "
            f"configuration home {home} lies under the throwaway test directory "
            f"{root}, so a detached producer would outlive the run that armed it "
            f"and poll a deleted home. Set {WATCH_ARMING_ENV}=on for a caller "
            "that reaps the producer it starts, or waive the watch instead.",
        )
    )


def _watch_executable() -> str:
    """Resolve the console entry point beside the running interpreter first."""
    adjacent = Path(sys.executable).with_name("reckon")
    if adjacent.is_file():
        return str(adjacent)
    executable = shutil.which("reckon")
    if executable:
        return executable
    raise CrewError(
        format_refusal("D19", "cannot start the project watcher: reckon is not on PATH")
    )


# The supervisor that starts one project's watcher as a background process and
# waits on it. It exports the watcher's log path into the environment the
# watcher inherits, taking the variable name and the path from its own argv.
#
# The path rides the argv rather than this process's environment because the
# fleet-delegated route does not spawn here: it hands this argv to the
# allocation's batch step, which starts it from the step's own environment, so
# a path only the arming process held would reach the watcher on the route that
# does not need it and be missing on the fleet route that does. Both routes are
# covered once the supervisor exports it, whichever process runs the argv.
_WATCH_PRODUCER_SUPERVISOR = (
    "import os, subprocess, sys; "
    "os.environ[sys.argv[1]] = sys.argv[2]; "
    "producer = subprocess.Popen(sys.argv[3:], stdin=subprocess.DEVNULL, "
    "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True); "
    "raise SystemExit(producer.wait())"
)


def _watch_producer_argv(project: str) -> list[str]:
    """Build the argv that starts one project's watcher as a background process."""
    return [
        sys.executable,
        "-c",
        _WATCH_PRODUCER_SUPERVISOR,
        WATCH_LOG_ENV,
        str(watch_log_path(project)),
        _watch_executable(),
        "crew",
        "watch",
        "--project",
        project,
    ]


@dataclasses.dataclass
class _SpawnedHandle:
    """The handle a fleet-delegated launch returns in place of a Popen.

    ``_ensure_watch_producer`` polls a producer handle to notice a producer
    that died before it took its seat. A spawn the batch step made cannot be
    polled from here — it is not this process's child — so the handle reports
    ``poll()`` as None and liveness is decided, as it always is, by the
    process-backed seat the producer registers when it starts.
    """

    pid: int

    def poll(self) -> None:
        return None


def _start_watch_producer(project: str) -> Any:
    """Start a detached supervisor that remains the watcher's live parent.

    On the fleet node the detached child is reaped with the session step that
    made it, so the producer is spawned through the same FIFO a worker is, by
    the allocation's batch step.
    """
    _refuse_arming_under_a_throwaway_home(project)
    argv = _watch_producer_argv(project)
    fleet = _read_fleet_record()
    if (
        _fleet_spawn_enabled()
        and fleet is not None
        and _runs_inside_fleet_allocation(fleet)
    ):
        runtime_dir = _fleet_runtime_dir(fleet)
        if runtime_dir is not None:
            directory = watch_lock_path(project).parent
            directory.mkdir(parents=True, exist_ok=True)
            spec_path = directory / "producer.json"
            _write_json(spec_path, {"project": project, "argv": argv})
            pid = _spawn_through_fleet(
                runtime_dir,
                f"watch-{_watch_request_slug(project)}",
                spec_path,
                directory / FLEET_SPAWN_ACK_NAME,
            )
            return _SpawnedHandle(pid=pid)
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def _stop_watch_producer_within(project: str, timeout: float) -> None:
    """Bound seat release even when the producer does not honour SIGTERM.

    Unwatch waits for the seat lock after signalling. A separate process lets
    arming cancel that wait without leaving a thread holding the arm lock.
    The child inherits neither the arm descriptor nor any other held lock.
    """
    try:
        subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "from reckon.crew.recovery import unwatch; unwatch(sys.argv[2])"
                ),
                str(Path(__file__).resolve().parents[2]),
                project,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            close_fds=True,
            timeout=timeout,
            check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise CrewError(
            f"watch producer for {project} did not release its seat within "
            f"{WATCHER_LOAD_BOUND_SECONDS:g}s; arming stopped"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise CrewError(
            f"cannot release watch producer for {project}: {exc.stderr}"
        ) from exc


def _ensure_watch_producer(
    project: str, *, session: str | None = None
) -> dict[str, Any]:
    """Return the watcher state, starting at most one producer across calls.

    The returned state reports whether a producer is live rather than raising
    when one cannot be brought up: admission is the caller's decision, and the
    caller is also the only place that holds the session whose delivery is a
    separate question from the watcher's liveness.
    """
    arming_lock = watch_stream_path(project).with_suffix(".arm.lock")
    arming_lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
    with arming_lock.open("a+b") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CrewError(
                        f"watch arming lock {arming_lock} remained held for "
                        f"{WATCHER_LOAD_BOUND_SECONDS:g}s; arming stopped"
                    ) from None
                time.sleep(0.05)
        # Only producer identity belongs inside the launch mutex. Following
        # pipes or enumerating the fleet can wait on unrelated long-lived work.
        state = watch_state(project)
        if state["watcher_live"] and watch_observer_alive(state["watcher"]) is False:
            _stop_watch_producer_within(project, max(0.0, deadline - time.monotonic()))
            state = watch_state(project)
        if not state["watcher_live"]:
            supervisor = _start_watch_producer(project)
            while time.monotonic() < deadline:
                if watch_state(project)["watcher_live"]:
                    break
                if supervisor.poll() is not None:
                    break
                time.sleep(0.05)
    return watch_state(project, session=session)


# The session host a Claude Code session runs declares its request FIFO in a
# directory under the node-local runtime root, named for the Claude process and
# its kernel start tick so the pair survives /clear. Dispatch resolves the same
# path to ask that session's host for a follower before it refuses.
SESSION_HOST_DIRECTORY = "reckon-session-host"


def _session_host_runtime_root() -> Path | None:
    """The node-local root a session host's FIFO lives under, or None.

    The same three candidates the host entry point chooses from, so a dispatch
    and the host it asks resolve the same directory: the session's own runtime
    directory first, then the per-user directory a login node provides, then the
    scratch root. Each is node-local, so waiting on the FIFO costs nothing on
    shared storage.
    """
    runtime = str(os.environ.get("XDG_RUNTIME_DIR") or "").strip()
    if runtime:
        return Path(runtime)
    run_user = Path(f"/run/user/{os.getuid()}")
    if run_user.is_dir():
        return run_user
    scratch = str(os.environ.get("TMPDIR") or "").strip()
    return Path(scratch) if scratch else None


def _session_host_owner() -> tuple[int, str] | None:
    """The calling Claude process as ``(pid, start tick)``, or None without one.

    The host belongs to this session's Claude process, not to the crew session
    name, so every path that names it -- the request FIFO and the census record
    -- is built from this pair, which stays fixed across ``/clear``. A caller
    not running under Claude Code, or one whose Claude process the kernel no
    longer reports, has no host to name.
    """
    harness, _session, _transcript = _coordinator_runtime()
    if harness != "claude-code":
        return None
    try:
        pid = int(str(os.environ.get("CLAUDE_PID") or ""))
    except ValueError:
        return None
    if pid <= 1:
        return None
    start = _process_start_time(pid)
    if not start:
        return None
    return pid, start


def _session_host_fifo() -> Path | None:
    """Resolve the calling Claude session's host FIFO, or None when there is none."""
    owner = _session_host_owner()
    root = _session_host_runtime_root()
    if owner is None or root is None:
        return None
    pid, start = owner
    return root / SESSION_HOST_DIRECTORY / f"{pid}-{start}.fifo"


def _session_host_waiting() -> bool:
    """Whether the calling session's host is waiting on its FIFO to be asked.

    A dry run must report the delivery a real dispatch would reach without
    writing to the FIFO, because a dry run starts nothing and a write starts a
    follower. A host publishes that it is waiting by holding its FIFO's
    descriptor open across the wait, so the liveness read here is the same
    non-blocking open the real request uses, closed without a byte: an open
    succeeds only while a reader holds the other end, and a FIFO with no reader
    is a host that has gone. A real dispatch writes that reader and the host
    attaches; with no reader it falls through to the Monitor path, and the
    prediction does too. A host that is wedged while still holding its FIFO
    open reads the same as a live one here; a dry run cannot tell a wedged host
    from a live one without writing a request, which is the one thing it must
    not do.
    """
    fifo = _session_host_fifo()
    if fifo is None:
        return False
    try:
        # A deadline already in the past asks for a single attempt: a reader
        # not yet on the FIFO is a fallback, not a wait.
        descriptor = _open_request_fifo(fifo, time.monotonic())
    except CrewError:
        return False
    os.close(descriptor)
    return True


# The host writes its census of running children to a directory the host module
# owns, one record per session, named for the Claude process and its kernel
# start tick. Dispatch reads that record to tell a follower the host runs from
# one a coordinator armed by hand.
def _session_host_record_path() -> Path | None:
    """The calling Claude session's host census record, or None without a host.

    Both the directory and the filename suffix are the host module's own, so the
    name dispatch reads is the name the host wrote rather than a second spelling
    of it, and an override that moves the host's records moves this reader with
    them.
    """
    owner = _session_host_owner()
    if owner is None:
        return None
    from reckon.crew.session_host import RECORD_SUFFIX, _state_dir

    pid, start = owner
    return _state_dir() / f"{pid}-{start}{RECORD_SUFFIX}"


def _session_host_runs_follower(
    project: str, session: str, follower_pid: int | None
) -> bool:
    """Whether the calling session's host runs the follower attached for a pair.

    A session may already be attached when a dispatch arrives -- an earlier
    dispatch asked its host, or the plugin's monitor attached a follower at
    session start. Asking the host again would change nothing, and reporting
    ``monitor`` would hand the caller an arming line for a follower the host
    already consumes. The host's own census names every child it started, so a
    child for this project and session carrying the pid the live registration
    holds is the fact that the follower is the host's rather than one a
    coordinator armed by hand.
    """
    path = _session_host_record_path()
    if path is None:
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, Mapping):
        return False
    for child in payload.get("children") or ():
        if not isinstance(child, Mapping):
            continue
        if str(child.get("project")) != project:
            continue
        if str(child.get("session")) != session:
            continue
        if follower_pid is None:
            continue
        if child.get("pid") == follower_pid:
            return True
    return False


def _ask_session_host_for_follower(project: str, session: str | None) -> bool:
    """Ask this session's host to follow the project, returning whether it did.

    The request is one JSON line naming the project and the session, written to
    the host's FIFO through the same non-blocking write the fleet spawn uses, so
    a FIFO with no reader means no host and falls back at once rather than
    stalling. The caller then waits, for at most the producer bound, until the
    session reads as attached -- the fact that matters, since the host attaching
    a follower is what makes the finished run reach the session. Without a host,
    or when it does not attach within the bound, this reports False and the
    caller admits exactly as it does today.
    """
    if not session:
        return False
    fifo = _session_host_fifo()
    if fifo is None:
        return False
    line = json.dumps({"project": project, "session": session}).encode("utf-8") + b"\n"
    try:
        # A deadline already in the past asks ``_write_fleet_request`` for a
        # single attempt: no reader on the FIFO is a fallback, not a wait.
        _write_fleet_request(fifo, line, time.monotonic())
    except CrewError:
        return False
    deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
    while time.monotonic() < deadline:
        if watch_state(project, session=session)["session_attached"]:
            return True
        time.sleep(0.1)
    return False


def _released_follower_warning(dispatch_watch: Mapping[str, Any]) -> str:
    """Name the released registration and the command that re-arms it.

    The text carries the paste-ready attach command verbatim, because a warning
    that only says delivery stopped leaves the operator to reconstruct the one
    command that restores it.
    """
    attach = str(dispatch_watch.get("attach_line") or "").strip()
    return (
        "this session armed a follower whose registration was released, so this "
        "run's completion will not wake it; re-arm delivery with "
        f"`{attach}`"
    )


def _unmet_follower_conditions(
    project: str,
    dispatch_watch: Mapping[str, Any],
    *,
    session: str | None,
) -> list[str]:
    """Name every reason this session's delivery is not in place, at once.

    The conditions stand alone — no producer, no registration, a follower
    whose lines reach nothing — and a caller can only fix the one a refusal
    names, so a refusal naming one at a time costs a round trip per condition.
    Measured on one worker: three refusals, about twenty-five minutes. The
    list is reported whole, and one command clears every session-side entry on
    it.
    """
    from reckon.crew.runs import follower_state, list_followers

    conditions: list[str] = []
    if not dispatch_watch.get("watcher_live"):
        ensure = str(dispatch_watch.get("ensure_line") or "").strip()
        line = f"no live crew watcher process is reading project {project!r}"
        if ensure:
            line += (
                f"; one is started with `{ensure}`, which is safe to run "
                "against a watcher that is already up"
            )
        conditions.append(line)
    if not session:
        conditions.append(
            "no session was named, so no follower's pane is this run's destination"
        )
        return conditions
    if dispatch_watch.get("session_attached") or dispatch_watch.get(
        "session_follower_released"
    ):
        # An attached session hears the run, and a released one is admitted
        # with the re-arm warning rather than a refusal.
        return conditions
    state = follower_state(project, session)
    # A peer's follower is project-global and feeds the peer, never this
    # session, so naming the sessions that do deliver answers the question the
    # caller is left with when its own delivery is not in place.
    others = sorted(
        str(row.get("session") or "")
        for row in list_followers(project)
        if row.get("live") and str(row.get("session") or "") != session
    )
    peers = (
        "; sessions delivering for this project right now: "
        + ", ".join(repr(name) for name in others)
        if others
        else ""
    )
    if state.get("registered"):
        conditions.append(
            f"session {session!r} has a follower that is not delivering: "
            f"{state.get('not_live_because')}{peers}"
        )
    else:
        conditions.append(f"session {session!r} has no registered follower{peers}")
    return conditions


class _FollowerAdmissionUnmet(WatcherRequired):
    """A dispatch refused for every unmet follower condition in one result.

    The conditions are independent and fixing one reveals the next, so a
    refusal that names a single condition teaches the caller one at a time.
    This refusal names the whole list and the one follower command that clears
    it, and it keeps the error key and exit code :class:`WatcherRequired`
    already answers with so a caller reading the documented channel sees the
    same verdict either way.
    """

    def __init__(
        self,
        project: str,
        watch: Mapping[str, Any],
        *,
        session: str | None,
        conditions: list[str],
    ) -> None:
        self.project = project
        self.watch = dict(watch)
        self.session = session
        self.conditions = list(conditions)
        attach = str(watch.get("attach_line") or "reckon crew follow").strip()
        unmet = "\n".join(f"- {condition}" for condition in self.conditions)
        count = len(self.conditions)
        plural = "condition" if count == 1 else "conditions"
        CrewError.__init__(
            self,
            format_refusal(
                "D13",
                f"session {session!r} would not hear this run finish; "
                f"{count} follower {plural} unmet, named together so one "
                f"dispatch reaches the fix:\n{unmet}\n"
                f"Arm `{attach}` with the harness primitive that reports each "
                "line as it is written -- named for this host in reckon-build "
                "references/orchestrator-harness/<harness>.md -- then dispatch "
                "again. A copied-but-wrongly-armed line is the common case: the "
                "command is right and its lines still end where nothing reads "
                "them. Or pass --no-watch to waive delivery for a synchronous "
                "dispatch",
            ),
        )


def _watcher_delivery_admission(
    project: str,
    dispatch_watch: Mapping[str, Any],
    *,
    session: str,
    launch_kind: str,
    delivery: str = "monitor",
) -> str | None:
    """Decide whether a session's delivery admits the dispatch.

    Three cases, and the whole point is that they are told apart. An attached
    session needs nothing. A session whose registration was released — it armed
    a follower that has since expired — is admitted while the watcher process
    is live, and handed the re-arm warning: the project is still watched, and
    the run's own record keeps the delivery it was missing visible to a later
    reader. Everything else is refused, and every unmet condition is named in
    the one refusal rather than one per round trip.

    ``delivery`` names how the session's follower was sought — ``"host"`` when a
    session host attached it, ``"monitor"`` otherwise. It rides the refusal too,
    so a caller that fell back to the Monitor path is told so rather than left
    to infer it.

    Returns the warning line when a released session proceeds, and ``None``
    when the session is attached or the launch kind carries no delivery.
    Raises :class:`WatcherRequired` for a session that would not hear the run,
    whatever the launch kind, and for any launch kind with no live producer.
    """
    if launch_kind != "cli":
        # A launch that is not a session delivery — an in-harness node preparing
        # a directive — has no follower of its own to judge, so the conditions
        # below do not apply to it. The producer does apply: every launch kind
        # reads the project's watch seat, so its absence is refused here for
        # this kind too, as the call site refused it for all kinds.
        if dispatch_watch.get("watcher_live"):
            return None
        refusal = WatcherRequired(project, dispatch_watch)
        refusal.delivery = delivery
        raise refusal
    if dispatch_watch.get("session_follower_released") and dispatch_watch.get(
        "watcher_live"
    ):
        return _released_follower_warning(dispatch_watch)
    conditions = _unmet_follower_conditions(project, dispatch_watch, session=session)
    if conditions:
        refusal = _FollowerAdmissionUnmet(
            project, dispatch_watch, session=session, conditions=conditions
        )
        refusal.delivery = delivery
        raise refusal
    return None


# The phases a live claim carries while its worker is still being composed.
# A pointer in any other phase — working, running, waiting, or anything a
# future writer adds — describes a run that has already passed its own
# admission, so its claim refuses newcomers however the registration times
# compare. An absent phase is read the same way: a claim that cannot be shown
# to be still composing is treated as established rather than quietly outranked.
_UNLAUNCHED_CLAIM_PHASES = frozenset(
    {"starting", "launching", "launcher", "dispatching"}
)


@dataclass(frozen=True)
class _RepositoryScopeClaim:
    """One live claim resolved to the repository that contains its path.

    ``binding`` answers whether the claim still fences its paths. It is judged
    once per pointer rather than once per path, because liveness and
    unintegrated work are properties of the run, not of the file.
    """

    project: str
    repository: Path | None
    run_id: str
    node_id: str
    path: str
    absolute_path: Path
    declared_path: str
    derived_from: str | None = None
    binding: bool = True
    disposition_reason: str = ""
    # When the claim was published, so two dispatches racing for the same paths
    # can be ordered; and whether the run has passed its own admission and
    # written the record its worker launches from. A claim still being composed
    # carries neither, and is what the ordering rule below arbitrates.
    registered_at: str = ""
    launched: bool = False


def _scope_derivation_project(
    project: str,
    repository: Path,
    repository_projects: Mapping[Path, tuple[str, ...]],
    preferred_projects: Iterable[str] = (),
) -> str:
    """Choose the project resource that owns derivations for a repository."""
    mounted = repository_projects.get(repository, ())
    if project in mounted:
        return project
    for preferred in preferred_projects:
        if preferred in mounted:
            return preferred
    return mounted[0] if mounted else project


def _resolved_scope_entries(
    paths: Iterable[str],
    *,
    base_repository: Path,
    repositories: Iterable[Path],
    project: str,
    repository_projects: Mapping[Path, tuple[str, ...]],
    preferred_projects: Iterable[str] = (),
) -> list[tuple[Path | None, str, Path, str, str | None]]:
    """Expand paths within the repository and project resource that own them."""
    roots = tuple(repositories)
    grouped: dict[Path | None, list[str]] = {}
    for declared in paths:
        repository = resolve_scope_repository(
            declared,
            base_repository=base_repository,
            repositories=roots,
        )
        grouped.setdefault(repository, []).append(declared)

    entries: list[tuple[Path | None, str, Path, str, str | None]] = []
    for repository, declared_paths in grouped.items():
        if repository is None:
            for declared in declared_paths:
                raw = Path(declared).expanduser()
                absolute = (
                    raw if raw.is_absolute() else base_repository / raw
                ).resolve()
                entries.append(
                    (None, absolute.as_posix(), absolute, absolute.as_posix(), None)
                )
            continue
        derivation_project = _scope_derivation_project(
            project,
            repository,
            repository_projects,
            preferred_projects,
        )
        derivations = _project_derivations(derivation_project, repository)
        for path, normalized_declared, derived_from in _expanded_scope_paths(
            declared_paths, repository, derivations
        ):
            entries.append(
                (
                    repository,
                    path,
                    (repository / path).resolve(),
                    normalized_declared,
                    derived_from,
                )
            )
    return entries


def _repository_scope_claims(
    *, exclude_run_ids: Iterable[str] = ()
) -> list[_RepositoryScopeClaim]:
    """Read live claims globally and group their paths by repository root.

    ``exclude_run_ids`` drops runs by identity. A dispatch that has already
    published a claim for the run it created reads every other live claim, never
    its own: an arbitration run against a run's own claim would refuse the
    dispatch that had just made it.
    """
    excluded = set(exclude_run_ids)
    repository_projects = mounted_repository_projects()
    claims: list[_RepositoryScopeClaim] = []
    for pointer in list_live():
        if str(pointer.get("run_id") or "") in excluded:
            continue
        pointer_repo_value = str(pointer.get("repo") or "")
        if not pointer_repo_value:
            continue
        # The claim's repository comes from the checkout the worker writes in.
        # ``repo`` is resolved from the run's PROJECT mount, so a run carrying
        # one project's plan into another project's checkout records a ``repo``
        # holding none of its declared paths — and the paths then resolve into a
        # repository that no other claim on the same file can intersect.
        pointer_repo = (
            claim_repository(pointer) or Path(pointer_repo_value).expanduser().resolve()
        )
        disposition = claim_disposition(pointer)
        project = str(pointer.get("project") or "")
        authority = pointer.get("authority")
        authority = authority if isinstance(authority, Mapping) else {}
        authority_roots = {
            Path(str(root)).expanduser().resolve()
            for root in authority.get("repositories") or ()
        }
        roots = {*repository_projects, *authority_roots, pointer_repo}
        write = authority.get("write")
        write = write if isinstance(write, Mapping) else {}
        preferred_projects = tuple(str(item) for item in write.get("projects") or ())
        node = pointer.get("node")
        if not isinstance(node, Mapping):
            continue
        run_id = str(pointer.get("run_id") or "unknown")
        node_id = str(node.get("id") or "unknown")
        for (
            repository,
            path,
            absolute,
            declared,
            derived_from,
        ) in _resolved_scope_entries(
            node.get("write_paths") or (),
            base_repository=pointer_repo,
            repositories=roots,
            project=project,
            repository_projects=repository_projects,
            preferred_projects=preferred_projects,
        ):
            claims.append(
                _RepositoryScopeClaim(
                    project=project,
                    repository=repository,
                    run_id=run_id,
                    node_id=node_id,
                    path=path,
                    absolute_path=absolute,
                    declared_path=declared,
                    derived_from=derived_from,
                    binding=disposition.binding,
                    disposition_reason=disposition.reason,
                    registered_at=str(pointer.get("created_at") or ""),
                    # A run that has written the record it launches its worker
                    # from, whose process has started, or whose phase has moved
                    # past the pre-spawn set has already passed its own
                    # admission: the claim is no longer forming, so it refuses
                    # newcomers as it always has. The launch claim published
                    # before the checks is the only shape that is still
                    # composing, and the phase is what names it.
                    launched=(
                        bool(pointer.get("worktree") or pointer.get("pid"))
                        or str(pointer.get("phase") or "")
                        not in _UNLAUNCHED_CLAIM_PHASES
                    ),
                )
            )
    return sorted(
        claims,
        key=lambda claim: (claim.run_id, claim.node_id, claim.absolute_path.as_posix()),
    )


# The exclusive claim that gates one node's worktree path. Two dispatches of
# one node under one worktree identity would otherwise both reach
# ``git worktree add`` on the same path, each creation losing directories the
# other is writing. A read of the live pointers cannot close that window —
# both dispatches can read before either publishes — so the claim is an
# exclusive file create under the crew store: the kernel decides which
# dispatch owns the path, and the loser reads the holder's record and refuses,
# naming it, before it has touched the worktree. A holder that died without
# releasing is found by its recorded pid and process start time and moved
# aside, so one killed dispatcher cannot block the node for everyone.
NODE_DISPATCH_CLAIM_DIRECTORY = "claims"

# How long a refused dispatch re-reads the holder's record before naming it
# without a run id. The holder writes its record immediately after the
# exclusive create, so the window is microseconds; the bound exists so a
# refusal still names the holder rather than a blank.
_NODE_CLAIM_RECORD_ATTEMPTS = 20
_NODE_CLAIM_RECORD_INTERVAL_SECONDS = 0.01

# How long a claim record that names no holder is left alone before a later
# dispatch may reclaim it. A dispatcher writes its record microseconds after
# the exclusive create, so an empty or unreadable file is either one still
# being written or one whose writer died in that window; only the second may
# be displaced, and the elapsed time separates them.
_NODE_CLAIM_EMPTY_RECORD_STALE_SECONDS = 5.0

# How many times a dispatch re-runs the reclaim-or-refuse decision before it
# gives up. One reclaim is the ordinary case; the bound only exists so a storm
# of reclaimers cannot spin.
_NODE_CLAIM_ATTEMPTS = 8


def _node_dispatch_claim_path(
    project: str, worktree_identity: str, node_id: str
) -> Path:
    """The file whose exclusive creation gates one node's worktree path."""
    identity = f"{project}\x00{worktree_identity}\x00{node_id}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", node_id).strip("-")[:48] or "node"
    return crew_home() / NODE_DISPATCH_CLAIM_DIRECTORY / f"{label}-{digest}.json"


def _read_node_dispatch_claim(path: Path) -> Mapping[str, Any]:
    """Read a holder's record, waiting briefly for its write to land."""
    for attempt in range(_NODE_CLAIM_RECORD_ATTEMPTS):
        try:
            payload = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            payload = {}
        if isinstance(payload, dict) and payload.get("run_id"):
            return payload
        if attempt + 1 < _NODE_CLAIM_RECORD_ATTEMPTS:
            time.sleep(_NODE_CLAIM_RECORD_INTERVAL_SECONDS)
    return {}


def _claim_holder_is_alive(holder: Mapping[str, Any]) -> bool:
    """Whether the process that wrote a claim record is still that process.

    A claim whose holder is gone makes the node undispatable for everyone, so
    a holder the kernel contradicts is expendable. A pid alone cannot answer
    the question: pid numbers are reused, so the start time pins the pid to
    one process, and a mismatch means the recorded holder is gone whatever now
    wears its number. A record that cannot be interrogated at all — no pid or
    start time recorded, no kernel start times to read, or a pid beyond the
    range the kernel takes — gets the conservative answer: the holder counts
    as present, because displacing a live dispatch whose record is still being
    written would leave two.
    """
    holder_pid = holder.get("pid")
    recorded_start = holder.get("process_start_time")
    if not isinstance(holder_pid, int) or not isinstance(recorded_start, str):
        return True
    if not Path("/proc").is_dir():
        return True
    try:
        os.kill(holder_pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError):
        return True
    current_start = _process_start_time(holder_pid)
    if current_start is None:
        return False
    return current_start == recorded_start


def _empty_claim_is_stale(path: Path, holder: Mapping[str, Any]) -> bool:
    """Whether a claim record naming no holder is old enough to reclaim.

    A dispatcher that dies between the exclusive create and its record write
    leaves a file with nothing in it, and no pid to interrogate, so without
    this it would block the node for everyone. Its age is the only evidence of
    whether a writer is still coming, and the modification time answers that:
    a file that has stayed empty for longer than a record write takes has no
    writer left to displace.
    """
    if holder:
        return False
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age >= _NODE_CLAIM_EMPTY_RECORD_STALE_SECONDS


def _reclaim_stale_node_dispatch_claim(path: Path, holder: Mapping[str, Any]) -> str:
    """Move a dead holder's claim out of the way so a newcomer can take it.

    The rename is atomic, so of two reclaimers only one moves the file; the
    other finds nothing to move and races the exclusive create, which only one
    of them can win.
    """
    stale = path.with_name(f"{path.name}.reclaimed-{holder.get('run_id') or 'unknown'}")
    with contextlib.suppress(FileNotFoundError):
        os.rename(path, stale)
    return str(stale)


def _node_dispatch_in_flight_text(
    holder: Mapping[str, Any],
    *,
    node_id: str,
    project: str,
    worktree_identity: str,
    claim_path: Path,
) -> str:
    """Name the in-flight dispatch a refusal is refusing against."""
    run_id = str(holder.get("run_id") or "unknown")
    holder_pid = holder.get("pid")
    created_at = str(holder.get("created_at") or "")
    observed = ""
    if holder_pid:
        observed = f", pid {holder_pid}"
        if created_at:
            observed += f" since {created_at}"
    return (
        f"a dispatch of node {node_id!r} for project {project!r} under worktree "
        f"identity {worktree_identity!r} is already in flight as run {run_id!r}"
        f"{observed}; its claim is {claim_path}"
    )


class _NodeDispatchClaim:
    """The held exclusive claim over one node's worktree path."""

    def __init__(
        self, path: Path, run_id: str, reclaimed: dict[str, Any] | None = None
    ) -> None:
        self.path = path
        self.run_id = run_id
        self.reclaimed = reclaimed

    def release(self) -> None:
        """Give the path up, so the node can be dispatched again."""
        self.path.unlink(missing_ok=True)


def _claim_node_dispatch(
    *,
    project: str,
    worktree_identity: str,
    node_id: str,
    run_id: str,
    session: str,
) -> _NodeDispatchClaim:
    """Take the one claim that lets a dispatch cut a node's worktree.

    The exclusive create is the whole arbitration: a second dispatch of the
    same node, project and worktree identity loses it and refuses, naming the
    in-flight dispatch, before any worktree exists for it to disturb. A claim
    whose holder process is gone — or whose pid is now a different process —
    no longer owns the path, and is moved aside so one dead dispatcher cannot
    block every later dispatch of the node. A claim that names no holder at
    all gets a short grace period for its writer to finish, and is reclaimed
    once that has passed, so a dispatcher killed between the exclusive create
    and its record write cannot wedge the node either.
    """
    path = _node_dispatch_claim_path(project, worktree_identity, node_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": run_id,
        "project": project,
        "session": session,
        "worktree_identity": worktree_identity,
        "node": node_id,
        "pid": os.getpid(),
        "process_start_time": _process_start_time(os.getpid()),
        "created_at": _utc_now(),
    }
    reclaimed: dict[str, Any] | None = None
    for _attempt in range(_NODE_CLAIM_ATTEMPTS):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            holder = _read_node_dispatch_claim(path)
            if _claim_holder_is_alive(holder) and not _empty_claim_is_stale(
                path, holder
            ):
                raise CrewError(
                    format_refusal(
                        "D12",
                        _node_dispatch_in_flight_text(
                            holder,
                            node_id=node_id,
                            project=project,
                            worktree_identity=worktree_identity,
                            claim_path=path,
                        ),
                    )
                ) from None
            # Another dispatch may have reclaimed and republished between the
            # read and the move; moving that claim aside would leave two
            # owners, so only the record this dispatch judged stale is
            # displaced.
            if _read_node_dispatch_claim(path) != holder:
                continue
            moved_to = _reclaim_stale_node_dispatch_claim(path, holder)
            if reclaimed is None:
                reclaimed = {
                    "run_id": holder.get("run_id"),
                    "pid": holder.get("pid"),
                    "created_at": holder.get("created_at"),
                }
            reclaimed["moved_to"] = moved_to
            continue
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(record, handle)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return _NodeDispatchClaim(path, run_id, reclaimed=reclaimed)
    raise CrewError(
        format_refusal(
            "D12",
            f"the claim over node {node_id!r} for project {project!r} under worktree "
            f"identity {worktree_identity!r} kept being reclaimed by other dispatches "
            f"at {path}",
        )
    )


def _publish_launch_claim(
    run_id: str,
    *,
    node: TaskNode,
    project: str,
    repo: Path,
    session: str,
    authority: Mapping[str, Any],
    member: str,
    backend: str,
    launch: str,
    agent: Mapping[str, Any],
    session_id: str | None,
    brief: Mapping[str, str] | None = None,
    registered_at: str | None = None,
) -> None:
    """Write this run's live pointer as a claim, before its launch is composed.

    Every arbitration surface — a peer dispatch's admission check, the review
    reflex deciding whether a review is already in flight, an operator reading
    the fleet — reads live pointers, and dispatch writes its pointer only after
    the worktree is cut, the prompt composed and the peer channels wired. A
    dispatch that leaves its claim unpublished for that whole span is invisible
    while it holds the paths, so a second dispatch arriving inside the span
    reads no claim, takes the same paths and launches a duplicate worker over
    the first. The claim therefore goes out at the run id's own moment: what is
    known then, and nothing invented.

    The record carries no ``pid``, exactly as the pointer written before the
    worker is spawned does not, so a reader sees a run whose process has not
    started yet rather than a run whose process has died. The full record
    overwrites this one at the same path, and a launch that refuses anywhere
    after this point unlinks it on the way out, so a refused dispatch leaves no
    claim behind. A shadow run publishes nothing and reads no claims, so that
    lineage is untouched.
    """
    record: dict[str, Any] = {
        "run_id": run_id,
        "project": project,
        "repo": str(repo),
        "authority": authority,
        "session": session,
        "node": node.as_dict(),
        "role": node.role,
        "member": member,
        "backend": backend,
        "launch": launch,
        "agent": dict(agent),
        "session_id": session_id,
        "manifest_path": node.manifest_path,
        "created_at": registered_at or _utc_now(),
        "phase": "starting",
    }
    if brief is not None:
        record["brief"] = brief
    _write_json(pointer_path(run_id), record)


def _release_launch_claim(run_id: str) -> None:
    """Give up a claim this dispatch published and is not going to use.

    The pointer goes first and the run directory follows: a dispatch refusal is
    made to leave nothing of its run behind, and a reader that found the run
    directory without the pointer would take a claim that no longer exists.
    Neither removal is an argument about whose claim it is — the path is this
    run's own — so a call for a run that published nothing removes nothing.
    """
    # Publish and release use the same lock, so a publish already in progress
    # finishes before the removal and cannot write the pointer back afterward.
    with _pointer_lock(run_id):
        pointer_path(run_id).unlink(missing_ok=True)
    shutil.rmtree(run_dir(run_id), ignore_errors=True)


@contextlib.contextmanager
def _claim_released_on_refusal(run_id: str, published: bool) -> Iterator[None]:
    """Return the claim this dispatch published if the guarded work refuses.

    The claim is published before the work that can refuse it — the ceiling,
    roster, scope and watcher checks, any of which can take seconds — so a
    refusal reached under this guard gives the claim back rather than leaving a
    live claim behind for a launch that never happened.
    """
    try:
        yield
    except Exception:
        if published:
            _release_launch_claim(run_id)
        raise


def _can_write_worktree(
    backend: Mapping[str, Any],
    *,
    repository: Path,
    run_directory: Path,
) -> bool:
    """Whether the worker can write its assigned worktree in this sandbox.

    The landing contract and its write-scope grant are keyed on this writability
    rather than on which dialect happens to relocate the process directory, so a
    role whose sandbox forbids repository writes is never granted a landing
    deliverable it cannot commit. Resolved through the same sandbox grants that
    scope the declared write paths, which keeps the grant and the reachability
    judgement reading the same authority.
    """
    roots = _backends.sandbox_write_roots(
        backend,
        repository=repository,
        run_directory=run_directory,
        reports_directory=reports_dir(),
        review_store_directory=review_store_root(),
    )
    return _backends.sandbox_can_write(
        repository, repository=repository, write_roots=roots
    )


def _shared_landing_paths(
    node: TaskNode,
    *,
    project: str,
    authority: Mapping[str, Any],
) -> set[Path]:
    """Return the plan file, evidence record and figure topic shared by every node.

    These three repository paths are landing files every node on a plan would
    otherwise hold: the plan file, the plan's cumulative evidence record, and the
    plan-wide figure topic. They are no longer granted by default, because a
    grant every node holds is exactly the merge conflict the per-node fragment
    default removes. The set is still computed so a coordinator that declares one
    of them explicitly keeps it as a non-exclusive claim and is warned, and so
    the peer-disclosure and conflict machinery keep treating it as shared. Files
    within the figure topic remain exclusive claims, so two nodes cannot replace
    the same rendered artifact.
    Resolved absolutely so the exclusive-claim machinery recognises them in
    whichever repository carries the plan. The grant is advisory: a plan that
    cannot be resolved contributes no plan-file path, and the evidence record
    path is deterministic and survives regardless.
    """
    plan = authority.get("plan")
    if not isinstance(plan, Mapping) or not node.plan:
        return set()
    try:
        docs_dir = Path(str(plan["docs"])).expanduser().resolve()
    except (KeyError, TypeError, ValueError):
        return set()
    paths: set[Path] = {
        (docs_dir / "evidence" / "archive" / f"{node.plan}-landed.html").resolve(),
        (docs_dir / "figures" / node.plan).resolve(),
    }
    from reckon.resources import resolve_resource

    try:
        resource = resolve_resource(
            docs_dir, project, node.plan, "plan", include_archived=False
        )
    except (ValueError, OSError):
        return paths
    if resource is not None:
        try:
            resolved = resource.path.resolve()
        except (ValueError, OSError):
            resolved = None
        if resolved is not None:
            paths.add(resolved)
    return paths


def _landing_fragment_paths(
    node: TaskNode,
    *,
    authority: Mapping[str, Any],
) -> set[Path]:
    """Return the fragment paths this node's landing record is written to.

    A plan node's landing scope is its own fragment rather than the plan's
    shared landing files: its evidence anchor under
    ``docs/evidence/fragments/<plan>/<node-id>.html`` and its figure topic under
    ``docs/figures/<plan>/<node-id>/``. Both are keyed by the node id, so two
    nodes on one plan hold disjoint scopes and their records merge without the
    add/add conflict a shared record produces, while a redispatch of one node
    resolves the same fragment and replaces its predecessor's. Resolved
    absolutely so the exclusive-claim machinery recognises them in whichever
    repository carries the plan.
    """
    plan = authority.get("plan")
    if not node.plan:
        return set()
    if not isinstance(plan, Mapping):
        return set()
    try:
        docs_dir = Path(str(plan["docs"])).expanduser().resolve()
    except (KeyError, TypeError, ValueError):
        return set()
    return {
        (docs_dir / "evidence" / "fragments" / node.plan / f"{node.id}.html").resolve(),
        (docs_dir / "figures" / node.plan / node.id).resolve(),
    }


def _resolve_declared_path(declared: str, base: Path) -> Path:
    """Resolve one declared write path against the repository that carries it."""
    raw = Path(str(declared)).expanduser()
    return (raw if raw.is_absolute() else base / raw).resolve()


def _grant_landing_write_paths(
    node: TaskNode,
    *,
    project: str,
    authority: Mapping[str, Any],
    warnings: list[str],
) -> None:
    """Declare this node's own landing fragment in its write scope.

    The default scope is the node's fragment, so two nodes on one plan hold
    disjoint scopes. A coordinator that declares one of the plan's shared landing
    files keeps it — the declaration is granted as written — and is warned,
    because a path every node on the plan holds is the merge conflict the
    fragment default removes.
    """
    plan = authority.get("plan")
    if not isinstance(plan, Mapping):
        return
    try:
        plan_repo = Path(str(plan["repository"])).expanduser().resolve()
    except (KeyError, TypeError, ValueError):
        return
    shared = _shared_landing_paths(node, project=project, authority=authority)
    if shared:
        warnings.extend(
            f"declared write path {declared!r} is a landing file shared by "
            "every node on this plan; dispatch grants each node its own "
            "fragment by default, and this explicit declaration "
            "reintroduces the merge conflict"
            for declared in node.write_paths
            if _resolve_declared_path(declared, plan_repo) in shared
        )
    within_plan_repo = [
        absolute.relative_to(plan_repo).as_posix()
        for absolute in sorted(_landing_fragment_paths(node, authority=authority))
        if absolute.is_relative_to(plan_repo)
    ]
    node.write_paths.extend(
        declared for declared in within_plan_repo if declared not in node.write_paths
    )


def _writes_its_landing_fragment(
    node: TaskNode,
    *,
    authority: Mapping[str, Any],
) -> bool:
    """Return whether this node's write scope carries its own landing fragment.

    The plan landing contract tells the worker to write its evidence anchor to
    the node's fragment and its figures to the node's figure directory, so it
    is stated only when that fragment is one of the node's resolved write
    paths. The default grant withholds the fragment from a role that may not
    land work in the tree, so a contract composed on worktree writability alone
    would tell such a worker to write a path its fence withholds. The fragment
    is derived by the same function the grant uses and each declared path is
    resolved against the same repository, so the contract and the grant cannot
    disagree about which scope is which.
    """
    plan = authority.get("plan")
    if not isinstance(plan, Mapping):
        return False
    try:
        plan_repo = Path(str(plan["repository"])).expanduser().resolve()
    except (KeyError, TypeError, ValueError):
        return False
    fragments = _landing_fragment_paths(node, authority=authority)
    if not fragments:
        return False
    return any(
        _resolve_declared_path(declared, plan_repo) in fragments
        for declared in node.write_paths
    )


def _compose_dispatch_prompt(
    *,
    node: TaskNode,
    project: str,
    authority: Mapping[str, Any],
    backend: Mapping[str, Any],
    repo_root: Path,
    run_directory: Path,
    worktree: str,
    working_directory: str,
    launch_instant: str = "",
    needs_help_after_failures: int,
    peer_scopes: Mapping[str, Iterable[str]] | None = None,
    run_id: str = "",
    peer_channels: Mapping[str, Mapping[str, str]] | None = None,
    peer_channel_path: str = "",
    host_line: str = "",
    brief: str = "",
) -> str:
    """Compose a worker prompt from a resolved node and its write scope.

    Both landing facts are resolved here — the worker's writability of its
    assigned worktree, and whether the node's own scope carries the landing
    fragment — so the contract, the fragment grant and the sandbox fence read
    one decision rather than three that can drift apart. Keeping the pair behind
    one call site lets a test compose exactly the prompt dispatch composes, so a
    change to either fact is visible rather than masked by a test that supplies
    its own copy.
    """
    return compose_prompt(
        node=node,
        project=project,
        worktree=worktree,
        working_directory=working_directory,
        can_write_worktree=_can_write_worktree(
            backend,
            repository=repo_root,
            run_directory=run_directory,
        ),
        writes_landing_fragment=_writes_its_landing_fragment(node, authority=authority),
        manifest_path=node.manifest_path,
        time_budget=node.time_budget,
        launch_instant=launch_instant,
        needs_help_after_failures=needs_help_after_failures,
        peer_scopes=peer_scopes,
        run_id=run_id,
        peer_channels=peer_channels,
        peer_channel_path=peer_channel_path,
        host_line=host_line,
        brief=brief,
    )


def _resolved_node_scope_entries(
    node: TaskNode,
    *,
    project: str,
    repo: Path,
    authority: Mapping[str, Any],
) -> list[tuple[Path | None, str, Path, str, str | None]]:
    """Expand the node's whole scope, dispatcher grants included, unfiltered."""
    repository_projects = mounted_repository_projects()
    repositories = tuple(
        repository_identity(root) or Path(str(root)).expanduser().resolve()
        for root in authority.get("repositories") or (repo,)
    )
    write = authority.get("write")
    write = write if isinstance(write, Mapping) else {}
    return _resolved_scope_entries(
        node.write_paths,
        base_repository=repository_identity(repo) or Path(repo).resolve(),
        repositories=repositories,
        project=project,
        repository_projects=repository_projects,
        preferred_projects=tuple(str(item) for item in write.get("projects") or ()),
    )


def _granted_landing_paths(
    node: TaskNode,
    *,
    project: str,
    repo: Path,
    authority: Mapping[str, Any],
) -> set[Path]:
    """The plan's landing files this node's own resolved scope holds.

    A plan file, its cumulative evidence record or its figures topic declared in
    a node's write paths is granted as written, so the node does hold a claim on
    it. The refusal exempts these paths because every node on the plan may hold
    them so their appends can merge, but a live holder of one is still a run the
    new dispatch shares a file with, and the report names it.
    """
    shared = _shared_landing_paths(node, project=project, authority=authority)
    if not shared:
        return set()
    return {
        absolute.resolve()
        for _repository, _path, absolute, _declared, _derived_from in (
            _resolved_node_scope_entries(
                node, project=project, repo=repo, authority=authority
            )
        )
        if absolute.resolve() in shared
    }


def _candidate_scope_entries(
    node: TaskNode,
    *,
    project: str,
    repo: Path,
    authority: Mapping[str, Any],
) -> list[tuple[Path | None, str, Path, str, str | None]]:
    entries = _resolved_node_scope_entries(
        node, project=project, repo=repo, authority=authority
    )
    shared = _shared_landing_paths(node, project=project, authority=authority)
    if not shared:
        return entries
    # The plan file, cumulative evidence record and plan-owned figure topic are
    # write claims every node on the plan holds, so they cannot be exclusive to
    # one of them: exclusivity would admit only the first of two concurrent nodes
    # and the merge that reconciles their appends would never be reached. Files
    # inside the figure topic stay exclusive. The shared paths are exempted from
    # the exclusive-claim machinery, never from the declared write scope.
    return [entry for entry in entries if entry[2].resolve() not in shared]


def _peer_scopes_without_shared_landing_paths(
    peer_scopes: Mapping[str, Iterable[str]],
    *,
    node: TaskNode,
    project: str,
    repo: Path,
    authority: Mapping[str, Any],
) -> dict[str, list[str]]:
    """Keep peer disclosure limited to paths that are exclusive claims."""
    shared = _shared_landing_paths(node, project=project, authority=authority)
    if not shared:
        return {
            name: sorted(str(path) for path in paths)
            for name, paths in peer_scopes.items()
        }
    repository_projects = mounted_repository_projects()
    repositories = tuple(
        repository_identity(root) or Path(str(root)).expanduser().resolve()
        for root in authority.get("repositories") or (repo,)
    )
    write = authority.get("write")
    write = write if isinstance(write, Mapping) else {}
    filtered: dict[str, list[str]] = {}
    for name, paths in peer_scopes.items():
        kept = []
        for path in paths:
            declared = str(path)
            entries = _resolved_scope_entries(
                [declared],
                base_repository=repository_identity(repo) or Path(repo).resolve(),
                repositories=repositories,
                project=project,
                repository_projects=repository_projects,
                preferred_projects=tuple(
                    str(item) for item in write.get("projects") or ()
                ),
            )
            if any(absolute.resolve() in shared for _, _, absolute, _, _ in entries):
                continue
            kept.append(declared)
        if kept:
            filtered[name] = sorted(kept)
    return filtered


def _live_conflict_rows(
    node: TaskNode,
    *,
    project: str,
    repo: Path,
    authority: Mapping[str, Any],
    claims: Iterable[_RepositoryScopeClaim],
    disregarded: list[str] | None = None,
    include_granted_landing: bool = True,
) -> list[dict[str, Any]]:
    candidates = (
        _resolved_node_scope_entries(
            node, project=project, repo=repo, authority=authority
        )
        if include_granted_landing
        else _candidate_scope_entries(
            node, project=project, repo=repo, authority=authority
        )
    )
    shared = _shared_landing_paths(node, project=project, authority=authority)
    landing = (
        _granted_landing_paths(node, project=project, repo=repo, authority=authority)
        if include_granted_landing
        else set()
    )
    shared_files = _shared_write_paths(project, repo)
    conflicts: list[dict[str, Any]] = []
    for claim in claims:
        claim_absolute = claim.absolute_path.resolve()
        if claim_absolute in shared and claim_absolute not in landing:
            # A live claim on a landing file this node does not itself hold is
            # not a conflict: the node writing its own fragment is the whole
            # point of the fragment default. One the node does hold is a
            # collision like any other, and is reported below.
            continue
        overlapping = [
            (path, absolute)
            for repository, path, absolute, _declared, _derived_from in candidates
            if repository == claim.repository
            and _scopes_overlap(absolute.as_posix(), claim.absolute_path.as_posix())
            and not (path in shared_files and path == claim.path)
        ]
        if not overlapping:
            continue
        if not claim.binding:
            if disregarded is not None and claim.disposition_reason not in disregarded:
                disregarded.append(claim.disposition_reason)
            continue
        paths = [
            {"left_path": path, "right_path": claim.path} for path, _ in overlapping
        ]
        conflict: dict[str, Any] = {
            "candidate": node.id,
            "run_id": claim.run_id,
            "node": claim.node_id,
            "claimed_path": claim.path,
            "paths": paths,
        }
        if claim.project != project:
            conflict["project"] = claim.project
        conflicts.append(conflict)
    return conflicts


def _absent_path_names_a_directory(path: Path) -> bool:
    """Whether an absent path's own name reads as a directory, not a leaf file.

    A file suffix does not settle it. A topic directory may carry a numeric,
    version-style suffix (``docs/evidence/2026.09``), so reading any suffix as a
    file extension would let a broad claim sweep a peer's tree unwarned. A
    final component with no suffix, or a suffix holding no letter, names a
    directory; an alphabetic extension (``notes.html``) names a leaf file.
    """
    suffix = path.suffix
    return not any(character.isalpha() for character in suffix)


def _directory_claim_overlaps(candidate: Path, claim: Path) -> bool:
    """Whether a candidate write path claims a directory, not an exact file.

    A directory claim can sweep up paths a peer already holds, so it is judged
    apart from an exact-file claim. Three arms: the path exists as a directory
    on disk; or it strictly contains the live claim by path component; or it is
    a directory that does not exist yet and sits strictly inside the live claim,
    since its subtree lies within the peer's claim. A candidate that names a
    plain file — an exact leaf inside a peer's directory claim — is none of
    these, so it keeps the plain refusal.

    A path that is absent is read as a directory or a file from its own name
    (``_absent_path_names_a_directory``): a topic directory such as
    ``docs/evidence/new-topic`` or ``docs/evidence/2026.09`` is declared by its
    tree, while ``tests/test_x.py`` names a file and its collision is a plain
    file conflict.
    """
    if candidate.is_dir():
        return True
    candidate_parts = candidate.parts
    claim_parts = claim.parts
    if (
        len(candidate_parts) < len(claim_parts)
        and claim_parts[: len(candidate_parts)] == candidate_parts
    ):
        return True
    return (
        not candidate.exists()
        and _absent_path_names_a_directory(candidate)
        and len(candidate_parts) > len(claim_parts)
        and candidate_parts[: len(claim_parts)] == claim_parts
    )


def _live_conflict_is_a_directory_claim(
    row: Mapping[str, Any], repo_root: Path
) -> bool:
    """Whether a reported live-conflict row is a directory claim.

    The row's own paths are the resolution's repository-relative spelling, so
    the directory judgement is made here rather than stored on the row: the
    stored row keeps exactly the shape every existing reader expects, and a
    directory claim is derived from its own paths when the caller needs it.
    """
    claimed = _live_conflict_path(row["claimed_path"], repo_root)
    for entry in row.get("paths") or ():
        candidate = _live_conflict_path(entry["left_path"], repo_root)
        if _directory_claim_overlaps(candidate, claimed):
            return True
    return False


def _live_conflict_path(value: str, repo_root: Path) -> Path:
    """Resolve one row path to an absolute path under the repository."""
    path = Path(str(value)).expanduser()
    return (path if path.is_absolute() else repo_root / path).resolve()


def _directory_claim_alternatives(
    node: TaskNode, *, repo: Path, candidate: str, claim_path: str
) -> list[str]:
    """The exact files a directory claim should name instead of the whole tree.

    The files the brief declares inside the claimed directory, when it declares
    any; otherwise the overlapping claim's own path, so the warning still names
    the one path that collides rather than leaving the caller to guess it.
    """
    candidate_parts = Path(candidate.rstrip("/")).parts
    inside: list[str] = []
    for raw in node.write_paths:
        declared = _repository_relative_scope(str(raw), repo)
        if declared is None:
            continue
        declared = declared.rstrip("/")
        parts = Path(declared).parts
        if len(parts) > len(candidate_parts) and parts[: len(candidate_parts)] == (
            candidate_parts
        ):
            inside.append(declared)
    return sorted(set(inside)) or [claim_path]


def _directory_claim_row(
    claim: _RepositoryScopeClaim, candidate: str
) -> dict[str, Any]:
    """Describe one accepted directory claim for the run record."""
    return {
        "candidate_path": candidate,
        "claimed_path": claim.path,
        "run_id": claim.run_id,
        "node": claim.node_id,
        "project": claim.project,
    }


def _directory_claim_acceptance_kwargs(
    accept_directory_claim: bool, accepted: list[dict[str, Any]]
) -> dict[str, Any]:
    """The extra arguments the claim walk needs only when the flag was given.

    A dispatch with no ``--accept-directory-claim`` passes no extra keyword, so
    the walk keeps its original shape for the callers and spies that wrap it.
    """
    if not accept_directory_claim:
        return {}
    return {"accept_directory_claim": True, "accepted": accepted}


def _directory_claim_warning_line(
    *,
    candidate: str,
    claimed_path: str,
    run_id: str,
    node_id: str,
    alternatives: Iterable[str],
) -> str:
    """One warning naming a directory-claim collision and the exact alternative."""
    listing = ", ".join(repr(path) for path in alternatives) or "none"
    return (
        f"write path {candidate!r} claims a directory overlapping the live claim "
        f"{claimed_path!r} held by run {run_id!r} (node {node_id!r}); declare the "
        f"files the brief names as the exact alternative ({listing}) or pass "
        "--accept-directory-claim to claim the whole directory"
    )


def _directory_claim_acceptance_line(row: Mapping[str, Any]) -> str:
    """One warning line recording an accepted directory claim."""
    return (
        f"directory claim {row['candidate_path']!r} accepted with "
        f"--accept-directory-claim over run {row['run_id']!r} "
        f"(node {row['node']!r}) claiming {row['claimed_path']!r}"
    )


def _fractional_digits(stamp: str) -> int:
    """Count the sub-second digits a timestamp spells, 0 when it names a whole second.

    Used only to tell whether two registration stamps carry the same resolution:
    a value truncated to the second and one carrying microseconds cannot be
    compared across a sub-second gap, because the truncation hides which moment
    is really the earlier.
    """
    dot = stamp.find(".")
    if dot < 0:
        return 0
    end = dot + 1
    while end < len(stamp) and stamp[end].isdigit():
        end += 1
    return end - dot - 1


def _peer_claim_is_a_later_racing_arrival(
    claim: _RepositoryScopeClaim,
    *,
    own_run_id: str | None,
    own_registered_at: str | None,
) -> bool:
    """Whether this dispatch outranks a peer claim that is still being composed.

    Two dispatches of overlapping paths can both publish a claim before either
    reaches its admission check, so each reads the other as a live claim and, in
    refusing on sight, both withdraw and the paths are left with no worker. The
    claim registered first owns the paths: a peer claim that has not launched a
    worker and was registered after this dispatch's own is disregarded here, so
    only that peer refuses when it checks, naming the winner. The two that can
    interleave are ordered the same way at both of them — by registration time,
    then by run id when the times are equal — so exactly one proceeds.

    A peer that has launched its worker keeps today's refusal: it has already
    passed its own admission and is no longer a racing arrival. A peer whose
    registration cannot be shown to follow this one — an absent or unreadable
    timestamp, or this dispatch holding no claim of its own — is treated as
    established and refused rather than quietly outranked.

    The two moments are compared as parsed instants, not as the text they were
    written in: the same instant is written ``...T04:00:00Z`` by one caller and
    ``...T04:00:00.500000+00:00`` by another, and those spellings sort the
    wrong way round as strings.

    A comparison that the data cannot support is inconclusive rather than
    decided by run id: when the two stamps carry different resolutions and name
    moments inside the coarser one's tick, which is the earlier is unknowable,
    so the peer claim stays established and this dispatch refuses.
    """
    if claim.launched:
        return False
    if not own_run_id or not own_registered_at or not claim.registered_at:
        return False
    peer_registered_at = parse_utc(claim.registered_at)
    own_registered = parse_utc(own_registered_at)
    if peer_registered_at is None or own_registered is None:
        return False
    if _fractional_digits(claim.registered_at) != _fractional_digits(
        own_registered_at
    ) and abs(peer_registered_at - own_registered) < timedelta(seconds=1):
        # The two stamps carry different resolutions and name moments inside the
        # coarser one's own tick, so which is the earlier is not decidable: a
        # second-precision stamp truncates a moment the other spells in full.
        # The newcomer must not be let through on a comparison the data cannot
        # support, so the peer stays established and this dispatch refuses.
        return False
    return (peer_registered_at, claim.run_id) > (own_registered, own_run_id)


# How long a dispatch that lost a registration race waits for the winning claim
# to launch or withdraw before refusing. The winner published moments earlier
# and reaches its own admission within a handful of seconds, so the bound is
# short on purpose: a wait holds the losing dispatch's whole turn, and a winner
# still composing after this long is treated as the established owner.
RACING_WINNER_WAIT_SECONDS = 15.0
RACING_WINNER_POLL_SECONDS = 0.25


def _peer_claim_is_an_unlaunched_racing_winner(
    claim: _RepositoryScopeClaim,
    *,
    own_run_id: str | None,
    own_registered_at: str | None,
) -> bool:
    """Whether this dispatch would refuse only because of an unlaunched peer.

    The mirror of ``_peer_claim_is_a_later_racing_arrival``: a peer claim whose
    registration precedes this dispatch's own and which has not launched its
    worker outranks it, so this dispatch refuses on sight. That refusal is the
    one a withdrawal by the winner would strand — the loser has already gone and
    the paths are left with no worker — so it is the only case the bounded wait
    covers.

    A launched peer, and one whose registration cannot be shown to precede this
    dispatch's own, are established and refuse exactly as before: the wait never
    touches them.
    """
    if claim.launched:
        return False
    if not own_run_id or not own_registered_at or not claim.registered_at:
        return False
    peer_registered = parse_utc(claim.registered_at)
    own_registered = parse_utc(own_registered_at)
    if peer_registered is None or own_registered is None:
        return False
    if _fractional_digits(claim.registered_at) != _fractional_digits(
        own_registered_at
    ) and abs(peer_registered - own_registered) < timedelta(seconds=1):
        return False
    return (peer_registered, claim.run_id) < (own_registered, own_run_id)


def _racing_claim_current(run_id: str) -> _RepositoryScopeClaim | None:
    """Re-read one live run's claim, so a wait can see a winner launch or go.

    The wait re-reads the peer rather than trusting the snapshot the check was
    handed: a winner that withdrew unlinks its pointer, and one that reached its
    admission rewrites it with the worktree and pid that mark it launched.
    """
    for claim in _repository_scope_claims():
        if claim.run_id == run_id:
            return claim
    return None


def _racing_clock() -> float:
    """The monotonic clock the racing wait measures its bound against."""
    return time.monotonic()


def _racing_pause(seconds: float) -> None:
    """Sleep between re-reads of a racing winner's claim."""
    time.sleep(seconds)


def _settle_racing_winner(
    claim: _RepositoryScopeClaim,
    *,
    own_run_id: str | None,
    own_registered_at: str | None,
    reread: Callable[[str], _RepositoryScopeClaim | None],
    clock: Callable[[], float],
    pause: Callable[[float], None],
) -> str:
    """Wait, bounded, for a racing winner to launch or withdraw.

    Returns ``"proceed"`` when the winner's claim has withdrawn or disappeared —
    the paths are this dispatch's after all — ``"launched"`` when the winner has
    passed its own admission, so the refusal stands, and ``"expired"`` when the
    bound passed with the winner still unlaunched. Only a claim already known to
    be an unlaunched racing winner is ever reached here.
    """
    deadline = clock() + RACING_WINNER_WAIT_SECONDS
    while True:
        current = reread(claim.run_id)
        if current is None or not current.binding:
            return "proceed"
        if current.launched:
            return "launched"
        if not _peer_claim_is_an_unlaunched_racing_winner(
            current, own_run_id=own_run_id, own_registered_at=own_registered_at
        ):
            return "proceed"
        if clock() >= deadline:
            return "expired"
        pause(RACING_WINNER_POLL_SECONDS)


def _raise_repository_scope_conflict(
    node: TaskNode,
    *,
    project: str,
    repo: Path,
    authority: Mapping[str, Any],
    claims: Iterable[_RepositoryScopeClaim],
    disregarded: list[str] | None = None,
    accept_directory_claim: bool = False,
    accepted: list[dict[str, Any]] | None = None,
    own_run_id: str | None = None,
    own_registered_at: str | None = None,
) -> None:
    candidates = _candidate_scope_entries(
        node, project=project, repo=repo, authority=authority
    )
    shared = _shared_landing_paths(node, project=project, authority=authority)
    shared_files = _shared_write_paths(project, repo)
    landing_fragments = _landing_fragment_paths(node, authority=authority)
    claims = tuple(claims)
    accepted_directories = {
        (claim.run_id, claim.absolute_path)
        for repository, _candidate, absolute, _declared, _derived_from in candidates
        for claim in claims
        if accept_directory_claim
        and repository == claim.repository
        and _scopes_overlap(absolute.as_posix(), claim.absolute_path.as_posix())
        and _directory_claim_overlaps(absolute, claim.absolute_path)
    }
    for _repository, candidate, absolute, _declared, _derived_from in candidates:
        for claim in claims:
            if claim.absolute_path.resolve() in shared:
                continue
            if _repository != claim.repository or not _scopes_overlap(
                absolute.as_posix(), claim.absolute_path.as_posix()
            ):
                continue
            # A file this project declares shareable admits a second claimant
            # editing a different region: worktrees isolate the in-flight work
            # and merging is the orchestrator's job, so a whole-file refusal
            # serialises nodes that do not actually collide. Only the exact
            # named file is shareable; a directory claim is a different path.
            if candidate in shared_files and candidate == claim.path:
                continue
            if not claim.binding:
                # Named on the record rather than passed over quietly: an
                # admission a reader cannot see is one nobody can check.
                if (
                    disregarded is not None
                    and claim.disposition_reason not in disregarded
                ):
                    disregarded.append(claim.disposition_reason)
                continue
            if _peer_claim_is_a_later_racing_arrival(
                claim,
                own_run_id=own_run_id,
                own_registered_at=own_registered_at,
            ):
                # A claim this dispatch registered before, still being composed:
                # it will meet this claim and refuse when it checks, so it does
                # not refuse this one here. See the helper for the ordering.
                continue
            racing_winner_refusal = ""
            if _peer_claim_is_an_unlaunched_racing_winner(
                claim,
                own_run_id=own_run_id,
                own_registered_at=own_registered_at,
            ):
                # The peer registered first and has not launched yet: refusing
                # on sight would strand the paths if that winner withdraws for
                # an unrelated reason. Wait, bounded, for it to launch (then the
                # refusal stands) or to go (then the paths are this dispatch's).
                settle = _settle_racing_winner(
                    claim,
                    own_run_id=own_run_id,
                    own_registered_at=own_registered_at,
                    reread=_racing_claim_current,
                    clock=_racing_clock,
                    pause=_racing_pause,
                )
                if settle == "proceed":
                    continue
                if settle == "expired":
                    racing_winner_refusal = (
                        f"the earlier dispatch {claim.run_id!r} has not launched "
                        f"within {RACING_WINNER_WAIT_SECONDS:g}s and its claim on "
                        "the paths still stands"
                    )
            if (
                accept_directory_claim
                and absolute.resolve() in landing_fragments
                and (claim.run_id, claim.absolute_path) in accepted_directories
            ):
                if accepted is not None:
                    accepted.append(_directory_claim_row(claim, candidate))
                continue
            if _directory_claim_overlaps(absolute, claim.absolute_path):
                # A directory claim is coarser than the exact file a reader sees
                # held by a peer, so it is refused with the exact alternative
                # named rather than silently, and only an explicit
                # --accept-directory-claim keeps the whole tree. An accepted
                # claim is written down on the record so the exception survives
                # the command line that gave it.
                if accept_directory_claim:
                    if accepted is not None:
                        accepted.append(_directory_claim_row(claim, candidate))
                    continue
                refusal = DirectoryClaimConflict(
                    run_id=claim.run_id,
                    node_id=claim.node_id,
                    candidate_path=candidate,
                    claimed_path=claim.path,
                    alternatives=_directory_claim_alternatives(
                        node, repo=repo, candidate=candidate, claim_path=claim.path
                    ),
                )
                refusal.project = claim.project
                message = str(refusal)
                if racing_winner_refusal:
                    message = f"{message}; {racing_winner_refusal}"
                if claim.project != project:
                    refusal.args = (
                        f"{message} in project {claim.project!r}",
                    )
                else:
                    refusal.args = (message,)
                raise refusal
            refusal = ScopeConflict(
                run_id=claim.run_id,
                node_id=claim.node_id,
                candidate_path=candidate,
                claimed_path=claim.path,
            )
            refusal.project = claim.project
            message = str(refusal)
            if claim.project != project:
                message = f"{message} in project {claim.project!r}"
            if racing_winner_refusal:
                message = f"{message}; {racing_winner_refusal}"
            if claim.disposition_reason:
                message = f"{message}; {claim.disposition_reason}"
            refusal.args = (message,)
            raise refusal


def refuse_widen_scope_conflicts(
    pointer: Mapping[str, Any], added_paths: Iterable[str]
) -> None:
    """Judge added fence paths with the same claim rule as dispatch."""
    node = pointer.get("node")
    if not isinstance(node, Mapping):
        raise CrewError("the run records no node holding a write scope")
    repo_value = str(pointer.get("repo") or "")
    if not repo_value:
        raise CrewError("the run records no repository for its write scope")
    repo = claim_repository(pointer) or Path(repo_value).expanduser().resolve()
    project = str(pointer.get("project") or "")
    authority = pointer.get("authority")
    authority = authority if isinstance(authority, Mapping) else {}
    candidate = TaskNode(
        id=str(node.get("id") or ""),
        goal=str(node.get("goal") or ""),
        plan=str(node.get("plan") or ""),
        section=str(node.get("section") or ""),
        write_paths=list(added_paths),
    )
    claims = _repository_scope_claims(
        exclude_run_ids=(str(pointer.get("run_id") or ""),)
    )
    _raise_repository_scope_conflict(
        candidate,
        project=project,
        repo=repo,
        authority=authority,
        claims=claims,
        own_run_id=str(pointer.get("run_id") or ""),
        own_registered_at=_utc_now(),
    )


def _scoped_python_files(paths: Iterable[str], repo: Path) -> tuple[Path, ...]:
    """Return existing Python files covered by repository-relative scopes."""
    files: set[Path] = set()
    for raw in paths:
        candidate = Path(str(raw)).expanduser()
        candidate = (
            candidate if candidate.is_absolute() else repo / candidate
        ).resolve()
        try:
            candidate.relative_to(repo)
        except ValueError:
            continue
        if candidate.is_file() and candidate.suffix == ".py":
            files.add(candidate)
        elif candidate.is_dir():
            files.update(path for path in candidate.rglob("*.py") if path.is_file())
    return tuple(sorted(files))


def _module_aliases(path: Path, repo: Path) -> set[str]:
    """Return import spellings that can identify one Python source file."""
    relative = path.relative_to(repo).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    aliases = {".".join(parts)} if parts else set()
    if parts and parts[0] in {"src", "lib"}:
        aliases.add(".".join(parts[1:]))
    return {alias for alias in aliases if alias}


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _python_references(path: Path, repo: Path) -> set[str]:
    """Read import and qualified-call references from one Python source file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError):
        return set()
    aliases = _module_aliases(path, repo)
    module_parts = min(aliases, key=len).split(".") if aliases else []
    package_parts = module_parts[:-1]
    references: set[str] = set()
    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            references.update(alias.name for alias in item.names)
        elif isinstance(item, ast.ImportFrom):
            if item.level:
                keep = max(0, len(package_parts) - item.level + 1)
                base_parts = package_parts[:keep]
                if item.module:
                    base_parts.extend(item.module.split("."))
                base = ".".join(base_parts)
            else:
                base = item.module or ""
            if base:
                references.add(base)
                references.update(
                    f"{base}.{alias.name}" for alias in item.names if alias.name != "*"
                )
        elif isinstance(item, ast.Call):
            dotted = _dotted_name(item.func)
            if dotted:
                references.add(dotted)
            if (
                dotted in {"__import__", "importlib.import_module"}
                and item.args
                and isinstance(item.args[0], ast.Constant)
                and isinstance(item.args[0].value, str)
            ):
                references.add(item.args[0].value)
    return references


def _references_any_module(references: set[str], aliases: set[str]) -> bool:
    return any(
        reference == alias or reference.startswith(f"{alias}.")
        for reference in references
        for alias in aliases
    )


def _nodes_are_adjacent(
    left_paths: Iterable[str], right_paths: Iterable[str], repo: Path
) -> bool:
    """Return whether Python imports or calls connect two disjoint scopes."""
    left_files = _scoped_python_files(left_paths, repo)
    right_files = _scoped_python_files(right_paths, repo)
    left_aliases = {
        alias for path in left_files for alias in _module_aliases(path, repo)
    }
    right_aliases = {
        alias for path in right_files for alias in _module_aliases(path, repo)
    }
    return any(
        _references_any_module(_python_references(path, repo), right_aliases)
        for path in left_files
    ) or any(
        _references_any_module(_python_references(path, repo), left_aliases)
        for path in right_files
    )


def _adjacent_live_peers(
    node: TaskNode,
    *,
    project: str,
    repo: Path,
    explicitly_named: set[str],
    exclude_run_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Find live node pairs that receive a durable peer channel.

    ``exclude_run_ids`` drops runs by identity, and a dispatch that has already
    published its own claim must pass its run id: its claim declares the same
    write paths this node does, so adjacency on those paths would make the run
    its own peer and wire a channel to the worker it has not spawned yet.
    """
    excluded = set(exclude_run_ids)
    adjacent: list[dict[str, Any]] = []
    for pointer in list_live(project=project):
        if str(pointer.get("run_id") or "") in excluded:
            continue
        if Path(str(pointer.get("repo") or "")).resolve() != repo:
            continue
        if str(pointer.get("phase") or "") in _TERMINAL_RUN_PHASES:
            continue
        peer_node = pointer.get("node")
        if not isinstance(peer_node, Mapping):
            continue
        peer_id = str(peer_node.get("id") or "")
        peer_named_new = node.id in {
            str(name) for name in (peer_node.get("peer_scopes") or {})
        }
        if not (
            peer_id in explicitly_named
            or peer_named_new
            or _nodes_are_adjacent(
                node.write_paths, peer_node.get("write_paths") or (), repo
            )
        ):
            continue
        adjacent.append(
            {
                "run_id": str(pointer.get("run_id") or ""),
                "node": peer_id,
                "paths": sorted(
                    str(path) for path in peer_node.get("write_paths") or ()
                ),
            }
        )
    return sorted(adjacent, key=lambda peer: (peer["node"], peer["run_id"]))


def _channel_root(run_id: str) -> Path:
    if not _SAFE_ID.fullmatch(str(run_id)):
        raise CrewError(f"run id {run_id!r} must match {_SAFE_ID.pattern}")
    return run_dir(run_id) / "peer-channel"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _update_peer_index(
    run_id: str, peer_run_id: str, details: Mapping[str, Any] | None
) -> None:
    root = _channel_root(run_id)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "peers.lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        index_path = root / "peers.json"
        index = _read_json(index_path) or {"run_id": run_id, "peers": {}}
        peers = index.setdefault("peers", {})
        if details is None:
            peers.pop(peer_run_id, None)
        else:
            peers[peer_run_id] = dict(details)
        index["updated_at"] = _utc_now()
        _write_json(index_path, index)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _wire_peer_channels(
    record: Mapping[str, Any], peers: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """Publish symmetric durable endpoints for adjacent live runs."""
    run_id = str(record["run_id"])
    node = record.get("node") or {}
    wired: dict[str, Any] = {}
    current_peer_run_id = ""
    try:
        for peer in peers:
            peer_run_id = str(peer["run_id"])
            current_peer_run_id = peer_run_id
            current_details = {
                "run_id": run_id,
                "node": str(node.get("id") or ""),
                "paths": sorted(str(path) for path in node.get("write_paths") or ()),
                "endpoint": str(_channel_root(run_id)),
            }
            peer_details = {
                "run_id": peer_run_id,
                "node": str(peer.get("node") or ""),
                "paths": sorted(str(path) for path in peer.get("paths") or ()),
                "endpoint": str(_channel_root(peer_run_id)),
            }
            _update_peer_index(run_id, peer_run_id, peer_details)
            _update_peer_index(peer_run_id, run_id, current_details)
            wired[peer_run_id] = peer_details
    except Exception:
        for peer_run_id in {*wired, current_peer_run_id} - {""}:
            _update_peer_index(run_id, peer_run_id, None)
            _update_peer_index(peer_run_id, run_id, None)
        raise
    return {
        "endpoint": str(_channel_root(run_id)),
        "peers": wired,
        "scope_transfer": False,
    }


def _unwire_peer_channels(run_id: str, peer_run_ids: Iterable[str]) -> None:
    for peer_run_id in peer_run_ids:
        _update_peer_index(peer_run_id, run_id, None)


def peer_list(run_id: str) -> dict[str, Any]:
    """Read one run's durable adjacent-peer registry."""
    index = _read_json(_channel_root(run_id) / "peers.json")
    return index or {"run_id": run_id, "peers": {}}


def _resolve_peer(run_id: str, peer: str) -> tuple[str, dict[str, Any]]:
    peers = peer_list(run_id).get("peers") or {}
    if peer in peers:
        return peer, dict(peers[peer])
    matches = [
        (peer_run_id, dict(details))
        for peer_run_id, details in peers.items()
        if str(details.get("node") or "") == peer
    ]
    if len(matches) != 1:
        raise CrewError(
            f"run {run_id!r} has no unique wired peer {peer!r}; "
            "read its peer list before asking"
        )
    return matches[0]


def _question_path(run_id: str, question_id: str) -> Path:
    if not _SAFE_ID.fullmatch(str(question_id)):
        raise CrewError(f"question id {question_id!r} must match {_SAFE_ID.pattern}")
    return _channel_root(run_id) / "questions" / f"{question_id}.json"


def peer_ask(run_id: str, peer: str, question: str) -> dict[str, Any]:
    """Persist one question in both adjacent run directories."""
    text = str(question).strip()
    if not text:
        raise CrewError("a peer question must not be empty")
    peer_run_id, peer_details = _resolve_peer(run_id, peer)
    own = read_pointer(run_id)
    question_id = f"q-{uuid.uuid4().hex}"
    event = {
        "id": question_id,
        "kind": "question",
        "question": text,
        "from_run": run_id,
        "from_node": str((own.get("node") or {}).get("id") or ""),
        "to_run": peer_run_id,
        "to_node": str(peer_details.get("node") or ""),
        "asked_at": _utc_now(),
        "reply": None,
    }
    paths = [
        _question_path(run_id, question_id),
        _question_path(peer_run_id, question_id),
    ]
    for path in paths:
        _write_json(path, event)
    return {**event, "evidence_paths": [str(path) for path in paths]}


def peer_reply(run_id: str, question_id: str, answer: str) -> dict[str, Any]:
    """Persist a reply beside both durable copies of its question."""
    text = str(answer).strip()
    if not text:
        raise CrewError("a peer reply must not be empty")
    local_path = _question_path(run_id, question_id)
    event = _read_json(local_path)
    if not event or event.get("to_run") != run_id:
        raise CrewError(f"question {question_id!r} is not addressed to run {run_id!r}")
    event["reply"] = {
        "answer": text,
        "from_run": run_id,
        "replied_at": _utc_now(),
    }
    paths = [
        _question_path(str(event["from_run"]), question_id),
        _question_path(str(event["to_run"]), question_id),
    ]
    for path in paths:
        _write_json(path, event)
    return {**event, "evidence_paths": [str(path) for path in paths]}


def _wait_seconds(bound: str | int | float) -> float:
    if isinstance(bound, bool):
        raise CrewError("peer wait bound must be a positive duration")
    if isinstance(bound, (int, float)):
        seconds = float(bound)
    else:
        seconds = float(parse_duration(str(bound)))
    if seconds <= 0:
        raise CrewError("peer wait bound must be a positive duration")
    return seconds


def _inotify_descriptor(directory: Path) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    library = ctypes.CDLL(None, use_errno=True)
    descriptor = library.inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
    if descriptor < 0:
        error = ctypes.get_errno()
        raise CrewError(f"cannot open blocking peer wait: {os.strerror(error)}")
    watch = library.inotify_add_watch(
        descriptor, os.fsencode(directory), _INOTIFY_EVENTS
    )
    if watch < 0:
        error = ctypes.get_errno()
        os.close(descriptor)
        raise CrewError(f"cannot watch peer channel: {os.strerror(error)}")
    return descriptor


def _needs_help_for_question(
    run_id: str, event: Mapping[str, Any], waited_seconds: float
) -> dict[str, Any]:
    pointer = read_pointer(run_id)
    question = str(event.get("question") or "")
    peer = str(event.get("to_node") or event.get("to_run") or "peer")
    report = f"""{NEEDS_HELP_MARKER} peer {peer} did not answer: {question}
tried: sent the durable peer question and blocked for {waited_seconds:g} seconds
options: the peer replies through the wired channel; the coordinator supplies the interface answer
leaning: obtain the peer's answer because that preserves adjacent-scope ownership
cost-if-wrong: caller work based on a guessed interface must be revised
node: {str((pointer.get("node") or {}).get("id") or "")}
status: blocked
commits: none
changed_paths: none
tests: not-run — blocked on the peer answer
test_logs: none
artifacts: {_question_path(run_id, str(event.get("id") or "unknown"))}
evidence_inputs: unanswered peer question {question}
follow_ons: none
blockers: unanswered peer question {question}
"""
    report_path = _channel_root(run_id) / f"needs-help-{event.get('id')}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    manifest_value = str(pointer.get("manifest_path") or "")
    manifest = Path(manifest_value) if manifest_value else None
    if manifest is not None:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(report, encoding="utf-8")
    return {
        "status": "needs-help",
        "question": dict(event),
        "report": report,
        "report_path": str(report_path),
        "manifest_path": str(manifest) if manifest is not None else "",
    }


def peer_read(
    run_id: str, question_id: str, *, wait: str | int | float
) -> dict[str, Any]:
    """Block on filesystem events until a reply arrives or help is emitted."""
    path = _question_path(run_id, question_id)
    event = _read_json(path)
    if not event or event.get("from_run") != run_id:
        raise CrewError(f"question {question_id!r} was not asked by run {run_id!r}")
    seconds = _wait_seconds(wait)
    deadline = time.monotonic() + seconds
    descriptor = _inotify_descriptor(path.parent)
    try:
        while True:
            event = _read_json(path)
            if isinstance(event.get("reply"), Mapping):
                return {"status": "answered", "question": event}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _needs_help_for_question(run_id, event, seconds)
            ready, _write, _error = select.select([descriptor], [], [], remaining)
            if not ready:
                return _needs_help_for_question(run_id, event, seconds)
            os.read(descriptor, 65536)
    finally:
        os.close(descriptor)


def _supervisor_command(argv: list[str]) -> int:
    """Entry point for the detached per-run supervisor process."""
    parser = argparse.ArgumentParser(prog="reckon-supervisor")
    parser.add_argument("--spec", required=True)
    arguments = parser.parse_args(argv)
    return _run_supervisor(Path(arguments.spec))


def _peer_command(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == SUPERVISOR_ENTRY:
        _require_fleet_gate_open()
        return _supervisor_command(arguments[1:])
    parser = argparse.ArgumentParser(description="Use a durable crew peer channel.")
    actions = parser.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("peer-list")
    listing.add_argument("--run", required=True)
    asking = actions.add_parser("peer-ask")
    asking.add_argument("--run", required=True)
    asking.add_argument("--peer", required=True)
    asking.add_argument("--question", required=True)
    reading = actions.add_parser("peer-read")
    reading.add_argument("--run", required=True)
    reading.add_argument("--question-id", required=True)
    reading.add_argument("--wait", required=True)
    replying = actions.add_parser("peer-reply")
    replying.add_argument("--run", required=True)
    replying.add_argument("--question-id", required=True)
    replying.add_argument("--answer", required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.action == "peer-list":
            result = peer_list(arguments.run)
        elif arguments.action == "peer-ask":
            result = peer_ask(arguments.run, arguments.peer, arguments.question)
        elif arguments.action == "peer-read":
            result = peer_read(
                arguments.run, arguments.question_id, wait=arguments.wait
            )
        else:
            result = peer_reply(arguments.run, arguments.question_id, arguments.answer)
    except CrewError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _stamp_agent_display(
    agent: Mapping[str, Any], backend: Mapping[str, Any]
) -> dict[str, Any]:
    """Freeze the alias and effort spelling decided at dispatch onto the run.

    Both are display decisions made when the run starts, and both belong in the
    operator's flight configuration. Persisting them beside the model the alias
    shortens means a later configuration edit cannot silently restate what ran:
    the fleet pane renders from the record, never from current config. Only the
    declared values are carried here — the ticker derives a fallback from the
    model and effort it already holds when either is absent.
    """
    stamped = dict(agent)
    alias = str(backend.get("alias") or "").strip()
    if alias:
        stamped["alias"] = alias
    spellings = backend.get("effort_spelling")
    if isinstance(spellings, Mapping):
        effort = str(agent.get("effort") or "").strip()
        spelling = str(spellings.get(effort) or "").strip()
        if spelling:
            stamped["effort_spelling"] = spelling
    return stamped


@dataclass
class DispatchPlan:
    """Everything a dispatch resolved, before anything on disk has changed.

    Separating resolution from effect is what lets a dry run be the *same*
    decision as a real dispatch rather than a second implementation of it: a
    caller can see the routing, the filled-in defaults and the verdict without
    a worktree or a process existing.
    """

    run_id: str
    backend: str
    launch: str
    backend_settings: dict[str, Any]
    node: TaskNode
    budget_ceiling: str
    validation: NodeValidation
    execution_fit: capability.ExecutionFit
    token_budget: int | None = None
    local: bool = False
    warnings: list[str] = field(default_factory=list)
    done_when_warnings: list[dict[str, str]] = field(default_factory=list)
    competence: dict[str, Any] | None = None
    authority: dict[str, Any] | None = None
    live_conflicts: list[dict[str, Any]] | None = None
    admission: dict[str, Any] | None = None
    directory_claim_acceptances: list[dict[str, Any]] | None = None
    sandbox_write_roots: tuple[Path, ...] | None = None
    requested_backend: str | None = None
    default_backend: str | None = None
    section_routing: dict[str, Any] | None = None
    lane_declaration: dict[str, Any] | None = None
    lane_reading: dict[str, Any] | None = None
    lane_gate: dict[str, Any] | None = None
    lane_allowance: dict[str, Any] | None = None
    orchestrator_lane_stop: dict[str, Any] | None = None
    orchestrator_lane_override: dict[str, str] | None = None
    lane_advisory: dict[str, Any] | None = None
    open_endedness: float | None = None
    picker_selection: dict[str, Any] | None = None
    route: str = "shadow"
    route_override: str | None = None
    watch: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        agent = _stamp_agent_display(
            _agent_configuration(self.backend, self.launch, self.backend_settings),
            self.backend_settings,
        )
        if self.local:
            agent["local"] = True
        payload = {
            "agent": agent,
            "backend": self.backend,
            "picker_selection": self.picker_selection,
            "route": self.route,
            "route_override": self.route_override,
            "default_backend": self.default_backend,
            "execution_fit": self.execution_fit.as_dict(),
            "launch": self.launch,
            "local": self.local,
            "lane_advisory": (
                None if self.lane_advisory is None else dict(self.lane_advisory)
            ),
            "lane_declaration": (
                None if self.lane_declaration is None else dict(self.lane_declaration)
            ),
            "lane_reading": (
                None if self.lane_reading is None else dict(self.lane_reading)
            ),
            "lane_gate": (
                None if self.lane_gate is None else dict(self.lane_gate)
            ),
            "lane_allowance": (
                None if self.lane_allowance is None else dict(self.lane_allowance)
            ),
            "node": self.node.as_dict(),
            "brief": _brief_record(self.node),
            "orchestrator_lane_stop": (
                None
                if self.orchestrator_lane_stop is None
                else dict(self.orchestrator_lane_stop)
            ),
            "orchestrator_lane_override": (
                None
                if self.orchestrator_lane_override is None
                else dict(self.orchestrator_lane_override)
            ),
            "requested_backend": self.requested_backend,
            "run_id": self.run_id,
            "section_routing": (
                None if self.section_routing is None else dict(self.section_routing)
            ),
            "sandbox": {
                "tier": self.backend_settings.get("sandbox"),
                "write_roots": (
                    None
                    if self.sandbox_write_roots is None
                    else [str(path) for path in self.sandbox_write_roots]
                ),
            },
            "time_budget": self.node.time_budget,
            "token_budget": self.token_budget,
            "validation": self.validation.as_dict(),
            "write_paths": list(self.node.write_paths),
            "warnings": list(self.warnings),
            "done_when_warnings": [dict(item) for item in self.done_when_warnings],
        }
        if self.competence is not None:
            payload["competence"] = dict(self.competence)
        if self.authority is not None:
            payload["authority"] = dict(self.authority)
        if self.live_conflicts is not None:
            payload["live_conflicts"] = [dict(item) for item in self.live_conflicts]
        if self.admission is not None:
            payload["admission"] = dict(self.admission)
            payload["record_assignment"] = {
                "state": "unevaluated",
                "fields": "all",
                "wave": "unevaluated",
            }
        if self.directory_claim_acceptances is not None:
            payload["directory_claim_acceptances"] = [
                dict(item) for item in self.directory_claim_acceptances
            ]
        if self.watch is not None:
            payload["watch"] = dict(self.watch)
        return payload


def _resolved_token_budget(
    config: Mapping[str, Any], backend: Mapping[str, Any]
) -> int | None:
    """Return a node's default token budget: role overlay first, fence fallback.

    Mirrors the time-budget resolution: the backend argument is the effective
    settings after the role and spec-level overlays are folded in, so the first
    candidate already carries any overlay-declared value. A value that does
    not coerce to a positive integer is unset rather than a refusal, so a bad
    declaration degrades to the wall-clock fence instead of blocking dispatch.
    """
    for candidate in (
        backend.get("token_budget"),
        (config.get("fences") or {}).get("token_budget"),
    ):
        if candidate is None or candidate == "":
            continue
        try:
            budget = int(candidate)
        except (TypeError, ValueError):
            continue
        if budget > 0:
            return budget
    return None


def _dispatch_lane_observation(
    project: str,
    *,
    root: str | Path | None,
    config: Mapping[str, Any],
    backend_name: str,
    backend: Mapping[str, Any],
) -> dict[str, Any]:
    """Read the quota position used to admit one metered dispatch.

    This is deliberately the read-only half of the budget gate. A dry run must
    make the same lane-declaration decision as a real dispatch without writing
    a preflight history row merely because a caller asked what would happen.
    """
    from reckon import budget as budget_module

    try:
        recorded = budget_module.latest_recorded(project, root=root, config=config)
        state = budget_module.state_for(
            backend_name,
            backend,
            recorded=recorded.get(backend_name),
            unattributed=recorded.unattributed,
        )
    except (OSError, TypeError, ValueError) as exc:
        return {
            "headroom": "unknown",
            "utilisation_pct": None,
            "observed_at": None,
            "detail": f"the dispatch-time budget reading was unavailable: {exc}",
        }
    return state.as_dict()


def _unmetered_dispatch_alternatives(
    config: Mapping[str, Any], *, role: str, spec_level: str
) -> list[str]:
    """Name configured unmetered backends that can resolve this node."""
    alternatives: list[str] = []
    for candidate in sorted((config.get("backends") or {}), key=str):
        candidate_name = str(candidate)
        if not ledger.is_unmetered_backend(candidate_name):
            continue
        try:
            _resolved_name, settings = resolve_role_override(
                config, role, spec_level, candidate_name
            )
        except CrewError:
            continue
        if settings.get("launch") in ("cli", "in-harness"):
            alternatives.append(candidate_name)
    return alternatives


def _lane_declaration_evidence(
    *,
    declared_backend: str,
    resolved_backend: str,
    observation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Pair the caller's lane choice with the position read at dispatch."""
    measured = observation or {}
    return {
        "backend": declared_backend or None,
        "resolved_backend": resolved_backend,
        "metered": not ledger.is_unmetered_backend(resolved_backend),
        "utilisation_pct": measured.get("utilisation_pct"),
        "observed_at": measured.get("observed_at"),
        "read_at": _utc_now(),
        "headroom": measured.get("headroom"),
    }


def _lane_declaration_finding(
    *,
    backend_name: str,
    observation: Mapping[str, Any],
    alternatives: Iterable[str],
) -> dict[str, str]:
    """Explain how to make an undeclared budget-checked route explicit."""
    utilisation = observation.get("utilisation_pct")
    figure = (
        "unknown"
        if not isinstance(utilisation, (int, float)) or isinstance(utilisation, bool)
        else f"{float(utilisation):g}%"
    )
    candidates = ", ".join(repr(name) for name in alternatives) or "none configured"
    return {
        "property": "fully-specified",
        "detail": (
            f"resolved backend {backend_name!r} is metered, but the caller declared "
            f"no lane; window utilisation read at dispatch is {figure}; unmetered "
            f"backends that would serve this node: {candidates}. Pass --backend "
            f"{backend_name} to declare this metered lane, or name one of the "
            "unmetered alternatives"
        ),
    }


# Committed runs of one node shape a lane must carry before its rework-charged
# cost can separate it from another lane. Below this the advisory says the
# evidence is too thin to name a lane rather than ranking one off a run or two;
# it is the same floor the shape-conditioned lane evidence module uses.
_LANE_ADVISORY_MINIMUM_SAMPLES = 10

# The horizon a projection is compared against when a node declares no time
# budget of its own, so a burn never reads as safe merely for want of a bound.
_LANE_ADVISORY_DEFAULT_HORIZON_SECONDS = 25 * 60


def _lane_advisory_lane(run: Mapping[str, Any]) -> str:
    """Name the lane a committed run was served on, or the empty string."""
    backend = str(run.get("backend") or "").strip()
    if backend:
        return backend
    agent = run.get("agent")
    if isinstance(agent, Mapping):
        return str(agent.get("backend") or "").strip()
    return ""


def _lane_advisory_costs(
    runs: Iterable[Mapping[str, Any]], *, role: str, spec_level: str
) -> dict[str, dict[str, Any]]:
    """Rework-charged input per durable node for every lane on one shape.

    The derivation is the one the routing figures use: a run counts as
    reworked when a later run on the same plan re-touches paths it declared,
    and a lane's cost is the median worker-plus-coordinator input over one
    minus its rework rate. Rework detection, the charged-input reader and the
    exclusion reasons are taken from the same module that derives the routing
    surface, so this carry cannot drift from the figure a reader sees there.

    A shape whose runs were all served on one lane therefore reports one lane,
    and a lane with too few usable runs to charge is reported with its sample
    depth and no cost rather than a cost drawn from a handful of runs.
    """
    from reckon import capabilities as capabilities_module

    usable = [
        run
        for run in runs
        if str(run.get("role") or "") == role
        and str(run.get("spec_level") or "") == spec_level
        and capabilities_module._routing_outcome_exclusion(run) is None
    ]
    later_paths: dict[str, list[tuple[int, tuple[str, ...]]]] = defaultdict(list)
    for index, run in enumerate(usable):
        plan = str(run.get("plan") or "").strip()
        paths = capabilities_module._write_paths(run)
        if plan and paths:
            later_paths[plan].append((index, paths))

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, run in enumerate(usable):
        lane = _lane_advisory_lane(run)
        if not lane:
            continue
        paths = capabilities_module._write_paths(run)
        plan = str(run.get("plan") or "").strip()
        reworked = bool(paths and plan) and any(
            later > index and capabilities_module._paths_overlap(paths, other)
            for later, other in later_paths.get(plan, ())
        )
        worker = capabilities_module._input_tokens(run)
        coordinator = capabilities_module._coordinator_input_tokens(run)
        charged = (
            worker + coordinator
            if worker is not None and coordinator is not None
            else None
        )
        grouped[lane].append({"reworked": reworked, "charged_input": charged})

    evidence: dict[str, dict[str, Any]] = {}
    for lane, observations in grouped.items():
        samples = len(observations)
        reworked = sum(bool(item["reworked"]) for item in observations)
        rework_rate = reworked / samples
        inputs = [
            float(item["charged_input"])
            for item in observations
            if item["charged_input"] is not None
        ]
        median_input = capabilities_module._median_or_none(inputs)
        evidence[lane] = {
            "samples": samples,
            "rework_rate": round(rework_rate, 6),
            "input_samples": len(inputs),
            # The floor sits on the observations the charged median is actually
            # drawn from, not on the usable runs beside them: a run with no
            # paired worker-and-coordinator reading contributes nothing to the
            # cost, so counting it toward the floor would clear a cost built
            # from a handful of readings. ``_charged_cost`` also refuses a
            # median of None, which is the same population stated as zero.
            "cost_per_durable_node": (
                capabilities_module._charged_cost(median_input, rework_rate)
                if len(inputs) >= _LANE_ADVISORY_MINIMUM_SAMPLES
                else None
            ),
        }
    return evidence


def _lane_advisory_cheaper_lane(
    runs: Iterable[Mapping[str, Any]],
    *,
    resolved_lane: str,
    role: str,
    spec_level: str,
    configured_lanes: Iterable[str],
) -> dict[str, Any]:
    """Name the lane measured rework serves this shape on more cheaply, or none.

    Only lanes the flight configures are named, so the clause always points at
    something a caller could actually route to. A lane whose rework-charged
    cost is not measured -- too few runs, or no paired coordinator reading --
    is never guessed at: the clause states the shortfall instead, because a
    recommendation drawn from one or two runs would read as evidence.
    """
    evidence = _lane_advisory_costs(runs, role=role, spec_level=spec_level)
    candidates = {
        name
        for name in evidence
        if name in set(configured_lanes) or name == resolved_lane
    }
    measured = {
        name: evidence[name]
        for name in candidates
        if evidence[name]["cost_per_durable_node"] is not None
    }
    resolved = evidence.get(resolved_lane)
    if resolved_lane not in measured:
        chargeable = resolved["input_samples"] if resolved else 0
        return {
            "lane": None,
            "state": "insufficient_evidence",
            "resolved_cost_per_durable_node": None,
            "candidates": sorted(measured),
            "detail": (
                f"the rework-charged cost of {resolved_lane!r} for {role!r} at "
                f"{spec_level!r} is not measured: {chargeable} usable run(s), "
                f"{_LANE_ADVISORY_MINIMUM_SAMPLES} needed, so no lane can be "
                "named cheaper on this evidence"
            ),
        }
    cheapest = min(measured, key=lambda name: measured[name]["cost_per_durable_node"])
    if cheapest == resolved_lane:
        return {
            "lane": None,
            "state": "none_cheaper",
            "resolved_cost_per_durable_node": resolved["cost_per_durable_node"],
            "candidates": sorted(measured),
            "detail": (
                f"no configured lane serves {role!r} at {spec_level!r} more "
                f"cheaply on measured rework than {resolved_lane!r} "
                f"({resolved['cost_per_durable_node']:g} input tokens per "
                "durable node)"
            ),
        }
    chosen = measured[cheapest]
    return {
        "lane": cheapest,
        "state": "measured",
        "resolved_cost_per_durable_node": resolved["cost_per_durable_node"],
        "cost_per_durable_node": chosen["cost_per_durable_node"],
        "rework_rate": chosen["rework_rate"],
        "samples": chosen["samples"],
        "candidates": sorted(measured),
        "detail": (
            f"measured rework puts {cheapest!r} at "
            f"{chosen['cost_per_durable_node']:g} input tokens per durable node "
            f"for {role!r} at {spec_level!r} against {resolved_lane!r} at "
            f"{resolved['cost_per_durable_node']:g}, over {chosen['samples']} "
            f"run(s) at a {chosen['rework_rate']:.3f} rework rate"
        ),
    }


def _lane_advisory_ledger_runs(
    project: str, ledger_root: str | Path | None
) -> list[dict[str, Any]]:
    """Read one project's committed runs in promotion order, or nothing.

    An absent ledger is the ordinary state of a project that has run no workers,
    so it reads as no evidence rather than an error: the advisory then states
    that no lane can be named instead of refusing the dispatch over it.
    """
    try:
        data, _version = ledger.load(project, root=ledger_root)
    except (OSError, ValueError, ledger.LedgerError):
        return []
    return [dict(run) for run in data.get("runs") or [] if isinstance(run, Mapping)]


def _lane_advisory_horizon_seconds(node: TaskNode) -> int:
    """The node's own fence as a horizon, falling back to the default bound."""
    declared = str(node.time_budget or "").strip()
    if declared:
        try:
            return int(parse_duration(declared))
        except CrewError:
            pass
    return _LANE_ADVISORY_DEFAULT_HORIZON_SECONDS


def _lane_advisory_instant(value: object) -> datetime | None:
    """Parse an ISO instant from a budget reading, or None when unreadable."""
    parsed = parse_iso(value)
    if parsed is None:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _dispatch_lane_advisory(
    *,
    backend_name: str,
    metered: bool,
    observation: Mapping[str, Any] | None,
    node: TaskNode,
    cheaper_lane: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Carry a lane's trajectory beside the routing, refusing nothing.

    The advisory exists because a coordinator learns its lane's trajectory only
    if it goes looking, and the one moment it is certainly not looking is while
    it dispatches. It therefore rides on the dispatch: the utilisation, burn
    multiple, projected exhaustion and reset are read from the same window
    reading the dispatch already took, and the projection is compared against
    the horizon of the work in hand. Nothing here refuses, holds or reroutes --
    the payload records the position and the resolved backend is untouched.

    ``state`` is ``emitted`` only when a metered lane's projection precedes the
    node's horizon, which is the moment the advice would change a decision;
    otherwise the carry is ``quiet`` and names why, so a silent payload is
    never mistaken for a lane that was checked and found safe.
    """
    measured = observation or {}
    horizon_seconds = _lane_advisory_horizon_seconds(node)
    moment = now or datetime.now(UTC)
    horizon_ends_at = moment + timedelta(seconds=horizon_seconds)
    projection = _lane_advisory_instant(measured.get("projected_exhaustion_at"))
    precedes: bool | None = None
    if projection is not None:
        precedes = projection <= horizon_ends_at
    carry = {
        "state": "quiet",
        "detail": "",
        "backend": backend_name,
        "metered": metered,
        "utilisation_pct": measured.get("utilisation_pct"),
        "burn_multiple": measured.get("burn_multiple"),
        "projected_exhaustion_at": measured.get("projected_exhaustion_at"),
        "resets_at": measured.get("resets_at"),
        "seconds_until_reset": measured.get("seconds_until_reset"),
        "observed_at": measured.get("observed_at"),
        "horizon_seconds": horizon_seconds,
        "horizon_ends_at": horizon_ends_at.isoformat(),
        "precedes_horizon": precedes,
        "cheaper_lane": cheaper_lane,
    }
    utilisation = measured.get("utilisation_pct")
    burn = measured.get("burn_multiple")
    if not metered:
        carry["detail"] = (
            f"{backend_name!r} is unmetered, so it has no window to exhaust; "
            "the local lane's scarcity is throughput, which it does not publish"
        )
        return carry
    if projection is None:
        carry["detail"] = (
            f"no projected exhaustion is available for {backend_name!r}; "
            "the burn projection needs a numeric utilisation bounded by a "
            "known window, and without one no horizon comparison is made"
        )
        return carry
    if not precedes:
        carry["detail"] = (
            f"{backend_name!r} is projected to exhaust at "
            f"{measured.get('projected_exhaustion_at')}, which is after this "
            f"node's {horizon_seconds}s horizon ending "
            f"{horizon_ends_at.isoformat()}"
        )
        return carry
    carry["state"] = "emitted"
    carry["detail"] = (
        f"{backend_name!r} sits at {utilisation}% utilisation burning "
        f"{burn}x, projected to exhaust at "
        f"{measured.get('projected_exhaustion_at')} -- before this node's "
        f"{horizon_seconds}s horizon ending {horizon_ends_at.isoformat()}; the "
        f"window resets at {measured.get('resets_at')}"
    )
    return carry


def _lane_reading_unknown(*, detail: str) -> dict[str, Any]:
    """Advisory carry for a lane reading the dispatch could not trust."""
    return {
        "state": "unknown",
        "headroom": "unknown",
        "binding_observed": "unknown",
        "mean_context": "unknown",
        "generating": "unknown",
        "waiting": "unknown",
        "throughput": _lane_document.blank_throughput(detail=detail),
        "observed_at": None,
        "age_seconds": None,
        "suggested_shelf_life_seconds": None,
        "detail": detail,
    }


def _metric_number(value: object) -> int | float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def _lane_reading_carry(
    document: Mapping[str, Any] | None, *, now: datetime | None = None
) -> dict[str, Any]:
    """Strictly parse one lane reading document into its advisory carry.

    A lane reading document is a JSON object a lane publishes about itself:
    ``headroom`` and ``mean_context`` as numbers, ``binding_observed`` naming
    the window observed binding, ``observed_at`` stamping when the reading was
    taken, and an optional ``suggested_shelf_life_seconds`` for how long the
    reading stays trustworthy. Parsing is strict, because the quiet failure
    runs toward apparent headroom: a missing or malformed figure is withheld
    as ``unknown`` naming which one it was, a field is never resolved to zero,
    and a reader that cannot understand the instrument says so rather than
    guessing. Strictness is per field rather than per document — what a
    reading does carry is still measured, and discarding it along with the
    field that failed serves nobody. What collapses the whole carry is a
    defect in the reading ITSELF: no document, one that will not parse, a
    missing or unintelligible ``observed_at``, or a reading older than its
    stated shelf life, whose age is then stated so a stale figure is never
    carried as if it were current.

    ``binding_observed`` is consumed as the document's own field and is never
    re-derived from whether the dispatch waited or was preempted — the carry
    takes no such inputs, so the only source of the flag is the document.

    The carry also answers what the lane is carrying and how fast it is going,
    because those two decide where the next node goes and a dispatcher that has
    to ask a second view for them is the compensation this carry exists to
    replace. ``generating`` is the lane's count of requests actively
    generating, ``waiting`` its count of requests queued behind them, and
    ``throughput`` the achieved rate with the vintage and the denominator that
    make it interpretable -- see ``lane_document.read_lane_throughput``. A document that
    publishes no count leaves that count ``unknown``, never zero, so an
    unmeasured lane and an idle one do not read alike.
    """
    if document is None:
        return _lane_reading_unknown(detail="no lane document")
    if not isinstance(document, Mapping):
        return _lane_reading_unknown(
            detail=f"lane document is {type(document).__name__}, not a JSON object"
        )
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    # The document's own keys -- the stamp it carries, the mean context, the
    # constraint observed binding and its shelf life -- are resolved by the
    # lane-document reader, so the dispatch holds no spelling of them and
    # cannot drift from the one reader that owns them.
    fields = _lane_document.read_lane_reading_fields(document)
    stamp = fields["observed_at"]
    if stamp is None:
        return _lane_reading_unknown(detail=fields["detail"])
    # Retained rather than routed through ``reckon._timestamps.parse_iso``: the
    # refusal quotes the parser's own exception text, which the shared parser
    # swallows to return ``None``, and the reading is strict enough to report
    # why a stamp was rejected.
    try:
        observed = datetime.fromisoformat(stamp)
    except ValueError as exc:
        return _lane_reading_unknown(
            detail=f"'observed_at' {stamp!r} is not an ISO-8601 timestamp: {exc}"
        )
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    age = now - observed
    if age.total_seconds() < 0:
        return _lane_reading_unknown(
            detail=f"'observed_at' {stamp!r} lies in the future"
        )
    # Each figure is withheld on its own. A document that omits one still
    # measured the others, and the timestamp beside them is what makes any of
    # them usable, so collapsing the reading over a single absent field
    # discards measurements the lane did take. The lane omits its concurrency
    # ceiling and nulls its headroom while the pool drains, which is precisely
    # when a reader needs the running count and the mean context.
    headroom = _metric_number(
        _lane_document.read_lane_document(document).get("headroom")
    )
    mean_context = fields["mean_context"]
    binding = fields["binding_observed"]
    shelf = fields["shelf_life_seconds"]
    if shelf is not None and shelf > 0 and age.total_seconds() > shelf:
        carry = _lane_reading_unknown(
            detail=(
                f"reading is {age.total_seconds():.0f}s old, older than its "
                f"{shelf:g}s shelf life"
            )
        )
        carry["age_seconds"] = int(age.total_seconds())
        carry["suggested_shelf_life_seconds"] = shelf
        return carry
    unreadable = [
        name
        for name, value in (
            ("headroom", headroom),
            ("mean_context", mean_context),
        )
        if value is None
    ]
    age_seconds = int(age.total_seconds())
    counts = _lane_document.read_lane_counts(document)
    return {
        "state": "fresh",
        "headroom": "unknown" if headroom is None else headroom,
        "generating": counts["generating"],
        "waiting": counts["waiting"],
        "throughput": _lane_document.read_lane_throughput(
            document, reading_stamp=stamp, reading_age_seconds=age_seconds, now=now
        ),
        # The field names WHICH constraint binds, and a lane with no such
        # constraint has nothing to name rather than nothing to report.
        "binding_observed": "unknown" if binding is None else binding,
        "mean_context": "unknown" if mean_context is None else mean_context,
        "observed_at": stamp,
        "age_seconds": age_seconds,
        "suggested_shelf_life_seconds": shelf,
        "detail": (
            ""
            if not unreadable
            else "lane document carries no numeric "
            + " or ".join(f"{name!r}" for name in unreadable)
        ),
    }


def _dispatch_lane_reading(backend: Mapping[str, Any]) -> dict[str, Any]:
    """Read the resolved lane's published reading and carry it, refusing nothing.

    A backend may declare ``lane_document``, a path to the local JSON the lane
    publishes about itself. The dispatch reads it strictly and attaches the
    carry to the plan as advisory data. No value in the document refuses,
    holds or reroutes a dispatch — the reading is carried so a consumer can see
    what the lane reported beside the routing decision, never instead of it.
    An absent declaration, an unreadable file, or an unparsable document
    collapses the carry to ``unknown`` naming the reason rather than to a
    figure.

    Two facts elsewhere do withhold a dispatch, and neither is a value the
    reading carries: the gate file's ``paused`` field, read from the backend's
    declared ``gate_document``, and a lane document whose published
    ``router_generation_gate.config_path`` differs from that declared path.
    Every other value of the lane document still withholds nothing, so the
    carry and the plan agree.
    """
    declared = backend.get("lane_document")
    if not declared:
        return _lane_reading_unknown(detail="backend declares no lane document")
    path = Path(str(declared)).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return _lane_reading_unknown(
            detail=f"lane document {str(path)!r} cannot be read — {exc}"
        )
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return _lane_reading_unknown(
            detail=f"lane document {str(path)!r} is not valid JSON — {exc}"
        )
    if not isinstance(payload, Mapping):
        return _lane_reading_unknown(
            detail=f"lane document {str(path)!r} is not a JSON object"
        )
    return _lane_reading_carry(payload)


# The states in which the lane gate withholds a dispatch rather than launching.
# A gate that says paused and a gate that cannot be answered both wait, because
# a gate nobody can confirm has ended is not an open one.
_LANE_GATE_WAITING_STATES = frozenset({"paused", "unreadable"})

# The gate file's own keys: a JSON boolean and an optional reason string.
_LANE_GATE_PAUSED_KEY = "paused"
_LANE_GATE_REASON_KEY = "reason"

# The path the lane document publishes under its gate block, compared against
# the backend's declared path so a pause written to a file the dispatch is not
# reading cannot pass unseen.
_LANE_GATE_CONFIG_PATH_KEY = "config_path"

# Each of the two gate reads has its own deadline rather than one budget shared
# between them, because a single stat on this filesystem has taken 8.5 s.
LANE_GATE_READ_DEADLINE_SECONDS = 5.0


class LanePaused(CrewError):  # noqa: N818 - named as the dispatch states it, beside BudgetHold
    """A dispatch waits on the lane gate rather than launching.

    Distinct from a refusal for the same reason a budget hold is: nothing was
    created and nothing is wrong with the node, so a caller retries once the
    gate opens rather than reshaping the work. The gate object the decision was
    taken from rides the exception, so every surface reports the same state the
    dispatch read.
    """

    def __init__(self, gate: Mapping[str, Any]) -> None:
        self.gate = dict(gate)
        super().__init__(
            str(gate.get("detail") or "").strip()
            or (f"the lane gate is {gate.get('state')!r} at {gate.get('gate_path')!r}")
        )


class LaneHeld(LanePaused):
    """A dispatch holds because the lane's own router grants it no worker slot.

    The lane is answering and its own arithmetic leaves this coordinator
    session no room, so the node is held rather than refused: nothing was
    created, the node is still ready, and the caller retries when the router's
    next reading grants a slot. The allowance decision that produced the hold
    rides the exception, so every surface reports the figure the router
    published rather than a second opinion about it.
    """


class _GateReadDeadline(Exception):  # noqa: N818 - an internal marker, not a raised API
    """A gate or lane-document read did not answer inside its own deadline."""


def _gate_text_reader(path: Path) -> str:
    """Read one gate-related file as text; an indirection a test can replace."""
    return path.read_text(encoding="utf-8")


def _read_text_under_deadline(
    path: Path, *, timeout: float, reader: Callable[[Path], str] | None = None
) -> str:
    """Read ``path`` under its own deadline, raising on timeout or ``OSError``.

    The read runs on a daemon thread so a stalled filesystem cannot hold the
    dispatch open: the deadline passing raises rather than waiting, and the
    abandoned thread dies with the process. The thread re-raises whatever the
    read raised, so a clean file-not-found stays distinguishable from a
    permission error, which the gate rule reads differently.
    """
    outcome: dict[str, Any] = {}

    def work() -> None:
        try:
            outcome["text"] = (reader or _gate_text_reader)(path)
        except BaseException as exc:  # noqa: BLE001 - re-raised to the caller
            outcome["error"] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise _GateReadDeadline(f"the read of {str(path)!r} exceeded {timeout:g}s")
    if "error" in outcome:
        raise outcome["error"]
    return str(outcome.get("text") or "")


def _gate_paths_agree(declared: Path, published: Path) -> bool:
    """Whether two gate paths name the same file once normalised."""
    return os.path.abspath(os.path.expanduser(str(declared))) == os.path.abspath(
        os.path.expanduser(str(published))
    )


def _gate_path_check(backend: Mapping[str, Any], declared: str) -> dict[str, Any]:
    """Compare the declared gate path against the lane document's published one.

    The declaration is a copy of a path the router derives, so it can drift. A
    mismatch waits, naming both paths, because a pause written to the file the
    dispatch is not reading would otherwise pass unseen. The lane document is
    read only for this comparison, and a declaration with nothing to compare
    against — no lane document, one that cannot be read inside its deadline one
    that is not a JSON object, one publishing no gate block, or one publishing
    no ``config_path`` — records ``skipped`` with its reason and never holds a
    dispatch on its own.
    """
    lane_declared = backend.get("lane_document")
    if not lane_declared:
        return {
            "state": "skipped",
            "detail": "backend declares no lane document to publish a config_path",
        }
    lane_path = Path(str(lane_declared)).expanduser()
    try:
        text = _read_text_under_deadline(
            lane_path, timeout=LANE_GATE_READ_DEADLINE_SECONDS
        )
    except _GateReadDeadline as exc:
        return {"state": "skipped", "detail": str(exc)}
    except OSError as exc:
        return {
            "state": "skipped",
            "detail": f"lane document {str(lane_path)!r} cannot be read — {exc}",
        }
    try:
        payload = json.loads(text)
    except ValueError as exc:
        return {
            "state": "skipped",
            "detail": f"lane document {str(lane_path)!r} is not valid JSON — {exc}",
        }
    if not isinstance(payload, Mapping):
        return {
            "state": "skipped",
            "detail": f"lane document {str(lane_path)!r} is not a JSON object",
        }
    gate_block = payload.get(_lane_document.GATE_KEY)
    if not isinstance(gate_block, Mapping):
        return {
            "state": "skipped",
            "detail": "lane document publishes no router_generation_gate block",
        }
    published = gate_block.get(_LANE_GATE_CONFIG_PATH_KEY)
    if not isinstance(published, str) or not published.strip():
        return {
            "state": "skipped",
            "detail": ("lane document publishes no router_generation_gate.config_path"),
        }
    published_path = Path(published.strip()).expanduser()
    if _gate_paths_agree(Path(declared), published_path):
        return {"state": "matched", "detail": ""}
    return {
        "state": "mismatch",
        "detail": (
            f"declared gate document {declared!r} differs from the lane "
            f"document's published config_path {str(published_path)!r}"
        ),
    }


def fleet_gate_path() -> Path:
    """The shared crew document that can hold launches on every backend."""
    return crew_home() / "fleet-gate.json"


def _fleet_gate_text_reader(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _dispatch_fleet_gate() -> dict[str, Any]:
    """Read the fleet gate under the same deadline as a backend gate."""
    path = fleet_gate_path()
    try:
        text = _read_text_under_deadline(
            path,
            timeout=LANE_GATE_READ_DEADLINE_SECONDS,
            reader=_fleet_gate_text_reader,
        )
    except FileNotFoundError:
        base = {"state": "open", "paused": False, "reason": None, "detail": ""}
    except (_GateReadDeadline, OSError) as exc:
        base = {
            "state": "unreadable",
            "paused": None,
            "reason": None,
            "detail": f"fleet gate {str(path)!r} cannot be read — {exc}",
        }
    else:
        try:
            base = _gate_rows_from_payload(json.loads(text), path)
        except ValueError as exc:
            base = {
                "state": "unreadable",
                "paused": None,
                "reason": None,
                "detail": f"fleet gate {str(path)!r} is not valid JSON — {exc}",
            }
    if base["state"] == "paused":
        base["detail"] = f"fleet gate is paused: {base['reason'] or 'no reason given'}"
    elif base["state"] == "unreadable" and "fleet gate" not in base["detail"]:
        base["detail"] = f"fleet gate {str(path)!r}: {base['detail']}"
    return {
        "gate": "fleet",
        "gate_path": str(path),
        **base,
        "path_check": "fleet-wide",
        "path_check_detail": "",
    }


def _require_fleet_gate_open() -> dict[str, Any]:
    """Read the shared gate at the last boundary before starting a worker."""
    gate = _dispatch_fleet_gate()
    if gate["state"] in _LANE_GATE_WAITING_STATES:
        raise LanePaused(gate)
    return gate


def _dispatch_lane_gate(backend: Mapping[str, Any]) -> dict[str, Any]:
    """Read the fleet gate, then the gate file a backend declares.

    The gate file is the authority for whether the lane admits work: a JSON
    object whose ``paused`` is a boolean, with an optional ``reason`` string.
    The rule this returns one row of:

    * no ``gate_document`` declared — ``not-declared``; the dispatch proceeds,
      which is a host or backend with no router;
    * the file reads as an object with ``paused`` true — ``paused``;
    * the file reads with ``paused`` false or the key absent — ``open``;
    * the read fails with a clean file-not-found and nothing else —
      ``declared-but-missing``; a mistyped path must not read like a quiet host
      with no router;
    * any other failure — a read past its deadline, a permission error, a file
      that is not a JSON object, a non-boolean ``paused``, or a declared path
      that differs from the lane document's published ``config_path`` —
      ``unreadable``, with the defect named.

    ``paused`` and ``unreadable`` are the two rows that withhold a dispatch;
    every other row lets it proceed. The lane document is read only for the
    path comparison, and a comparison that cannot be made records ``skipped``
    in ``path_check`` without holding the dispatch on its own.
    """
    fleet_gate = _dispatch_fleet_gate()
    if fleet_gate["state"] in _LANE_GATE_WAITING_STATES:
        return fleet_gate
    declared = str(backend.get("gate_document") or "").strip()
    if not declared:
        return {
            "state": "not-declared",
            "gate_path": None,
            "paused": None,
            "reason": None,
            "detail": "",
            "path_check": "not-declared",
            "path_check_detail": "",
        }
    path = Path(declared).expanduser()
    try:
        text = _read_text_under_deadline(path, timeout=LANE_GATE_READ_DEADLINE_SECONDS)
    except FileNotFoundError:
        base: dict[str, Any] = {
            "state": "declared-but-missing",
            "paused": None,
            "reason": None,
            "detail": "gate path declared but missing",
        }
    except _GateReadDeadline as exc:
        base = {
            "state": "unreadable",
            "paused": None,
            "reason": None,
            "detail": str(exc),
        }
    except OSError as exc:
        base = {
            "state": "unreadable",
            "paused": None,
            "reason": None,
            "detail": f"gate document {str(path)!r} cannot be read — {exc}",
        }
    else:
        try:
            payload = json.loads(text)
        except ValueError as exc:
            base = {
                "state": "unreadable",
                "paused": None,
                "reason": None,
                "detail": f"gate document {str(path)!r} is not valid JSON — {exc}",
            }
        else:
            base = _gate_rows_from_payload(payload, path)
    check = _gate_path_check(backend, declared)
    if check["state"] == "mismatch":
        base = {
            "state": "unreadable",
            "paused": None,
            "reason": None,
            "detail": check["detail"],
        }
    return {
        "gate_path": declared,
        **base,
        "path_check": check["state"],
        "path_check_detail": check["detail"],
    }


def _gate_rows_from_payload(payload: object, path: Path) -> dict[str, Any]:
    """Resolve the gate row a parsed gate-file payload stands for."""
    if not isinstance(payload, Mapping):
        return {
            "state": "unreadable",
            "paused": None,
            "reason": None,
            "detail": f"gate document {str(path)!r} is not a JSON object",
        }
    paused = payload.get(_LANE_GATE_PAUSED_KEY)
    reason = payload.get(_LANE_GATE_REASON_KEY)
    reason_text = reason.strip() if isinstance(reason, str) and reason.strip() else None
    if _LANE_GATE_PAUSED_KEY in payload and not isinstance(paused, bool):
        return {
            "state": "unreadable",
            "paused": None,
            "reason": None,
            "detail": (
                f"gate document {str(path)!r} carries a non-boolean "
                f"'paused' ({paused!r})"
            ),
        }
    if paused is True:
        return {"state": "paused", "paused": True, "reason": reason_text, "detail": ""}
    return {"state": "open", "paused": False, "reason": reason_text, "detail": ""}


# The flight-config key a backend sets to declare that its lane carries this
# deployment's orchestrators. Declared, never inferred from the backend's name:
# the orchestrator role is a property of the deployment, so a rule matching on
# a name breaks the moment an orchestrator runs elsewhere and misses an alias
# that points at one. A lane that declares nothing serves no orchestrator and
# is dispatchable as before.
ORCHESTRATOR_LANE_DECLARATION_KEY = "serves_orchestrators"

# What the stop does to a dispatch that resolves to a declaring lane: it
# records rather than refuses. A refusal does not remove the work, it moves it
# onto whatever lane remains, and that is only safe while the receiving lane
# serves it reliably: the locally served lane's mid-turn death rate is stated
# for the window before its repair and has not been re-measured after it, so a
# refusal landing there loses the dispatch rather than relocating it. The
# record still removes the silent case — the lane, why it is fenced and the
# discharge all reach the run's record and the payload.
ORCHESTRATOR_LANE_STOP_SEVERITY = "recorded"


def _orchestrator_lane_discharge_candidates(
    config: Mapping[str, Any], *, role: str, spec_level: str
) -> list[str]:
    """Name configured lanes that serve no orchestrator and can resolve the node."""
    candidates: list[str] = []
    backends = config.get("backends") or {}
    for candidate in sorted(backends, key=str):
        candidate_name = str(candidate)
        settings = backends.get(candidate_name)
        if not isinstance(settings, Mapping):
            continue
        if settings.get(ORCHESTRATOR_LANE_DECLARATION_KEY):
            continue
        try:
            _resolved, effective = resolve_role_override(
                config, role, spec_level, candidate_name
            )
        except CrewError:
            continue
        if effective.get("launch") in ("cli", "in-harness"):
            candidates.append(candidate_name)
    return candidates


def _dispatch_orchestrator_lane_stop(
    *,
    backend_name: str,
    backend: Mapping[str, Any],
    config: Mapping[str, Any],
    role: str,
    spec_level: str,
) -> dict[str, Any]:
    """Report a resolved lane that declares it serves this deployment's orchestrators.

    One subscription runs every orchestrator here, so background work placed on
    the same lane spends the capacity the sessions that dispatch, merge, promote
    and record need. The end state is not a slow node: a lane saturated there
    stops every session at once, including the ones that would have noticed, and
    work already in flight is then unreachable by the only processes that could
    reconcile it.

    The declaration is read from the resolved backend, so ``--local``, an
    explicit ``--backend``, a role overlay and a budget fallback all reach it,
    and a lane that declares nothing is dispatchable exactly as before. The
    stop is composed in resolution, before a run directory, a live pointer or a
    worktree exists, so it cannot be lost to a failure part way through the
    writes that follow.

    ``severity`` states what the stop does with the launch. It is ``recorded``
    rather than ``refused`` because the lane a refusal would push this node
    onto is not measurably reliable at the effort such work needs, so refusing
    would trade a lane that is too busy for a lane that does not finish. The
    record names the lane, why it is fenced and the discharge, and it reaches
    the run's own record and the dispatch payload either way.
    """
    if not backend.get(ORCHESTRATOR_LANE_DECLARATION_KEY):
        return {
            "state": "not-declared",
            "severity": None,
            "lane": backend_name,
            "detail": (
                f"resolved lane {backend_name!r} declares no orchestrator role, "
                "so it is dispatchable as before"
            ),
            "discharge": "",
        }
    candidates = _orchestrator_lane_discharge_candidates(
        config, role=role, spec_level=spec_level
    )
    if candidates:
        discharge = (
            "route this node to a lane that serves no orchestrator, declared "
            "with --backend: " + ", ".join(repr(name) for name in candidates)
        )
    else:
        discharge = (
            "no configured lane that serves no orchestrator can resolve this "
            "node; add a backend that declares no orchestrator role and route "
            "the node to it"
        )
    return {
        "state": "declared",
        "severity": ORCHESTRATOR_LANE_STOP_SEVERITY,
        "lane": backend_name,
        "detail": (
            f"resolved lane {backend_name!r} declares "
            f"{ORCHESTRATOR_LANE_DECLARATION_KEY}. Do not dispatch background "
            "work to an orchestrator lane: it runs the orchestrators; background "
            "work there costs orchestrator capacity, and saturating it stops "
            "every session rather than one node"
        ),
        "discharge": discharge,
    }


def _orchestrator_lane_stop_line(stop: Mapping[str, Any]) -> str:
    """Render the stop as the one line a run's warnings carry."""
    return f"{stop['detail']}; {stop['discharge']}"


def _path_is_tmpfs(path: str | Path) -> bool:
    """Return whether a path resolves beneath a tmpfs or ramfs mount."""
    target = Path(path).expanduser().resolve()
    best: tuple[int, str] | None = None
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return False
    for line in lines:
        fields, separator, trailing = line.partition(" - ")
        if not separator:
            continue
        parts = fields.split()
        trailing_parts = trailing.split()
        if len(parts) < 5 or not trailing_parts:
            continue
        mount = Path(parts[4].replace("\\040", " ")).resolve()
        if target == mount or target.is_relative_to(mount):
            candidate = (len(mount.parts), trailing_parts[0])
            if best is None or candidate[0] > best[0]:
                best = candidate
    return bool(best and best[1] in {"tmpfs", "ramfs"})


# A declared endpoint is probed before launch, so the probe is bounded: a slow
# or absent router must refuse the placement rather than hold the dispatch open.
_REQUIREMENT_PROBE_TIMEOUT_SECONDS = 3.0


def _is_node_local_path(path: str | Path) -> bool:
    """Whether a path lives on this node's own storage rather than shared.

    The per-user runtime directory is named first because it is the case that
    reads as present: it exists on every node, so a launch against it succeeds
    and the worker then finds different bytes on the node it runs on. The mount
    table is the general answer beneath it, covering any tmpfs the host mounts
    for scratch.
    """
    text = str(Path(str(path)).expanduser())
    if text == "/run/user" or text.startswith("/run/user/"):
        return True
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        runtime_root = str(Path(runtime).expanduser())
        if text == runtime_root or text.startswith(runtime_root.rstrip("/") + "/"):
            return True
    return _path_is_tmpfs(text)


def _endpoint_answers(endpoint: str) -> tuple[bool, str]:
    """Whether a host:port endpoint accepts a connection, and why not.

    Bounded, because the check is a precondition of the launch rather than part
    of it: a router that is slow to answer must refuse the placement rather
    than hold the dispatch open.
    """
    host, separator, port_text = str(endpoint).strip().rpartition(":")
    if not separator or not host:
        return False, "not a host:port endpoint"
    try:
        port = int(port_text)
    except ValueError:
        return False, f"{port_text!r} is not a port"
    try:
        with socket.create_connection(
            (host, port), timeout=_REQUIREMENT_PROBE_TIMEOUT_SECONDS
        ):
            return True, "reachable"
    except OSError as exc:
        return False, f"{host}:{port} refused the connection — {exc}"


def check_placement_requirements(
    placement: Mapping[str, Any] | None, *, backend_name: str
) -> None:
    """Refuse a placement before launch when something it declares is invisible.

    Coordinator-side and ahead of every side effect, because the check decides
    whether the launch is worth making: a requirement the target node cannot
    see fails inside the worker and reads as a worker defect. A node-side check
    would need the node to start, which is the thing being refused.

    Only the declaration is read — no scheduler is invoked and no job is
    submitted — so a dry run reaches the verdict a real dispatch reaches. A
    backend declaring no placement is untouched, which keeps every backend
    that declares none launching exactly as before.
    """
    if not isinstance(placement, Mapping) or not placement:
        return
    scheduler = str(placement.get("scheduler") or "")
    queries = flight.placement_scheduler_queries(placement)
    if scheduler and set(queries) != {"state_query", "reason_query"}:
        raise CrewError(
            placement_query_undeclared(backend=backend_name, scheduler=scheduler)
        )
    for name, path, endpoint in _placement_requirement_targets(placement):
        if path is None and endpoint is None:
            raise CrewError(
                placement_requirement_unmet(
                    backend=backend_name,
                    placement=placement,
                    name=name,
                    detail="the requirement declares neither a path nor an endpoint",
                )
            )
        if path is not None:
            if _is_node_local_path(path):
                raise CrewError(
                    placement_requirement_node_local(
                        backend=backend_name,
                        placement=placement,
                        name=name,
                        path=path,
                    )
                )
            if not Path(path).expanduser().exists():
                raise CrewError(
                    placement_requirement_unmet(
                        backend=backend_name,
                        placement=placement,
                        name=name,
                        detail=f"path {path!r} does not exist",
                    )
                )
            continue
        assert endpoint is not None
        reachable, why = _endpoint_answers(endpoint)
        if not reachable:
            raise CrewError(
                placement_requirement_unmet(
                    backend=backend_name,
                    placement=placement,
                    name=name,
                    detail=f"endpoint {endpoint!r} is unreachable — {why}",
                )
            )


def _placement_requirement_targets(
    placement: Mapping[str, Any],
) -> Iterable[tuple[str, str | None, str | None]]:
    """Yield each declared requirement as (name, path, endpoint).

    An entry declaring neither target is yielded with both absent rather than
    skipped, so a malformed declaration is refused by the caller instead of
    silently passing a check it never ran.
    """
    for index, entry in enumerate(flight.placement_requirement_entries(placement)):
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or f"requirement {index + 1}")
        path = str(entry["path"]) if entry.get("path") else None
        endpoint = str(entry["endpoint"]) if entry.get("endpoint") else None
        yield name, path, endpoint


def _resolved_write_paths(
    backend: Mapping[str, Any], *, run_directory: Path
) -> list[str]:
    """Return a role's default write scope, or [] when it declares none.

    A role's ``write_paths`` are relative and resolve against this dispatch's
    own run directory rather than the repository, so the shipped default names
    no host-specific location and grants no reach into repository source. A
    node that declares its own write_paths is never touched here.
    """
    declared = backend.get("write_paths")
    if not declared:
        return []
    return [str((run_directory / str(entry)).resolve()) for entry in declared]


def _require_write_paths_in_authority(
    node: TaskNode, authority: Mapping[str, Any]
) -> None:
    """Confine writes to resolved repositories or durable delivery roots."""
    work_repo = Path(str(authority["write"]["repository"])).resolve()
    repository_roots: list[Path] = []
    for value in authority.get("repositories") or (work_repo,):
        root = Path(str(value)).expanduser().resolve()
        if root not in repository_roots:
            repository_roots.append(root)
    if work_repo not in repository_roots:
        repository_roots.append(work_repo)
    roots = delivery_roots()
    allowed_roots = (*repository_roots, *roots)
    for declared in node.write_paths:
        raw = Path(declared).expanduser()
        resolved = (raw if raw.is_absolute() else work_repo / raw).resolve()
        if any(resolved.is_relative_to(root) for root in allowed_roots):
            continue
        repositories = ", ".join(str(root) for root in repository_roots)
        raise CrewError(
            f"write path {declared!r} resolves outside the authorised work repository "
            f"{work_repo}, every other repository registered by the dispatch authority "
            f"({repositories}), and Reckon delivery directories {', '.join(str(root) for root in roots)}; "
            "the repository containing this path is missing from "
            "mounts.json or outside the resolved plan authority"
        )


def _sandbox_reachability(
    node: TaskNode,
    *,
    backend: Mapping[str, Any],
    repository: Path,
    run_directory: Path,
) -> tuple[tuple[Path, ...] | None, list[dict[str, str]]]:
    """Resolve writable roots and report declared paths outside every grant."""
    roots = _backends.sandbox_write_roots(
        backend,
        repository=repository,
        run_directory=run_directory,
        reports_directory=reports_dir(),
        manifest_path=node.manifest_path,
        review_store_directory=review_store_root(),
    )
    if "execution_capable" not in backend:
        return roots, []
    tier = str(backend.get("sandbox") or _backends.READ_ONLY)
    unreachable = [
        str(path)
        for path in node.write_paths
        if not _backends.sandbox_can_write(
            path,
            repository=repository,
            write_roots=roots,
        )
    ]
    grants = "unrestricted" if roots is None else ", ".join(str(root) for root in roots)
    return roots, [
        {
            "property": "scoped",
            "detail": (
                f"write path {path!r} is unreachable in resolved sandbox tier "
                f"{tier!r}; writable grants: {grants or 'none'}"
            ),
        }
        for path in unreachable
    ]


def _fence_write_roots(
    *,
    backend: Mapping[str, Any],
    repository: str | Path,
    run_directory: str | Path,
    manifest_path: str | Path | None,
    worktree: str | Path | None,
    declared_write_paths: Iterable[str],
) -> tuple[Path, ...]:
    """Return every root a fenced launch re-binds writable.

    The fence seals each protected path and re-binds only the roots it is
    handed, so a tier's own write roots are not enough on their own: an
    ``unrestricted`` tier is unrestricted only while nothing seals the machine.
    Under the fence the delivery stores a restricted tier gets are named here
    too, together with every declared write path outside the worktree, so a
    worker delivers into the same place whichever tier it runs on.
    """
    return _backends.fenced_write_roots(
        backend,
        repository=repository,
        run_directory=run_directory,
        reports_directory=reports_dir(),
        review_store_directory=review_store_root(),
        manifest_path=manifest_path,
        declared_write_paths=declared_write_paths,
        worktree=worktree,
    )


def _brief_digest(source: str | Path) -> str:
    """Return the sha256 of a brief's stored bytes.

    The digest joins a promoted row to the exact text its worker read, so it is
    taken over the file's bytes rather than a parsed form: a brief is its own
    authority text, and its spelling is part of what the worker was told. A
    brief that cannot be read is refused here, where the caller still has the
    path it named, rather than inside the worker.
    """
    path = Path(source).expanduser()
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise CrewError(f"the brief {source!r} is not readable: {exc}") from exc
    return hashlib.sha256(data).hexdigest()


def _store_brief(directory: Path, source: str) -> Path:
    """Copy a brief into the run directory and return the stored path.

    The brief is durable authority, so its copy lives beside the run's own
    record — under the configuration home, never inside the worktree — and the
    pointer names that copy, because the source path a coordinator handed in
    may be a scratch file no later reader can open. The suffix is kept so the
    stored file reads as the document it was.
    """
    destination = directory / f"brief{Path(source).suffix or '.md'}"
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(source).expanduser(), destination)
    except OSError as exc:
        raise CrewError(f"the brief {source!r} could not be stored: {exc}") from exc
    return destination


def _brief_record(node: TaskNode) -> dict[str, str] | None:
    """Return the pointer's brief block, or None for a plan-carried node."""
    if not node.brief.strip():
        return None
    return {
        "sha256": node.brief_sha256,
        "path": node.brief_path or node.brief,
        "source_path": node.brief,
    }


def _brief_text(node: TaskNode) -> str:
    """Return the brief text the composed prompt carries verbatim.

    The stored copy is read when dispatch has made one, so the prompt and the
    bytes the pointer names are the same document even if the source moved
    between the digest and the composition.
    """
    if not node.brief.strip():
        return ""
    source = node.brief_path or node.brief
    try:
        return Path(source).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise CrewError(f"the brief {node.brief!r} is not readable: {exc}") from exc


PICKER_DISPATCH_TIMEOUT_SECONDS = 5.0

# A picker answer carries per-call measurements: how long the ask itself took.
# They differ between two asks of the same node by construction, so a report
# that keeps them cannot be compared with a second run of the same call — which
# is the whole point of a dry run. The live run record keeps them, where the
# figures are the point; the resolved plan a preview reports drops them.
_PICKER_PER_CALL_FIELDS = ("latency_ms", "jev_latency_ms")


def _reportable_picker_selection(
    selection: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """The picker answer a resolved plan records so two previews compare.

    The per-call latencies are removed, leaving the decision — the action, the
    selected backend and model, the probabilities and the reason — which is what
    a preview exists to show. Observation stamps reach the report already
    carrying their ``_at`` suffix, which the comparison masks as a clock reading
    rather than a decision.
    """
    if selection is None:
        return None
    return {
        key: value
        for key, value in selection.items()
        if key not in _PICKER_PER_CALL_FIELDS
    }


def _picker_fallback(
    reason: str,
    comment: str,
    *,
    input_errors: Mapping[str, str] | None = None,
    latency_ms: float | None = None,
) -> dict[str, Any]:
    """The selection dispatch records when the picker did not decide one.

    One shape serves every fallback — a picker that raised, one that ran past
    its bound, and one whose inputs could not be built — so a reader settles
    each case by the ``fallback_reason`` rather than by which keys are present.
    """
    client = sys.modules.get("reckon.crew.picker.client")
    return {
        "action": "fallback",
        "backend": None,
        "family": None,
        "model": None,
        "effort": None,
        "probabilities": {},
        "confidence": None,
        "jev_model": getattr(client, "JEV_MODEL", None),
        "fallback_reason": reason,
        "latency_ms": latency_ms,
        "offered": [],
        "excluded": [],
        "comment": comment,
        **({"input_errors": dict(input_errors)} if input_errors else {}),
    }


def _picker_ledger_rows(project: str, ledger_root: Path) -> list[dict[str, Any]]:
    return ledger.picker_runs(project, root=ledger_root)


def _picker_verdict_inputs(project: str, repo_root: Path) -> Mapping[str, Any]:
    return shared_verdict_inputs(project, repo_root)


def _picker_budget_snapshot(
    project: str,
    config: Mapping[str, Any],
    repo_root: Path,
    records: list[dict[str, Any]] | None,
) -> Mapping[str, Any]:
    from reckon.crew.picker import snapshot as picker_snapshot

    return picker_snapshot.budget_view(
        project, dict(config), repo_root, records, cached_only=True
    )


def _picker_input(
    name: str, build: Callable[[], Any], errors: dict[str, str]
) -> Any:
    """Build one dispatch-scope picker input, recording a failure instead of raising.

    These inputs are advisory: the picker re-reads whatever it is not handed, so
    a damaged ledger, a conflicting merge marker or a missing mount that makes
    one of them unreadable must leave the dispatch to reach its own verdict
    rather than abort the run's bookkeeping before the picker is consulted.
    """
    try:
        return build()
    except Exception as exc:  # noqa: BLE001 - a picker input never blocks dispatch
        errors[name] = f"{type(exc).__name__}: {exc}"
        return None


def build_picker_inputs(
    project: str,
    config: Mapping[str, Any],
    repo: str | Path,
    *,
    ledger_root: Path | None = None,
) -> tuple[
    list[dict[str, Any]] | None,
    Mapping[str, Any] | None,
    Mapping[str, Any] | None,
    dict[str, str],
]:
    """Build the three dispatch-scope picker inputs once, capturing failures.

    The ledger rows, the verdict inputs and the budget snapshot are read here,
    beside the pick, so their cost is paid outside the picker's own latency
    bound and the picker re-reads nothing per candidate. A caller that picks
    separately calls this too, so the same work is done once whichever entry
    asks. Each input is built behind its own guard: a failure is recorded
    against the input that failed and returned in the error map rather than
    raised, so the picker falls back with the failure named instead of the
    dispatch aborting before it is consulted.
    """
    input_errors: dict[str, str] = {}
    repo_root = Path(repo)
    if ledger_root is None:
        ledger_root = _picker_input(
            "records",
            lambda: resolve_dispatch_ledger_root(
                resolve_dispatch_authority(project, repo_root)
            ),
            input_errors,
        )
    records = (
        _picker_input(
            "records",
            lambda: _picker_ledger_rows(project, ledger_root),
            input_errors,
        )
        if ledger_root is not None
        else None
    )
    verdict_inputs = _picker_input(
        "verdict_inputs",
        lambda: _picker_verdict_inputs(project, repo_root),
        input_errors,
    )
    budget_snapshot = _picker_input(
        "budget_snapshot",
        lambda: _picker_budget_snapshot(project, config, repo_root, records),
        input_errors,
    )
    return records, verdict_inputs, budget_snapshot, input_errors


def dispatch_picker_selection(
    *,
    node: TaskNode,
    config: Mapping[str, Any],
    project: str,
    repo: Path,
    session: str = "",
    comment: str = "",
    records: list[dict[str, Any]] | None = None,
    verdict_inputs: Mapping[str, Any] | None = None,
    budget_snapshot: Mapping[str, Any] | None = None,
    input_errors: Mapping[str, str] | None = None,
    authority: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask the picker without letting its latency or failure stop dispatch."""
    if input_errors:
        # An input the dispatcher could not build is not re-read here: the same
        # source that failed once would only fail again, so the picker's own
        # pick is skipped and the failure is named in the recorded fallback.
        return _picker_fallback(
            "; ".join(f"{name}: {detail}" for name, detail in input_errors.items()),
            comment,
            input_errors=input_errors,
            latency_ms=0.0,
        )
    finished = threading.Event()
    result: dict[str, Any] = {}
    started = time.monotonic()

    def ask() -> None:
        try:
            from reckon.crew.picker import PickRequest, pick
            from reckon.crew.picker import snapshot as picker_snapshot

            # The dispatcher's own authority travels into the estimate and the
            # pick, so a dispatcher-granted landing fragment is exempt in the
            # figure Jev weighs exactly as it is in every candidate's context-fit
            # verdict. A caller that does not hold the resolved authority (the
            # advisory shadow path, the dry-run pick) resolves the same one here;
            # a resolution that fails leaves the picker with no authority rather
            # than aborting an advisory.
            pick_authority = authority
            if pick_authority is None:
                try:
                    pick_authority = resolve_dispatch_authority(project, repo)
                except (CrewError, PlanVisibilityError, OSError, KeyError, TypeError):
                    pick_authority = None

            # The estimate is the same deterministic measurement the context-fit
            # verdict charges a node against, measured with the same authority
            # and the same harness-independent standing chain, so the request's
            # figure and every candidate block's are one. It is advisory: a
            # failure to measure leaves the figure at zero and never aborts the
            # pick.
            try:
                estimated_context = picker_snapshot.estimated_context_tokens(
                    node, repo, authority=pick_authority
                )
            except Exception:  # noqa: BLE001 - the estimate is advisory to the pick
                estimated_context = 0

            inputs = {
                "records": records,
                "verdict_inputs": verdict_inputs,
                "budget_snapshot": budget_snapshot,
                "authority": pick_authority,
            }
            parameters = inspect.signature(pick).parameters.values()
            if not any(item.kind is item.VAR_KEYWORD for item in parameters):
                accepted = {item.name for item in parameters}
                inputs = {
                    key: value for key, value in inputs.items() if key in accepted
                }
            selection = pick(
                PickRequest(
                    project,
                    node,
                    comment=comment,
                    session=session,
                    estimated_context=estimated_context,
                ),
                dict(config),
                repo=repo,
                cached_only=True,
                **inputs,
            )
            result["selection"] = selection.as_dict()
        except Exception as exc:  # noqa: BLE001 - shadow routing cannot block dispatch
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            finished.set()

    threading.Thread(target=ask, name="dispatch-picker", daemon=True).start()
    if not finished.wait(PICKER_DISPATCH_TIMEOUT_SECONDS):
        result["error"] = "timeout"
    if "selection" in result and "error" not in result:
        return result["selection"]
    return _picker_fallback(
        result.get("error") or "picker returned no selection",
        comment,
        latency_ms=round((time.monotonic() - started) * 1000, 3),
    )


def _write_existing_pointer(run_id: str, record: Mapping[str, Any]) -> bool:
    """Write a present pointer while its caller holds the removal lock.

    The run directory checks also keep a removed directory from being restored
    by a late supervisor write.
    """
    path = pointer_path(run_id)
    directory = run_dir(run_id)
    if not path.exists() or not directory.is_dir():
        return False
    _write_json(path, record)
    if not directory.is_dir():
        path.unlink(missing_ok=True)
        return False
    return True


def _attach_shadow_picker_selection(run_id: str, selection: Mapping[str, Any]) -> None:
    """Update a live pointer without recreating a discarded run."""
    with _pointer_lock(run_id):
        if pointer_path(run_id).exists():
            pointer = read_pointer(run_id)
            pointer["picker_selection"] = dict(selection)
            _write_existing_pointer(run_id, pointer)


def _record_shadow_picker_selection(spec_path: Path) -> None:
    """Finish an advisory pick after launch and attach it to the live run."""
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    node = TaskNode(**spec["node"])
    repo = Path(spec["repo"])
    result: dict[str, Any] = {}
    finished = threading.Event()
    started = time.monotonic()

    def pick_shadow() -> None:
        try:
            records, inputs, budget, errors = build_picker_inputs(
                spec["project"],
                spec["config"],
                repo,
                ledger_root=Path(spec["ledger_root"]),
            )
            result["selection"] = dispatch_picker_selection(
                node=node,
                config=spec["config"],
                project=spec["project"],
                repo=repo,
                session=spec["session"],
                comment=spec["comment"],
                records=records,
                verdict_inputs=inputs,
                budget_snapshot=budget,
                input_errors=errors,
            )
        except Exception as exc:  # noqa: BLE001 - an advisory cannot stop a run
            result["selection"] = _picker_fallback(
                f"{type(exc).__name__}: {exc}", spec["comment"]
            )
        finally:
            finished.set()

    threading.Thread(target=pick_shadow, name="shadow-picker", daemon=True).start()
    if not finished.wait(PICKER_DISPATCH_TIMEOUT_SECONDS):
        result["selection"] = _picker_fallback(
            "timeout",
            spec["comment"],
            latency_ms=round((time.monotonic() - started) * 1000, 3),
        )
    selection = result["selection"]
    _attach_shadow_picker_selection(spec["run_id"], selection)


def _start_shadow_picker_selection(
    *,
    run_id: str,
    node: TaskNode,
    config: Mapping[str, Any],
    project: str,
    repo: Path,
    ledger_root: Path,
    session: str,
    comment: str,
) -> None:
    """Start a bounded detached reader without extending dispatch's lifetime."""
    directory = run_dir(run_id)
    spec_path = directory / "shadow-picker.json"
    _write_json(
        spec_path,
        {
            "run_id": run_id,
            "node": node.as_dict(),
            "config": dict(config),
            "project": project,
            "repo": str(repo),
            "ledger_root": str(ledger_root),
            "session": session,
            "comment": comment,
        },
    )
    log = (directory / "shadow-picker.log").open("a", encoding="utf-8")
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; from reckon.crew.dispatch import _record_shadow_picker_selection; import sys; _record_shadow_picker_selection(Path(sys.argv[1]))",
                str(spec_path),
            ],
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).parents[2])
                + os.pathsep
                + os.environ.get("PYTHONPATH", ""),
            },
        )
    finally:
        log.close()


def _picker_refusal_reasons(selection: Mapping[str, Any]) -> str:
    reasons = [
        str(selection[key])
        for key in ("reason", "fallback_reason")
        if selection.get(key)
    ]
    if selection.get("action") == "hold":
        confidence = selection.get("confidence")
        hold_probability = (selection.get("probabilities") or {}).get("hold")
        detail = "picker selected hold"
        if isinstance(confidence, (int, float)):
            detail += f" at confidence {confidence:g}"
        if isinstance(hold_probability, (int, float)):
            detail += f" (hold probability {hold_probability:g})"
        reasons.insert(0, detail)
    reasons.extend(
        str(reason)
        for candidate in selection.get("excluded") or ()
        for reason in candidate.get("reasons") or ()
    )
    return "; ".join(dict.fromkeys(reasons)) or "no eligible backend"


def resolve_dispatch_route(config: Mapping[str, Any], route: str | None) -> str:
    """Resolve a per-dispatch override before the layered picker setting."""
    if route is not None:
        if route not in {"shadow", "picker", "deterministic"}:
            raise CrewError(f"unknown dispatch route {route!r}")
        return route
    mode = (config.get("routing") or {}).get("picker", "shadow")
    if mode not in {"shadow", "route"}:
        raise CrewError(f"unknown routing.picker value {mode!r}")
    return "picker" if mode == "route" else "shadow"


def plan_dispatch(
    *,
    node: TaskNode,
    config: Mapping[str, Any],
    locked_decisions: Iterable[str] = (),
    peer_scopes: Mapping[str, Iterable[str]] | None = None,
    run_id: str | None = None,
    project: str = "",
    repo: str | Path | None = None,
    base: str = "HEAD",
    execution_override: bool = False,
    orchestrator_lane_reason: str | None = None,
    authority: Mapping[str, Any] | None = None,
    report_live_conflicts: bool = False,
    local: bool = False,
    backend_override: str | None = None,
    default_backend_override: str | None = None,
    declared_backend: str | None = None,
    member: str = "",
    allow_unreviewed_plan: bool = False,
    session: str = "",
    watch_required: bool = False,
    watch_override: bool = False,
    repairs: str = "",
    accept_directory_claim: bool = False,
    route: str | None = None,
    picker_selection: Mapping[str, Any] | None = None,
) -> DispatchPlan:
    """Resolve routing and defaults for one node and judge it. No side effects.

    Mutates only the node it was handed, filling the defaults a dispatch would
    fill — the time budget from the resolved fence and the manifest path from
    the run directory — so the verdict is the one a real dispatch would reach.

    ``backend_override`` and ``default_backend_override`` re-resolve the role's
    own overlay against a caller-requested backend instead of the configured
    default. Keeping both request surfaces here makes them inherit the same
    disagreement refusal and every later check this function performs
    (execution fit, sandbox reachability, and write-path scope).
    """
    if not _SAFE_ID.fullmatch(node.id):
        raise CrewError(f"node id {node.id!r} must match {_SAFE_ID.pattern}")
    if node.spec_level not in ("", "exact", "guided", "open"):
        raise CrewError(
            f"spec level {node.spec_level!r} is not one of exact, guided, open, "
            "or empty (undeclared)"
        )
    fleet_gate = _dispatch_fleet_gate()
    if fleet_gate["state"] in _LANE_GATE_WAITING_STATES:
        raise LanePaused(fleet_gate)
    require_worker_scratch_headroom(config)
    # Proven here rather than at worktree creation so that a dry run, whose
    # documented job is to validate the call, cannot report a dispatchable
    # node that the real dispatch then refuses on a missing precondition.
    _fleet_script()
    # A dry run must reach the verdict the real dispatch reaches, and the real
    # dispatch refuses a repository that is not the project's mount, so the
    # same resolution runs here. A caller that named no repository keeps its
    # ``None``: only the dispatch path turns that into the mount.
    if repo is not None and project_mount_repository(project) is not None:
        repo = resolve_project_repository(project, repo)
    # The run a --repairs dispatch repairs must already have landed, so the
    # declaration is judged here, beside the other preconditions, before a
    # worktree, pointer or process exists. A dry run reaches the same refusal
    # because this is the one place the check runs.
    if repairs:
        _require_repairs_target(project, repairs, authority=authority)
    requested_backend = str(backend_override or default_backend_override or "").strip()
    route_override = route
    route = resolve_dispatch_route(config, route)
    selection_absent = route == "picker" and picker_selection is None
    if selection_absent:
        # A caller reaches this function without a selection whenever it does
        # not run dispatch's own picker step — a validating dry run, or an
        # internal re-dispatch. The route still has to resolve, so fall back to
        # the routing that would have run without the picker and record why the
        # picker's answer is absent rather than refusing the whole dispatch.
        picker_selection = _picker_fallback("picker-selection-absent", "")
    if route == "picker" and picker_selection is not None:
        action = str(picker_selection.get("action") or "")
        if action == "hold":
            raise BudgetHold(
                {
                    "held": True,
                    "reason": _picker_refusal_reasons(picker_selection),
                    "picker_selection": dict(picker_selection),
                }
            )
        named_backend = (
            str(picker_selection["backend"])
            if action == "route" and picker_selection.get("backend")
            else ""
        )
        if named_backend:
            requested_backend = named_backend
        elif action in ("route", "refuse", "fallback"):
            # The picker names no backend it can route to — it refused, its route
            # carries no backend, or it fell back — so the dispatch continues
            # exactly as deterministic routing would: the configured default
            # stands in and the gates below produce their own refusal or hold.
            # A picker that finds nothing eligible must never make a refuse worse
            # than the deterministic routing it replaces. The selection stays
            # recorded on the run so the reader sees the picker found nothing.
            # A caller that already named a backend keeps its request; otherwise
            # the configured default stands in, which is the routing a
            # deterministic dispatch would have resolved on its own.
            if not selection_absent:
                requested_backend = str(config.get("default_backend") or "")
        else:
            raise CrewError(f"picker returned unknown action {action!r}")
    # The configured local lane, named here so a ``--local`` dispatch has one
    # concrete backend to agree or disagree with. The CLI has already merged it
    # into ``default_backend``, so this is the same value role routing would
    # fall through to; reading it directly is what makes the flag a request
    # rather than a silent default a member's harness can displace.
    local_backend_name = str(config.get("local_backend") or "").strip() if local else ""
    caller_declared_backend = str(
        requested_backend if declared_backend is None else declared_backend
    ).strip()
    # The command passes an empty string when its option is omitted. ``None``
    # belongs to internal callers that did not invoke that routing surface.
    if member and (
        local or backend_override is not None or default_backend_override is not None
    ):
        # A caller that named a lane and no repository reaches this function
        # with ``None``, while the launching path resolves the project's
        # registered mount before it ever gets here. Resolving the same mount
        # here keeps the validating and launching answers one decision, and a
        # project with no registered mount still refuses with the flag named.
        if repo is None:
            repo = resolve_project_repository(project, None)
        member_authority = dict(
            authority or resolve_dispatch_authority(project, Path(repo).resolve())
        )
        roster_member = ledger.member(
            project,
            member,
            root=resolve_dispatch_ledger_root(member_authority),
        )
        if roster_member is None:
            raise CrewError(
                format_refusal(
                    "D14",
                    f"project {project!r} has no crew member {member!r}; register it "
                    "with `reckon crew member add` before dispatching to it",
                )
            )
        member_harness = str(roster_member.get("harness") or "").strip()
        # A ``--local`` request names the local lane, so a member whose harness
        # is a different backend cannot run the node: refuse rather than let the
        # harness silently displace the flag and report a local run that landed
        # on a metered lane.
        if (
            local_backend_name
            and member_harness
            and member_harness != local_backend_name
        ):
            raise CrewError(
                format_refusal(
                    "D15",
                    f"--local resolves the configured local backend "
                    f"{local_backend_name!r}, but crew member {member!r} declares "
                    f"harness {member_harness!r}",
                )
            )
        if requested_backend and member_harness and requested_backend != member_harness:
            raise CrewError(
                format_refusal(
                    "D15",
                    f"dispatch requests backend {requested_backend!r}, but crew member "
                    f"{member!r} declares harness {member_harness!r}",
                )
            )
        requested_backend = requested_backend or member_harness
    # Only a dispatch with no caller or roster request may fall through to role
    # and default routing. A wrong lane that announces itself costs one
    # redispatch; a wrong lane that reports success can look merely quiet
    # indefinitely.
    section_routing: dict[str, Any] | None = None
    if requested_backend:
        backend_name, backend = resolve_role_override(
            config, node.role, node.spec_level, requested_backend
        )
    else:
        # The section's own record steers the lane, so a section that keeps
        # costing attempts lands on the class its count earns without anyone
        # deciding it by hand. A node with no readable record resolves exactly
        # as role routing always resolved it, and so does one whose rule raised
        # while being read: the failure is recorded instead of the lane, since a
        # rule that cannot be resolved must not stop a node dispatching.
        section_routing = _dispatch_section_routing(
            config,
            node=node,
            project=project,
            repo=repo,
            authority=authority,
        )
        if section_routing is None or section_routing.get("failure") is not None:
            backend_name, backend = resolve_role(config, node.role, node.spec_level)
        else:
            backend_name = str(section_routing["backend"])
            backend = dict(section_routing["backend_settings"])
    launch_kind = backend.get("launch")
    if launch_kind not in ("cli", "in-harness"):
        raise CrewError(
            format_refusal(
                "D22",
                f"backend {backend_name!r} declares launch {launch_kind!r}; "
                "expected 'cli' or 'in-harness'",
            )
        )
    # A placement's declared requirements are checked here rather than at
    # launch, so a dry run reports what a real dispatch would and the refusal
    # lands before a worktree, a pointer or a job exists. A requirement the
    # target node cannot see fails inside the worker and reads as a worker
    # defect, which is the reading this refuses to hand anyone.
    check_placement_requirements(backend.get("placement"), backend_name=backend_name)
    # Local is a property of where the dispatch actually landed, not of the
    # flag the caller passed: a request that resolved onto another backend — a
    # budget fallback, or a lane the caller named alongside the flag — is not a
    # local run and must not be recorded as one.
    local_resolved = bool(
        local and local_backend_name and backend_name == local_backend_name
    )
    default_budget = resolved_time_budget(config, backend)
    budget_ceiling = resolved_time_ceiling(config)
    default_token_budget = _resolved_token_budget(config, backend)
    node.time_budget = node.time_budget or default_budget
    node.section = normalize_section(node.section)
    resolved_run_id = run_id or new_run_id(node.id)
    durable_manifest = str(run_dir(resolved_run_id) / "manifest.md")
    caller_manifest = bool(node.manifest_path)
    node.manifest_path = node.manifest_path or durable_manifest
    warnings = list(getattr(config, "warnings", ()))
    if caller_manifest and _path_is_tmpfs(node.manifest_path):
        warnings.append(
            f"manifest path {node.manifest_path!r} is on tmpfs; use the durable "
            f"default {durable_manifest!r} so delivery survives session cleanup"
        )
    if not node.write_paths:
        node.write_paths = _resolved_write_paths(
            backend, run_directory=run_dir(resolved_run_id)
        )
    node.peer_scopes = {
        name: list(paths) for name, paths in (peer_scopes or {}).items()
    }
    execution_fit = capability.assess_execution_fit(
        node.done_when,
        role=node.role,
        execution_capable=backend.get("execution_capable"),
        override=execution_override,
    )
    verdict = validate_node(
        node, locked_decisions=locked_decisions, budget_ceiling=budget_ceiling
    )
    # A node whose scope includes a test file writes a check, and a check whose
    # author never named the mutation it must fail against is a guard that
    # passed by not exercising anything. The trigger is the declared write path
    # rather than the done-when prose, so the refusal rests on a structured
    # field the dispatcher can read.
    control_finding = negative_control_finding(node)
    if control_finding is not None:
        verdict = NodeValidation(
            ok=False, findings=[*verdict.findings, control_finding]
        )
    # The gate command is the check the brief tells the worker to run, so a
    # population that names no file the repository holds is caught here, where
    # it costs a refusal, rather than inside the worker, where it costs the
    # worker's judgement about which substitute the coordinator meant. A
    # caller that named no repository has no store to ask and is left alone.
    if repo is not None:
        population_finding = gate_population_finding(
            node, repository=Path(repo).resolve()
        )
        if population_finding is not None:
            verdict = NodeValidation(
                ok=False, findings=[*verdict.findings, population_finding]
            )
    if not execution_fit.allowed:
        verdict = NodeValidation(
            ok=False,
            findings=[
                *verdict.findings,
                {
                    "property": "fully-specified",
                    "detail": execution_fit.refusal_detail(),
                },
            ],
        )
    # Judged on the scope the node itself declares, before the shared landing
    # paths are appended to it below. The three of those are dispatch's own
    # bookkeeping -- every node on a plan carries them -- so counting them as
    # the node's artifacts would score every node on a plan above the
    # prescribed band the bar's local outcome is defined by, and the band would
    # describe nothing rather than the work.
    open_endedness = open_endedness_score(node)
    resolved_authority: dict[str, Any] | None = None
    sandbox_write_roots: tuple[Path, ...] | None = None
    if verdict.ok and repo is not None:
        resolved_authority = dict(
            authority or resolve_dispatch_authority(project, repo)
        )
        # A landing grant is for a role that lands work in the tree. Sandbox
        # writability is necessary but not sufficient: a verifier role's
        # sandbox can write the worktree (the `test` role is `worktree-full`),
        # yet promotion refuses a verifier commit that touches any repository
        # path. Gating on the role predicate as well keeps the default fragment
        # scope and the promotion refusal reading one authority, so a verifier
        # is never offered a repository write path it cannot land.
        if role_may_write_repository_paths(node.role) and _can_write_worktree(
            backend,
            repository=Path(repo).resolve(),
            run_directory=run_dir(resolved_run_id),
        ):
            _grant_landing_write_paths(
                node,
                project=project,
                authority=resolved_authority,
                warnings=warnings,
            )
        _require_write_paths_in_authority(node, resolved_authority)
        if node.brief.strip():
            # A brief is authority text whether it stands alone or beside a
            # plan section, so its digest is taken either way. It is taken
            # from the run's own stored copy whenever dispatch has made one —
            # a later read (a lane change, a resume) rebuilds the node from the
            # pointer, and the source path a coordinator handed in may be a
            # scratch file no longer on disk. At first dispatch no copy exists
            # yet, so the source is read.
            node.brief_sha256 = _brief_digest(node.brief_path or node.brief)
        if not node.plan.strip():
            # A brief alone names no committed plan section, so the gates that
            # join a node to a base blob and a stored review have nothing to
            # read and are skipped around their call sites rather than inside
            # the shared gate. A brief beside a plan section takes the plan
            # branch below: the plan is still the authority the gates read.
            resolved_authority["plan"] = {
                **resolved_authority["plan"],
                "base_sha": "",
            }
        else:
            plan_commit = require_plan_section_visible(
                node=node,
                project=project,
                repo=repo,
                base=base,
                authority=resolved_authority,
            )
            review_warning = require_plan_reviewed(
                node=node,
                project=project,
                repo=repo,
                authority=resolved_authority,
                allow_unreviewed=allow_unreviewed_plan,
                enforce=flight.plan_review_gate_enforces(config),
            )
            if review_warning is not None:
                warnings.append(review_warning)
            resolved_authority["plan"] = {
                **resolved_authority["plan"],
                "base_sha": plan_commit,
            }
            overlap_warning = _done_when_plan_overlap_warning(
                node=node,
                project=project,
                authority=resolved_authority,
                plan_commit=plan_commit,
            )
            if overlap_warning is not None:
                warnings.append(overlap_warning)
        sandbox_write_roots, sandbox_findings = _sandbox_reachability(
            node,
            backend=backend,
            repository=Path(repo).resolve(),
            run_directory=run_dir(resolved_run_id),
        )
        if sandbox_findings:
            verdict = NodeValidation(
                ok=False,
                findings=[*verdict.findings, *sandbox_findings],
            )
    lane_declaration: dict[str, Any] | None = None
    lane_advisory: dict[str, Any] | None = None
    if verdict.ok:
        ledger_root = (
            resolve_dispatch_ledger_root(resolved_authority)
            if resolved_authority is not None
            else None
        )
        observation = (
            _dispatch_lane_observation(
                project,
                root=ledger_root,
                config=config,
                backend_name=backend_name,
                backend=backend,
            )
            if backend.get("budget_check")
            else None
        )
        lane_declaration = _lane_declaration_evidence(
            declared_backend=caller_declared_backend,
            resolved_backend=backend_name,
            observation=observation,
        )
        lane_advisory = _dispatch_lane_advisory(
            backend_name=backend_name,
            metered=not ledger.is_unmetered_backend(backend_name),
            observation=observation,
            node=node,
            cheaper_lane={"lane": None, "state": "not_evaluated", "detail": ""},
        )
        if lane_advisory["state"] == "emitted":
            lane_advisory["cheaper_lane"] = _lane_advisory_cheaper_lane(
                _lane_advisory_ledger_runs(project, ledger_root),
                resolved_lane=backend_name,
                role=node.role,
                spec_level=node.spec_level,
                configured_lanes=sorted(
                    str(name) for name in (config.get("backends") or {})
                ),
            )
        if backend.get("budget_check") and not caller_declared_backend:
            verdict = NodeValidation(
                ok=False,
                findings=[
                    *verdict.findings,
                    _lane_declaration_finding(
                        backend_name=backend_name,
                        observation=observation,
                        alternatives=_unmetered_dispatch_alternatives(
                            config, role=node.role, spec_level=node.spec_level
                        ),
                    ),
                ],
            )
    if not verdict.ok:
        verdict = NodeValidation(
            ok=False,
            findings=[
                {
                    **finding,
                    "detail": format_refusal("D07", str(finding["detail"])),
                }
                for finding in verdict.findings
            ],
        )
    lane_reading = _dispatch_lane_reading(backend)
    lane_gate = _dispatch_lane_gate(backend)
    lane_allowance = _dispatch_lane_allowance(backend, session=session)
    orchestrator_lane_stop = _dispatch_orchestrator_lane_stop(
        backend_name=backend_name,
        backend=backend,
        config=config,
        role=node.role,
        spec_level=node.spec_level,
    )
    reason = (
        None
        if orchestrator_lane_reason is None
        else str(orchestrator_lane_reason).strip()
    )
    if reason == "":
        raise CrewError("--allow-orchestrator-lane requires a non-empty reason")
    if reason and orchestrator_lane_stop["state"] != "declared":
        raise CrewError(
            f"resolved lane {backend_name!r} does not declare "
            f"{ORCHESTRATOR_LANE_DECLARATION_KEY}; --allow-orchestrator-lane "
            "override does not apply"
        )
    orchestrator_lane_override = None
    if reason and orchestrator_lane_stop["state"] == "declared":
        orchestrator_lane_override = {"lane": backend_name, "reason": reason}
        orchestrator_lane_stop = {
            **orchestrator_lane_stop,
            "state": "overridden",
            "severity": "overridden",
            "reason": reason,
        }
    resolution = DispatchPlan(
        run_id=resolved_run_id,
        backend=backend_name,
        launch=str(launch_kind),
        backend_settings=backend,
        node=node,
        budget_ceiling=budget_ceiling,
        token_budget=default_token_budget,
        validation=verdict,
        execution_fit=execution_fit,
        local=local_resolved,
        warnings=warnings,
        done_when_warnings=done_when_warnings(node.done_when),
        authority=resolved_authority,
        requested_backend=requested_backend or None,
        default_backend=str(config.get("default_backend") or "") or None,
        section_routing=(
            None
            if section_routing is None
            else _section_routing_evidence(section_routing)
        ),
        lane_declaration=lane_declaration,
        lane_reading=lane_reading,
        lane_gate=lane_gate,
        lane_allowance=lane_allowance,
        orchestrator_lane_stop=orchestrator_lane_stop,
        orchestrator_lane_override=orchestrator_lane_override,
        lane_advisory=lane_advisory,
        open_endedness=open_endedness,
        # A resolved plan records the picker answer only when the route used it.
        # A shadow (or deterministic) preview reads the same whatever the picker
        # happened to say, so two dry runs of one node differ in no field the
        # picker touched; a routed preview keeps the decision it routed by, minus
        # the per-call measurements that differ between two asks.
        picker_selection=(
            _reportable_picker_selection(picker_selection)
            if route == "picker"
            else None
        ),
        route=route,
        route_override=route_override,
    )
    if orchestrator_lane_stop["state"] == "declared":
        # The stop reaches the warnings a caller reads and the record a later
        # reader opens, so a run that spent an orchestrator lane carries the
        # fact whether or not anyone read the dispatch payload at the time.
        resolution.warnings.append(_orchestrator_lane_stop_line(orchestrator_lane_stop))
    if verdict.ok and repo is not None:
        resolution.competence = _competence_verdict(
            resolution=resolution, project=project, repo=Path(repo).resolve()
        )
        if report_live_conflicts:
            repo_root = Path(repo).resolve()
            claims = _repository_scope_claims()
            # The directory-claim judgement reads exactly the rows the
            # exclusive-claim walk produced at base, so the granted report
            # below adds rows to the record but nothing it adds can drop a
            # refusal or judge a collision the walk never saw.
            refusal_rows = _live_conflict_rows(
                node,
                project=project,
                repo=repo_root,
                authority=resolved_authority,
                claims=claims,
                disregarded=resolution.warnings,
                include_granted_landing=False,
            )
            resolution.live_conflicts = _live_conflict_rows(
                node,
                project=project,
                repo=repo_root,
                authority=resolved_authority,
                claims=claims,
                disregarded=resolution.warnings,
            )
            directory_rows = [
                row
                for row in refusal_rows
                if _live_conflict_is_a_directory_claim(row, repo_root)
            ]
            if directory_rows:
                if accept_directory_claim:
                    resolution.directory_claim_acceptances = [
                        {
                            "candidate_path": entry["left_path"],
                            "claimed_path": row["claimed_path"],
                            "run_id": row["run_id"],
                            "node": row["node"],
                            "project": row.get("project", project),
                        }
                        for row in directory_rows
                        for entry in row["paths"]
                    ]
                    resolution.warnings.extend(
                        _directory_claim_acceptance_line(entry)
                        for entry in resolution.directory_claim_acceptances
                    )
                else:
                    for row in directory_rows:
                        for entry in row["paths"]:
                            alternatives = _directory_claim_alternatives(
                                node,
                                repo=repo_root,
                                candidate=entry["left_path"],
                                claim_path=row["claimed_path"],
                            )
                            resolution.warnings.append(
                                _directory_claim_warning_line(
                                    candidate=entry["left_path"],
                                    claimed_path=row["claimed_path"],
                                    run_id=row["run_id"],
                                    node_id=row["node"],
                                    alternatives=alternatives,
                                )
                            )
                    resolution.validation = NodeValidation(
                        ok=False,
                        findings=[
                            *resolution.validation.findings,
                            {
                                "property": "write-scope",
                                "detail": (
                                    "a declared directory write path overlaps a "
                                    "live claim; declare the files the brief "
                                    "names, or pass --accept-directory-claim"
                                ),
                            },
                        ],
                    )
            try:
                _raise_repository_scope_conflict(
                    node,
                    project=project,
                    repo=repo_root,
                    authority=resolved_authority,
                    claims=claims,
                    disregarded=resolution.warnings,
                    **_directory_claim_acceptance_kwargs(accept_directory_claim, []),
                )
            except ScopeConflict as exc:
                resolution.admission = {
                    "state": "refused",
                    "error": "scope-conflict",
                    "detail": str(exc),
                    "conflicting_run_id": exc.run_id,
                    "conflicting_node_id": exc.node_id,
                    "candidate_path": exc.candidate_path,
                    "claimed_path": exc.claimed_path,
                }
            else:
                resolution.admission = {"state": "admitted"}
    resolution.sandbox_write_roots = sandbox_write_roots
    # A dry run must reach the verdict a real dispatch reaches, so the watcher
    # gate is evaluated here too when the caller asks for it. It reads the
    # watcher state and never starts a producer — arming is the real dispatch's
    # effect, and a validating caller must not leave one behind. An attached
    # session passes, a released one is warned and proceeds, and one that never
    # registered a follower is refused, exactly as the launching path decides.
    if watch_required and not watch_override and session and str(launch_kind) == "cli":
        preview = watch_state(project, session=session)
        # Only a live watcher settles the delivery question; without a producer
        # the real dispatch may still arm one, so a validating caller leaves the
        # verdict to the launch rather than reporting a refusal it cannot know.
        if preview["watcher_live"]:
            delivery = "monitor"
            attached = bool(preview["session_attached"])
            # The real admission reports host delivery in two cases, and the
            # dry run mirrors both without its one forbidden write. A waiting
            # host attaches the session on the real request, so the delivery
            # that request would reach is read from the host's own liveness
            # rather than by asking it. A session a follower already runs is
            # host delivery only when the host's own census names that follower,
            # which is how the real dispatch tells a host's follower from one a
            # coordinator armed by hand.
            if not attached and _session_host_waiting():
                delivery = "host"
                attached = True
            elif _session_host_runs_follower(
                project, session, (preview.get("follower") or {}).get("pid")
            ):
                delivery = "host"
            admission = _watcher_delivery_admission(
                project,
                {**dict(preview), "session_attached": attached},
                session=session,
                launch_kind=str(launch_kind),
                delivery=delivery,
            )
            if admission:
                resolution.warnings.append(admission)
            resolution.watch = {
                "delivery": delivery,
                "predicted": True,
                "watcher_live": True,
                "session": session,
                "session_attached": attached,
            }
    return resolution


def shadow_source(
    run_id: str,
    *,
    repo: str | Path,
) -> dict[str, Any]:
    """Resolve one committed primary and reconstruct its shadow node."""
    repo_root = Path(repo).resolve()
    projects = mounted_repository_projects().get(repo_root, ())
    if not projects:
        state_root = repo_root / "docs" / "state"
        projects = (
            tuple(
                sorted(
                    path.name
                    for path in state_root.iterdir()
                    if path.is_dir() and (path / "crew.json").is_file()
                )
            )
            if state_root.is_dir()
            else ()
        )
    matches = [
        (project, record)
        for project in projects
        for record in ledger.runs(project, root=repo_root)
        if str(record.get("run_id") or "") == run_id
    ]
    if not matches:
        raise CrewError(
            format_refusal(
                "D20",
                f"run {run_id!r} is not a committed ledger record in repository "
                f"{repo_root}",
            )
        )
    if len(matches) > 1:
        raise CrewError(
            format_refusal(
                "D20",
                f"run {run_id!r} appears in more than one project ledger in "
                f"{repo_root}",
            )
        )
    project, primary = matches[0]
    lineage = primary.get("lineage")
    if isinstance(lineage, Mapping) and lineage.get("kind") == "shadow":
        raise CrewError(
            format_refusal(
                "D20",
                f"run {run_id!r} is itself a shadow and cannot be a shadow parent",
            )
        )
    agent = primary.get("agent")
    if not isinstance(agent, Mapping) or not agent:
        raise CrewError(
            format_refusal(
                "D20",
                f"committed run {run_id!r} has no recorded agent configuration; "
                "the shadow cannot inherit a configuration without guessing",
            )
        )
    definition = primary.get("node_definition")
    if not isinstance(definition, Mapping):
        raise CrewError(
            format_refusal(
                "D20",
                f"committed run {run_id!r} has no stored node definition and cannot "
                "be shadowed without re-authoring its contract",
            )
        )
    required = ("id", "goal", "plan", "done_when", "write_paths")
    missing = [name for name in required if not definition.get(name)]
    if missing:
        raise CrewError(
            format_refusal(
                "D20",
                f"committed run {run_id!r} has an incomplete stored node definition: "
                + ", ".join(missing),
            )
        )
    base_sha = str(primary.get("base_sha") or "")
    if not base_sha:
        raise CrewError(
            format_refusal("D20", f"committed run {run_id!r} records no base_sha")
        )
    node = TaskNode(
        id=str(definition["id"]),
        goal=str(definition["goal"]),
        plan=str(definition["plan"]),
        section=str(definition.get("section") or ""),
        brief=str(definition.get("brief") or ""),
        brief_sha256=str(definition.get("brief_sha256") or ""),
        brief_path=str(definition.get("brief_path") or ""),
        role=str(definition.get("role") or primary.get("role") or "implement"),
        spec_level=str(definition.get("spec_level") or primary.get("spec_level") or ""),
        done_when=str(definition["done_when"]),
        write_paths=[str(path) for path in definition.get("write_paths") or ()],
        negative_control=str(definition.get("negative_control") or ""),
        estimated_hours=definition.get("estimated_hours"),
        requires_decisions=[
            str(key) for key in definition.get("requires_decisions") or ()
        ],
    )
    return {
        "project": str(project),
        "primary": dict(primary),
        "node": node,
        "base_sha": base_sha,
    }


def _shadow_dispatch_config(
    *,
    config: Mapping[str, Any],
    node: TaskNode,
    primary_agent: Mapping[str, Any],
    candidate_backend: str,
    configuration_overrides: Iterable[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve a candidate while retaining unmodified primary agent settings."""
    resolved_backend, candidate = resolve_role(config, node.role, node.spec_level)
    if resolved_backend != candidate_backend:
        raise CrewError(
            format_refusal(
                "D21",
                f"candidate backend {candidate_backend!r} resolved to "
                f"{resolved_backend!r}; route the node explicitly to the candidate",
            )
        )

    explicit = {str(config_key) for config_key in configuration_overrides}
    effective = dict(candidate)
    for config_key in ("effort", "sandbox"):
        if config_key not in explicit:
            effective[config_key] = primary_agent.get(config_key)

    shadow_agent = _agent_configuration(
        candidate_backend, str(effective.get("launch") or ""), effective
    )
    substituted: dict[str, dict[str, Any]] = {}
    inherited: dict[str, Any] = {}
    for config_key in ("backend", "launch", "model", "effort", "sandbox"):
        before = primary_agent.get(config_key)
        after = shadow_agent.get(config_key)
        via = (
            "backend"
            if config_key == "backend"
            else "override"
            if config_key in explicit
            else ""
        )
        if config_key in ("launch", "model") and before != after:
            via = "backend"
        if via:
            substituted[config_key] = {
                "primary": before,
                "shadow": after,
                "via": via,
            }
        else:
            inherited[config_key] = after

    override_evidence: dict[str, dict[str, Any]] = {}
    backend_layer = config.get("backends", {}).get(candidate_backend, {})
    role_layer = config.get("roles", {}).get(node.role, {})
    level_layer = (
        role_layer.get("by_spec_level", {}).get(node.spec_level, {})
        if isinstance(role_layer, Mapping)
        else {}
    )
    for config_key in sorted(explicit):
        layers = {
            name: layer[config_key]
            for name, layer in (
                ("backend", backend_layer),
                ("role", role_layer),
                ("spec_level", level_layer),
            )
            if isinstance(layer, Mapping) and config_key in layer
        }
        override_evidence[config_key] = {
            "layers": layers,
            "resolved": shadow_agent.get(config_key, effective.get(config_key)),
        }

    shadow_config = dict(config)
    backends = dict(config.get("backends") or {})
    backends[candidate_backend] = effective
    shadow_config["backends"] = backends
    roles = dict(config.get("roles") or {})
    roles[node.role] = {"backend": candidate_backend}
    shadow_config["roles"] = roles
    return shadow_config, {
        "substituted": substituted,
        "inherited": inherited,
        "overrides": override_evidence,
        "resolved": {"effort": shadow_agent.get("effort")},
    }


def shadow(
    run_id: str,
    *,
    candidate_backend: str,
    config: Mapping[str, Any],
    repo: str | Path,
    session: str,
    wave: str = "",
    member: str = "",
    configuration_overrides: Iterable[str] = (),
    dry_run: bool = False,
    launcher=None,
) -> dict[str, Any]:
    """Dispatch a committed node at its original base as isolated evidence."""
    source = shadow_source(run_id, repo=repo)
    project = source["project"]
    node = source["node"]
    base_sha = source["base_sha"]
    primary = source["primary"]
    dispatching_session = str(session)
    if not dispatching_session:
        raise CrewError(
            format_refusal(
                "D20", "shadow needs a dispatching session on its request or primary"
            )
        )
    wave_id = str(wave or primary.get("wave") or "")
    primary_agent = primary["agent"]
    explicit = {str(config_key) for config_key in configuration_overrides}
    shadow_config, comparison = _shadow_dispatch_config(
        config=config,
        node=node,
        primary_agent=primary_agent,
        candidate_backend=candidate_backend,
        configuration_overrides=explicit,
    )
    _backend_name, shadow_backend = resolve_role(
        shadow_config, node.role, node.spec_level
    )
    role_time_budget = resolved_time_budget(shadow_config, shadow_backend)
    primary_time_budget = str(primary.get("time_budget") or "")
    if "time_budget" in explicit:
        node.time_budget = role_time_budget
        comparison["substituted"]["time_budget"] = {
            "primary": primary_time_budget or None,
            "shadow": role_time_budget,
            "via": "override",
        }
    elif primary_time_budget:
        node.time_budget = primary_time_budget
        comparison["inherited"]["time_budget"] = primary_time_budget
    else:
        node.time_budget = role_time_budget
        comparison["inherited"]["time_budget"] = role_time_budget
        comparison["fallbacks"] = {
            "time_budget": {
                "source": "resolved_role_default",
                "value": role_time_budget,
            }
        }
    comparison["resolved"]["time_budget"] = node.time_budget
    if "time_budget" in explicit:
        backend_layer = config.get("backends", {}).get(candidate_backend, {})
        comparison["overrides"]["time_budget"] = {
            "layers": (
                {"backend": backend_layer["time_budget"]}
                if isinstance(backend_layer, Mapping) and "time_budget" in backend_layer
                else {}
            ),
            "resolved": node.time_budget,
        }
    worktree_component = uuid.uuid4().hex[:12]
    lineage = {
        "kind": "shadow",
        "primary_run_id": run_id,
        "worktree_component": worktree_component,
        "configuration": comparison,
    }
    if dry_run:
        resolution = plan_dispatch(
            node=node,
            config=shadow_config,
            locked_decisions=node.requires_decisions,
            peer_scopes={},
            project=project,
            repo=repo,
            base=base_sha,
            backend_override=_backend_name,
            # A shadow names its candidate backend, so the picker has nothing to
            # select and asking it would only be refused.
            route="deterministic",
        )
        return {
            "dry_run": True,
            "primary_run_id": run_id,
            "project": project,
            "base_sha": base_sha,
            "lineage": lineage,
            **resolution.as_dict(),
        }
    return dispatch(
        node=node,
        project=project,
        repo=repo,
        config=shadow_config,
        session=dispatching_session,
        wave=wave_id,
        worktree_session=shadow_worktree_session(
            run_id, _backend_name, worktree_component
        ),
        base=base_sha,
        locked_decisions=node.requires_decisions,
        peer_scopes={},
        member=member,
        launcher=launcher,
        lineage_override=lineage,
        backend_override=_backend_name,
        route="deterministic",
    )


def _session_unreconciled_refusal(
    runs: Iterable[Mapping[str, Any]],
    peer_runs: Iterable[Mapping[str, Any]],
    grace: str,
) -> UnreconciledRuns:
    """Build an own-session refusal that keeps observed peer rows visible."""
    refusal = UnreconciledRuns(runs, grace)
    refusal.peer_runs = [dict(row) for row in peer_runs]
    if refusal.peer_runs:
        peer_lines = "\n".join(
            f"- {row['run_id']} (session {(row.get('session') or '<unknown>')!s}): visible, not counted"
            for row in refusal.peer_runs
        )
        peer_heading = (
            f"{refusal!s}\nPeer-session unreconciled runs observed but not counted "
        )
        refusal.args = (peer_heading + f"toward this session's refusal:\n{peer_lines}",)
    return refusal


def _resolved_wave_id(project: str, session: str, requested: str) -> str:
    """Return an explicit wave or the newest non-empty wave in this session."""
    if requested:
        return requested
    for record in reversed(list_live(project=project)):
        if str(record.get("session") or "") != session:
            continue
        if wave := str(record.get("wave") or ""):
            return wave
    return f"wave-{uuid.uuid4().hex}"


# How much of its own shape a node leaves a worker to decide, per declared
# input, each banded on that one axis. Declared here rather than inferred from
# a node's name, and declared as a total order over each vocabulary so a score
# can be compared against a bar that moves. A value outside a map bands at the
# middle rather than at an extreme: an unrecognised level or role is not
# evidence of a maximally open-ended node, and scoring one as though it were
# would move a dispatch off the metered lane on a typo.
SPEC_LEVEL_OPENNESS = {"exact": 0.0, "guided": 0.5, "open": 1.0}
ROLE_OPENNESS = {
    "cleanup": 0.0,
    "documentation": 0.0,
    "review": 0.0,
    "test": 0.0,
    "verify": 0.0,
    "implement": 0.5,
    "design": 1.0,
    "investigate": 1.0,
}
UNKNOWN_OPENNESS = 0.5


def open_endedness_score(node: TaskNode) -> float:
    """Score how much of its own shape a node leaves a worker to decide.

    The bar admits a node to the metered lane on this score, so it is read from
    what dispatch already holds about the node and nothing else: the
    specification level the node declares, its role, and how completely it is
    prescribed. The last is the prescription module's own judgement, read rather
    than restated -- it names every property the node fails, and the score reads
    that fraction rather than a second opinion about it.

    The prescription verdict selects the band and the declared inputs order a
    node within it. That is the arrangement in which the two constants the
    design already fixes agree exactly: the prescription module decides whether
    a node is prescribed at all, and the bar's ``PRESCRIBED_MAX`` is the top of
    the band a prescribed node scores inside. So a node failing any property
    scores above the band however fixed its level and role look, which is what
    keeps work that is not prescribed off the free lane, and a node failing none
    scores inside it however open those look, which is what makes prescription
    decidable before the window is read and a prescribed node never held at a
    full one. Below the band the third input is identically zero, so the two
    declared inputs are averaged across the band's own width; above it the
    failures take half the remaining scale and the declared inputs the other
    half, since the third input is the one the evidence behind the score is
    about and it is what the two bands are told apart by.

    The score is reported rounded, because rows are compared against one another
    and last-bit noise would make two identically shaped nodes read as
    different. A maximally prescribed node scores exactly zero and a node
    leaving everything to invent scores exactly one, so the two ends the bar
    names are the two ends of this scale rather than approximations of them.
    """
    prescribed = prescription_module.judge_prescribed(node)
    failures = [str(name) for name in prescribed.get("failures") or ()]
    properties = prescription_module.PRESCRIBED_PROPERTIES
    declared = (
        SPEC_LEVEL_OPENNESS.get(
            str(node.spec_level or "").strip().lower(), UNKNOWN_OPENNESS
        )
        + ROLE_OPENNESS.get(str(node.role or "").strip().lower(), UNKNOWN_OPENNESS)
    ) / 2.0
    if not failures:
        return round(bar_module.PRESCRIBED_MAX * declared, 6)
    failed = len(failures) / len(properties) if properties else 0.0
    return round(
        bar_module.PRESCRIBED_MAX
        + (1.0 - bar_module.PRESCRIBED_MAX) * (declared + failed) / 2.0,
        6,
    )


def _plan_impl_at_dispatch(
    project: str, plan: str, root: str | Path | None
) -> float | None:
    """Read the authored implementation fraction for promotion-time comparison.

    Imported lazily because the promotion module imports this one at module
    load; the reader lives there so the value recorded here and the value
    compared at promotion come from one implementation.
    """
    from reckon.crew.promotion import plan_impl_at

    return plan_impl_at(project, plan, root)


def _repairs_ledger_root(
    project: str, authority: Mapping[str, Any] | None
) -> Path | None:
    """Resolve the checkout owning the ledger a --repairs target is read from.

    The dispatch path already carries a resolved authority; a dry run may not,
    so the project's registered docs mount is the fallback. ``None`` leaves
    ``ledger.load`` its config-home default rather than making the refusal
    depend on a resolution that failed for an unrelated reason.
    """
    if authority is not None:
        return resolve_dispatch_ledger_root(authority)
    try:
        docs = flight.mounted_project_docs().get(project)
    except flight.FlightConfigError:
        return None
    if docs is None:
        return None
    return docs.parent.resolve()


def _require_repairs_target(
    project: str, repairs: str, *, authority: Mapping[str, Any] | None
) -> None:
    """Refuse a --repairs target that is not a promoted run of this project.

    A repair declares that the plan movement its predecessor produced carries
    forward, so the named run must already be in this project's ledger — the
    bookkeeping it inherits exists only for a run that has landed. An unknown
    run, a run still in flight, and a run from another project are three
    distinct mistakes, refused separately so the caller reads which one it made
    rather than a generic rejection. A ledger that cannot be read is a fourth:
    the target may well be promoted in it, so refusing it as an unknown run
    would state a cause the check never established. That refusal names the
    ledger and the error the read reported.
    """
    target = str(repairs or "").strip()
    if not target:
        return
    root = _repairs_ledger_root(project, authority)
    if _SAFE_ID.fullmatch(target):
        try:
            data, _version = ledger.load(project, root=root)
        except (ledger.LedgerError, _store.CorruptEnvelopeError) as exc:
            raise CrewError(
                f"--repairs {target!r} cannot be checked: the ledger at "
                f"{ledger.ledger_path(project, root)} could not be read ({exc}); "
                "a ledger whose history cannot be read is not proof the run is "
                "absent, so repair the ledger before dispatching a repair "
                "against it"
            ) from exc
        if any(
            isinstance(row, Mapping) and str(row.get("run_id")) == target
            for row in data.get("runs", [])
        ):
            return
        try:
            pointer = read_pointer(target)
        except CrewError:
            pointer = None
        if isinstance(pointer, Mapping):
            owner = str(pointer.get("project") or "").strip()
            if owner and owner != project:
                raise CrewError(
                    f"--repairs {target!r} belongs to project {owner!r}, not "
                    f"{project!r}; a repair is declared against this project's "
                    "own ledger"
                )
            raise CrewError(
                f"--repairs {target!r} is not yet promoted in project "
                f"{project!r}; a repair names a run that has already landed, "
                "so retire it with `reckon crew complete` first"
            )
    raise CrewError(
        f"--repairs {target!r} names no run in project {project!r}'s ledger; the "
        "promoted run this dispatch would repair does not exist"
    )


def _lane_allowance_unknown(detail: str) -> dict[str, Any]:
    """The allowance decision when no slot figure and no headroom could be read."""
    return {
        "state": "unknown",
        "allowance": None,
        "source": "none",
        "rests_on_observed_window": False,
        "held": False,
        "verdict": _lane_document.UNKNOWN,
        "headroom": None,
        "session": "",
        "reason": detail,
        "detail": detail,
    }


# The router averages its slot arithmetic over a window it reports as
# ``observed_seconds``, and it reports that window from its first reading. A
# slot figure is therefore used only when that window is positive: a zero or
# negative figure states that no window has been observed, and a figure resting
# on no history is read exactly as a block that states no window at all, so the
# allowance falls back to headroom, which needs none.
def _lane_worker_allowance(document: object, *, session: str) -> dict[str, Any]:
    """Choose the extra-worker allowance the lane's router grants this session.

    The router's own arithmetic is the authority and its own preference orders
    the choice, most specific first: the session's share from the admission
    block's ``sessions`` map when that map lists this session; the
    ``new_session_worker_slots`` share when the map is present and does not
    list it; the global ``worker_slots``; and, only when no slot figure is
    published, the request ``headroom`` read as a worker count. A slot figure
    is used as the router published it once the block states a positive window
    it was averaged over; a window of zero or below is no history, so the
    allowance falls back to headroom, which needs none, exactly as it does when
    the block states no window at all.

    An allowance of zero or less *holds*: the router has granted no room and
    the reason names the router's own verdict. An allowance that could not be
    read holds nothing, because absence of a signal is not exhaustion. Reckon
    does no fairness arithmetic of its own: every figure carried here is one
    the router published.

    The ``source`` label is prose for a reader. A caller that must decide
    *whether* the figure rests on observed history reads
    ``rests_on_observed_window`` instead: the structured field is true exactly
    when the allowance was taken from a router slot figure inside the block
    that states the window it was averaged over, and it stays true for every
    spelling of the label, so no caller needs to match the label's text.
    """
    reading = _lane_document.read_lane_document(document)
    admission = _lane_document.read_lane_admission(document)
    headroom = _metric_number(reading.get("headroom"))
    verdict = str(reading.get("admission_verdict") or _lane_document.UNKNOWN)
    verdict_reason = str(reading.get("admission_reason") or "")
    session_id = str(session or "").strip()

    observed = admission.get(_lane_document.ADMISSION_OBSERVED_SECONDS_KEY)
    history_is_trusted = (
        isinstance(observed, (int, float))
        and not isinstance(observed, bool)
        and observed > 0
    )

    allowance: int | float | None = None
    source = "none"
    if admission.get("present") and history_is_trusted:
        sessions_present = bool(admission.get("sessions_present"))
        listed = (
            admission["sessions"].get(session_id)
            if sessions_present and session_id
            else None
        )
        if listed is not None:
            share = _metric_number(listed.get("worker_slots"))
            if share is not None:
                allowance = share
                source = "the session's own worker slots"
        elif sessions_present:
            share = _metric_number(
                admission.get(_lane_document.ADMISSION_NEW_SESSION_WORKER_SLOTS_KEY)
            )
            if share is not None:
                allowance = share
                source = "the new-session worker slots"
        if allowance is None:
            share = _metric_number(
                admission.get(_lane_document.ADMISSION_WORKER_SLOTS_KEY)
            )
            if share is not None:
                allowance = share
                source = "the global worker slots"
    # Captured before the headroom fallback: only a figure taken inside the
    # trusted-window block above rests on observed history, and headroom --
    # which needs no window -- never does.
    rests_on_observed_window = allowance is not None

    if allowance is None and headroom is not None:
        allowance = headroom
        source = "the request headroom"

    if allowance is None:
        detail = str(admission.get("detail") or reading.get("detail") or "").strip()
        detail = detail or "no worker-slot figure and no headroom were published"
        return _lane_allowance_unknown(detail) | {"session": session_id}

    held = allowance <= 0
    grant = (
        f"the lane's router grants {allowance:g} extra workers to session "
        f"{session_id or 'unidentified'} ({source})"
    )
    if held:
        reason = f"{grant}; the router's own verdict is {verdict}"
        if verdict_reason and verdict_reason != _lane_document.UNKNOWN:
            reason = (
                f"{grant}; the router's own verdict is {verdict} — {verdict_reason}"
            )
        state = "held"
    else:
        reason = grant
        detail = (
            reason
            if admission.get("present")
            else f"{reason}; no admission block was published"
        )
        state = "measured"
    return {
        "state": state,
        "allowance": allowance,
        "source": source,
        "rests_on_observed_window": rests_on_observed_window,
        "held": held,
        "verdict": verdict,
        "headroom": headroom,
        "session": session_id,
        "reason": reason,
        "detail": reason if held else detail,
    }


def _dispatch_lane_allowance(
    backend: Mapping[str, Any], *, session: str
) -> dict[str, Any] | None:
    """Read the resolved lane's published allowance for this coordinator session.

    A backend may declare ``lane_document``, the local JSON the lane publishes
    about itself. The document is resolved through the shared lane reader and
    the allowance is chosen from the router's own figures. An absent
    declaration returns ``None`` -- a lane that publishes nothing has nothing
    to hold on -- and a document that cannot be read or parsed resolves to an
    unknown allowance that holds nothing, because absence of a signal is not
    exhaustion.
    """
    declared = backend.get("lane_document")
    if not declared:
        return None
    path = Path(str(declared)).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return _lane_allowance_unknown(
            f"lane document {str(path)!r} cannot be read — {exc}"
        )
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return _lane_allowance_unknown(
            f"lane document {str(path)!r} is not valid JSON — {exc}"
        )
    if not isinstance(payload, Mapping):
        return _lane_allowance_unknown(
            f"lane document {str(path)!r} is not a JSON object"
        )
    return _lane_worker_allowance(payload, session=session)


def dispatch(
    *,
    node: TaskNode,
    project: str,
    repo: str | Path | None,
    config: Mapping[str, Any],
    session: str,
    wave: str = "",
    base: str = "HEAD",
    locked_decisions: Iterable[str] = (),
    peer_scopes: Mapping[str, Iterable[str]] | None = None,
    member: str = "",
    launcher=None,
    check_budget: bool = True,
    budget_state: Mapping[str, Any] | None = None,
    execution_override: bool = False,
    orchestrator_lane_reason: str | None = None,
    unreconciled_override: bool = False,
    unreviewed_plan_override: bool = False,
    watch_required: bool = False,
    watch_override: bool = False,
    lineage_override: Mapping[str, Any] | None = None,
    worktree_session: str | None = None,
    local: bool = False,
    backend_override: str | None = None,
    default_backend_override: str | None = None,
    repairs: str = "",
    accept_directory_claim: bool = False,
    no_fence_reason: str = "",
    route: str | None = None,
    comment: str = "",
    picker_selection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate, prepare and launch one node; return its run record.

    The single branch is on launch kind. A ``cli`` backend is spawned here and
    the caller yields on the returned run id. An ``in-harness`` backend cannot
    be spawned by reckon at all, so everything a worker needs is prepared and
    returned as a directive the calling harness dispatches itself, binding its
    task back with :func:`attach`.

    Naming a roster ``member`` routes the node into that member's long-lived
    session, and that member's own in-flight run refuses a second dispatch to
    it. Omitting it makes the dispatch disposable: the run carries its own
    identity and registers no roster row, so no unrelated task is refused for
    the member another run in flight happens to hold. Two dispatches of one
    node for the same project, session and node id are serialised by an
    exclusive claim over the node's worktree path: the second is refused
    before any worktree exists, naming the in-flight dispatch, so exactly one
    of two concurrent dispatches launches.

    A node whose backend has no headroom left is *held* rather than dispatched:
    :class:`BudgetHold` is raised before any worktree exists, so the node stays
    ready and nothing has to be judged or unwound. Holding costs nothing; a wave
    launched into a spent quota costs its whole setup plus half-finished commits.

    Either way the operation is atomic: a failure after the worktree exists
    removes it and writes no pointer, so no orphan is left holding write scope.
    An execution-fit override is an explicit exception to a heuristic refusal;
    the matched measure and resolved role stay on the run record so the exception
    remains visible after the request that supplied it is gone.

    An unreconciled-run override is narrower: it waives only the terminal
    backlog observed by this dispatch. The exact runs and resolving commands
    are copied onto the new record so the exception survives its command line.

    Dispatch arms the project watcher before creating a worktree when watcher
    policy is enabled. The producer is detached from the caller and keeps a
    supervisor as its live parent, so it remains valid after the dispatching
    process exits. A watch override records both the arming command and the
    liveness observed at the dispatch gate.

    The repository is the project's own mount, resolved before any worktree,
    pointer or ledger row exists, so a dispatch run from another checkout
    cannot cut its worktree from that checkout.
    """
    repo_root = resolve_project_repository(project, repo)
    worktree_identity = str(worktree_session or session)
    shadow_lineage = (
        dict(lineage_override)
        if isinstance(lineage_override, Mapping)
        and lineage_override.get("kind") == "shadow"
        else None
    )
    if lineage_override is not None and shadow_lineage is None:
        raise CrewError(
            format_refusal(
                "D20", "only shadow lineage may be supplied explicitly at dispatch"
            )
        )
    authority = resolve_dispatch_authority(project, repo_root)
    ledger_root = resolve_dispatch_ledger_root(authority)
    # Captured before plan_dispatch fills in per-backend defaults, so a budget
    # fallback's re-resolution (below) starts from what the caller actually
    # asked for rather than carrying the held backend's defaults forward.
    caller_time_budget = node.time_budget
    caller_write_paths = list(node.write_paths)
    # Each picker input is built independently behind a guard: an unreadable
    # ledger, a conflicting mount or a raising budget view is recorded against
    # the input that failed and the picker is left to fall back, so a picker
    # meant only to inform the dispatch can never abort the dispatch itself.
    resolved_route = resolve_dispatch_route(config, route)
    deferred_shadow_selection = (
        picker_selection is None
        and resolved_route != "picker"
        and (
            resolved_route == "deterministic"
            or local
            or bool(backend_override or default_backend_override)
        )
    )
    if picker_selection is not None:
        # The caller already asked the picker and ran the availability check on
        # its answer, so asking again would both double the pick's latency and
        # let the second pick choose a backend the check never saw. Reuse the
        # caller's answer so the checked backend is the dispatched backend.
        picker_selection = dict(picker_selection)
    elif not deferred_shadow_selection:
        (
            picker_records,
            picker_inputs,
            picker_budget,
            picker_input_errors,
        ) = build_picker_inputs(project, config, repo_root, ledger_root=ledger_root)
        picker_selection = dispatch_picker_selection(
            node=node,
            config=config,
            project=project,
            repo=repo_root,
            session=session,
            comment=comment,
            authority=authority,
            records=picker_records,
            verdict_inputs=picker_inputs,
            budget_snapshot=picker_budget,
            input_errors=picker_input_errors,
        )
    try:
        resolution = plan_dispatch(
            node=node,
            config=config,
            locked_decisions=locked_decisions,
            peer_scopes=peer_scopes,
            project=project,
            repo=repo_root,
            base=base,
            execution_override=execution_override,
            orchestrator_lane_reason=orchestrator_lane_reason,
            authority=authority,
            local=local,
            backend_override=backend_override,
            default_backend_override=default_backend_override,
            member=member,
            allow_unreviewed_plan=unreviewed_plan_override,
            repairs=repairs,
            session=session,
            route=route,
            picker_selection=picker_selection,
        )
    except BudgetHold:
        if (
            resolve_dispatch_route(config, route) == "picker"
            and picker_selection is not None
            and picker_selection.get("action") == "hold"
        ):
            from reckon.crew.picker.outcomes import record_picker_hold

            record_picker_hold(
                project=project,
                docs=Path(str(authority["plan"]["docs"])),
                node=node.id,
                plan=node.plan,
                selection=picker_selection,
                reason=_picker_refusal_reasons(picker_selection),
            )
        raise
    route = resolution.route
    if not resolution.validation.ok:
        raise CrewError(
            "node is not dispatchable — "
            + "; ".join(
                f"{finding['property']}: {finding['detail']}"
                for finding in resolution.validation.findings
            )
        )

    _workspace_roots(repo_root)
    live_claims = [] if shadow_lineage else _repository_scope_claims()
    if shadow_lineage:
        peer_claims = []
    else:
        candidate_scope = _candidate_scope_entries(
            node, project=project, repo=repo_root, authority=authority
        )
        candidate_repositories = {
            repository
            for repository, _path, _absolute, _declared, _derived_from in candidate_scope
            if repository is not None
        }
        peer_claims = [
            claim for claim in live_claims if claim.repository in candidate_repositories
        ]

    competence = resolution.competence or _competence_verdict(
        resolution=resolution, project=project, repo=repo_root
    )
    if not competence["allowed"]:
        raise CompetenceLimit(competence)

    fences = config.get("fences") or {}
    unreconciled_grace = str(fences.get("unreconciled_run_grace") or "")
    from reckon.crew.recovery import (
        _partition_session_rows,
        overdue_unreconciled_runs,
    )

    project_unreconciled = overdue_unreconciled_runs(
        project=project,
        grace=unreconciled_grace,
    )
    unreconciled, peer_unreconciled = _partition_session_rows(
        project_unreconciled, session
    )
    if unreconciled and not unreconciled_override:
        raise _session_unreconciled_refusal(
            unreconciled, peer_unreconciled, unreconciled_grace
        )
    waiver = (
        {
            "requested": True,
            # The waived backlog is copied as a field rather than referenced, so
            # the record states exactly which runs the exception covered rather
            # than pointing at the ledger's state in a later moment.
            "grace": unreconciled_grace,
            "waived_runs": unreconciled,
        }
        if unreconciled_override
        else None
    )
    # A waived plan review is narrower than the unreconciled waiver: it excuses
    # only the missing review of the plan this node builds, and is recorded with
    # the plan it waived so the exception names what was let through.
    plan_review_waiver = (
        {"requested": True, "plan": node.plan} if unreviewed_plan_override else None
    )

    budget_warnings: list[str] = []
    budget_fallback: dict[str, Any] | None = None
    requested_backend = resolution.requested_backend
    if check_budget:
        # Before the worktree, not after: a hold that had already cut a worktree
        # would leave write scope claimed by a node nobody is running.
        requested_backend_name = resolution.backend
        budget_config = config
        if route == "picker":
            budget_config = {
                **config,
                "budget": {
                    **(config.get("budget") or {}),
                    "resume_reserve_pct": 0,
                    "coordinator_reserve_pct": 0,
                },
            }
        verdict = _budget_verdict(
            project=project,
            root=ledger_root,
            config=budget_config,
            backend_name=resolution.backend,
            backend=resolution.backend_settings,
            purpose="dispatch",
            budget_state=budget_state,
        )
        if route == "picker":
            state = verdict.get("state") or {}
            from reckon import budget as budget_module

            ceiling = float(budget_module.policy(config)["utilisation_ceiling_pct"])
            utilisation = state.get("utilisation_pct")
            if (
                state.get("headroom") == "known"
                and isinstance(utilisation, (int, float))
                and utilisation >= ceiling
            ):
                verdict = {
                    **verdict,
                    "held": True,
                    "reason": (
                        f"backend {resolution.backend!r} is at {utilisation:g}% "
                        f"utilisation, at or above the provider's {ceiling:g}% "
                        "hard ceiling"
                    ),
                }
        budget_warnings.extend(verdict.get("warnings") or ())
        if verdict["held"]:
            if route == "picker":
                raise _actionable_budget_hold(verdict, config=budget_config)
            substitute = resolve_budget_fallback(
                config,
                node.role,
                node.spec_level,
                resolution.backend,
                resolution.backend_settings,
            )
            if substitute is None:
                raise _actionable_budget_hold(verdict, config=config)
            fallback_name, _fallback_settings = substitute
            # Re-resolve fully rather than patch the existing DispatchPlan, so
            # the fallback gets its own execution-fit, sandbox and write-path
            # checks instead of inheriting the held backend's. The caller's
            # own time_budget/write_paths are restored first because the held
            # backend's plan_dispatch call already defaulted them in place.
            node.time_budget = caller_time_budget
            node.write_paths = caller_write_paths
            resolution = plan_dispatch(
                node=node,
                config=config,
                locked_decisions=locked_decisions,
                peer_scopes=peer_scopes,
                project=project,
                repo=repo_root,
                base=base,
                execution_override=execution_override,
                orchestrator_lane_reason=orchestrator_lane_reason,
                authority=authority,
                local=local,
                run_id=resolution.run_id,
                backend_override=fallback_name,
                declared_backend=(
                    str(resolution.lane_declaration.get("backend") or "")
                    if resolution.lane_declaration is not None
                    else ""
                ),
                allow_unreviewed_plan=unreviewed_plan_override,
                session=session,
                route=resolution.route_override,
                picker_selection=picker_selection,
            )
            resolution.requested_backend = requested_backend
            if not resolution.validation.ok:
                raise CrewError(
                    f"node is not dispatchable on budget fallback {fallback_name!r} — "
                    + "; ".join(
                        f"{finding['property']}: {finding['detail']}"
                        for finding in resolution.validation.findings
                    )
                )
            fallback_verdict = _budget_verdict(
                project=project,
                root=ledger_root,
                config=config,
                backend_name=resolution.backend,
                backend=resolution.backend_settings,
                purpose="dispatch",
                budget_state=budget_state,
            )
            budget_warnings.extend(fallback_verdict.get("warnings") or ())
            if fallback_verdict["held"]:
                # No fallback-of-fallback chain: a declared fallback is a single
                # named substitute, not a search, so a held fallback refuses on
                # its own verdict rather than guessing a third lane.
                raise _actionable_budget_hold(fallback_verdict, config=config)
            budget_fallback = {
                "requested_backend": requested_backend_name,
                "used_backend": resolution.backend,
                "hold": verdict,
            }

    backend_name = resolution.backend
    backend = resolution.backend_settings
    launch_kind = resolution.launch
    run_id = resolution.run_id

    # The lane gate is read before anything is created: a paused gate, one that
    # cannot be answered, or a declared path that differs from the lane
    # document's published one holds the dispatch here, so no pointer and no
    # worktree is left behind for a worker nobody may launch. Re-reading the
    # resolved backend here catches a pause added after plan_dispatch; both
    # ``--local`` and an explicit ``--backend`` reach that resolved backend.
    lane_gate = _dispatch_lane_gate(backend)
    resolution.lane_gate = lane_gate
    if lane_gate.get("state") in _LANE_GATE_WAITING_STATES:
        raise LanePaused(lane_gate)

    # The lane's own allowance, chosen from the router's published figures: an
    # allowance of zero or less holds the node here, before a pointer or a
    # worktree exists, so the caller retries when the router's next reading
    # grants a slot rather than unwinding a launch. This is the same wait the
    # gate-withholds dispatch above is, and it is raised distinctly so the
    # reason a surface reports is the allowance the router published.
    lane_allowance = resolution.lane_allowance or {}
    if lane_allowance.get("held"):
        raise LaneHeld(lane_allowance)

    # A cli worker launches inside the fence, and the fence is bubblewrap over a
    # user namespace. A host with neither cannot seal a worker's writes, so the
    # dispatch is refused before any worktree, pointer or run directory exists
    # rather than launched unfenced: a fence that disappears silently still
    # reports a protection it does not have. ``--no-fence REASON`` is the one
    # deliberate way through, and the reason is recorded on the run, its
    # composed plan and its ledger row.
    fence_waiver = str(no_fence_reason).strip()
    compose_fence = launch_kind == "cli" and FENCE_WORKERS
    if compose_fence and not fence_waiver:
        capability_problem = _backends.fence_capability_problem()
        if capability_problem is not None:
            missing, detail = capability_problem
            raise CrewError(
                format_refusal(
                    "D22",
                    f"refusing to dispatch run {run_id!r}: {detail} "
                    f"(missing capability: {missing}). The fence is what keeps "
                    "a worker out of the operator's home and every other run's "
                    "worktree, so a dispatch that cannot build one is refused "
                    "rather than launched unprotected; pass --no-fence REASON to "
                    "launch unfenced and record why",
                )
            )
    if fence_waiver:
        compose_fence = False

    # The pace this dispatch is judged against, composed once here — before
    # anything is created — and reused on the record below, so the bookend
    # reserve refuses against the very reading the record carries rather than a
    # second one taken a moment later that could differ from it.
    from reckon import budget as budget_module

    pace_record = budget_module.pace_row(
        config,
        project=project,
        lane=backend_name,
        node=node.id,
        score=resolution.open_endedness,
        root=ledger_root,
        hold=None if budget_fallback is None else budget_fallback["hold"],
    )
    if check_budget and route != "picker":
        _refuse_against_the_bookend_reserve(
            config=config, role=node.role, pace_record=pace_record
        )

    directory = run_dir(run_id)
    # A brief is authority text, so its durable copy is taken with the run's id
    # rather than at launch: a later reader opening the pointer finds the same
    # bytes the worker read, and a refusal below unwinds the directory that
    # holds them. The digest was taken in plan_dispatch, where the dry run
    # reaches it too.
    if node.brief.strip():
        node.brief_path = str(_store_brief(directory, node.brief))
    brief = _brief_record(node)
    # The declared paths are this run's from the moment its id exists, so the
    # claim goes out here, before the refusals, reads and watcher arming below,
    # any of which can take seconds. A dispatch that published only once the
    # launch was composed held its paths invisibly for that whole span, so a
    # second dispatch arriving inside it read no claim and launched a duplicate
    # worker over the first. The guard opened under this block gives the claim
    # back if any refusal below is reached. See _publish_launch_claim.
    effective_member = member or _disposable_member_id(run_id)
    agent = _stamp_agent_display(
        _agent_configuration(backend_name, launch_kind, backend), backend
    )
    if resolution.local:
        agent["local"] = True
    claim_published = not shadow_lineage
    # The moment this dispatch's claim is registered, held so the admission
    # checks below can order it against a peer still composing its own claim.
    claim_registered_at = _utc_now() if claim_published else ""
    if claim_published:
        _publish_launch_claim(
            run_id,
            node=node,
            project=project,
            repo=repo_root,
            session=session,
            authority=resolution.authority,
            member=effective_member,
            backend=backend_name,
            launch=launch_kind,
            agent=agent,
            session_id=None,
            brief=brief,
            registered_at=claim_registered_at,
        )
    with _claim_released_on_refusal(run_id, claim_published):
        # A dispatch that would exceed a resource bound — the placement's
        # admitted partition cores or the login memory slice — refuses before
        # anything is created or spawned. A fallback backend resolved above
        # gets the same reading as a directly chosen one, so a held lane never
        # reroutes onto an exhausted resource.
        _refuse_over_concurrency_ceiling(
            backend_name, backend, project, exclude_run_ids=(run_id,)
        )
        explicitly_named_peers = set() if shadow_lineage else set(node.peer_scopes)
        peers = (
            {} if shadow_lineage else _merge_peer_scopes(peer_claims, node.peer_scopes)
        )
        peers = _peer_scopes_without_shared_landing_paths(
            peers,
            node=node,
            project=project,
            repo=repo_root,
            authority=authority,
        )
        node.peer_scopes = peers

        reap_idle_session_members(
            project,
            root=ledger_root,
            idle_window=str(
                fences.get("member_idle_window") or DEFAULT_MEMBER_IDLE_WINDOW
            ),
        )
        named_member = bool(member)
        # An unnamed dispatch is disposable: it carries a per-run identity instead
        # of the dispatching session's shared one, and it gets no roster row. So
        # two unnamed dispatches of one coordinator — every reflex review among
        # them — are never serialised against each other, and one run in flight
        # cannot refuse an unrelated task with `member-in-flight`. A named member
        # remains a deliberate route to a durable worker, so it keeps the roster
        # lookup, the D14 check and the refusal. The identity is minted with the
        # claim above, so the roster below is only ever asked about a named one.
        roster_member = (
            ledger.member(project, effective_member, root=ledger_root)
            if named_member
            else None
        )
        if named_member:
            if roster_member is None:
                raise CrewError(
                    format_refusal(
                        "D14",
                        f"project {project!r} has no crew member {member!r}; register it "
                        "with `reckon crew member add` before dispatching to it",
                    )
                )
        live_pointers = [
            pointer
            for pointer in list_live(project=project)
            if str(pointer.get("run_id") or "") != run_id
        ]
        if roster_member is not None:
            for pointer in live_pointers:
                if pointer.get("member") == effective_member:
                    refuse_member_in_flight(effective_member, pointer)
        disregarded_claims: list[str] = []
        accepted_directory_claims: list[dict[str, Any]] = []
        if shadow_lineage:
            adjacent_peers = []
        else:
            # A declared path is claimed whole, not piecemeal: a figure topic
            # directory is claimed as a tree, so any live claim that contains or is
            # contained by a candidate is refused with the owner named — never a
            # shared workspace, because a figure is replaced wholesale and a merged
            # half is never correct. The walk is path-based only, so a topic with
            # no files on disk yet binds exactly like one that does.
            _raise_repository_scope_conflict(
                node,
                project=project,
                repo=repo_root,
                authority=authority,
                claims=live_claims,
                disregarded=disregarded_claims,
                # The acceptance sink is passed only when the flag is given, so
                # a call that carries no directory claim keeps the walk's own
                # signature rather than threading an unused sink through it.
                **_directory_claim_acceptance_kwargs(
                    accept_directory_claim, accepted_directory_claims
                ),
                own_run_id=run_id,
                own_registered_at=claim_registered_at,
            )
            adjacent_peers = _adjacent_live_peers(
                node,
                project=project,
                repo=repo_root,
                explicitly_named=explicitly_named_peers,
                exclude_run_ids=(run_id,),
            )
        committed_runs = ledger.runs(project, root=ledger_root)
        session_resolution = (
            _task_session_resolution(
                node,
                project,
                committed_runs=committed_runs,
                live_pointers=live_pointers,
                harness=_backends.dialect_for(backend).name
                if launch_kind == "cli"
                else "",
            )
            if backend.get("session_reuse")
            else {"session_id": None, "withheld": None}
        )
        reuse_session = session_resolution["session_id"]
        prior_node_runs = [
            item
            for item in committed_runs
            if str(item.get("node") or "") == node.id
            and not (
                isinstance(item.get("lineage"), Mapping)
                and item["lineage"].get("kind") == "shadow"
            )
        ]
        lineage = shadow_lineage
        attempt = 1
        if shadow_lineage:
            primary = next(
                (
                    item
                    for item in committed_runs
                    if str(item.get("run_id") or "")
                    == str(shadow_lineage.get("primary_run_id") or "")
                ),
                None,
            )
            if primary is None:
                raise CrewError(
                    format_refusal(
                        "D20", "shadow lineage names no committed primary run"
                    )
                )
            attempt = int(primary.get("attempt") or 1)
        elif prior_node_runs:
            previous = prior_node_runs[-1]
            previous_lineage = previous.get("lineage") or {}
            previous_attempt = previous.get("attempt") or previous_lineage.get(
                "attempt"
            )
            try:
                attempt = int(previous_attempt) + 1
            except (TypeError, ValueError):
                attempt = len(prior_node_runs) + 1
            lineage = {
                "kind": "redispatch",
                "attempt": attempt,
                "root_run_id": previous_lineage.get("root_run_id")
                or str(prior_node_runs[0].get("run_id") or ""),
                "previous_run_id": str(previous.get("run_id") or ""),
            }

        dispatch_watch = watch_state(project, session=session)
        session_delivery = "monitor"
        released_follower_warning: str | None = None
        if watch_required and not watch_override and watch_arming_suppressed():
            # Opting in is the caller's act. An environment that forbids arming
            # turns the requirement into the recorded waiver below rather than
            # into a producer nobody will reap.
            watch_override = True
        if watch_required and not watch_override:
            dispatch_watch = _ensure_watch_producer(project, session=session)
            # A session that is not attached may be run by a host that can attach
            # it without a turn spent arming a Monitor watch. Asking is a write
            # to the host's FIFO and a bounded wait, and a session without a host
            # falls back to the Monitor path unchanged -- so this only ever
            # upgrades delivery, never refuses a dispatch the old path admitted.
            if str(launch_kind) == "cli" and session:
                attached = bool(dispatch_watch.get("session_attached"))
                if not attached and _ask_session_host_for_follower(project, session):
                    session_delivery = "host"
                    dispatch_watch = watch_state(project, session=session)
                # A session may already be attached by a follower the host runs,
                # whether an earlier dispatch asked it or the plugin's monitor
                # attached it at session start. That is host delivery just as
                # much as one just asked for, and reading it as monitor would
                # hand the caller an arming line for a follower the host already
                # consumes. The host's census record, not this session's watch
                # state, is what tells a host's follower from a hand-armed one.
                elif _session_host_runs_follower(
                    project, session, (dispatch_watch.get("follower") or {}).get("pid")
                ):
                    session_delivery = "host"
            # The watcher requirement is answered by the process, read from the
            # watcher's own state — never by a session's follower, which is how a
            # project with no watcher process at all kept admitting dispatches.
            # Whether this session hears what the producer writes is a separate
            # fact, and the only one that decides if the finished run gets
            # noticed. Both are judged here, before a worktree exists, and one
            # refusal names every unmet condition so the caller reaches the fix
            # in one dispatch rather than one condition per round trip. A
            # released registration proceeds with a re-arm warning rather than
            # a refusal.
            released_follower_warning = _watcher_delivery_admission(
                project,
                dispatch_watch,
                session=session,
                launch_kind=launch_kind,
                delivery=session_delivery,
            )
        watcher_waiver = (
            {
                "requested": True,
                "arming_line": dispatch_watch["arming_line"],
                "attach_line": dispatch_watch["attach_line"],
                "watcher_live": bool(dispatch_watch["watcher_live"]),
                "session_attached": bool(dispatch_watch["session_attached"]),
            }
            if watch_override
            else None
        )
        gates = config.get("gates") or {}
        suite_command = str(gates.get("suite_command") or "").strip() or None
        wave_id = _resolved_wave_id(project, session, wave)

    worktree: dict[str, Any] | None = None
    spawned_pid: int | None = None
    spawned_start_time: str | None = None
    wired_peer_run_ids: list[str] = []
    node_claim: _NodeDispatchClaim | None = None
    try:
        # The claim over this node's worktree path is taken here, immediately
        # before the worktree, so a dispatch that loses it refuses before
        # touching the path. It is held until the launch is decided, and
        # released only after a refusal has unwound — the unwind removes the
        # worktree, and a next dispatch taking the claim early would race that
        # removal against its own creation.
        node_claim = _claim_node_dispatch(
            project=project,
            worktree_identity=worktree_identity,
            node_id=node.id,
            run_id=run_id,
            session=session,
        )
        worktree = _create_worktree(repo_root, worktree_identity, node.id, base)
        directory.mkdir(parents=True, exist_ok=True)
        working_directory = worktree["path"]
        if launch_kind == "cli":
            try:
                working_directory = _backends.launch_working_directory(
                    backend=backend,
                    worktree=worktree["path"],
                    manifest_path=node.manifest_path,
                )
            except _backends.BackendError as exc:
                raise CrewError(format_refusal("D22", str(exc))) from exc
        # One read of the clock is both the attempt's recorded launch instant
        # and the instant its fence states, so the prompt and the record cannot
        # disagree about when this attempt started.
        attempt_started_at = _utc_now()
        dispatch_host = _current_host_facts()
        prompt = _compose_dispatch_prompt(
            node=node,
            project=project,
            authority=authority,
            backend=backend,
            repo_root=repo_root,
            run_directory=directory,
            worktree=worktree["path"],
            working_directory=working_directory,
            launch_instant=attempt_started_at,
            needs_help_after_failures=int(fences.get("needs_help_after_failures", 2)),
            peer_scopes=peers,
            run_id=run_id,
            peer_channels={
                str(peer["node"]): {"run_id": str(peer["run_id"])}
                for peer in adjacent_peers
            },
            peer_channel_path=str(_channel_root(run_id)),
            host_line=_worker_host_line(dispatch_host, directory),
            brief=_brief_text(node),
        )
        if shadow_lineage:
            prompt += (
                "\n\nSHADOW RUN — produce the named evidence without committing. "
                "The durable deliverable is the worktree patch retained at completion; "
                "this run is never merged.\n"
            )
        prompt_path = directory / "prompt.txt"
        prompt_path.write_text(prompt)
        log_path = directory / "stream.jsonl"
        stderr_path = directory / "stderr.log"
        final_path = directory / "final.txt"
        coordinator = _coordinator_accounting(session)
        node_definition = node.as_dict()
        node_definition["requested_backend"] = resolution.requested_backend
        node_definition["lane_declaration"] = resolution.lane_declaration
        node_definition["lane_reading"] = resolution.lane_reading
        # The token budget is resolved here, at dispatch, so the run record is
        # authoritative and a later config edit cannot silently re-charge a run
        # that launched under another allowance. It rides the node block beside
        # time_budget, which is where recovery reads both allowances back.
        node_definition["token_budget"] = resolution.token_budget
        # Promotion deliberately rebuilds the committed row from selected live
        # fields. The authored node definition is one of those durable fields,
        # so attribution lives there as well as at the pointer's top level.
        node_definition["coordinator"] = coordinator

        # The pace this dispatch was judged against, composed by the module that
        # owns every figure in it and carried on the record the run already
        # writes, which is where its evidence lives. Promotion rebuilds the
        # committed row from selected fields rather than whole, and carries this
        # one across by reading it back from the run's own pointer while that
        # pointer is still open, so a week of dispatch decisions replays from
        # those rows alone rather than from the streams they were read out of.
        # The row reports the reading's age and its source, so a row that paced
        # a dispatch on stale evidence says so itself, and a lane declaring no
        # wallet records that no group paced it rather than a wallet nothing
        # read. The row is composed once, above the refusals, so the reading
        # this record carries is the same one the bookend reserve judged.
        record: dict[str, Any] = {
            "run_id": run_id,
            "project": project,
            "repo": str(repo_root),
            "authority": resolution.authority,
            "session": session,
            "wave": wave_id,
            "coordinator": coordinator,
            "node": node_definition,
            "brief": brief,
            "role": node.role,
            "backend": backend_name,
            "requested_backend": resolution.requested_backend,
            "lane_declaration": resolution.lane_declaration,
            "lane_reading": resolution.lane_reading,
            # What the section's own record contributed to the lane this run
            # took: the raise it earned, or the failure that left it on role
            # routing. The pointer is the record a later reader reaches without
            # the dispatching process, so a raise that could not be resolved
            # has to be visible here and not only in the dispatch payload.
            "section_routing": (
                None
                if resolution.section_routing is None
                else dict(resolution.section_routing)
            ),
            "lane_gate": resolution.lane_gate,
            # The orchestrator-lane stop the dispatch resolved, recorded even
            # when the fence did not fire: the pointer is the record a reader
            # reaches without the dispatching process, so "the lane declared
            # nothing" has to be distinguishable from a record written before
            # the declaration existed.
            "orchestrator_lane_stop": resolution.orchestrator_lane_stop,
            "orchestrator_lane_override": resolution.orchestrator_lane_override,
            "local": resolution.local,
            "execution_fit": resolution.execution_fit.as_dict(),
            "launch": launch_kind,
            "sandbox": backend.get("sandbox"),
            # Whether this launch was composed inside the fence wrapper,
            # overwritten from the composed plan below. The boundary check reads
            # this recorded fact rather than the current default, so a later
            # change to the default cannot redefine what an already-dispatched
            # run is checked against. A record written before the field existed
            # carries neither value and keeps the full scan.
            "fenced": False,
            "sandbox_write_roots": (
                None
                if resolution.sandbox_write_roots is None
                else [str(path) for path in resolution.sandbox_write_roots]
            ),
            # A backend permitting a run to continue an earlier session is a
            # property of the configuration, so it is named as one: a reader
            # taking a bare ``session_reuse`` for an observation reaches a true
            # answer to a question nobody asked. Whether this run actually
            # carried a session is written from its own launch below.
            "session_reuse_capable": bool(backend.get("session_reuse")),
            # Overwritten from the launched argv for a spawned run. An
            # in-harness launch is delegated, not spawned, so reckon cannot put
            # a prior session on its command line and the value stays false.
            "session_resumed": False,
            "member": effective_member,
            # The configuration that actually ran the node, recorded now because
            # a later config layer change makes it unreconstructable — and
            # without it a measured duration cannot be attributed to anything.
            "agent": agent,
            "competence": competence,
            "worktree": worktree["path"],
            "base": worktree["base"],
            "base_sha": worktree["base_sha"],
            # The directory this run's scratch was created at, recorded at
            # dispatch so the promotion or discard that later removes it removes
            # exactly that path rather than re-deriving it under a root that may
            # have moved.
            "scratch": str(worker_scratch_dir(run_id)),
            # The authored implementation fraction at dispatch, so promotion can refuse a passing
            # implement landing whose plan did not move. An unreadable value
            # stays absent, which exempts the run rather than recording a false
            # zero that would look like a plan that never moved.
            "plan_impl_at_dispatch": _plan_impl_at_dispatch(
                project, node.plan, ledger_root
            ),
            "suite_command": suite_command,
            "prompt_path": str(prompt_path),
            "log_path": str(log_path),
            "stderr_path": str(stderr_path),
            "final_message_path": str(final_path),
            "manifest_path": node.manifest_path,
            "manifest_baseline_mtime_ns": _manifest_mtime_ns(node.manifest_path),
            "peer_scopes": {name: sorted(paths) for name, paths in peers.items()},
            "peer_channel": {
                "endpoint": str(_channel_root(run_id)),
                "peers": {},
                "scope_transfer": False,
            },
            "created_at": _utc_now(),
            "attempt": attempt,
            "attempt_kind": (
                "shadow" if shadow_lineage else "redispatch" if lineage else "dispatch"
            ),
            # The promoted run this dispatch repairs, declared with --repairs.
            # Promotion reads it to exempt the impl move: the movement belongs
            # to the run being repaired. Absent when the dispatch declares none.
            "repairs": str(repairs or "").strip() or None,
            "attempt_started_at": attempt_started_at,
            "phase": "starting",
            "session_id": reuse_session,
            # A run that carries no session id names why it does not, from the
            # moment it is created rather than only once something folds its
            # stream in. A dispatch-time absence is the pending kind — the run's
            # own stream or harness task may still supply one — and observation
            # replaces this with the id or with the point the capture reached.
            "session_id_absent": _dispatch_session_absence(
                backend,
                reused=reuse_session,
                withheld=session_resolution["withheld"],
            ),
            # A prior run of this task whose session ended too large to
            # continue is not resumed, and that is written down rather than
            # left silent: a peer whose worker starts a fresh conversation
            # reads the session and the reason it was passed over here.
            "session_withheld": session_resolution["withheld"],
            "task": None,
            "pid": None,
            "argv": None,
            # The harness the launch resolves to, recorded explicitly rather
            # than left to be read off argv[0]: a placed launch prefixes the
            # scheduler onto the argv, so its first word names the scheduler and
            # a later reader reconstructing the backend from it would translate
            # the wrong lane.
            "command": None,
            "dialect": None,
            "budget": _backends.unknown_budget("no events yet"),
            "budget_fallback": budget_fallback,
            "picker_selection": picker_selection,
            "route_mode": (
                "explicit"
                if backend_name
                == str(
                    backend_override
                    or default_backend_override
                    or (config.get("local_backend") if local else "")
                    or ""
                )
                else "picker"
                if resolution.route == "picker"
                and picker_selection is not None
                and picker_selection.get("action") == "route"
                and picker_selection.get("backend") == backend_name
                else "shadow"
            ),
            "route": resolution.route,
            "route_override": resolution.route_override,
            "pace": pace_record,
            "warnings": [
                *resolution.warnings,
                *budget_warnings,
                *disregarded_claims,
                *(
                    [
                        _directory_claim_acceptance_line(row)
                        for row in accepted_directory_claims
                    ]
                ),
                *([released_follower_warning] if released_follower_warning else []),
            ],
            "done_when_warnings": [
                dict(item) for item in resolution.done_when_warnings
            ],
            "directory_claim_acceptances": list(accepted_directory_claims),
            "lineage": lineage,
            "unreconciled_override": waiver,
            "unreviewed_plan_override": plan_review_waiver,
            "watch_override": watcher_waiver,
            "watch": {
                "arming_line": _watch_arming_line(project),
                "attach_line": _watch_attach_line(project, session=session),
                "delivery": session_delivery,
                "watcher_live": False,
                "session": session,
                "session_attached": False,
                "session_follower_released": False,
                "watcher": {},
            },
        }

        # The advisory the dispatch computed rides the record when it exists,
        # beside the lane declaration and reading it was derived from. An
        # absent advisory is left off entirely rather than written as a null:
        # a null key would read as a lane that was checked and found quiet,
        # which is the opposite of a lane that was never assessed.
        if resolution.lane_advisory is not None:
            record["lane_advisory"] = resolution.lane_advisory

        # A deliberate unfenced dispatch survives on the record with its reason,
        # so a later reader can tell a launch that declined the fence from one
        # that never asked for it — the field is written only when the flag was
        # given, never as a null that would read as a fence that was built.
        if launch_kind == "cli" and fence_waiver:
            record["fence_waiver"] = {"reason": fence_waiver}

        # The defaults a layer names under ``unprotected_paths`` are left out of
        # this run's fence, so the run carries the list it left out. Written
        # only when the fence actually composed and only when a default was
        # removed: a run that removes nothing records no such key rather than an
        # empty one, which would read as a fence that was built and found whole.
        fence_unprotected = _backends.fence_unprotected_paths(config=config)

        if launch_kind == "cli":
            try:
                # Refused before composition, which seeds the run's harness
                # home: an absent backend must leave no run behind.
                preflight_launch_command(
                    backend_name, backend, fence=compose_fence, facts=dispatch_host
                )
                fence_roots = _fence_write_roots(
                    backend=backend,
                    repository=repo_root,
                    run_directory=run_dir(run_id),
                    manifest_path=node.manifest_path,
                    worktree=worktree["path"],
                    declared_write_paths=node.write_paths,
                )
                plan = resolve_launch_executable(
                    _backends.launch_plan(
                        backend_name=backend_name,
                        backend=backend,
                        prompt=prompt,
                        worktree=worktree["path"],
                        manifest_path=node.manifest_path,
                        writable_directories=fence_roots,
                        final_message_path=str(final_path),
                        resume_session=reuse_session,
                        fence=compose_fence,
                        fence_config=config,
                        fence_waiver=fence_waiver or None,
                    ),
                    facts=dispatch_host,
                )
                # Read before the placement wraps the plan: the harness sits at
                # the position the plan composes it at, and after the wrap that
                # element is the scheduler's rather than the harness's.
                harness_command = (
                    str(plan.argv[harness_command_index(plan.argv)])
                    if plan.argv
                    else None
                )
                record["session_harness"] = plan.dialect if reuse_session else None
                plan = resolve_backend_placement(plan, backend, project, payload=record)
            except (_backends.BackendError, flight.FlightConfigError, OSError) as exc:
                raise CrewError(format_refusal("D22", str(exc))) from exc
            placement = flight.placement_for(backend)
            job_id, job_id_status = placement_job_id(placement, run_id=run_id)
            record.update(
                {
                    # Whether the plan this dispatch composed carries the fence
                    # wrapper. Read from the composed argv, so a cli launch that
                    # asked for the fence is recorded as fenced while any launch
                    # that composed none is not.
                    "fenced": _plan_composed_the_fence(plan),
                    **(
                        {
                            "fence_unprotected_paths": [
                                str(path) for path in fence_unprotected
                            ]
                        }
                        if fence_unprotected and _plan_composed_the_fence(plan)
                        else {}
                    ),
                    # The pointer's pid is the per-run supervisor's, written
                    # once it is running, further down. Until then the run has no
                    # process identity, which is why it starts empty rather than
                    # naming a worker that is not spawned yet.
                    "pid": None,
                    "pid_start_time": None,
                    "argv": list(plan.argv),
                    "command": harness_command,
                    "dialect": plan.dialect,
                    "session_resumed": _launched_prior_session(plan) is not None,
                    # A placed launch is charged to a scheduler job rather than
                    # to the coordinator's own login slice, so the job is the
                    # process identity a liveness read needs; it is recorded
                    # beside the pid, from which it is not derivable.
                    "job_id": job_id,
                    # The queries the placement declares are carried onto the
                    # record with it: a liveness read happens long after the
                    # configuration that launched the run may have changed, and
                    # the record is the only place the placement was ever
                    # recorded. A record that keeps the wrapper without its
                    # query cannot be asked about its own job.
                    "placement": (
                        None
                        if placement is None
                        else {
                            "scheduler": str(placement["scheduler"]),
                            "options": [
                                str(item) for item in placement.get("options") or ()
                            ],
                            "job_id_status": job_id_status,
                            **flight.placement_scheduler_queries(placement),
                        }
                    ),
                }
            )
        else:
            # An in-harness launch composes no plan here and no fence argv, so
            # it is recorded unfenced and its boundary check keeps the full scan
            # of every registered worktree.
            plan = None
            record["fenced"] = False
            record["directive"] = {
                "attach_with": f"reckon crew attach --run {run_id} --task <task-id>",
                "fences": {
                    "delivery": node.manifest_path,
                    "evidence": node.done_when,
                    "scope": list(node.write_paths),
                    "time": node.time_budget,
                },
                "prompt_path": str(prompt_path),
                "sandbox": {
                    "tier": backend.get("sandbox"),
                    "write_roots": record["sandbox_write_roots"],
                },
                "worktree": worktree["path"],
            }
            if dispatch_host.in_allocation:
                record["directive"]["environment"] = _persisted_worker_environment(
                    {}, facts=dispatch_host
                )
            record["directive"]["environment"] = _worker_runtime_environment(
                record["directive"].get("environment"),
                run_id=run_id,
                manifest_path=node.manifest_path,
                attempt_started_at=attempt_started_at,
                coordinator_session=session,
                claude_headers=False,
            )

        # Read the claims once more, now that the worktree, the prompt and the
        # peer wiring exist: two dispatches can both pass the admission check
        # before either has published a claim, and whichever arrives here
        # second must be the one refused, naming the first. This run's own
        # claim is excluded by identity, so the dispatch never arbitrates
        # against the claim it made itself.
        if not shadow_lineage:
            _raise_repository_scope_conflict(
                node,
                project=project,
                repo=repo_root,
                authority=authority,
                claims=_repository_scope_claims(exclude_run_ids=(run_id,)),
                **_directory_claim_acceptance_kwargs(
                    accept_directory_claim, accepted_directory_claims
                ),
                own_run_id=run_id,
                own_registered_at=claim_registered_at,
            )
        # Publish the pointer before probing the watcher. Otherwise a watcher
        # could drain an empty fleet between the probe and this write, leaving
        # a new run behind a payload that incorrectly said it was watched.
        _write_json(pointer_path(run_id), record)
        record["peer_channel"] = _wire_peer_channels(record, adjacent_peers)
        wired_peer_run_ids = list(record["peer_channel"]["peers"])
        record["watch"] = watch_state(project, session=session)
        # The delivery verdict rides the payload so a later reader can tell a
        # session the host attached from one that still needs the Monitor tool.
        # A host-delivered session carries no arming instruction: the host armed
        # the follower, and re-arming a Monitor watch would double-deliver.
        record["watch"]["delivery"] = session_delivery
        if session_delivery == "host":
            record["watch"]["arming_line"] = ""
        _write_json(pointer_path(run_id), record)
        # Starting the supervisor is dispatch's last repository-facing step.
        # Every write dispatch makes inside a repository — the worktree, and
        # nothing else now that no dispatch registers a member of its own — is
        # complete before this point, so the boundary baseline can follow it
        # with no handshake: there is nothing left for dispatch to write that
        # the baseline must follow. After this dispatch writes only the pointer,
        # which lives under the configuration home outside every repository.
        # Dispatch waits for neither the snapshot nor the spawn.
        if launch_kind == "cli" and plan is not None:
            # Starting the worker is the one operation a caller-supplied launcher
            # stands in for and the one that fails for reasons outside
            # dispatch's own writes: a harness executable that is absent or not
            # executable, a refused fork, an exhausted process table. The plan
            # above is wrapped for exactly that reason; the spawn is not, so an
            # OSError from it would escape as a traceback. It is a launch
            # refusal and it is rendered as one, and because it is raised
            # inside the unwind below the refusal still leaves no pointer, run
            # directory or worktree behind.
            try:
                if launcher is None:
                    _prepare_attempt_records(
                        directory,
                        run_id=run_id,
                        attempt=int(record["attempt"]),
                        attempt_kind=str(record["attempt_kind"]),
                        attempt_started_at=str(record["attempt_started_at"]),
                    )
                    spec_path = directory / SUPERVISOR_SPEC_NAME
                    _write_json(
                        spec_path,
                        _supervisor_spec(
                            run_id=run_id,
                            run_directory=directory,
                            repo_root=repo_root,
                            worktree=Path(worktree["path"]),
                            plan=plan,
                            fenced=bool(record["fenced"]),
                            prompt_path=prompt_path,
                            log_path=log_path,
                            stderr_path=stderr_path,
                            facts=dispatch_host,
                            attempt=int(record["attempt"]),
                            attempt_kind=str(record["attempt_kind"]),
                            attempt_started_at=str(record["attempt_started_at"]),
                        ),
                    )
                    _require_fleet_gate_open()
                    spawned_pid = _start_supervisor(spec_path, directory, run_id)
                else:
                    _require_fleet_gate_open()
                    spawned_pid = launcher(
                        plan,
                        log_path=log_path,
                        stderr_path=stderr_path,
                        prompt_path=prompt_path,
                    )
            except OSError as exc:
                raise CrewError(
                    format_refusal("D22", f"the worker launch could not start: {exc}")
                ) from exc
            if launcher is not None:
                # A caller-supplied launcher is a test seam that stands in for
                # the supervisor: it spawns synchronously and the boundary
                # baseline is taken inline, exactly as the supervisor would.
                record["repository_tree_snapshot"] = _repository_tree_snapshot(
                    repo_root,
                    roots=_boundary_snapshot_roots(
                        repo_root, Path(worktree["path"]), fenced=bool(record["fenced"])
                    ),
                )
            spawned_start_time = _process_start_time(spawned_pid)
            record["pid"] = spawned_pid
            record["pid_start_time"] = spawned_start_time
            # The supervisor may already have advanced this pointer's phase.
            # Merge the launch identity under the same lock as that advance so
            # neither writer replaces the other's newer fields with its copy.
            def attach_launch_identity(pointer: dict[str, Any]) -> dict[str, Any]:
                pointer["pid"] = spawned_pid
                pointer["pid_start_time"] = spawned_start_time
                if "repository_tree_snapshot" in record:
                    pointer["repository_tree_snapshot"] = record[
                        "repository_tree_snapshot"
                    ]
                return pointer

            record = _mutate_pointer(run_id, attach_launch_identity)
        else:
            # A delegated launch spawns no process, so there is no supervisor to
            # take the boundary baseline after dispatch's writes. Dispatch takes
            # it here instead, in the same last repository-facing step, so the
            # baseline still predates every write this run's worker will make.
            _write_boundary_tree_snapshot(
                directory,
                repo_root,
                worktree=Path(str(worktree["path"])),
                fenced=bool(record["fenced"]),
            )
    except Exception as exc:
        # Undoing this run is the rollback, and a step of a rollback that fails
        # must not become the error the caller reads: the dispatch stopped for
        # the reason above, and an unwind that refused here — a worktree
        # removal answering a claim — would report the cleanup instead, so the
        # operator would re-run the dispatch by hand to find out what actually
        # happened. The unwind's own failure therefore rides the original —
        # attached as its cause when the original names none, and as a note
        # when it already does, so the cause a launch refusal carries, the
        # OSError that ended the spawn, is preserved rather than replaced.
        # It is printed here too, where a caller that shows only the original's
        # message would otherwise lose the tree the rollback could not remove.
        try:
            _unwire_peer_channels(run_id, wired_peer_run_ids)
            if spawned_pid is not None:
                try:
                    _signal_process_group(
                        spawned_pid,
                        spawned_start_time,
                        run_dir=run_dir(run_id),
                        reason="dispatch-rollback",
                    )
                except (CrewError, OSError):
                    pass
            # The pointer goes first. The worktree remover refuses a worktree
            # that a live pointer still claims, and until this run's own pointer
            # is gone it is that claim — so removing the worktree first raised,
            # and the unlink and the run-directory removal below never ran. A
            # refusal reached after the pointer write therefore left a live
            # pointer behind, which a reader takes for a run whose process died
            # without a manifest. Unlinking first clears the claim, and the run
            # directory follows, so a refusal is indistinguishable from a
            # dispatch that never ran.
            _release_launch_claim(run_id)
            # A refusal can land before the worktree exists — the creation
            # itself is the first thing inside this guard — and there is then
            # nothing of this run's to remove. The pre-existing worktree of an
            # earlier run is left in place either way, which is what its owner
            # expects.
            if worktree is not None:
                _remove_worktree(repo_root, worktree["path"])
        except Exception as rollback_failure:
            print(
                f"crew: undoing run {run_id} failed: {rollback_failure}",
                file=sys.stderr,
            )
            # A launch refusal already carries the error that ended the spawn
            # as its cause. Chaining the unwind onto it would replace that
            # cause and drop the inner error a reader needs to diagnose an
            # absent harness executable, so preserve it and carry the unwind
            # as a note, which a traceback prints beside the cause.
            cause = exc.__cause__
            if cause is None:
                raise exc from rollback_failure
            exc.add_note(f"undoing run {run_id} failed: {rollback_failure}")
            raise exc from cause
        raise
    finally:
        # Released only now, after any unwind has removed the worktree a
        # refusal cut: a next dispatch taking the claim earlier would race its
        # own creation against that removal.
        if node_claim is not None:
            node_claim.release()
    if node_claim is not None and node_claim.reclaimed:
        record["reclaimed_node_claim"] = node_claim.reclaimed
    if deferred_shadow_selection:
        try:
            _start_shadow_picker_selection(
                run_id=run_id,
                node=node,
                config=config,
                project=project,
                repo=repo_root,
                ledger_root=ledger_root,
                session=session,
                comment=comment,
            )
        except (OSError, TypeError, ValueError) as exc:
            selection = _picker_fallback(
                f"shadow launch failed: {type(exc).__name__}: {exc}", comment
            )
            record["picker_selection"] = selection
            _attach_shadow_picker_selection(run_id, selection)
    return record


# Every worker this process has launched but not yet waited on. The waiting is
# the reaper's, not the caller's: an ordinary dispatch CLI is gone before its
# worker finishes, and a long-lived follower that sweeps on a cadence only
# looks again at the next tick, so between a worker finishing and someone
# observing it, it would sit unreaped. A defunct child is still a live pid to
# a zero-signal liveness probe, which is exactly how a finished resume reads
# as running and refuses the next one. The follower reaps what it launched, so
# no corpse exists for that probe to misread. Owned by the process that called
# :func:`_spawn`, because only the parent may wait on a child.
_LAUNCHED_WORKERS: set[int] = set()
# What each launched pid was launched as, so a reap can judge the exit against
# the run it belongs to. A pid is only ever in both this map and the set above
# together; the map is popped with the pid.
_LAUNCHED_WORKER_RUNS: dict[int, dict[str, Any]] = {}
_LAUNCHED_WORKERS_LOCK = threading.Lock()
_LAUNCHED_WORKERS_WAKE = threading.Event()
# The reaper thread, once started, behind the same lock as the set it guards.
# A mutable holder rather than a rebound global so the start-once guard can
# record it without a module-level reassignment.
_LAUNCHED_WORKER_REAPER: dict[str, threading.Thread | None] = {"thread": None}


# The stream file a launch writes its turn records into. A launch that produced
# no byte of it never reached a model: the process is gone, so there is no turn
# to wait for and no session to reuse, and the run is stopped rather than
# retried. Named as a phase so every reader of the pointer sees the same thing.
LAUNCH_FAILED_PHASE = "launch-failed"

# How much of the failed launch's stderr is kept on the run. Enough for a
# traceback or an exec diagnostic, bounded so a chatty backend cannot grow a
# pointer without limit.
_LAUNCH_FAILURE_STDERR_BYTES = 2048


def _launched_worker_record(
    plan: _backends.LaunchPlan, log_path: Path, stderr_path: Path
) -> dict[str, Any] | None:
    """Describe a launched worker, or None when its stream names no run.

    The stream path is ``<run dir>/<turn>.jsonl``, so the run it belongs to is
    the directory's name. A launch outside a run directory — a lane probe —
    hands back None and is reaped exactly as before.
    """
    run_id = Path(log_path).parent.name
    if not run_id or Path(log_path).parent != run_dir(run_id):
        return None
    return {
        "run_id": run_id,
        "stream_path": str(log_path),
        "stderr_path": str(stderr_path),
        "argv": list(plan.argv),
        "backend": plan.backend,
    }


def _placement_job_state(
    placement: Mapping[str, Any] | None, job_id: Any
) -> str | None:
    """The scheduler's state for a placed job, or None when it cannot be read."""
    if not placement:
        return None
    try:
        return scheduler_job_state(placement, job_id)
    except (OSError, CrewError):
        return None


def _placement_job_alive(
    placement: Mapping[str, Any] | None, job_id: Any
) -> bool | None:
    """Whether a placed job is still in the system, or None when unread."""
    if not placement:
        return None
    try:
        return placement_job_alive({"placement": placement, "job_id": job_id})
    except (OSError, CrewError):
        return None


def _placed_record_identity(run_id: str) -> tuple[dict[str, Any] | None, Any]:
    """The placement and job id a run's pointer carries, reading it once.

    A reap holds nothing but the launched plan, so the run's own pointer is the
    only place the placement was ever recorded. A pointer that cannot be read —
    reclaimed between the reap and this question — answers no placement, which
    leaves the reap on its original empty-stream rule.
    """
    try:
        pointer = read_pointer(run_id)
    except CrewError:
        return None, None
    placement = pointer.get("placement")
    if not isinstance(placement, Mapping) or not placement:
        return None, None
    return dict(placement), pointer.get("job_id")


def _wait_status_record(exit_status: int) -> dict[str, Any]:
    """How the launcher's wait on its own child ended, as two distinct cases.

    ``os.waitstatus_to_exitcode`` negates the signal number when the process was
    terminated rather than exited, so the sign carries the whole distinction and
    a reader must not have to know that to act on it: the two cases want
    different responses. A signalled process chose nothing, so it records no
    exit code and names the signal that ended it; an exited one records its code
    and names no signal. A signal outside the named set — a realtime signal, for
    instance — is still recorded by number, so an unfamiliar kill is legible
    rather than dropped.
    """
    record: dict[str, Any] = {"exit_code": None, "signal": None, "signal_name": None}
    if exit_status < 0:
        number = -exit_status
        record["signal"] = number
        try:
            record["signal_name"] = signal.Signals(number).name
        except ValueError:
            record["signal_name"] = f"signal {number}"
    else:
        record["exit_code"] = exit_status
    return record


def _launch_failure_record(
    launched: Mapping[str, Any],
    *,
    exit_status: int,
    placement: Mapping[str, Any] | None = None,
    job_id: Any = None,
) -> dict[str, Any]:
    """The one record written for a launch that exited before any turn.

    A placed launch is charged to a scheduler job, so its exit status is the
    scheduler client's rather than the worker's and the payload log, not that
    status, is what decides whether the work ran; the job's own terminal state
    and reason are recorded beside it. A job the scheduler ended at a time or
    memory limit is named distinctly, because resubmitting it unchanged fails
    the same way.
    """
    stderr_tail = ""
    try:
        raw = Path(str(launched["stderr_path"])).read_bytes()
        stderr_tail = raw[-_LAUNCH_FAILURE_STDERR_BYTES:].decode("utf-8", "replace")
    except OSError:
        stderr_tail = ""
    state = _placement_job_state(placement, job_id)
    reason = scheduler_job_reason(placement, job_id)
    kind = scheduler_kill_class(state, reason) or "launch-failed"
    record = {
        "recorded_at": _utc_now(),
        "kind": kind,
        "backend": str(launched.get("backend") or ""),
        "exit_status": exit_status,
        "argv": list(launched.get("argv") or ()),
        "stderr_tail": stderr_tail,
        "stream_path": str(launched.get("stream_path") or ""),
        **_wait_status_record(exit_status),
    }
    if placement:
        record["scheduler_state"] = state
        record["scheduler_reason"] = reason
    return record


def _empty_stream_launch_failure(
    record: Mapping[str, Any], data: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The launch-failure entry for a run that died before writing a turn.

    A stream of zero bytes means the worker exited before its first event, so
    nothing in the stream explains the exit and the reason sits in the run's
    stderr, where no reader looked. The entry is built only when stderr carries
    something to quote: a process that exited with nothing in either file is an
    orphan with no reason to attach, and claiming a launch failure there would
    name an end that was never recorded. The shape is the one the launcher's
    reap writes, so a reader of ``launch_failures`` sees one vocabulary however
    the failure was found.
    """
    stream = Path(str(record.get("log_path") or ""))
    try:
        if stream.stat().st_size:
            return None
    except OSError:
        # A stream never created is the same fact as an empty one.
        pass
    try:
        raw = Path(str(record.get("stderr_path") or "")).read_bytes()
    except OSError:
        return None
    tail = raw[-_LAUNCH_FAILURE_STDERR_BYTES:].decode("utf-8", "replace").strip()
    if not tail:
        return None
    entry: dict[str, Any] = {
        "recorded_at": _utc_now(),
        "kind": LAUNCH_FAILED_PHASE,
        "backend": str(record.get("backend") or ""),
        "argv": list(record.get("argv") or ()),
        "stderr_tail": tail,
        "stream_path": str(stream),
    }
    exit_status = data.get("exit_status")
    if isinstance(exit_status, int) and not isinstance(exit_status, bool):
        entry["exit_status"] = exit_status
        entry.update(_wait_status_record(exit_status))
    return entry


def _record_launch_failure(launched: Mapping[str, Any], *, exit_status: int) -> None:
    """Record how a launched worker's process ended, on its run.

    Every reap records the exit the launcher observed, except where the payload log
    tells us the status is not the worker's to report. A run whose stream is
    non-empty otherwise reads as still working with no trace of its process
    having gone, which is how a worker ended by a signal stays
    indistinguishable from a live one. The payload log, not the step's exit
    status, still decides whether a worker turn ran: a placed launch's exit
    status belongs to the scheduler client, and a step the scheduler reports
    COMPLETED can still have aborted before reaching a model. A non-empty
    stream is a turn that ran whatever the status says, so an unplaced run
    keeps its phase and gains the wait status.

    A launch that wrote no byte at all reached no model: there is nothing to
    resume from and nothing the lift loop can usefully retry. That one is a
    launch failure as well — it records the failure and sets the phase, which
    stops a further lift until a person resumes or completes the run, the
    measured loop of one 0-byte stream every two minutes.
    """
    stream = Path(str(launched.get("stream_path") or ""))
    try:
        size = stream.stat().st_size
    except OSError:
        # A stream that was never created is the same fact as an empty one.
        size = 0
    run_id = str(launched.get("run_id") or "")
    if not run_id:
        return
    placement, job_id = _placed_record_identity(run_id)
    # The pid that finished is the scheduler client, not the worker, so a job
    # still in the system means the worker has not ended and there is no failure
    # to record yet. Only a job that has left the queue is judged.
    if placement and _placement_job_alive(placement, job_id) is True:
        return
    if size and placement:
        # A placed launch's wait status is the scheduler client's, not the
        # worker's, so a run whose payload log shows a turn ran has nothing
        # here that describes the worker: the client's status says nothing
        # about a process the scheduler still owns. A placed run that wrote no
        # byte is judged on the payload log, exactly as before.
        return
    wait_status = _wait_status_record(exit_status)
    failure = (
        None
        if size
        else _launch_failure_record(
            launched,
            exit_status=exit_status,
            placement=placement,
            job_id=job_id,
        )
    )

    def mutation(pointer: dict[str, Any]) -> dict[str, Any]:
        phase = str(pointer.get("phase") or "")
        if phase == LAUNCH_FAILED_PHASE or phase in _TERMINAL_RUN_PHASES:
            return pointer
        pointer["wait_status"] = wait_status
        if failure is None:
            return pointer
        pointer["phase"] = LAUNCH_FAILED_PHASE
        failures = list(pointer.get("launch_failures") or ())
        failures.append(failure)
        pointer["launch_failures"] = failures
        return pointer

    try:
        _mutate_pointer(run_id, mutation)
    except CrewError:
        # The pointer was reclaimed between the reap and the write; there is
        # no run left to mark, which is not this reaper's failure to report.
        return


def _reap_launched_workers() -> None:
    """Wait on every finished worker this process launched, without blocking.

    Only the pids written here are touched. A child a caller manages
    synchronously — a condition probe, a lane probe — is never a member, so
    this cannot reap it out from under its own ``wait``. A pid that no longer
    names a child has already been reaped or never was one; it is dropped so
    the loop does not probe a reused identifier forever.
    """
    with _LAUNCHED_WORKERS_LOCK:
        pending = list(_LAUNCHED_WORKERS)
    for pid in pending:
        try:
            got, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            with _LAUNCHED_WORKERS_LOCK:
                _LAUNCHED_WORKERS.discard(pid)
            continue
        except OSError:
            continue
        if got:
            with _LAUNCHED_WORKERS_LOCK:
                _LAUNCHED_WORKERS.discard(pid)
                launched = _LAUNCHED_WORKER_RUNS.pop(pid, None)
            if launched is not None:
                _record_launch_failure(
                    launched, exit_status=os.waitstatus_to_exitcode(status)
                )


def _worker_reaper_loop() -> None:
    """Poll the launched-worker set until the process dies.

    A daemon thread so it lives exactly as long as the process that owns the
    sweep — a short-lived dispatch CLI exits and takes it with it, a long-lived
    follower keeps it for the life of the session. It wakes promptly while work
    is outstanding and slows to a heartbeat when there is nothing to wait on.
    """
    while True:
        _LAUNCHED_WORKERS_WAKE.clear()
        with _LAUNCHED_WORKERS_LOCK:
            outstanding = bool(_LAUNCHED_WORKERS)
        _reap_launched_workers()
        # A registration then wakes this loop immediately, so a worker that
        # finishes seconds after launch is reaped seconds after registration
        # rather than on the next idle heartbeat. The slow branch only runs
        # when nothing is outstanding, and only decides how long to nap.
        _LAUNCHED_WORKERS_WAKE.wait(0.1 if outstanding else 2.0)


def _ensure_launched_worker_reaper() -> None:
    """Start the process's reaper whenever no live reaper is running.

    The recorded thread object is not evidence that the thread is running: a
    thread that ended for any reason — an uncaught exception in the poll loop
    — leaves its object in the holder, and a start-once guard keyed to the
    object would then silently stop reaping for the life of the process.
    """
    with _LAUNCHED_WORKERS_LOCK:
        recorded = _LAUNCHED_WORKER_REAPER["thread"]
        if recorded is not None and recorded.is_alive():
            return
        thread = threading.Thread(
            target=_worker_reaper_loop,
            name="reckon-worker-reaper",
            daemon=True,
        )
        thread.start()
        _LAUNCHED_WORKER_REAPER["thread"] = thread


# The launched-worker set crosses a follower's in-place process reload in the
# environment, which a process image replacement preserves. A file would
# survive too, but a stale file from a replacement that never happened could
# be adopted by an unrelated later process; the env var dies with the process
# and only this handover consumes it. A pipe survives as well and offers
# nothing over an env var, and module state is the carrier the replacement
# destroys, which is the defect the handover exists to fix.
_LAUNCHED_WORKERS_HANDOVER_ENV = "RECKON_FOLLOWER_LAUNCHED_PIDS"


def _export_launched_workers_for_reexec() -> None:
    """Hand the outstanding launched pids to the follower's replacement image.

    A follower that adopts newly installed code replaces its own process image
    with ``os.execv``. The replacement keeps the pid and every parent-child
    relationship and destroys all threads and module state, so a reaper thread
    and the launched-worker set built before the swap vanish even though the
    process is still the parent of the children it launched. Those children
    can then never be collected by anyone: their parent is alive, so they are
    not reparented to the init process that would collect them, and the new
    image has forgotten them. The reloader already carries its reader
    checkpoint across the boundary in the environment, so the pids cross the
    same way, at the same moment. Only outstanding pids are exported: a
    follower that launched nothing hands over nothing and the new image starts
    clean.
    """
    with _LAUNCHED_WORKERS_LOCK:
        outstanding = sorted(_LAUNCHED_WORKERS)
        carried = {
            str(pid): dict(_LAUNCHED_WORKER_RUNS[pid])
            for pid in outstanding
            if pid in _LAUNCHED_WORKER_RUNS
        }
    if outstanding:
        os.environ[_LAUNCHED_WORKERS_HANDOVER_ENV] = json.dumps(
            {"pids": outstanding, "runs": carried}
        )
    else:
        os.environ.pop(_LAUNCHED_WORKERS_HANDOVER_ENV, None)


def _adopt_launched_workers_from_reexec() -> None:
    """Register pids handed across a process image replacement, or start clean.

    The new follower image after an ``os.execv`` owns the same process, so the
    pids its previous image launched are still its children; registering them
    exactly as a launch would makes the reaper their owner again, because it
    is the same process and remains their parent. A carrier that is absent,
    empty or unparseable leaves the registry empty and does not raise, because
    a follower that refuses to start is worse than one that misses a reap.
    """
    raw = os.environ.pop(_LAUNCHED_WORKERS_HANDOVER_ENV, "")
    if not raw:
        return
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return
    # A bare pid list is the older carrier; the mapping is the current one.
    # Any other parsed type — a scalar, a string — is unparseable as a carrier
    # and starts the new image clean rather than raising from the iteration.
    if isinstance(payload, dict):
        pids = [int(pid) for pid in payload.get("pids") or ()]
        runs_carried = payload.get("runs") or {}
    elif isinstance(payload, (list, tuple)):
        pids = [int(pid) for pid in payload]
        runs_carried = {}
    else:
        return
    with _LAUNCHED_WORKERS_LOCK:
        _LAUNCHED_WORKERS.update(pid for pid in pids)
        for pid in pids:
            entry = runs_carried.get(str(pid))
            if isinstance(entry, dict):
                _LAUNCHED_WORKER_RUNS[pid] = dict(entry)
        adopted = bool(_LAUNCHED_WORKERS)
        if adopted:
            _LAUNCHED_WORKERS_WAKE.set()
    if adopted:
        _ensure_launched_worker_reaper()


def _launched_prior_session(plan: _backends.LaunchPlan | None) -> str | None:
    """Return the prior session this launch carried into argv, or None.

    A backend's ``session_reuse`` setting says the lane permits a run to
    continue an earlier session; only the command line says whether this run
    did. The plan records the session it was handed, and the answer is read
    back off the launch plan argv so a plan naming a session in its command line
    does not carry is not reported as a resumption.
    """
    if plan is None:
        return None
    session = plan.resumed_session
    if not session:
        return None
    carried = str(session)
    if str(session) not in [str(token) for token in plan.argv]:
        return None
    return carried


class LaunchResolutionError(CrewError):
    """A backend command could not be resolved against the launching PATH."""


def _current_host_facts() -> Any:
    """Read placement once at the decision that consumes it."""
    from reckon import host

    return host.host_facts()


def _worker_shim_directory() -> Path:
    """Return the scheduler-shim directory from its owning module."""
    from reckon.nested_launch import shim_directory

    return shim_directory()


def _worker_git_shim_directory() -> Path:
    """Return the git-shim directory from its owning module."""
    from reckon.worker_git_shim import worker_shim_directory

    return worker_shim_directory()


def _worker_host_line(facts: Any, run_directory: str | Path) -> str:
    """State the compute-host contract in one worker-prompt line."""
    if not facts.in_allocation:
        return ""
    node = facts.node or "unknown"
    job = facts.job_id or "unknown"
    scratch = "/tmp"  # noqa: S108 — the host probe's explicit scratch path
    tmp_clause = (
        f"{scratch} is node-local"
        if facts.tmp_is_node_local
        else f"{scratch} is not node-local"
    )
    return (
        f"HOST — ALLOCATION: node {node}; job {job}; {tmp_clause}; the work "
        "runs in place — do not open an srun or salloc step in this job or an "
        "sbatch into it; a separate partition job (for example betelgeuse or a "
        "*_debug partition) is submitted with RECKON_ALLOW_NESTED_LAUNCH=1 when "
        "the done-when names one; logs a later reader needs go under "
        f"{run_directory}."
    )


def launch_search_path(
    environment: Mapping[str, str] | None = None,
    *,
    facts: Any | None = None,
) -> str:
    """Return the effective worker PATH for one launch.

    The git shim is first on every worker's path, inside an allocation or out
    of one: it refuses a mutating verb aimed at a repository other than the
    run's worktree, which is a hazard wherever the worker runs. The scheduler
    shims follow it only inside an allocation, because there is no scheduler
    out of one to refuse.

    An inherited entry holding a reckon shim for any of those tools is dropped,
    whichever checkout it belongs to. A dispatch from a worktree or a copy of
    reckon inherits the main checkout's shims from the worker it runs in, and
    two checkouts' shims on one path resolve to each other rather than to the
    real tool.
    """
    from reckon.shim_lookup import holds_shim

    merged = {**os.environ, **(environment or {})}
    inherited = str(merged.get("PATH") or os.defpath)
    placement = _current_host_facts() if facts is None else facts
    directories = [str(_worker_git_shim_directory())]
    if placement.in_allocation:
        directories.append(str(_worker_shim_directory()))
    resolved = {os.path.realpath(directory) for directory in directories}
    shimmed = sorted(
        {name for directory in directories for name in _shim_names(directory)}
    )
    inherited_entries = [
        entry
        for entry in inherited.split(os.pathsep)
        if entry
        and os.path.realpath(entry) not in resolved
        and not holds_shim(entry, shimmed)
    ]
    return os.pathsep.join([*directories, *inherited_entries])


def _shim_names(directory: str) -> list[str]:
    """The tool names the shim directory stands in for."""
    try:
        return [entry.name for entry in os.scandir(directory) if entry.is_file()]
    except OSError:
        return []


def _persisted_worker_environment(
    environment: Mapping[str, str] | None = None,
    *,
    facts: Any | None = None,
) -> dict[str, str]:
    """Return only the launch overlay safe to persist in a run record."""
    persisted = dict(environment or {})
    for key in (
        "RECKON_RUN_ID",
        "RECKON_MANIFEST",
        "RECKON_ATTEMPT_STARTED_AT",
    ):
        persisted.pop(key, None)
    headers = str(persisted.get("ANTHROPIC_CUSTOM_HEADERS") or "").splitlines()
    if (
        len(headers) >= 2
        and headers[-2].startswith("X-Reckon-Run-Id:")
        and headers[-1].startswith("X-Reckon-Session:")
    ):
        headers = headers[:-2]
        if headers:
            persisted["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(headers)
        else:
            persisted.pop("ANTHROPIC_CUSTOM_HEADERS", None)
    placement = _current_host_facts() if facts is None else facts
    persisted["PATH"] = launch_search_path(environment, facts=placement)
    return persisted


# A worker's scratch lives and dies with its run. The host's temp root is
# node-local and shared by every session on the machine, so a run that writes
# there unattended leaves entries nothing owns and nothing removes. Each run is
# therefore handed a private directory beneath a reckon-owned root, named for
# its run id, and pointed at it by TMPDIR; promotion and discard remove that
# directory. Release also removes direct siblings bearing that run's timestamp
# stem, because worker-created basetemps may sit beside TMPDIR. Other run ids
# remain outside the release's reach.
WORKER_SCRATCH_ROOT_ENV = "RECKON_WORKER_SCRATCH_ROOT"
WORKER_SCRATCH_ROOT_NAME = "reckon-crew-scratch"
# The node's own tmp, pinned rather than taken from TMPDIR, the way the fleet
# supervisor pins the runtime sockets it owns: the allocator may point TMPDIR at
# shared storage, and a scratch root that followed TMPDIR would then place every
# run's scratch on GPFS and — worse — resolve differently in the dispatcher and
# in the promotion or discard that later removes it, so the removal would miss
# and the directory would leak.
WORKER_SCRATCH_ROOT_DEFAULT = "/tmp"  # noqa: S108 - the node's own tmp, never shared
# The size above which a run's scratch is reported at the moment it lands, so a
# run that wrote gigabytes is visible on its own ledger row rather than only to
# whoever later finds the temp directory full. Node-local disk is a scarce shared
# allocation, so the figure is a report and never a refusal: a run that legitimately
# needs more space is not stopped, it is named.
WORKER_SCRATCH_BUDGET_BYTES = 2 * 1024**3


class TmpHeadroomError(CrewError):
    """The scratch filesystem cannot admit another worker."""

    def __init__(self, free_bytes: int, floor_bytes: int):
        self.free_bytes = free_bytes
        self.floor_bytes = floor_bytes
        super().__init__(
            f"tmp-headroom-refusal: {free_bytes} bytes free, floor "
            f"{floor_bytes} bytes; run `reckon crew gc --project <project>` "
            "to inspect reclaimable worker scratch"
        )


def require_worker_scratch_headroom(config: Mapping[str, Any]) -> dict[str, int]:
    """Check the filesystem that will hold run scratch before worktree creation."""
    root = worker_scratch_root()
    probe = root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    stat = os.statvfs(probe)
    free = stat.f_bavail * stat.f_frsize
    total = stat.f_blocks * stat.f_frsize
    worktree = config.get("worktree") or {}
    floor = max(
        int(worktree.get("scratch_min_free_bytes", 5 * 1024**3)),
        total * int(worktree.get("scratch_min_free_pct", 10)) // 100,
    )
    if free < floor:
        raise TmpHeadroomError(free, floor)
    return {"free_bytes": free, "floor_bytes": floor}


def worker_scratch_root() -> Path:
    """The node-local root every run's scratch directory sits beneath.

    ``RECKON_WORKER_SCRATCH_ROOT`` overrides it, so a test can synthesise a root
    without writing to the host's real temp directory and a caller can place the
    whole fleet's scratch somewhere it would rather own. Without an override the
    root is pinned to the node's own tmp rather than read from ``TMPDIR``: a
    shared-storage ``TMPDIR`` would otherwise move every run's scratch onto GPFS,
    and a ``TMPDIR`` that differs between the dispatcher and the promotion or
    discard that removes the directory would make each resolve a different root,
    so the removal would report the directory absent and leak it.
    """
    override = os.environ.get(WORKER_SCRATCH_ROOT_ENV)
    if override:
        return Path(override)
    return Path(WORKER_SCRATCH_ROOT_DEFAULT) / WORKER_SCRATCH_ROOT_NAME


def worker_scratch_dir(run_id: str) -> Path:
    """The scratch directory one run owns, named for its run id."""
    return worker_scratch_root() / str(run_id)


def tree_size_bytes(path: Path, *, limit: int | None = None) -> int:
    """Total bytes of the regular files beneath ``path``, symlinks excluded.

    Taken while the directory still exists, because the size is recorded on the
    run's terminal row at the moment the directory dies and there is no second
    chance to measure it afterwards. A file that cannot be stat'd is skipped
    rather than failing the walk: a size a few bytes short still names the tree
    that filled the disk, while raising here would leave the directory in place.
    ``limit`` bounds the walk, so a caller measuring a directory it does not own
    on a shared temp root cannot be made to traverse a whole corpus; the sum is
    then a floor rather than an exact figure, which is all a report needs.
    """
    total = 0
    seen = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            seen += 1
            if limit is not None and seen > limit:
                return total
            candidate = Path(root) / name
            try:
                if not candidate.is_symlink():
                    total += candidate.stat().st_size
            except OSError:
                continue
    return total


def ensure_worker_scratch(run_id: str) -> Path:
    """Create the run's scratch directory if it is absent and return it."""
    path = worker_scratch_dir(run_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _removable_scratch_target(
    run_id: str, recorded_path: str | os.PathLike[str] | None
) -> tuple[Path | None, str]:
    """The directory a run may remove, or ``None`` and the reason it may not.

    Every check the removal depends on lives here so it applies whatever path a
    caller offers. A ``recorded_path`` gets no more reach than the path computed
    from the run id: it must resolve to a direct child of the resolved scratch
    root, its final component must equal the validated run id, and it must be a
    real directory rather than a symlink. A recorded path is therefore only ever
    allowed to equal ``<scratch root>/<run id>`` beneath the root that resolves
    now, so it narrows what may be removed rather than widening it. A record made
    under a scratch root that has since moved names a directory no longer a child
    of the resolved root; it is withheld and left in place, never removed.
    """
    name = str(run_id or "").strip()
    # A run id is a single path component and is never a parent reference. A
    # separator, an absolute path, or a bare "." / ".." names no scratch
    # directory this function may remove: ".." would otherwise resolve to the
    # temp directory itself and be removed.
    if not name or name in {".", ".."} or Path(name).name != name:
        return None, "run id names no scratch directory"
    root = worker_scratch_root()
    path = Path(recorded_path) if recorded_path else root / name
    # Checked before the resolve below, because a symlink resolves to its
    # target and would let a recorded path point anywhere under the root.
    if path.is_symlink():
        return None, "scratch path is a symlink"
    resolved_root = root.resolve()
    try:
        resolved_path = path.resolve()
    except OSError:
        resolved_path = path
    if resolved_path.parent != resolved_root or resolved_path.name != name:
        return None, "scratch path is not the directory this run owns"
    if not path.is_dir():
        return None, "scratch directory is no longer present"
    return path, ""


def remove_worker_scratch(
    run_id: str,
    *,
    recorded_path: str | os.PathLike[str] | None = None,
    budget_bytes: int | None = None,
) -> dict[str, Any]:
    """Remove a run's scratch directories, printing each path and size.

    The path removed is ``<scratch root>/<run id>``, or the ``recorded_path`` a
    dispatch recorded when it created the directory — which is accepted only
    when it names exactly that directory beneath the root that resolves now (see
    ``_removable_scratch_target``), so a record whose scratch field names a
    different run's directory removes nothing. A record made under a scratch
    root that has since moved is therefore withheld and left in place, never
    removed. An absent directory is reported rather than raised: a run whose
    scratch was already reclaimed has nothing left to remove.

    Direct siblings whose names begin with the run's timestamp stem are also
    removed. The size of each removed directory is retained in
    ``scratch_removed_paths``, while ``scratch_bytes`` reports their sum. A
    total above ``budget_bytes`` adds a warning but never refuses removal.
    """
    name = str(run_id or "").strip()
    attempted = (
        Path(recorded_path)
        if recorded_path
        else (worker_scratch_root() / name if name else None)
    )
    path, reason = _removable_scratch_target(run_id, recorded_path)
    result: dict[str, Any] = {
        "scratch_removed": False,
        "scratch_path": str(path or attempted) if (path or attempted) else None,
        "scratch_withheld": reason,
        "scratch_bytes": None,
        "scratch_removed_paths": [],
    }
    if path is None and reason != "scratch directory is no longer present":
        return result
    targets = [path] if path is not None else []
    # A worker may create siblings named for the timestamp stem instead of
    # placing every temporary file below TMPDIR. That stem is unique to the
    # run; only direct, real directories with the same stem are in its reach.
    stem = re.match(r"^(r-\d{8}T\d{12})(?:-|$)", name)
    root = worker_scratch_root()
    if stem and root.is_dir():
        targets.extend(
            child
            for child in root.iterdir()
            if child != path
            and child.name.startswith(stem.group(1))
            and child.is_dir()
            and not child.is_symlink()
        )
    if not targets:
        return result
    sizes = {target: tree_size_bytes(target) for target in targets}
    result["scratch_bytes"] = sum(sizes.values())
    if budget_bytes is not None and result["scratch_bytes"] > budget_bytes:
        result["scratch_warning"] = (
            f"run scratch for {name} is {result['scratch_bytes']} bytes, "
            f"above the {budget_bytes} byte budget"
        )
        print(
            f"warning: run scratch for {name} is {result['scratch_bytes']} bytes, "
            f"above the {budget_bytes} byte budget"
        )
    failures = []
    for target in targets:
        size = sizes[target]
        print(f"removing worker scratch directory {target} ({size} bytes)")
        try:
            shutil.rmtree(target)
        except OSError as exc:
            failures.append(f"{target}: {exc}")
            continue
        result["scratch_removed_paths"].append({"path": str(target), "bytes": size})
        print(f"removed worker scratch directory {target}")
    result["scratch_removed"] = not failures
    result["scratch_withheld"] = "; ".join(failures)
    return result


def _worker_runtime_environment(
    environment: Mapping[str, str] | None,
    *,
    run_id: str,
    manifest_path: str,
    attempt_started_at: str,
    coordinator_session: str,
    claude_headers: bool,
) -> dict[str, str]:
    """Add one attempt's identity to the environment inherited by its worker."""
    runtime = dict(environment or {})
    runtime.update(
        {
            "RECKON_RUN_ID": run_id,
            "RECKON_MANIFEST": manifest_path,
            "RECKON_ATTEMPT_STARTED_AT": attempt_started_at,
        }
    )
    if run_id:
        runtime["TMPDIR"] = str(ensure_worker_scratch(run_id))
    if claude_headers:
        inherited = str(
            runtime.get("ANTHROPIC_CUSTOM_HEADERS")
            or os.environ.get("ANTHROPIC_CUSTOM_HEADERS")
            or ""
        ).rstrip("\n")
        attribution = (
            f"X-Reckon-Run-Id: {run_id}\nX-Reckon-Session: {coordinator_session}"
        )
        runtime["ANTHROPIC_CUSTOM_HEADERS"] = (
            f"{inherited}\n{attribution}" if inherited else attribution
        )
    return runtime


def _worker_runtime_plan(
    plan: _backends.LaunchPlan,
    *,
    run_id: str,
    manifest_path: str,
    attempt_started_at: str,
    coordinator_session: str,
) -> _backends.LaunchPlan:
    """Return a launch plan carrying the current run and attempt identity."""
    return dataclasses.replace(
        plan,
        environment=_worker_runtime_environment(
            plan.environment,
            run_id=run_id,
            manifest_path=manifest_path,
            attempt_started_at=attempt_started_at,
            coordinator_session=coordinator_session,
            claude_headers=plan.dialect == "claude",
        ),
    )


def _worker_process_environment(
    environment: Mapping[str, str] | None, *, dialect: str
) -> dict[str, str]:
    """Merge inherited launch state while withholding Claude headers from Codex."""
    merged = {**os.environ, **(environment or {})}
    if dialect != "claude":
        merged.pop("ANTHROPIC_CUSTOM_HEADERS", None)
    return merged


def harness_command_index(argv: Any) -> int:
    """Position of a launch argv's harness command.

    A fenced launch is ``<fence> <binds...> -- <harness> ...``, so the harness is
    the first token behind the fence's own ``--`` separator, searched for only
    after the fence element because the fence element is what distinguishes a
    composition from a bare argv. An argv whose first element is not the fence
    binary is not fenced, and its harness is its own first element at index 0.

    Read by the composed-plan rewrite and by the pre-flight resolution, so the
    two cannot disagree about which token is the backend.
    """
    if not isinstance(argv, (list, tuple)) or not argv:
        return 0
    if Path(str(argv[0])).name != _backends.FENCE_BINARY:
        return 0
    if "--" not in argv[1:]:
        return 0
    return argv.index("--", 1) + 1


def _unresolved_backend_command(binary: str, searched: str) -> str:
    """The one refusal an unresolvable backend command produces."""
    return (
        f"backend command {binary!r} cannot be resolved on the PATH this "
        f"launch would search: {searched} — install it or add its directory "
        "to PATH, then retry; nothing has been launched"
    )


def preflight_launch_command(
    backend_name: str,
    backend: Mapping[str, Any],
    *,
    fence: bool,
    facts: Any | None = None,
) -> str:
    """Resolve a backend's harness command before its plan is composed.

    Composing the plan seeds the run's own harness home, so a refusal that
    waited for the composed argv would leave a half-written run behind on a
    resume and a run directory behind on a dispatch. The harness's position is
    read through the same helper the composed-plan rewrite uses, so the
    pre-flight and that rewrite cannot disagree about which token is the
    backend. Returns the absolute command, or raises
    :class:`LaunchResolutionError`.

    A backend naming no command returns empty rather than refusing here, so the
    missing-command refusal stays where it belongs — at composition, which
    knows the launch kind.
    """
    from reckon.flight import expand_backend_environment

    command = str(backend.get("command") or "")
    if not command:
        return ""
    # The shape the composed launch will name: the harness alone when unfenced,
    # and behind the harness offset by the fence and its separator when fenced.
    # The binds the fence inserts between are irrelevant to the position, which
    # the helper reads from the fence element and the first separator after it.
    shape = [command] if not fence else [_backends.FENCE_BINARY, "--", command]
    binary = shape[harness_command_index(shape)]
    environment = expand_backend_environment(backend_name, backend)
    searched = launch_search_path(environment, facts=facts)
    resolved = shutil.which(binary, path=searched) if binary else None
    if not resolved:
        raise LaunchResolutionError(_unresolved_backend_command(binary, searched))
    return os.path.abspath(resolved)


def resolve_launch_executable(
    plan: _backends.LaunchPlan,
    *,
    environment: Mapping[str, str] | None = None,
    facts: Any | None = None,
) -> _backends.LaunchPlan:
    """Return the plan with the backend's own binary replaced by an absolute path.

    The launch inherits the PATH of whoever started it, so a watcher armed
    without the backend directory execs a bare name, dies at exec and leaves an
    empty stream that reads as a worker turn. Resolving at plan construction
    makes the launch either runnable or an explicit refusal, and the refusal
    names the binary and the PATH that was searched so the repair is a command
    rather than an investigation.

    A fenced composition's first element is the fence binary, not the backend:
    the harness is the command behind the fence's ``--`` separator. Resolving
    the first element would resolve the fence itself, and the fence binary is
    present on every host able to launch at all, so a missing backend would be
    accepted and the launch would die behind the fence with nothing naming why.
    The fence element is therefore never taken as the backend.

    ``environment`` is the overlay the launch will run with; absent, the launch's
    own environment is used, which is what every construction site passes.
    """
    selected_environment = plan.environment if environment is None else environment
    searched = launch_search_path(selected_environment, facts=facts)
    element = harness_command_index(plan.argv)
    binary = str(plan.argv[element]) if element < len(plan.argv) else ""
    resolved = shutil.which(binary, path=searched) if binary else None
    if not resolved:
        raise LaunchResolutionError(_unresolved_backend_command(binary, searched))
    # Absolute, not canonical: a launcher installed as ``bin/codex`` symlinked
    # to ``codex.js`` must still be exec'd under the name the launch was
    # configured with, because that name is how the command's dialect is
    # selected and how the run records what it ran.
    resolved = os.path.abspath(resolved)
    argv = list(plan.argv)
    argv[element] = resolved
    return dataclasses.replace(plan, argv=argv)


def apply_backend_placement(
    plan: _backends.LaunchPlan,
    backend: Mapping[str, Any],
    project: str | None = None,
) -> _backends.LaunchPlan:
    """Prefix an already-resolved launch with its backend's declared placement.

    The resolved argv is carried through unchanged behind the scheduler
    invocation, so the absolute executable, the environment, and the stdin,
    stdout and stderr paths the launch was built with are the ones that run.
    The scheduler executable is resolved against the same PATH the launch
    searches, because an unresolvable wrapper would die at exec and leave an
    empty stream that reads as a worker turn — the failure the launch resolution
    above exists to turn into an explicit refusal.

    A backend declaring no placement is returned untouched, which is what keeps
    an undeclared backend launching as a child of the coordinator exactly as it
    does today.
    """
    from reckon import flight

    placement = flight.placement_for(backend)
    if placement is None:
        return plan
    searched = launch_search_path(plan.environment)
    scheduler = str(placement["scheduler"])
    found = shutil.which(scheduler, path=searched)
    if not found:
        raise LaunchResolutionError(
            f"the declared placement names scheduler {scheduler!r}, which cannot "
            f"be resolved on the PATH this launch would search: {searched} — "
            "install it or add its directory to PATH, then retry; nothing has "
            "been launched"
        )
    from reckon.crew import placement as placement_module

    options = [str(item) for item in placement.get("options") or ()]
    # The one shared reservation, which any project's dispatch resolves: a
    # worker placed into it runs where it was sized to and is counted against
    # the roster that admits it.
    reservation = placement_module.read_reservation(project)
    if reservation and placement_module.reservation_alive(reservation):
        # The reservation is held and its job id is published, so this worker
        # runs inside it as an overlapping step rather than as an allocation of
        # its own. The id is resolved from the shared state rather than taken on
        # the command line, which is what makes one reservation shared by every
        # session instead of one per dispatcher.
        prefix = [
            os.path.abspath(found),
            *placement_module.step_prefix(str(reservation["job_id"]), options),
        ]
    else:
        prefix = [os.path.abspath(found), *options]
    return dataclasses.replace(plan, argv=[*prefix, *plan.argv])


def _placement_hold_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    """The hold's report as a dispatch payload carries it.

    A session that arms the reservation through a dispatch reads the run's
    record, not the ensure's return value, so what the hold states about the
    roster cap — its reach, and which projects' placed runs it counts right now
    — is copied onto the payload where that session reads it. The job id and
    the reason are carried beside it so a reader can tell an allocation this
    dispatch minted from one it joined.
    """
    return {
        "job_id": result.get("job_id"),
        "reason": result.get("reason"),
        "detail": result.get("detail"),
        "roster_reach": result.get("roster_reach"),
    }


def resolve_backend_placement(
    plan: _backends.LaunchPlan,
    backend: Mapping[str, Any],
    project: str | None = None,
    *,
    payload: dict[str, Any] | None = None,
) -> _backends.LaunchPlan:
    """Hold or join the shared reservation, then place the launch inside it.

    A dispatch declaring a placement runs inside the one allocation every worker
    of the host shares. The declared wrapping on its own prefixes a bare step
    client, which carries no job id and publishes nothing for the next dispatch
    to find, so every dispatch mints an allocation of its own; the ensure is
    reached instead so the allocation is created once, by whichever dispatch
    arrives first, and every later dispatch joins it. ``ensure_reservation``
    decides on the liveness probe under a cross-process lock and starts nothing
    when a live reservation is already held, which is what makes a dispatch that
    finds one — or that races another — join the allocation rather than mint a
    second beside it.

    The ensure's result is kept rather than discarded: when a ``payload`` is
    given, the hold's reach statement is carried on it under
    ``placement_reservation``, because dispatch is how most sessions arm the
    reservation and the run's record is the only report they read.

    A backend declaring no placement runs outside any reservation, so it is
    returned untouched: nothing is held and the scheduler is not asked after.
    """
    from reckon import flight

    if flight.placement_for(backend) is None:
        return plan
    from reckon.crew import placement as placement_module

    hold = placement_module.ensure_reservation(project=project)
    if payload is not None:
        payload["placement_reservation"] = _placement_hold_payload(hold)
    return apply_backend_placement(plan, backend, project)


# How long a declared job-id probe is given to answer, and how many times it is
# retried. The scheduler assigns the identifier as the job is admitted, so a
# probe read in the same instant as the spawn can precede the assignment.
_PLACEMENT_PROBE_TIMEOUT_SECONDS = 10
_PLACEMENT_PROBE_ATTEMPTS = 3


def placement_job_id(
    placement: Mapping[str, Any] | None,
    *,
    run_id: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> tuple[str | None, str]:
    """Ask the scheduler which job a placed launch became.

    Returns the identifier and a status naming what happened, because a
    placement that reaches a job always identifies it while one that cannot
    must say so rather than record a fabricated id. ``{run}`` in the probe
    argument vector is replaced by the run id, which is how a probe addresses
    the job it is asking about without reckon knowing any scheduler's own
    vocabulary.
    """
    if not placement:
        return None, "no-placement"
    probe = placement.get("job_id_probe")
    if not probe:
        return None, "no-probe-declared"
    argv = [str(token).replace("{run}", run_id) for token in probe]
    run = runner or subprocess.run
    last = ""
    for attempt in range(_PLACEMENT_PROBE_ATTEMPTS):
        try:
            completed = run(
                argv,
                capture_output=True,
                text=True,
                timeout=_PLACEMENT_PROBE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            last = f"probe failed to run — {exc}"
        else:
            for token in str(completed.stdout or "").split():
                if token.isdigit():
                    return token, "recorded"
            last = (
                "probe answered no identifier "
                f"(exit {completed.returncode})"
            )
        if attempt + 1 < _PLACEMENT_PROBE_ATTEMPTS:
            time.sleep(1.0)
    return None, last or "probe answered no identifier"


def assert_routable_backends_resolvable(
    project: str,
    config: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Refuse when any backend this project can route to has no executable.

    A watcher that cannot resolve a backend it may be asked to lift is a
    watcher that reads as armed and silently loses every park it lifts, so the
    check runs before the registration is taken rather than at the first lift.
    """
    resolved: list[dict[str, str]] = []
    from reckon import flight

    for name in sorted((config.get("backends") or {}), key=str):
        backend = (config.get("backends") or {})[name] or {}
        if backend.get("launch") != "cli":
            continue
        command = str(backend.get("command") or "")
        if not command:
            continue
        environment = flight.expand_backend_environment(str(name), backend)
        path = launch_search_path(environment)
        found = shutil.which(command, path=path)
        if not found:
            raise LaunchResolutionError(
                f"project {project!r} routes to backend {name!r} whose command "
                f"{command!r} cannot be resolved on the PATH the launch would "
                f"search: {path} — install it or add its directory to PATH, "
                "then arm the watcher again; it is not armed"
            )
        resolved.append(
            {
                "backend": str(name),
                "command": command,
                "executable": os.path.abspath(found),
            }
        )
    return resolved


def _spawn(
    plan: _backends.LaunchPlan,
    *,
    log_path: Path,
    stderr_path: Path,
    prompt_path: Path,
) -> int:
    """Start one backend attempt, supervising continuations by run identity.

    Fresh dispatches already construct their supervisor before reaching this
    compatibility seam. The two callers that execute a later attempt name that
    attempt in its stream path: ``resume-*`` for a same-lane continuation and
    ``lane-change-*`` for a redispatch. Those attempts must leave through the
    same supervisor as the first one so the pointer names the supervisor and a
    worker exit is collected after the caller returns.

    Other callers retain the detached worker helper below. They are internal
    launch probes with no continuation identity; treating an arbitrary stream
    filename as a run attempt would make its parent directory into a run by
    accident.
    """
    _require_fleet_gate_open()
    stream_name = Path(log_path).name
    if stream_name.startswith(("resume-", "lane-change-")):
        directory = Path(log_path).parent
        record = read_pointer(directory.name)
        worktree = str(record.get("worktree") or "")
        return supervised_launch(
            plan,
            run_directory=directory,
            repo_root=Path(str(record.get("repo") or directory)),
            worktree=Path(worktree) if worktree else directory,
            log_path=Path(log_path),
            stderr_path=Path(stderr_path),
            prompt_path=Path(prompt_path),
        )
    return _spawn_detached_worker(
        plan,
        log_path=log_path,
        stderr_path=stderr_path,
        prompt_path=prompt_path,
    )


# bwrap refuses a launch whose read-only bind source cannot be found, naming
# the source in its refusal — one phrase when the source is gone before the
# mount is attempted, another when the kernel refuses it. The fence composes
# those binds from the paths that exist when it is built, and a writer that
# replaces a file by rename leaves the name missing for an instant, so a spawn
# landing in that instant dies before the worker starts while a launch a moment
# later would have begun normally. A spawn that dies this way is retried rather
# than recorded as a launch failure.
_VANISHED_BIND_REFUSAL_PHRASES = ("Can't bind mount", "Can't find source path")

# How long a freshly spawned worker is given to prove it started, and how often
# its exit is looked for inside that window. bwrap refuses a missing bind source
# before it execs the harness, so the window only has to cover bwrap's own
# startup — but that startup competes with every other process on the host, so
# the window is sized for a loaded host rather than for the refusal itself: a
# child still running at the end of it is a launch that began.
SANDBOX_STARTUP_WINDOW_SECONDS = 0.5
VANISHED_BIND_STARTUP_POLL_SECONDS = 0.01


def _read_only_bind_sources(argv: Iterable[str]) -> list[str]:
    """The read-only bind sources a launch argv would mount."""
    words = list(argv)
    sources: list[str] = []
    for index, word in enumerate(words):
        if str(word) == "--ro-bind" and index + 2 < len(words):
            sources.append(str(words[index + 1]))
    return sources


def _refusal_names_a_bind_source(refusal: str, sources: Iterable[str]) -> bool:
    """Whether bwrap's refusal is the missing-source one for this launch."""
    if not any(phrase in refusal for phrase in _VANISHED_BIND_REFUSAL_PHRASES):
        return False
    return any(source and source in refusal for source in sources)


def _died_over_a_vanished_bind_source(
    process: subprocess.Popen,
    *,
    argv: Iterable[str],
    stderr_path: Path,
) -> bool:
    """Whether a just-spawned worker already died over a missing bind source.

    The window a rename leaves is momentary, so this only answers true for the
    failure that window causes: the child exited inside the startup poll, and
    bwrap's refusal names one of the sources this launch asked it to mount. A
    child still running at the end of the window started, and a child that
    exited over anything else is the launch failure its caller records.
    """
    sources = _read_only_bind_sources(argv)
    if not sources:
        return False
    # A caller may hand the launch a stand-in rather than a real process; such a
    # launch cannot be observed starting, and the refusal is proved by the
    # child's own exit, so there is nothing the retry could key on.
    poll = getattr(process, "poll", None)
    if poll is None:
        return False
    deadline = time.monotonic() + SANDBOX_STARTUP_WINDOW_SECONDS
    while poll() is None and time.monotonic() < deadline:
        time.sleep(VANISHED_BIND_STARTUP_POLL_SECONDS)
    if poll() is None or process.returncode == 0:
        return False
    try:
        refusal = stderr_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return _refusal_names_a_bind_source(refusal, sources)


def _spawn_worker_retrying_a_vanished_bind(
    attempt: Callable[[], subprocess.Popen],
    *,
    argv: Iterable[str],
    stderr_path: Path,
) -> subprocess.Popen:
    """Spawn a worker, retrying while the fence's own bind source is missing.

    The retry is inside one attempt: a worker that starts on a later try is the
    attempt that launched, so nothing is recorded as a failure for the tries a
    momentary rename defeated. The tries are bounded by the constants the
    composition's own wait uses. A spawn that failed for any other reason is
    returned at once for its caller to record as the launch failure it is.
    """
    process = attempt()
    tries = 1
    limit = max(1, _backends.PROTECTED_BIND_WAIT_ATTEMPTS)
    while tries < limit and _died_over_a_vanished_bind_source(
        process, argv=argv, stderr_path=stderr_path
    ):
        # The dead try is reaped here so it cannot be mistaken later for the
        # worker's own exit by whoever collects the run's children.
        process.wait()
        time.sleep(_backends.PROTECTED_BIND_WAIT_INTERVAL_SECONDS)
        tries += 1
        process = attempt()
    return process


def _spawn_detached_worker(
    plan: _backends.LaunchPlan,
    *,
    log_path: Path,
    stderr_path: Path,
    prompt_path: Path,
) -> int:
    """Start a backend process detached, with its event stream landing on disk.

    The prompt is fed from a file rather than a pipe so the caller never blocks
    on a full pipe buffer, and so the exact prompt stays recoverable beside the
    stream it produced. ``start_new_session`` detaches the worker from the
    caller's process group: a dispatching agent that ends its turn must not take
    its workers down with it.

    Every launched pid is registered with the reaper, which owns the wait the
    caller will never get around to making. The worker is still the caller's
    child — it has to be, for the pid to mean anything — but it is a child the
    caller does not owe a wait on.
    """
    argv = [str(word) for word in plan.argv]

    def attempt() -> subprocess.Popen:
        with (
            open(prompt_path, "rb") as stdin,
            open(log_path, "wb") as stdout,
            open(stderr_path, "wb") as stderr,
        ):
            return subprocess.Popen(
                argv,
                cwd=plan.cwd,
                env=_worker_process_environment(
                    plan.environment,
                    dialect=plan.dialect,
                ),
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )

    process = _spawn_worker_retrying_a_vanished_bind(
        attempt, argv=argv, stderr_path=Path(stderr_path)
    )
    launched = _launched_worker_record(plan, log_path, stderr_path)
    # A poll of a child that has already exited collects its status, and the
    # retry's startup check polls the child. A worker that died inside that
    # window is therefore no longer waitable: registering the pid would hand
    # the reaper a child it cannot wait, and both the failure record and the
    # registered pid would be lost. The exit the check collected is recorded
    # here exactly as a reap would record it.
    exit_status = getattr(process, "returncode", None)
    if exit_status is not None:
        if launched is not None:
            _record_launch_failure(launched, exit_status=exit_status)
        return process.pid
    with _LAUNCHED_WORKERS_LOCK:
        _LAUNCHED_WORKERS.add(process.pid)
        if launched is not None:
            _LAUNCHED_WORKER_RUNS[process.pid] = launched
    _LAUNCHED_WORKERS_WAKE.set()
    _ensure_launched_worker_reaper()
    return process.pid


# ── The per-run supervisor ──────────────────────────────────────────────────
#
# Dispatch must return as soon as a worker exists, but two things must happen
# after it returns and neither can be left to a process that is gone: the
# boundary tree snapshot — a walk whose cost grows with the repository's
# worktree count — and collecting the worker's exit, which a reparented worker
# hands to init and reckon never sees. Both move to one small process, started
# per run in its own session, that outlives the dispatch command. Dispatch
# writes the run's pointer naming that process and exits; the supervisor takes
# the snapshot, spawns the worker, waits on it and writes the exit to the run
# directory, then exits itself.
#
# The supervisor never writes the pointer and never creates the run directory.
# Everything it produces lands beside the worker's stream under the run
# directory, so a discard that removes only the pointer leaves the exit record
# findable, and a discard that removes the whole directory drops the write
# rather than bringing the run back.
SUPERVISOR_SPEC_NAME = "supervisor.json"
TREE_SNAPSHOT_NAME = "tree-snapshot.json"
WORKER_RECORD_NAME = "worker.json"
EXIT_RECORD_NAME = "exit.json"
ATTEMPT_RECORD_NAME = "attempt.json"

# The argv token that runs this module as the per-run supervisor. Named with
# underscores so it can never collide with a real crew subcommand.
SUPERVISOR_ENTRY = "__supervise__"


def _supervisor_write(path: Path, payload: Mapping[str, Any]) -> bool:
    """Write JSON into a run directory without ever creating that directory.

    The run directory is the supervisor's only durable home, and it may be
    removed while the supervisor is mid-run — a discard takes it. A write that
    recreated the directory would bring a discarded run back into existence, so
    a write whose directory has gone is dropped and reported by its return
    value rather than by raising.
    """
    if not path.parent.is_dir():
        return False
    try:
        _store.write_json_atomically(path, payload, create_parents=False)
    except OSError:
        return False
    return True


def _stream_record_facts(paths: Iterable[Path]) -> tuple[int, Any]:
    """Count a run's stream records and name the newest one's type.

    Streams arrive newest write first, so the newest record is the last
    parseable line of the first stream that holds any. A line that is not a
    JSON object is not a record, so a truncated tail cannot masquerade as one.
    """
    total = 0
    newest_type: Any = None
    for path in paths:
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        count = 0
        last_type: Any = None
        with handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, Mapping):
                    continue
                count += 1
                last_type = event.get("type")
        total += count
        if count and newest_type is None:
            newest_type = last_type
    return total, newest_type


def _attempt_artifact_path(run_directory: Path, name: str, attempt: int) -> Path:
    """Return the immutable path carrying one attempt's worker or exit record."""
    return run_directory / f"attempt-{attempt}-{name}"


def _prepare_attempt_records(
    run_directory: Path,
    *,
    run_id: str,
    attempt: int,
    attempt_kind: str,
    attempt_started_at: str,
) -> None:
    """Publish current attempt identity and retire prior canonical records.

    The immutable attempt files retain every worker and exit receipt. The two
    canonical names remain the classifier's current-attempt surface, so they
    are cleared before a new supervisor can start. Publishing the marker first
    prevents a late exit from the previous supervisor from reclaiming those
    canonical names while the replacement is launching.
    """
    _supervisor_write(
        run_directory / ATTEMPT_RECORD_NAME,
        {
            "run_id": run_id,
            "attempt": attempt,
            "attempt_kind": attempt_kind,
            "attempt_started_at": attempt_started_at,
        },
    )
    for name in (WORKER_RECORD_NAME, EXIT_RECORD_NAME):
        current = run_directory / name
        try:
            payload = json.loads(current.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, Mapping):
            archived = dict(payload)
            try:
                recorded_attempt = int(archived.get("attempt") or attempt - 1)
            except (TypeError, ValueError):
                recorded_attempt = max(1, attempt - 1)
            archived["attempt"] = recorded_attempt
            _supervisor_write(
                _attempt_artifact_path(run_directory, name, recorded_attempt),
                archived,
            )
        current.unlink(missing_ok=True)


def _attempt_is_current(run_directory: Path, attempt: int) -> bool:
    """Whether the run directory still names this supervisor's attempt."""
    try:
        marker = json.loads(
            (run_directory / ATTEMPT_RECORD_NAME).read_text(encoding="utf-8")
        )
        return isinstance(marker, Mapping) and int(marker.get("attempt")) == attempt
    except (OSError, TypeError, ValueError):
        return False


def _write_attempt_artifact(
    run_directory: Path,
    name: str,
    payload: Mapping[str, Any],
    *,
    attempt: int,
) -> bool:
    """Write an immutable receipt and expose it only while its attempt is current."""
    record = {**payload, "attempt": attempt}
    written = _supervisor_write(
        _attempt_artifact_path(run_directory, name, attempt), record
    )
    if _attempt_is_current(run_directory, attempt):
        written = _supervisor_write(run_directory / name, record) and written
    return written


# The environment variables the supervisor must inherit to find the run it was
# asked to hold. A per-run supervisor resolves the run through reckon's crew
# home, so one started with the fleet batch step's own environment reads the
# node's default home, finds no run, and exits at once -- while the dispatch
# that asked for it records a live pid and returns success. Carrying these
# values in the spec is what lets the batch step start the supervisor under the
# dispatcher's configuration rather than its own.
#
#   RECKON_HOME           the crew home: live pointers, manifests, worktrees
#   RECKON_STATE_ROOT     the state root the crew home resolves under
#   RECKON_MOUNTS_PATH    the mounts.json naming each project's docs tree
#   RECKON_FLIGHT_CONFIG  the routing and lane configuration
#   RECKON_RUN_STORE      the durable run store's database path
CREW_STATE_ENVIRONMENT = (
    "RECKON_HOME",
    "RECKON_STATE_ROOT",
    "RECKON_MOUNTS_PATH",
    "RECKON_FLIGHT_CONFIG",
    "RECKON_RUN_STORE",
)

# How long dispatch waits, after the supervisor has been started, to confirm it
# is still running before reporting the run directory's pid as a live worker. A
# supervisor that cannot find its run exits within milliseconds of starting, so
# a short window tells a supervisor that reached its run from one that did not
# without holding a healthy dispatch open.
SUPERVISOR_SURVIVAL_SECONDS = 2.0
SUPERVISOR_SURVIVAL_POLL_SECONDS = 0.05


def _carried_crew_environment() -> dict[str, str]:
    """The dispatcher's values of every variable that relocates crew state.

    Only variables that are set are carried: an unset one must arrive at the
    supervisor as absent rather than as an empty string, which resolves
    differently from absent in every reader that falls back on a default.
    """
    return {
        name: os.environ[name]
        for name in CREW_STATE_ENVIRONMENT
        if os.environ.get(name)
    }


def _supervisor_exit_detail(run_directory: Path) -> str | None:
    """The detail from the supervisor's exit record, or None while none exists.

    Each attempt clears the canonical exit record before its supervisor starts,
    so the one read here belongs to the supervisor this dispatch just started
    rather than to a predecessor's.
    """
    try:
        record = json.loads(
            (run_directory / EXIT_RECORD_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(record, Mapping):
        return None
    detail = str(record.get("detail") or "").strip()
    return detail or "the supervisor exited without recording a detail"


def _supervisor_launched_worker(run_directory: Path) -> bool:
    """Whether the supervisor's exit record names a worker it spawned.

    A supervisor that reached its run spawns the worker and writes the exit
    record with the worker's pid; a launch failure or a pre-spawn stop writes
    the same record with no worker pid and a detail. So a record naming a worker
    is the receipt of a launch that happened, whatever the worker then did. Each
    attempt clears the canonical record before its supervisor starts, so the one
    read here belongs to the supervisor this dispatch just started.
    """
    try:
        record = json.loads(
            (run_directory / EXIT_RECORD_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    if not isinstance(record, Mapping):
        return False
    return record.get("worker_pid") is not None


def _confirm_supervisor_survived(pid: int, run_directory: Path, run_id: str) -> str:
    """Confirm the supervisor reached its run, and name the run on success.

    A dispatch that launched a worker must never be refused, so a supervisor
    whose exit record names the worker it spawned counts as having reached its
    run: the launch happened and its outcome belongs to the worker, not to the
    dispatch. That is the case a fast launch produces — the supervisor scans,
    spawns, collects and records inside this window — and it must return, not
    refuse.

    The refusal is reserved for a supervisor that exits inside the window
    without such a record: one that could not find its run, because it started
    under the wrong crew home, or one whose spawn failed. The batch step
    acknowledges a spawn as soon as the child exists, so a dispatch that
    returned such a pid would report a live worker that never started. The
    supervisor writes its exit record before it ends and ``process_alive`` reads
    a zombie as dead, so a short bounded wait tells a supervisor that reached
    its run from one that did not.
    """
    deadline = time.monotonic() + SUPERVISOR_SURVIVAL_SECONDS
    while True:
        if _supervisor_launched_worker(run_directory):
            return run_id
        detail = _supervisor_exit_detail(run_directory)
        if detail is not None:
            raise CrewError(
                f"the supervisor for {run_id} exited within "
                f"{SUPERVISOR_SURVIVAL_SECONDS:g}s of starting: {detail}"
            )
        if process_alive(pid) is False:
            raise CrewError(
                f"the supervisor for {run_id} exited within "
                f"{SUPERVISOR_SURVIVAL_SECONDS:g}s of starting without writing "
                "an exit record"
            )
        if time.monotonic() >= deadline:
            return run_id
        time.sleep(SUPERVISOR_SURVIVAL_POLL_SECONDS)


def _supervisor_spec(
    *,
    run_id: str,
    run_directory: Path,
    repo_root: Path,
    worktree: Path,
    plan: _backends.LaunchPlan,
    fenced: bool = False,
    prompt_path: Path,
    log_path: Path,
    stderr_path: Path,
    facts: Any | None = None,
    attempt: int = 1,
    attempt_kind: str = "dispatch",
    attempt_started_at: str = "",
) -> dict[str, Any]:
    """Describe everything the supervisor needs to take over the launch.

    The plan is written out in full — argv, working directory and environment —
    because the supervisor is a fresh process that never sees the resolved
    launch plan otherwise. Everything else names a path the supervisor writes
    to or reads from.
    """
    return {
        "run_id": run_id,
        "run_directory": str(run_directory),
        "repo": str(repo_root),
        # The variables that relocate crew state, carried so the batch step
        # starts this supervisor under the dispatcher's home rather than its own.
        "environment": _carried_crew_environment(),
        "worktree": str(worktree),
        # Whether this launch was composed inside the fence, carried so the
        # supervisor's boundary baseline reads the same two trees the dispatch
        # record names.
        "fenced": fenced,
        "prompt_path": str(prompt_path),
        "log_path": str(log_path),
        "stderr_path": str(stderr_path),
        "attempt": attempt,
        "attempt_kind": attempt_kind,
        "attempt_started_at": attempt_started_at or _utc_now(),
        # The argv the fleet's batch step runs verbatim when it is asked to
        # spawn this run, carried in the spec so the batch step stays free of
        # any knowledge of reckon's module layout.
        "argv": _supervisor_argv(spec_path=run_directory / SUPERVISOR_SPEC_NAME),
        "plan": {
            "argv": list(plan.argv),
            "cwd": plan.cwd,
            "environment": _persisted_worker_environment(plan.environment, facts=facts),
            "dialect": plan.dialect,
            "backend": plan.backend,
        },
    }


def _supervisor_argv(*, spec_path: Path) -> list[str]:
    """The argv that starts the per-run supervisor for one spec path.

    Kept in one place because the same vector is both forked here and written
    into the spec for the fleet's batch step to run verbatim, and the two must
    agree or a fleet spawn would start something other than what this process
    would have forked.

    The entry module is ``reckon.crew.supervisor_main`` and not dispatch itself:
    the crew package imports dispatch when it loads, so running dispatch as the
    main module re-executes an already-imported module and opens the
    supervisor's stderr with runpy's duplicate-import warning. Nothing imports
    the entry module, so the launch is quiet.
    """
    return [
        sys.executable,
        "-m",
        "reckon.crew.supervisor_main",
        SUPERVISOR_ENTRY,
        "--spec",
        str(spec_path),
    ]


# ── Launching through the fleet's batch step ────────────────────────────────
#
# On the SLURM fleet node every session runs as a step of one allocation, and a
# ``setsid``-detached child is reaped when its step ends — so a supervisor
# forked by a session dies with the session and takes its worker with it. The
# only process tree that outlives every step is the allocation's batch step,
# which reads one request per line from a FIFO in the runtime directory the
# fleet record publishes. When this process runs inside that allocation, the
# spawn is therefore delegated to the batch step through the FIFO rather than
# forked here, and the pid the batch step acknowledges is recorded on the run
# exactly as a forked supervisor's pid would be.
FLEET_RECORD_PATH_ENV = "RECKON_FLEET_RECORD"
FLEET_SPAWN_ENV = "RECKON_FLEET_SPAWN"
FLEET_FIFO_NAME = "requests"
FLEET_SPAWN_ACK_NAME = "spawned.json"
FLEET_SPAWN_ACK_BOUND_SECONDS = 10.0
FLEET_REQUEST_POLL_SECONDS = 0.02


def _fleet_record_path() -> Path:
    """Where the fleet record that publishes the batch step's location lives."""
    override = os.environ.get(FLEET_RECORD_PATH_ENV, "").strip()
    if override:
        return Path(override)
    state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "fleet" / "record.json"


def _read_fleet_record() -> dict[str, Any] | None:
    """The published fleet record, or None when none is readable."""
    try:
        payload = json.loads(_fleet_record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _fleet_spawn_enabled() -> bool:
    """Whether a launch may be delegated to the fleet's batch step.

    The lane is opt-in rather than inferred from the record alone. The batch
    step's ``spawn`` verb and this half land separately, and this workstation's
    fleet node publishes a record for every session running on it — so keying
    on the record would route every dispatch on the node into a FIFO whose
    reader may not yet know the verb, turning an ordinary dispatch into a
    ten-second refusal. The allocation that carries the verb enables the lane;
    everywhere else, and in every test that does not opt in, launches fork as
    they always have.
    """
    return os.environ.get(FLEET_SPAWN_ENV, "").strip().lower() in {
        "on",
        "1",
        "true",
        "yes",
    }


def _runs_inside_fleet_allocation(record: Mapping[str, Any]) -> bool:
    """Whether this process runs inside the allocation the record names.

    Two local proofs, either sufficient: the runtime directory the record
    publishes is the one this process was given, which only the allocation
    itself sets, or the job id matches this process's SLURM job. Anything else
    — no record, a stale record, a session on the login node — is a dispatch
    that forks as it always has.
    """
    runtime = str(record.get("runtime_dir") or "").strip()
    if runtime and os.environ.get("XDG_RUNTIME_DIR", "").strip() == runtime:
        return True
    job_id = str(record.get("job_id") or "").strip()
    return bool(job_id) and os.environ.get("SLURM_JOB_ID", "").strip() == job_id


def _fleet_runtime_dir(record: Mapping[str, Any]) -> Path | None:
    runtime = str(record.get("runtime_dir") or "").strip()
    return Path(runtime) if runtime else None


def _watch_request_slug(project: str) -> str:
    """A request id for a project's producer, free of the line's separator.

    The spawn request is one line of space-separated fields, so a project name
    that carries a space would otherwise split the id and shift the spec path
    into the wrong field.
    """
    return re.sub(r"\s+", "-", project.strip()) or "project"


def _open_request_fifo(fifo: Path, deadline: float) -> int:
    """Open a request FIFO for writing, retrying while no reader holds it.

    Opening a FIFO for writing blocks until a reader holds the other end, so
    the open is non-blocking: a reader not yet there is retried until the
    deadline rather than hung on, and no reader within it is a refusal. The
    caller owns the returned descriptor and closes it. This is the one spelling
    of the reconnect-or-fall-back open, shared by the batch step's request
    write and by the dry run's read of the session host's liveness. A deadline
    already in the past asks for a single attempt.
    """
    while True:
        try:
            return os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno == errno.ENXIO and time.monotonic() < deadline:
                time.sleep(FLEET_REQUEST_POLL_SECONDS)
                continue
            raise CrewError(
                f"the fleet's request FIFO {fifo} could not be written: {exc}"
            ) from exc


def _write_fleet_request(fifo: Path, line: bytes, deadline: float) -> None:
    """Write one request line to the batch step's FIFO within the deadline.

    No reader within the deadline is a refusal, not a wait; the non-blocking
    open with its retry lives in :func:`_open_request_fifo`.
    """
    descriptor = _open_request_fifo(fifo, deadline)
    try:
        os.write(descriptor, line)
    except OSError as exc:
        raise CrewError(
            f"the fleet's request FIFO {fifo} refused the spawn request: {exc}"
        ) from exc
    finally:
        os.close(descriptor)


def _read_spawn_ack(ack_path: Path) -> int | None:
    """The pid the batch step acknowledged, or None while none is written."""
    try:
        payload = json.loads(ack_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    try:
        pid = int(payload.get("pid"))
    except (TypeError, ValueError):
        return None
    return pid if pid > 1 else None


def _spawn_through_fleet(
    runtime_dir: Path, request_id: str, spec_path: Path, ack_path: Path
) -> int:
    """Ask the batch step to start one spec and return the acknowledged pid.

    One line of three space-separated fields — the word ``spawn``, the run id
    and the spec path — is written to the runtime directory's FIFO, and the
    batch step answers by writing the started pid to the run directory. A
    stale acknowledgement is removed first so a previous attempt's file can
    never stand in for this one's.
    """
    deadline = time.monotonic() + FLEET_SPAWN_ACK_BOUND_SECONDS
    ack_path.unlink(missing_ok=True)
    line = f"spawn {request_id} {spec_path}\n".encode()
    _write_fleet_request(runtime_dir / FLEET_FIFO_NAME, line, deadline)
    while time.monotonic() < deadline:
        pid = _read_spawn_ack(ack_path)
        if pid is not None:
            return pid
        time.sleep(FLEET_REQUEST_POLL_SECONDS)
    raise CrewError(
        f"the fleet's batch step did not acknowledge the spawn of {request_id} "
        f"within {FLEET_SPAWN_ACK_BOUND_SECONDS:g}s"
    )


def supervised_launch(
    plan: _backends.LaunchPlan,
    *,
    run_directory: Path,
    repo_root: Path,
    worktree: Path,
    log_path: Path,
    stderr_path: Path,
    prompt_path: Path,
) -> int:
    """Start a run's supervisor for an already-built plan and return its pid.

    A resumption and a review dispatch reach the supervisor through here rather
    than through an in-process ``_spawn`` call: a worker started inline is a
    child of whoever is sweeping, so on the fleet it dies with that step, and
    off it the run's exit is never recorded because nothing waits on it. The
    spec is written before the supervisor is asked for, in the same order the
    dispatch path writes it, so a fleet spawn always finds its stderr path on
    disk.
    """
    _require_fleet_gate_open()
    record = read_pointer(run_directory.name)
    attempt = int(record.get("attempt") or 1) + 1
    stream_name = Path(log_path).name
    attempt_kind = "lane-change" if stream_name.startswith("lane-change-") else "resume"
    attempt_started_at = _utc_now()
    _prepare_attempt_records(
        run_directory,
        run_id=run_directory.name,
        attempt=attempt,
        attempt_kind=attempt_kind,
        attempt_started_at=attempt_started_at,
    )
    spec_path = run_directory / SUPERVISOR_SPEC_NAME
    _write_json(
        spec_path,
        _supervisor_spec(
            run_id=run_directory.name,
            run_directory=run_directory,
            repo_root=repo_root,
            worktree=worktree,
            plan=plan,
            fenced=bool(record.get("fenced")),
            prompt_path=prompt_path,
            log_path=log_path,
            stderr_path=stderr_path,
            attempt=attempt,
            attempt_kind=attempt_kind,
            attempt_started_at=attempt_started_at,
        ),
    )
    return _start_supervisor(spec_path, run_directory, run_directory.name)


def _start_supervisor(spec_path: Path, run_directory: Path, run_id: str) -> int:
    """Start the per-run supervisor and return the pid that runs it.

    Off the fleet this starts the supervisor through a short-lived intermediate
    that puts it in a session of its own and exits at once, so the supervisor's
    parent is never the launching process and it outlives that launcher. Its pid
    is a process group ``crew stop`` can signal, since the worker is spawned
    inside that group. On the fleet node a spawned supervisor would be reaped
    with the session step that made it, so the same argv is handed to the
    allocation's batch step instead and the caller waits, bounded, for the pid
    the step acknowledges.
    """
    _require_fleet_gate_open()
    fleet = _read_fleet_record()
    if (
        _fleet_spawn_enabled()
        and fleet is not None
        and _runs_inside_fleet_allocation(fleet)
    ):
        runtime_dir = _fleet_runtime_dir(fleet)
        if runtime_dir is not None:
            pid = _spawn_through_fleet(
                runtime_dir,
                run_id,
                spec_path,
                run_directory / FLEET_SPAWN_ACK_NAME,
            )
            _confirm_supervisor_survived(pid, run_directory, run_id)
            return pid
    argv = _supervisor_argv(spec_path=spec_path)
    pid = _spawn_detached_supervisor(argv, run_directory / "supervisor.stderr.log")
    _confirm_supervisor_survived(pid, run_directory, run_id)
    return pid


def _spawn_detached_supervisor(argv: list[str], stderr_path: Path) -> int:
    """Start the supervisor under an intermediate that exits at once.

    A single ``Popen`` makes the supervisor the launcher's child, so the
    launcher's own end — a coordinator turn, a shell that returns — takes the
    supervisor with it. A short-lived intermediate runs here instead: it starts
    the supervisor in a session of its own, hands its pid back and exits, so the
    supervisor is reparented away from the launching process before it does any
    work. The returned pid leads its own session and process group — the group
    ``crew stop`` signals, with the worker spawned inside it.

    The intermediate is a fresh interpreter rather than an in-process ``fork``:
    the launcher may be threaded (the MCP server is one), where a forked child
    that runs Python is not safe. Every ``Popen`` here keeps ``close_fds=True``,
    so neither the supervisor nor the intermediate inherits the launcher's
    descriptors.
    """
    payload = json.dumps({"argv": list(argv), "stderr": str(stderr_path)})
    intermediate = subprocess.Popen(
        [sys.executable, "-c", _SUPERVISOR_LAUNCHER_SOURCE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        close_fds=True,
    )
    stdout, stderr = intermediate.communicate(f"{payload}\n")
    reported = stdout.strip()
    if not reported:
        raise CrewError(
            "the supervisor's launcher exited without reporting a pid"
            f"{_intermediate_failure_detail(stderr, intermediate.returncode)}"
        )
    return int(reported.splitlines()[0])


def _intermediate_failure_detail(stderr: str, returncode: int | None) -> str:
    """The exit status and stderr tail that explain a silent intermediate.

    An intermediate that prints no pid failed before it could start a
    supervisor, and its own stderr — which a caller cannot see once this
    recursive launch returns — is the only account of why. The last few lines
    carry the exception or the refusal.
    """
    status = "unknown" if returncode is None else str(returncode)
    lines = [line for line in stderr.strip().splitlines() if line.strip()]
    tail = " | ".join(lines[-3:])
    if tail:
        return f" (exit status {status}; stderr: {tail})"
    return f" (exit status {status})"


# The intermediate: start the supervisor from the argv it is handed and report
# the pid, then exit. Kept as a source string because it is run by a fresh
# interpreter, which is what keeps the launcher's own process image — and any
# lock a threaded launcher holds — out of the child.
_SUPERVISOR_LAUNCHER_SOURCE = """
import json
import subprocess
import sys

payload = json.loads(sys.stdin.readline())
with open(payload["stderr"], "ab") as errors:
    process = subprocess.Popen(
        payload["argv"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=errors,
        start_new_session=True,
        close_fds=True,
    )
print(process.pid, flush=True)
"""


def _worker_default_signals() -> None:
    """Give the worker the default signal dispositions the supervisor changed.

    The supervisor installs its own handlers for SIGTERM and SIGHUP, and the
    worker is spawned into the supervisor's signal environment. The worker must
    end on the group signal that stops it rather than carry a handler the
    supervisor needed for itself, so it starts with the defaults its launch
    would otherwise have had. The mask is cleared as well: the spawn runs with
    those signals blocked so a stop inside the window cannot be missed, and the
    child inherits the mask across the fork, so a worker that kept it would
    never see the stop the group sends it.
    """
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGHUP})
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGHUP, signal.SIG_DFL)


def _wait_status_from_returncode(returncode: int) -> int:
    """The wait status a ``Popen`` return code stands for.

    ``subprocess`` reports a signalled child as the negated signal number and an
    exited one as its code, while the supervisor's exit record is composed from
    the wait statuses ``os.waitpid`` returns. A status the startup poll
    collected is a ``Popen`` return code, so it is converted back at this
    boundary and the record is composed by the same path a reaped wait uses.
    """
    if returncode < 0:
        return -returncode
    return (returncode & 0xFF) << 8


class _WorkerPid(int):
    """A spawned worker's pid, carrying any exit its startup poll collected.

    The vanished-bind retry polls the worker it has just spawned, and a poll of
    a child that has exited collects its status. That status is then the only
    copy — the pid is no longer waitable — so it is carried rather than dropped:
    a supervisor waiting on such a pid gets ECHILD and writes an exit record
    naming neither a code nor a signal. The carrier stays an int because every
    other reader wants only the pid, and it must keep comparing and serialising
    as one.
    """

    collected_wait_status: int | None

    def __new__(cls, pid: int, collected_wait_status: int | None = None) -> _WorkerPid:
        worker_pid = super().__new__(cls, pid)
        worker_pid.collected_wait_status = collected_wait_status
        return worker_pid


def _supervisor_spawn_worker(spec: Mapping[str, Any]) -> int:
    """Spawn the worker inside the supervisor's own process group.

    The answer names the pid and, when the spawn retry's startup poll already
    collected the worker's exit, carries that exit — as an ``_WorkerPid`` — so
    the supervisor writes it instead of waiting on a pid that has been reaped.
    """
    plan = spec["plan"]
    record = read_pointer(str(spec["run_id"]))
    environment = _worker_runtime_environment(
        plan.get("environment") or {},
        run_id=str(spec["run_id"]),
        manifest_path=_recorded_manifest_path(record, str(spec["run_id"])),
        attempt_started_at=_utc_now(),
        coordinator_session=str(record.get("session") or ""),
        claude_headers=str(plan.get("dialect") or "") == "claude",
    )
    argv = [str(word) for word in plan["argv"]]

    def attempt() -> subprocess.Popen:
        with (
            open(str(spec["prompt_path"]), "rb") as stdin,
            open(str(spec["log_path"]), "wb") as stdout,
            open(str(spec["stderr_path"]), "wb") as stderr,
        ):
            return subprocess.Popen(
                argv,
                cwd=plan.get("cwd"),
                env=_worker_process_environment(
                    environment,
                    dialect=str(plan.get("dialect") or ""),
                ),
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                start_new_session=False,
                # The worker must start with the default signal dispositions
                # rather than the supervisor's own handlers, so a stop aimed at
                # the group ends the worker. This runs in the supervisor's
                # single-threaded child as it starts.
                preexec_fn=_worker_default_signals,  # noqa: PLW1509
            )

    process = _spawn_worker_retrying_a_vanished_bind(
        attempt,
        argv=argv,
        stderr_path=Path(str(spec["stderr_path"])),
    )
    # A poll of a child that has already exited collects its status, and the
    # retry's startup check is such a poll. The status is carried on the pid
    # from here: the child is no longer waitable, so a supervisor that only
    # held the pid would record neither its code nor its signal.
    collected = getattr(process, "returncode", None)
    return _WorkerPid(
        process.pid,
        None if collected is None else _wait_status_from_returncode(collected),
    )


def _plan_composed_the_fence(plan: _backends.LaunchPlan | None) -> bool:
    """Whether a launch plan's argv carries the fence wrapper.

    The fence is composed inside the plan builder and only for a cli launch
    that asked for it, so the fact is read from the composed argv rather than
    from the request to compose one. A launch that composed no fence — an
    in-harness lane, whose plan is absent here, or any lane whose argv is a
    bare harness — is not fenced, and its boundary check keeps the full scan of
    every registered worktree, because nothing stopped it writing elsewhere.
    """
    return plan is not None and bool(_harness_behind_the_fence(plan.argv))


def _boundary_snapshot_roots(
    repo_root: Path, worktree: Path | None, *, fenced: bool
) -> list[Path] | None:
    """The trees a boundary snapshot reads, or None for the full registry.

    A fenced run reads only its own worktree and the main checkout. A run that
    was not fenced keeps the full registry scan, because nothing stopped it
    from writing elsewhere.
    """
    if not fenced or worktree is None:
        return None
    return _boundary_tree_roots(repo_root, worktree)


def _write_boundary_tree_snapshot(
    run_directory: Path,
    repo_root: Path,
    *,
    worktree: Path | None = None,
    fenced: bool = False,
) -> dict[str, Any]:
    """Write the boundary baseline into the run directory and return it.

    The snapshot is a boundary baseline: it must follow dispatch's own writes
    and predate any write the worker makes in another tree. A snapshot that
    raises is recorded and the launch continues — a scan failure must never
    kill a worker nor turn a launch into a refusal. The write never creates the
    run directory, so a baseline racing a discard is dropped rather than
    bringing a discarded run back into existence.

    Both launch lanes write this one artifact. A spawned run's supervisor takes
    it after dispatch's writes and before the worker's spawn; a delegated run
    spawns nothing, so dispatch takes it inline, in the same last
    repository-facing step. Promotion reads it from the run directory either
    way, so a stray uncommitted edit in another tree is refused for both.

    A fenced launch reads only the run's own worktree and the main checkout;
    the rest of the worktree registry is a write the fence already refused.
    """
    try:
        snapshot: dict[str, Any] = _repository_tree_snapshot(
            repo_root,
            roots=_boundary_snapshot_roots(repo_root, worktree, fenced=fenced),
        )
    except Exception as exc:  # noqa: BLE001 - a scan failure never kills a launch
        snapshot = {"available": False, "detail": f"{type(exc).__name__}: {exc}"}
    _supervisor_write(run_directory / TREE_SNAPSHOT_NAME, snapshot)
    return snapshot


def _supervisor_tree_snapshot(spec: Mapping[str, Any]) -> None:
    """Write the boundary snapshot, or its failure, into the run directory."""
    worktree_value = str(spec.get("worktree") or "").strip()
    _write_boundary_tree_snapshot(
        Path(str(spec["run_directory"])),
        Path(str(spec["repo"])),
        worktree=Path(worktree_value) if worktree_value else None,
        fenced=bool(spec.get("fenced")),
    )


def _supervisor_exit_record(
    *,
    run_id: str,
    attempt: int,
    worker_pid: int | None,
    launched_at: str,
    status: int | None,
    run_directory: Path,
) -> dict[str, Any]:
    """Compose the one record written for a worker's exit or launch failure."""
    paths = stream_paths_newest_first(run_directory)
    count, last_type = _stream_record_facts(paths)
    record: dict[str, Any] = {
        "run_id": run_id,
        "attempt": attempt,
        "recorded_by": "supervisor",
        "worker_pid": worker_pid,
        "launched_at": launched_at,
        "exited_at": _utc_now(),
        "stream_records_seen": count,
        "last_record_type": last_type,
        # A worker that wrote no byte of its stream reached no model: that is a
        # launch failure, and it wants a different recovery from a death while
        # working, so the two are distinguished on the record itself.
        "ended_during": "launch" if count == 0 else "working",
    }
    if status is None:
        record.update({"exit_code": None, "signal": None, "signal_name": None})
    else:
        record.update(_wait_status_record(os.waitstatus_to_exitcode(status)))
    return record


def _record_launch_abandoned_before_spawn(
    spec: Mapping[str, Any],
    run_directory: Path,
    *,
    attempt: int,
    launched_at: str,
) -> None:
    """Write the one launch-failure record for a stop that ended the launch.

    A stop that arrives before the worker exists abandons the launch, so none
    is spawned. The record is still written, because the run directory outlives
    the pointer a discard removes and a reader needs to tell this launch from a
    worker that started and died.
    """
    exit_record = _supervisor_exit_record(
        run_id=str(spec.get("run_id") or ""),
        attempt=attempt,
        worker_pid=None,
        launched_at=launched_at,
        status=None,
        run_directory=run_directory,
    ) | {"detail": "crew stop arrived before the worker was spawned"}
    _write_attempt_artifact(
        run_directory,
        EXIT_RECORD_NAME,
        exit_record,
        attempt=attempt,
    )
    _publish_stored_phase(spec, ended=True, exit_record=exit_record)


def _stop_is_requested(flag: threading.Event, blocked: set[int]) -> bool:
    """Whether a stop has arrived, including one the signal mask is holding.

    A blocked SIGTERM or SIGHUP is not delivered, so its handler cannot run and
    the flag cannot be set; the signal stays pending and is the only observable
    a stop leaves while the mask is applied. Both are read here so a stop
    delivered either before the mask or inside the window it guards is seen.
    """
    return flag.is_set() or bool(signal.sigpending() & blocked)


def _record_stop_before_spawn() -> threading.Event:
    """Install the supervisor's stop handler and return the flag it sets.

    The supervisor starts in its own session, so ``crew stop`` reaches it as a
    group signal — the same signal that must end the worker. It cannot take the
    default disposition for SIGTERM or SIGHUP, or a stop delivered after the
    spawn would take the supervisor down before it recorded the exit the stop
    caused. It cannot ignore them either: an ignored stop is reported as done
    while the supervisor goes on to spawn that stop's worker. The handler
    records the request and lets the supervisor finish its snapshot, and the
    pre-spawn launch is abandoned when the flag is set.
    """
    requested = threading.Event()

    def record(_signum: int, _frame: Any) -> None:
        requested.set()

    signal.signal(signal.SIGTERM, record)
    signal.signal(signal.SIGHUP, record)
    return requested


def _record_is_this_attempt(record: Mapping[str, Any], spec: Mapping[str, Any]) -> bool:
    """Whether a live pointer still names the attempt this supervisor runs as."""
    try:
        return int(record.get("attempt") or 1) == int(spec.get("attempt") or 1)
    except (TypeError, ValueError):
        return False


def _started_phase(record: Mapping[str, Any]) -> str:
    """``working`` for a pointer the launcher left at a pre-spawn label, else ""."""
    from reckon.crew.recovery import _PRE_SPAWN_PHASES

    return "working" if str(record.get("phase") or "") in _PRE_SPAWN_PHASES else ""


def _delivered_phase(
    record: Mapping[str, Any], exit_record: Mapping[str, Any] | None
) -> str:
    """The phase a finished worker's own delivery supports, or "" when none does.

    A delivered manifest is the worker's own verdict, so its terminal status is
    the phase the pointer should carry. A worker that reached no model at all —
    the exit record says it ended during launch — is a launch failure rather
    than a run that stopped mid-turn. Anything else, including a worker that
    exited after only a partial report, keeps the phase the spawn wrote.
    """
    from reckon.crew.reports import TERMINAL_MANIFEST_STATUSES, parse_manifest

    manifest = Path(str(record.get("manifest_path") or ""))
    try:
        status = str(
            parse_manifest(
                manifest.read_text(encoding="utf-8"), path=str(manifest)
            ).get("status")
            or ""
        )
    except (OSError, ValueError):
        status = ""
    if status in TERMINAL_MANIFEST_STATUSES:
        return status
    if (
        exit_record is not None
        and str(exit_record.get("ended_during") or "") == "launch"
    ):
        return LAUNCH_FAILED_PHASE
    return ""


def _publish_stored_phase(
    spec: Mapping[str, Any],
    *,
    ended: bool,
    exit_record: Mapping[str, Any] | None = None,
) -> None:
    """Rewrite the live pointer's stored phase from the run's own evidence.

    The launcher writes ``starting`` and nothing else, so without this the
    stored phase a reader sees — the follower's re-arm replay, the MCP views, a
    peer's tooling — stays the pre-spawn label for the whole life of the run.
    The supervisor is the only process that touches the pointer while the run is
    alive, so the advance is written here: ``working`` once the worker is
    spawned, and the delivered phase once the worker has exited.

    A pointer that is gone — a discard took it — is left gone rather than
    recreated, and a pointer whose attempt has moved on belongs to that attempt.
    """
    run_id = str(spec.get("run_id") or "")
    if not run_id:
        return

    with _pointer_lock(run_id):
        try:
            record = read_pointer(run_id)
        except CrewError:
            return
        if not _record_is_this_attempt(record, spec):
            return
        phase = (
            _delivered_phase(record, exit_record) if ended else _started_phase(record)
        )
        if phase:
            record["phase"] = phase
        _write_existing_pointer(run_id, record)


# A worker whose manifest reaches one of these has delivered its verdict and
# will do no more work, so the supervisor stops waiting for it and ends it. The
# terminal ``blocked`` is excluded on purpose: a blocked run is resumed in
# place, and its process and disposable identity are kept for that resume.
# ``in-progress`` is non-terminal and is waited on as before.
_WORKER_DONE_MANIFEST_STATUSES = frozenset({"complete", "failed"})

# The env var a test (or an operator) shortens the grace with. The default
# bounds how long a finished worker may hold its slot before the supervisor
# ends it; a worker whose manifest is complete or failed should exit at once,
# so the grace covers only the flush between the manifest write and the exit.
TERMINAL_MANIFEST_GRACE_ENV = "RECKON_WORKER_TERMINAL_GRACE_SECONDS"
TERMINAL_MANIFEST_GRACE_DEFAULT = 300.0
# How often the supervisor rechecks a manifest while it waits for the worker.
# Coarse on purpose: the file lives on shared storage, and a manifest the
# worker has not written again cannot have changed its status, so poll often
# enough to notice a delivery against a multi-minute grace without reading the
# manifest across the whole life of every worker.
_WORKER_MANIFEST_POLL_SECONDS = 3.0
# How long a worker gets to end on the grace signal before the supervisor
# escalates to SIGKILL, so a worker ignoring SIGTERM cannot hold the slot.
_WORKER_GRACE_KILL_SECONDS = 10.0
# How long a worker gets to end after ``crew stop`` reaches its process group
# before the supervisor escalates to SIGKILL. The stop signal is already
# delivered to the worker; the grace only covers the flush between receiving it
# and exiting. Without a bound here, a worker that ignores the stop holds the
# supervisor for as long as the worker itself lives.
STOP_GRACE_ENV = "RECKON_WORKER_STOP_GRACE_SECONDS"
STOP_GRACE_DEFAULT = _WORKER_GRACE_KILL_SECONDS
# A manifest's mtime must rest for this long before its terminal status counts
# as a delivery. A worker writes its manifest line by line, so it passes through
# states where the status line already reads terminal while the list fields
# below it are still being written; signalling on such a read is what cut a
# manifest off mid-path near the deadline — wrote complete, then kept writing,
# and the grace expired against the half-written file.
_WORKER_MANIFEST_QUIET_SECONDS = 3.0
# The overall ceiling on deferring a signal. A whole terminal manifest that
# will not rest — its mtime keeps advancing, so the quiet period is never met —
# may defer its signal for at most this long; past it the whole read the
# supervisor holds is taken as the delivery. An incomplete manifest is never
# signalled on, at the ceiling or before it.
_WORKER_MANIFEST_CEILING_SECONDS = 600.0


def _terminal_manifest_grace_seconds() -> float:
    """The grace a finished worker is given to exit on its own, in seconds.

    Read from the environment so a test can shorten it, and floored at zero so
    a nonsensical value degrades to an immediate end rather than to no bound.
    """
    raw = os.environ.get(TERMINAL_MANIFEST_GRACE_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = None
        if value is not None and value >= 0:
            return value
    return TERMINAL_MANIFEST_GRACE_DEFAULT


def _stop_grace_seconds() -> float:
    """The grace a stopped worker is given to exit before it is killed.

    Read from the environment so a test can shorten it, and floored at zero so
    a nonsensical value degrades to an immediate end rather than to no bound.
    """
    raw = os.environ.get(STOP_GRACE_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = None
        if value is not None and value >= 0:
            return value
    return STOP_GRACE_DEFAULT


def _supervisor_manifest_path(run_id: str) -> Path:
    """The manifest the supervisor watches for its run's terminal verdict.

    Read from the live pointer, whose manifest path the launcher wrote; a
    pointer already gone (a discard took it) falls back to the run directory,
    so the watch names a path rather than raising.
    """
    try:
        record: Mapping[str, Any] = read_pointer(run_id)
    except CrewError:
        record = {}
    return Path(_recorded_manifest_path(record, run_id))


def _worker_manifest_done_status(manifest_path: Path) -> str:
    """The done status a delivered manifest carries, or "" for none yet.

    A manifest that is absent, unreadable, still carries the dispatch
    template's placeholder or names a status the supervisor waits on all read
    as "" — the worker is still working, or has declared a wait, and is left
    to its own exit.
    """
    from reckon.crew.reports import manifest_status_is_template, parse_manifest

    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    try:
        status = str(parse_manifest(text, path=str(manifest_path)).get("status") or "")
    except ValueError:
        return ""
    status = status.strip().lower()
    if not status or manifest_status_is_template(status):
        return ""
    return status if status in _WORKER_DONE_MANIFEST_STATUSES else ""


def _worker_manifest_is_whole(manifest_path: Path) -> bool:
    """Whether every list field on the manifest closes its bracket.

    The tolerant reader accepts a value and splits it, so a ``changed_paths:
    [a, b, c`` line cut off mid-path still parses to a done manifest with a
    plausible list — the shape a worker at its pen passes through. A list field
    whose raw value opens a ``[`` must close it before the manifest counts as
    delivered; an unbalanced bracket is a write in progress, not a delivery.

    A field written in the block form (a bare key over ``- item`` lines) carries
    no bracket to check; the quiet period, not this check, is what covers it.
    """
    from reckon.crew.reports import _MANIFEST_LIST_KEYS

    keys = frozenset((*_MANIFEST_LIST_KEYS, "orientation_write_paths"))
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        match = re.match(
            r"^(?:[-*]\s+)?(?:\*\*)?(?P<key>[a-z][a-z0-9_-]*)\s*:\s*(?P<value>.*)$",
            line,
            re.IGNORECASE,
        )
        if not match:
            continue
        if match.group("key").lower().replace("-", "_") not in keys:
            continue
        value = match.group("value").strip().strip("*").strip()
        if "[" in value and value.count("[") != value.count("]"):
            return False
    return True


def _iso_stamp_to_ns(stamp: str) -> int | None:
    """An ISO-8601 instant as epoch nanoseconds, or None for an unreadable one."""
    parsed = parse_utc(stamp)
    if parsed is None:
        return None
    return int(parsed.timestamp() * 1_000_000_000)


def _attempt_started_ns(
    spec: Mapping[str, Any], record: Mapping[str, Any], supervisor_started_at: str
) -> int | None:
    """The current attempt's own start, as epoch nanoseconds.

    The spec is authoritative — it is written by the launch that started this
    supervisor — and the pointer is the fallback for a spec that omits the
    field. The supervisor's own launch instant is the last resort before the
    clock is unreadable.
    """
    for stamp in (
        spec.get("attempt_started_at"),
        record.get("attempt_started_at"),
        supervisor_started_at,
    ):
        if not stamp:
            continue
        parsed = _iso_stamp_to_ns(str(stamp))
        if parsed is not None:
            return parsed
    return None


def _supervisor_manifest_baseline_ns(
    run_id: str, spec: Mapping[str, Any], *, supervisor_started_at: str = ""
) -> int:
    """The manifest generation this attempt may call its own.

    A resumed run keeps its manifest path, so the previous attempt's delivered
    manifest is still on disk when the new worker starts, carrying an mtime from
    before this attempt began. A generation recorded on the pointer at dispatch
    time predates that manifest, so honouring it alone would read the stale
    manifest as this attempt's delivery. The baseline is therefore never earlier
    than the attempt's own start: a recorded generation counts only when it is
    at or after that start, which is the floor. A manifest written before this
    attempt began cannot count as its delivery.

    When the attempt clock is unreadable everywhere, the recorded generation is
    taken as before — a manifest is then treated as delivery, the pre-existing
    behaviour rather than a regression.
    """
    try:
        record: Mapping[str, Any] = read_pointer(run_id)
    except CrewError:
        record = {}
    baseline: int | None = None
    recorded = record.get("manifest_baseline_mtime_ns")
    if recorded is not None:
        try:
            baseline = int(recorded)
        except (TypeError, ValueError):
            baseline = None
    started_ns = _attempt_started_ns(spec, record, supervisor_started_at)
    if started_ns is None:
        return baseline if baseline is not None else 0
    if baseline is None:
        return started_ns
    return max(baseline, started_ns)


def _reap_worker_on_its_terminal_manifest(
    pid: int,
    *,
    run_directory: Path,
    manifest_path: Path,
    grace_seconds: float,
    baseline_ns: int,
    stop_requested: threading.Event | None = None,
    stop_grace_seconds: float = 0.0,
) -> int | None:
    """Collect a worker's exit, ending it once its manifest is delivered or a stop arrives.

    A worker that delivered (a complete or failed manifest) and then kept
    running holds its run's slot long after its work was finished. The
    supervisor notices the delivery, gives the worker the grace period to exit
    on its own, and then ends its process — writing the sender record before it
    signals, so the signal is attributable to the run's own directory.

    A delivery is a manifest this attempt wrote that reads as a done status,
    parses in full with every list field closed, and has rested without a write
    for the quiet period. A manifest that is still being written — its mtime
    advancing, or a list field whose bracket is not yet closed — is a worker at
    its pen, and the supervisor defers to the next poll rather than signal a
    half-written record. A manifest that reads non-terminal, or blocked (kept
    for resume), is waited on as before, and so is a worker that rewrites a done
    manifest back to a non-done status: withdrawing the delivery clears the
    deadline it had set. A done manifest that reads whole but will not rest
    cannot defer its signal forever: past the overall ceiling the supervisor
    takes the whole read it holds as the delivery. An incomplete manifest is
    never signalled on, at the ceiling or before it. Returns the wait status, or
    ``None`` when the child was already reaped elsewhere.

    A recorded stop bounds the wait as well. ``crew stop`` delivers its signal
    to the worker's whole process group, so the worker has already been asked to
    end; the supervisor gives it ``stop_grace_seconds`` to do so, then kills it.
    Without this the supervisor would block on ``waitpid`` for as long as the
    worker lives, so a worker that ignores the stop would hold the supervisor,
    and its slot, past any bound. A stop does not wait on a manifest: it ends
    the run outright.

    The manifest is stat'd each poll; its status and wholeness are read only
    when its mtime has advanced past the attempt's baseline and changed since
    the last read, so an unchanged manifest is never reparsed and a manifest
    left by a previous attempt — a resumed run's own complete record — is not
    mistaken for this attempt's delivery.
    """
    seen_mtime_ns: int | None = None
    written_at: float | None = None
    terminal_at: float | None = None
    is_done = False
    is_whole = False
    deadline: float | None = None
    signalled_at: float | None = None
    stop_deadline: float | None = None
    stop_killed = False
    while True:
        try:
            waited_pid, status = os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            return None
        if waited_pid == pid:
            return status
        now = time.monotonic()
        if (
            stop_requested is not None
            and stop_deadline is None
            and not stop_killed
            and stop_requested.is_set()
        ):
            # A stop ends the run: the group signal has already reached the
            # worker, and re-signalling it here names the sender in the run's
            # worker record. The grace covers only the flush between receiving
            # the stop and exiting.
            signal_worker(
                pid,
                signal.SIGTERM,
                reason="run-stop",
                run_dir=run_directory,
            )
            stop_deadline = now + stop_grace_seconds
        if stop_deadline is not None and now >= stop_deadline:
            # The worker outlived the stop grace. SIGKILL cannot be ignored, so
            # the supervisor ends it rather than waiting on it further.
            signal_worker(
                pid,
                signal.SIGKILL,
                reason="worker-ignored-the-stop-grace-signal",
                run_dir=run_directory,
            )
            stop_deadline = None
            stop_killed = True
        try:
            mtime_ns = manifest_path.stat().st_mtime_ns
        except OSError:
            mtime_ns = None
        if mtime_ns is not None and mtime_ns != seen_mtime_ns:
            seen_mtime_ns = mtime_ns
            written_at = now
            fresh = mtime_ns > baseline_ns
            is_done = fresh and bool(_worker_manifest_done_status(manifest_path))
            is_whole = is_done and _worker_manifest_is_whole(manifest_path)
            if is_done:
                if terminal_at is None:
                    terminal_at = now
            else:
                terminal_at = None
        # Delivered: a done manifest this attempt wrote, whole, and rested past
        # the quiet period. The grace is measured from the manifest's own write,
        # so a delivery noticed late still ends on schedule; the age is
        # subtracted unclamped, so once it reaches the grace the deadline is
        # already in the past and the worker is ended at once.
        settled = (
            mtime_ns is not None
            and is_done
            and is_whole
            and written_at is not None
            and now - written_at >= _WORKER_MANIFEST_QUIET_SECONDS
        )
        # A whole done manifest that will not rest cannot defer its signal past
        # the overall ceiling: the whole read the supervisor holds is the
        # delivery. An unwhole manifest never reaches this branch.
        ceiling_due = (
            mtime_ns is not None
            and is_done
            and is_whole
            and terminal_at is not None
            and now - terminal_at >= _WORKER_MANIFEST_CEILING_SECONDS
        )
        if settled or ceiling_due:
            age = time.time() - (mtime_ns / 1_000_000_000)
            deadline = now + grace_seconds - age
        else:
            # Nothing whole, quiet and terminal is on disk: either the delivery
            # was withdrawn (the worker is working again, or has declared a wait
            # for resume) or a done manifest is still being written, so any
            # deadline an earlier read set is cleared and the worker is waited
            # on as before rather than ended on a withdrawn or half-written
            # delivery's clock.
            deadline = None
        if deadline is not None and signalled_at is None and now >= deadline:
            signal_worker(
                pid,
                signal.SIGTERM,
                reason="worker-lingered-after-terminal-manifest",
                run_dir=run_directory,
            )
            signalled_at = now
        elif signalled_at is not None and now - signalled_at >= _WORKER_GRACE_KILL_SECONDS:
            # The worker ignored the grace signal. SIGKILL cannot be ignored,
            # and the record names this second, harder signal.
            signal_worker(
                pid,
                signal.SIGKILL,
                reason="worker-ignored-the-terminal-grace-signal",
                run_dir=run_directory,
            )
            signalled_at = now
        time.sleep(_WORKER_MANIFEST_POLL_SECONDS)


# A supervisor's argv is not always the worker itself: some lanes start the
# worker through an intermediate, which exits while the worker runs on. The
# worker is then an orphan, and without this the kernel reparents it to init,
# where its exit cannot be collected and the attempt would be recorded as ended
# while its worker still works.
_PR_SET_CHILD_SUBREAPER = 36


def _become_child_subreaper() -> None:
    """Have the kernel reparent an orphaned worker to this supervisor.

    A worker is collectable only where its parent waits for it, and a launcher
    that exits ahead of its worker would otherwise hand the worker to init. As
    a child subreaper the supervisor receives it as its own child, so the wait
    for the worker and the exit record that ends the attempt both stay here.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise CrewError(
            f"prctl(PR_SET_CHILD_SUBREAPER) failed: {os.strerror(ctypes.get_errno())}"
        )


def _child_processes_of(pid: int) -> list[int]:
    """The pids the kernel currently parents to a process.

    Read from /proc rather than from a record, because a reparented worker is
    named nowhere: it was parented to a launcher that is gone by the time the
    wait for it begins. A child that already exited shows here too, as the
    zombie its parent has yet to reap.
    """
    children: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return children
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            parent = int(stat[stat.rindex(")") + 2 :].split()[1])
        except (ValueError, IndexError):
            continue
        if parent == pid:
            children.append(int(entry))
    return sorted(children)


def _reap_the_launched_worker(
    pid: int,
    status: int | None,
    *,
    run_directory: Path,
    manifest_path: Path,
    grace_seconds: float,
    baseline_ns: int,
    stop_requested: threading.Event | None = None,
    stop_grace_seconds: float = 0.0,
) -> tuple[int, int | None]:
    """Collect the exit of the worker a launcher started and left behind.

    Returns the pid and wait status the attempt's exit record should name. When
    the immediate child was the worker they are unchanged. When it was an
    intermediate that exited first, the worker it started was reparented to
    this supervisor as a child subreaper, and it is waited on exactly as a
    directly spawned worker is — under the same terminal-manifest grace and the
    same stop bound — so the record names the exit that ended the attempt's
    worker rather than a launcher's exit taken while the worker still runs.
    """
    worker_pid, worker_status = pid, status
    if status is not None and os.WIFSIGNALED(status):
        # A launcher hands the attempt to its worker by exiting; it is not
        # killed. A spawned child that ended by signal ended the attempt's own
        # worker, and any descendants it leaves behind are leftovers of that
        # worker rather than a worker still running the attempt. The signal is
        # the fact the exit record carries, so it is written now rather than
        # after a leftover ends.
        return worker_pid, worker_status
    while True:
        children = _child_processes_of(os.getpid())
        if not children:
            return worker_pid, worker_status
        for child in children:
            child_status = _reap_worker_on_its_terminal_manifest(
                child,
                run_directory=run_directory,
                manifest_path=manifest_path,
                grace_seconds=grace_seconds,
                baseline_ns=baseline_ns,
                stop_requested=stop_requested,
                stop_grace_seconds=stop_grace_seconds,
            )
            if child_status is not None:
                # The exit that ended the wait is the attempt's exit; a child
                # reaped elsewhere leaves the one already held standing.
                worker_pid, worker_status = child, child_status


def _run_supervisor(spec_path: Path) -> int:
    """Take the snapshot, launch the worker, collect its exit, and stop.

    The supervisor holds the worker's parentage for the whole run, so it is the
    only process that can collect the exit a reparented worker would otherwise
    hand to init. A stop is recorded rather than ignored or acted on directly:
    the supervisor survives one so it can write the exit a post-spawn stop
    caused, and it abandons the launch when the stop arrived before the worker
    existed.
    """
    try:
        spec = json.loads(spec_path.read_text())
    except (OSError, ValueError):
        return 0
    if not isinstance(spec, Mapping):
        return 0
    run_directory = Path(str(spec.get("run_directory") or ""))
    run_id = str(spec.get("run_id") or "")
    try:
        attempt = int(spec.get("attempt") or 1)
    except (TypeError, ValueError):
        attempt = 1
    stop_requested = _record_stop_before_spawn()
    _supervisor_tree_snapshot(spec)
    launched_at = _utc_now()
    if stop_requested.is_set():
        # The stop arrived while the snapshot ran, before any worker existed,
        # so none is spawned and the launch is over before it began.
        _record_launch_abandoned_before_spawn(
            spec, run_directory, attempt=attempt, launched_at=launched_at
        )
        return 0
    # SIGTERM and SIGHUP stay blocked across the stop read and the spawn, so a
    # stop delivered between them is deferred rather than running its handler
    # and being missed. It is read again here, immediately before the spawn: a
    # stop delivered inside this window means no worker is ever spawned.
    blocked = {signal.SIGTERM, signal.SIGHUP}
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    try:
        if _stop_is_requested(stop_requested, blocked):
            _record_launch_abandoned_before_spawn(
                spec, run_directory, attempt=attempt, launched_at=launched_at
            )
            return 0
        try:
            # Before the worker exists, so an intermediate that exits ahead of
            # the worker it starts leaves that worker parented here rather than
            # to init, where its exit would be uncollectable.
            _become_child_subreaper()
            pid = _supervisor_spawn_worker(spec)
        except (OSError, ValueError, KeyError, CrewError) as exc:
            failure_record = _supervisor_exit_record(
                run_id=str(spec.get("run_id") or ""),
                attempt=attempt,
                worker_pid=None,
                launched_at=launched_at,
                status=None,
                run_directory=run_directory,
            ) | {"detail": f"worker did not spawn: {type(exc).__name__}: {exc}"}
            _write_attempt_artifact(
                run_directory,
                EXIT_RECORD_NAME,
                failure_record,
                attempt=attempt,
            )
            _publish_stored_phase(spec, ended=True, exit_record=failure_record)
            return 0
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    _write_attempt_artifact(
        run_directory,
        WORKER_RECORD_NAME,
        {
            "run_id": str(spec.get("run_id") or ""),
            "attempt": attempt,
            "pid": pid,
            "pid_start_time": _process_start_time(pid),
            "launched_at": launched_at,
            "backend": str(spec["plan"].get("backend") or ""),
            "argv": list(spec["plan"].get("argv") or ()),
        },
        attempt=attempt,
    )
    _publish_stored_phase(spec, ended=False)
    # The spawn retry's startup poll reaps a worker that exits inside its
    # window, and the pid it hands back then carries that exit: waiting on the
    # pid would get ECHILD and the record would name neither a code nor a
    # signal, so the carried exit is the attempt's exit. A worker that outlived
    # the poll carries none and is supervised exactly as before.
    status = getattr(pid, "collected_wait_status", None)
    if status is not None:
        # The exit was collected from the worker itself, so it stands as the
        # attempt's exit on its own: the worker has been reaped, so no child of
        # this supervisor is a still-running worker whose later exit could be
        # the one that ended the attempt.
        worker_pid = pid
    else:
        status = _reap_worker_on_its_terminal_manifest(
            pid,
            run_directory=run_directory,
            manifest_path=_supervisor_manifest_path(run_id),
            grace_seconds=_terminal_manifest_grace_seconds(),
            baseline_ns=_supervisor_manifest_baseline_ns(
                run_id, spec, supervisor_started_at=launched_at
            ),
            stop_requested=stop_requested,
            stop_grace_seconds=_stop_grace_seconds(),
        )
        worker_pid, status = _reap_the_launched_worker(
            pid,
            status,
            run_directory=run_directory,
            manifest_path=_supervisor_manifest_path(run_id),
            grace_seconds=_terminal_manifest_grace_seconds(),
            baseline_ns=_supervisor_manifest_baseline_ns(
                run_id, spec, supervisor_started_at=launched_at
            ),
            stop_requested=stop_requested,
            stop_grace_seconds=_stop_grace_seconds(),
        )
    exit_record = _supervisor_exit_record(
        run_id=str(spec.get("run_id") or ""),
        attempt=attempt,
        worker_pid=worker_pid,
        launched_at=launched_at,
        status=status,
        run_directory=run_directory,
    )
    _write_attempt_artifact(
        run_directory,
        EXIT_RECORD_NAME,
        exit_record,
        attempt=attempt,
    )
    _publish_stored_phase(spec, ended=True, exit_record=exit_record)
    return 0


def attach(run_id: str, task: str) -> dict[str, Any]:
    """Bind an in-harness dispatch to its live pointer.

    Reckon cannot spawn the calling harness's delegation primitive, so the
    harness dispatches its own task and reports the identity back here. That
    binding is what makes an in-harness run observable on the same surface as a
    spawned one.
    """

    def bind(record: dict[str, Any]) -> dict[str, Any]:
        if record.get("launch") != "in-harness":
            raise CrewError(
                f"run {run_id!r} is a {record.get('launch')!r} launch; attach binds "
                "an in-harness task, and a spawned run already has its pid"
            )
        if record.get("task"):
            raise CrewError(
                f"run {run_id!r} is already attached to task {record['task']!r}; "
                "a second binding would hide which worker holds the write scope"
            )
        if not str(task).strip():
            raise CrewError("attach requires a non-empty task identifier")
        record["task"] = str(task).strip()
        record["attached_at"] = _utc_now()
        record["phase"] = "working"
        return record

    return _mutate_pointer(run_id, bind)


def _stored_phase_survives(stored_phase: str, observed_phase: str) -> bool:
    """Whether a fold keeps a phase the run already reached, unopened.

    Two stored phases are final for their pointer and a fold may not reopen
    them: a terminal phase, which the supervisor wrote once the worker exited,
    and ``launch-failed``, which records that the worker exited before its
    first event and stops a further lift until a person acts. An observation
    that derives a different phase from a stream, a manifest or a dead pid
    folds over a run whose own evidence already settled it.
    """
    return _terminal_phase_survives(stored_phase, observed_phase) or (
        stored_phase == LAUNCH_FAILED_PHASE and observed_phase != LAUNCH_FAILED_PHASE
    )


def _terminal_phase_survives(stored_phase: str, observed_phase: str) -> bool:
    """Whether a fold keeps a terminal phase the supervisor already stored.

    The supervisor is the one writer of a terminal stored phase: it sets the
    delivered manifest's status once the worker exits, and a run that has
    finished stays finished. A late observation reads the run's own stream, and
    a stream that was rewritten or truncated, or a stream whose terminal event
    the observer did not reach, reports a live phase for a run that has already
    ended. Folding that over the stored terminal phase would show a finished
    run as still running, so a terminal stored phase is kept against a
    non-terminal observation.
    """
    return (
        stored_phase in _TERMINAL_RUN_PHASES
        and observed_phase not in _TERMINAL_RUN_PHASES
    )


def observe(run_id: str, *, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    from reckon.crew.query import _resumability
    from reckon.crew.recovery import _apply_budget_watchdog
    from reckon.crew.reports import parse_manifest

    """Fold a run's on-disk evidence back into its pointer and return it.

    Reads the event stream, the manifest path and process liveness, then writes
    the derived phase, session id and budget block into the record. Everything
    it reports is recoverable from disk, so a fresh session can observe a run it
    did not dispatch.
    """

    def fold(record: dict[str, Any]) -> dict[str, Any]:
        # Resolve before folding the stream into the pointer so the answer says
        # where the session was recovered rather than always reporting pointer.
        session = _current_harness_session(record, config=config)
        backend_name = str(record.get("backend") or "")
        manifest = Path(record.get("manifest_path") or "")
        manifest_file_present, manifest_fresh = _manifest_freshness(record)
        record["manifest_file_present"] = manifest_file_present
        record["manifest_fresh"] = manifest_fresh
        record["manifest_present"] = manifest_fresh
        record["process_alive"] = record_process_alive(record, process_alive)
        record["observed_at"] = _utc_now()
        stored_phase = str(record.get("phase") or "")
        stopped = stored_phase == "stopped"

        if record.get("launch") == "cli":
            backend = _backend_settings(record, config)
            observation = _backends.observe_log(
                backend_name=backend_name,
                backend=backend,
                log_path=record.get("log_path", ""),
            )
            data = observation.as_dict()
            record["budget"] = data["budget"]
            record["events"] = data["events"]
            record["exit_status"] = data["exit_status"]
            record["final_message"] = data["final_message"]
            record["throughput"] = data["throughput"]
            observed_phase = "stopped" if stopped else data["phase"]
            if _stored_phase_survives(stored_phase, observed_phase):
                # The run finished under the supervisor's terminal phase; a
                # stream that reports it as live does not reopen it.
                observed_phase = stored_phase
            record["phase"] = observed_phase
            if (
                not stopped
                and record.get("attempt_kind") == "resume"
                and record["process_alive"] is True
                and record["phase"] in _TERMINAL_RUN_PHASES
            ):
                record["phase"] = "working"
            record["session_id"] = data["session_id"] or session.get("session_id")
            if data["detail"]:
                record["detail"] = data["detail"]
            final_file = Path(record.get("final_message_path") or "")
            if not record["final_message"] and final_file.is_file():
                record["final_message"] = final_file.read_text().strip() or None
            if (
                not stopped
                and data["phase"] in ("starting", "working")
                and record["process_alive"] is False
                and not _stored_phase_survives(stored_phase, "orphaned")
            ):
                # A dead process with no terminal event is a recoverable orphan,
                # not a finished run. An empty log counts because argument
                # failures can exit before the first event is written. A run the
                # supervisor already finished is neither: its terminal stored
                # phase stands and a spent pid does not reopen it. A zero-length
                # stream whose stderr carries a message is the third case: the
                # worker exited before its first turn and wrote the reason where
                # the launcher's reap could quote it but nothing read it, so the
                # reason is read here and the run is classified as the launch
                # failure it was instead of as an orphan with no cause.
                failure = _empty_stream_launch_failure(record, data)
                if failure is not None:
                    record["phase"] = LAUNCH_FAILED_PHASE
                    cause = str(failure["stderr_tail"]).strip().splitlines()[-1]
                    record["detail"] = (
                        "the worker exited before writing any stream record; "
                        f"stderr: {cause}"
                    )
                    failures = list(record.get("launch_failures") or ())
                    failures.append(failure)
                    record["launch_failures"] = failures
                else:
                    record["phase"] = "orphaned"
                    record["detail"] = (
                        "process exited without a terminal event in its log; "
                        f"check {record.get('stderr_path')}"
                    )
        elif record.get("task") and record["manifest_present"] and not stopped:
            manifest_status = str(
                parse_manifest(manifest.read_text()).get("status") or ""
            ).strip()
            if manifest_status and not _stored_phase_survives(
                stored_phase, manifest_status
            ):
                record["phase"] = manifest_status

        _apply_budget_watchdog(record, config)
        _apply_orientation_check(record, manifest if manifest_fresh else None)

        worktree = str(record.get("worktree") or "").strip()
        resumable, reason = _resumability(
            session,
            worktree_exists=bool(worktree) and Path(worktree).is_dir(),
            process_alive=record["process_alive"],
        )
        record["session_source"] = session["source"]
        record["session_resolution"] = session
        record["resumable"] = resumable
        record["resumable_reason"] = reason
        record["resume_session_id"] = session["session_id"] if resumable else None
        # The pointer records the session id the run carried, or names why it
        # carried none. A bare null left a reader unable to tell a run whose
        # stream had not been read from one whose stream had no id to give, and
        # that ambiguity is what promoted five resumable runs.
        absence = _capture_session_absence(record, session)
        if absence is None:
            record.pop("session_id_absent", None)
        else:
            record["session_id_absent"] = absence

        capture = _capture_member_session(record)
        if capture is not None:
            record["session_capture"] = capture
        return record

    return _mutate_pointer(run_id, fold)


def _apply_orientation_check(record: dict[str, Any], manifest: Path | None) -> None:
    """Block a run whose first reported orientation differs from its pointer."""
    from reckon.crew.reports import parse_manifest

    prior = record.get("orientation_check")
    if isinstance(prior, Mapping):
        if prior.get("matched") is False:
            record["phase"] = "blocked"
            record["detail"] = str(prior.get("detail") or "orientation mismatch")
        return
    if manifest is None:
        return

    reported = parse_manifest(manifest.read_text())
    names = ("orientation_worktree", "orientation_base_sha", "orientation_write_paths")
    if any(not reported.get(name) for name in names):
        return

    raw_paths = reported["orientation_write_paths"]
    try:
        decoded_paths = json.loads(str(raw_paths))
    except json.JSONDecodeError:
        decoded_paths = raw_paths
    if isinstance(decoded_paths, list) and all(
        isinstance(path, str) for path in decoded_paths
    ):
        reported_paths: Any = sorted(decoded_paths)
    else:
        reported_paths = raw_paths

    expected = {
        "worktree": str(record.get("worktree") or ""),
        "base_sha": str(record.get("base_sha") or ""),
        "write_paths": sorted(
            str(path) for path in (record.get("node") or {}).get("write_paths") or ()
        ),
    }
    actual = {
        "worktree": str(reported["orientation_worktree"]),
        "base_sha": str(reported["orientation_base_sha"]),
        "write_paths": reported_paths,
    }
    mismatches = [name for name in expected if actual[name] != expected[name]]
    if not mismatches:
        record["orientation_check"] = {
            "checked_at": _utc_now(),
            "matched": True,
        }
        return

    detail = "orientation mismatch: " + "; ".join(
        f"{name} expected={json.dumps(expected[name], sort_keys=True)} "
        f"reported={json.dumps(actual[name], sort_keys=True)}"
        for name in mismatches
    )
    record["orientation_check"] = {
        "checked_at": _utc_now(),
        "matched": False,
        "mismatches": mismatches,
        "expected": expected,
        "reported": actual,
        "detail": detail,
    }
    record["phase"] = "blocked"
    record["detail"] = detail


def _record_node_id(record: Mapping[str, Any]) -> str:
    """The node id a run record names, whether it stores the block or the id.

    A live pointer carries the node definition under ``node`` while a promoted
    row stores the id alone there and keeps the definition beside it, so a
    reader of either shape takes the same id from either spelling.
    """
    node = record.get("node")
    if isinstance(node, Mapping):
        return str(node.get("id") or "")
    return str(node or "")


def _record_plan(record: Mapping[str, Any]) -> str:
    """The plan a run record serves, from the node block or the row's own key."""
    node = record.get("node")
    if isinstance(node, Mapping) and node.get("plan"):
        return str(node["plan"])
    return str(record.get("plan") or "")


def _is_review_run(record: Mapping[str, Any]) -> bool:
    """Whether a record is a review, by its role or by its node id.

    The role survives a renamed node id and the prefix survives a record
    written before a role was carried, so either alone recognises a review —
    and a review left unrecognised would take a fresh session where its own
    task has one to continue.
    """
    from reckon.crew.recovery import _is_review_node

    return str(record.get("role") or "") == "review" or _is_review_node(record)


def _reviewed_run_id(source: str, records: Iterable[Mapping[str, Any]]) -> str:
    """Resolve what a review node's source names to the reviewed run's id.

    The review reflex composes a review node's id from the record of the run it
    reviews, so the remainder after the prefix is that run's node id where it
    has one and its run id otherwise. Both spellings resolve here to the
    reviewed run's id: a re-review of one run therefore continues the earlier
    review's session, while a review of a different run starts fresh even when
    the two share a node lineage.
    """
    if not source:
        return ""
    runs = list(records)
    if any(str(item.get("run_id") or "") == source for item in runs):
        return source
    named = [item for item in runs if _record_node_id(item) == source]
    if not named:
        return source
    named.sort(
        key=lambda item: str(
            item.get("completed_at") or item.get("created_at") or ""
        )
    )
    return str(named[-1].get("run_id") or source)


def _task_identity(
    record: Mapping[str, Any],
    project: str,
    records: Iterable[Mapping[str, Any]],
) -> tuple[str, ...]:
    """The task a run belongs to, which is what a session may be continued for.

    An implement, test or investigate run's task is its (project, plan, node
    id); a review's is the run it reviews. Two dispatches may therefore share a
    task without sharing a legacy roster member, and a member may hold sessions
    of several tasks — which is exactly why the member is the wrong key.
    """
    if _is_review_run(record):
        source = _record_node_id(record)[len(REVIEW_NODE_PREFIX) :]
        return ("review", str(project), _reviewed_run_id(source, records))
    return ("node", str(project), _record_plan(record), _record_node_id(record))


def _run_stream_path(record: Mapping[str, Any]) -> Path | None:
    """The stream this run wrote, from its recorded path or its run directory."""
    log = str(record.get("log_path") or "").strip()
    if log:
        return Path(log)
    run_id = str(record.get("run_id") or "").strip()
    if run_id:
        return run_dir(run_id) / "stream.jsonl"
    return None


def _session_too_large_to_continue(record: Mapping[str, Any]) -> str | None:
    """Name why a prior run's session cannot be continued, or None.

    Two endings leave a transcript the endpoint will refuse again: the prompt
    was too long for the model's window, or a compaction announced itself and
    never completed a boundary, so the session is still over the window. Both
    are read from the run's own stream, because a run is promoted only on
    success and the very endings that disqualify its session are the ones no
    promoted row records.
    """
    stream = _run_stream_path(record)
    if stream is None:
        return None
    compaction_announced = False
    boundary_seen = False
    refusal = ""
    try:
        handle = stream.open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    with handle:
        for line in handle:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, Mapping):
                continue
            kind = str(event.get("type") or "")
            if kind == "system":
                subtype = str(event.get("subtype") or "")
                if (
                    subtype == "status"
                    and str(event.get("status") or "") == "compacting"
                ):
                    compaction_announced = True
                elif subtype == "compact_boundary":
                    boundary_seen = True
            elif kind == "result":
                if event.get("is_error") and "Prompt is too long" in str(
                    event.get("result") or ""
                ):
                    refusal = "Prompt is too long"
                elif str(event.get("terminal_reason") or "") == "blocking_limit":
                    refusal = "blocking_limit"
    if refusal:
        return (
            f"the run ended with {refusal!r}, so its session is too large to "
            "continue"
        )
    if compaction_announced and not boundary_seen:
        return (
            "the run announced a compaction that never completed a boundary, "
            "so its session is still too large to continue"
        )
    return None


def _prior_same_task_run(
    identity: tuple[str, ...],
    records: Iterable[Mapping[str, Any]],
    *,
    project: str,
) -> Mapping[str, Any] | None:
    """The most recent prior run of one task that carried a session id.

    Ordered by completion so a redispatch continues the attempt it succeeds
    rather than an older branch of the same task. A shadow run is skipped: it
    is a parallel lineage of the task rather than a prior attempt of it, and
    continuing its conversation would carry the shadow's context into the work
    it was only meant to inform.
    """
    from reckon.crew.resumption import resolve_session

    runs = list(records)
    candidates = []
    for record in runs:
        lineage = record.get("lineage")
        if isinstance(lineage, Mapping) and lineage.get("kind") == "shadow":
            continue
        if _task_identity(record, project, runs) != identity:
            continue
        session = resolve_session(
            str(record.get("run_id") or ""),
            record=record,
            project=project,
            root=record.get("repo"),
        )
        if not session["resolved"]:
            continue
        candidates.append(record)
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: str(
            item.get("completed_at") or item.get("created_at") or ""
        )
    )
    return candidates[-1]


def _prior_session_still_held(
    prior: Mapping[str, Any], live_pointers: Iterable[Mapping[str, Any]]
) -> str | None:
    """Why the prior run's session cannot be continued, or None when it can.

    A session id is a single-writer resource: the backend's thread store
    refuses a second writer for a thread another process still holds, and the
    refused launch exits before its first event. A prior run that is still a
    live pointer is judged by the member guard's own verdict, so the semantic
    that refuses a dispatch onto a busy member is the one that withholds its
    session: the session may be continued only once its worker is proven gone
    on this host. A committed record is a run that has ended, so it carries no
    such question and its session stays continueable; a live pointer whose
    worker is running, or whose liveness cannot be established here, withholds
    the session — continuity is worth less than the node.
    """
    run_id = str(prior.get("run_id") or "")
    live = next(
        (
            pointer
            for pointer in live_pointers
            if str(pointer.get("run_id") or "") == run_id
        ),
        None,
    )
    if live is None:
        return None
    verdict = member_in_flight_verdict(live)
    if not verdict.blocks:
        return None
    return (
        "its worker has not been proven stopped, so continuing its session "
        f"could collide with a live writer ({verdict.reason})"
    )


def _task_session_resolution(
    node: Any,
    project: str,
    *,
    committed_runs: Iterable[Mapping[str, Any]] = (),
    live_pointers: Iterable[Mapping[str, Any]] = (),
    harness: str = "",
) -> dict[str, Any]:
    """Resolve this dispatch's prior same-task session, or name none.

    Selection is keyed to the installation, never to the roster: the session a
    dispatch continues is the one an earlier run *of this task* left behind,
    read from the committed run records and the live pointers. A member that
    last ran another node therefore offers nothing to a new node — the defect
    this removes — and continuing a conversation is decided by what the task
    did, not by which member happened to carry it.

    A prior run whose own stream ended too large to continue is withheld rather
    than composed, and the withholding names it, so a reader sees the refusal
    rather than a bare absence. A prior run that is still a live pointer whose
    worker has not been proven stopped is withheld the same way: its session is
    still held by a writer, and this run starts a fresh conversation instead of
    colliding with it. The substitution is reported rather than silent.
    """
    from reckon.crew.resumption import resolve_session

    records = [*committed_runs, *live_pointers]
    identity = _task_identity(
        {
            "node": {
                "id": str(getattr(node, "id", "") or ""),
                "plan": str(getattr(node, "plan", "") or ""),
            },
            "role": str(getattr(node, "role", "") or ""),
            "session_id": "",
        },
        project,
        records,
    )
    prior = _prior_same_task_run(identity, records, project=project)
    if prior is None:
        return {"session_id": None, "withheld": None}
    session = resolve_session(
        str(prior.get("run_id") or ""),
        record=prior,
        project=project,
        root=prior.get("repo"),
    )
    session_id = str(session.get("session_id") or "").strip()
    owner = str(
        prior.get("session_harness")
        or prior.get("dialect")
        or (prior.get("agent") or {}).get("dialect")
        or ""
    )
    held = _prior_session_still_held(prior, live_pointers)
    if held is not None:
        disqualifier = held
    elif harness and owner != harness:
        disqualifier = (
            f"its session belongs to harness {owner or 'unknown'!r}, not {harness!r}"
        )
    else:
        disqualifier = _session_too_large_to_continue(prior)
    if disqualifier is not None:
        return {
            "session_id": None,
            "withheld": {
                "session_id": session_id or None,
                "owner": None,
                "reason": (
                    f"the prior run {prior.get('run_id')!r} of this task left a "
                    f"session that cannot be continued: {disqualifier}"
                ),
            },
        }
    return {"session_id": session_id, "withheld": None}


def _dispatch_session_absence(
    backend: Mapping[str, Any],
    *,
    reused: str | None,
    withheld: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Name why a freshly dispatched run carries no session id, or None.

    Dispatch is the first of the two points a session id can be attached, and
    a run that reaches it without one must not record a bare null — which reads
    as a verdict on a run that has simply not got one yet. The three situations
    want different operator responses, so each is named: no earlier run of this
    task left a session to continue, one did and its stream ended too large to
    continue, or the resolved backend cannot resume at all. Observation
    replaces this with the id the run's own stream supplies, or with the point
    the capture reached.
    """
    if reused:
        return None
    if withheld:
        return {
            "point": "dispatch-same-task-session-withheld",
            "reason": str(withheld.get("reason") or ""),
        }
    if not backend.get("session_reuse"):
        return {
            "point": "dispatch-session-not-reuseable",
            "reason": (
                "the resolved backend records no session reuse, so no earlier "
                "session is offered to this run"
            ),
        }
    return {
        "point": "dispatch-no-same-task-session",
        "reason": (
            "no earlier run of this task left a session to continue, so the "
            "run starts a fresh conversation"
        ),
    }


def _capture_session_absence(
    record: Mapping[str, Any], session: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Name the point a run's session-id capture reached, or None when it did.

    Called on every observation, so a run that resolves a session records the
    id and drops any earlier absence, and a run that resolves none names which
    of the distinct situations produced it: a launch with no stream to carry
    one, a stream not yet readable, or a stream read and found without one. The
    last is the case that made resume structurally unavailable, and it is a
    measurement rather than an outage — the backend simply has not announced an
    id for this run.
    """
    if session.get("session_id"):
        return None
    if str(record.get("launch") or "") != "cli":
        return {
            "point": "harness-launch",
            "reason": (
                "a session id is read from a backend stream and this launch "
                "writes none, so nothing will ever capture one for it"
            ),
        }
    log = Path(str(record.get("log_path") or ""))
    if not log.is_file():
        return {
            "point": "stream-unreadable",
            "reason": (
                f"the recorded stream {str(log)!r} is not a readable file, so "
                "there was nothing to read a session id from"
            ),
        }
    return {
        "point": "stream-without-id",
        "reason": (
            "the run's own stream was read and carries no session id, so the "
            "backend has not announced one for this run"
        ),
    }


def _capture_member_session(record: dict[str, Any]) -> dict[str, Any] | None:
    """Compatibility entry point for promotion; capture writes only the run."""
    return capture_run_session(record)


def _current_harness_session(
    record: Mapping[str, Any], *, config: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Resolve only sessions owned by the run's current harness.

    A fresh harness boundary excludes earlier streams even when their event
    vocabulary is readable by the new parser. Once this harness captures a
    session, its pointer also permits recovery across its own resume streams.
    """
    from reckon.crew.resumption import resolve_session

    run_id = str(record.get("run_id") or "")
    resolved = resolve_session(run_id, record=record)
    if record.get("launch") != "cli":
        return resolved
    owner = str(record.get("session_harness") or "")
    boundary = record.get("lane_change") or {}
    changed_harness = boundary.get("session") == "fresh" and boundary.get(
        "from_harness"
    ) != boundary.get("to_harness")
    if not owner and not changed_harness:
        return resolved
    backend = _backend_settings(record, config)
    harness = _backends.dialect_for(backend).name
    if (not changed_harness and (not owner or owner == harness)) or (
        resolved["resolved"] and owner == harness
    ):
        return resolved
    observation = _backends.observe_log(
        backend_name=str(record.get("backend") or ""),
        backend=backend,
        log_path=record.get("log_path", ""),
    )
    found = observation.session_id
    reason = (
        f"the current harness {harness!r} has no captured session; sessions "
        "from another harness cannot be continued"
    )
    return {
        "run_id": run_id,
        "session_id": found,
        "resolved": bool(found),
        "source": "stream" if found else None,
        "consulted": ["current-harness-stream"],
        "detail": None if found else reason,
        "withheld": None
        if found
        else {
            "session_id": resolved.get("session_id") or boundary.get("session_id"),
            "reason": reason,
        },
    }


def _names_the_fence(command: str) -> bool:
    """Whether a recorded command names the fence wrapper rather than a harness.

    The fence binary's own name is matched, bare or absolute.
    """
    return bool(command) and Path(command).name == _backends.FENCE_BINARY


def _harness_behind_the_fence(argv: Any) -> str:
    """The harness command a composed, fenced argv carries, or "".

    A fenced launch is ``<fence> <binds...> -- <harness> ...``, optionally
    behind a scheduler a placement prefixed. The fence element is located first
    and the separator searched for after it, because a scheduler's own options
    may carry a bare ``--`` and its separator is not the fence's. An argv with
    no fence element is not a fenced composition this can read.
    """
    if not isinstance(argv, (list, tuple)):
        return ""
    start = next(
        (
            index
            for index, token in enumerate(argv)
            if Path(str(token)).name == _backends.FENCE_BINARY
        ),
        None,
    )
    if start is None or "--" not in argv[start:]:
        return ""
    tail = argv[argv.index("--", start) + 1 :]
    return str(tail[0]).strip() if tail else ""


def _backend_settings(
    record: Mapping[str, Any], config: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Rebuild the settings a recorded run's stream is read with.

    Two authorities carry different parts of the rebuild, and dropping either
    corrupts the reading. The recorded command is the ground truth for the
    harness, so a run stays observable after its config layer changes — except
    where it names the fence, which is a wrapper rather than a harness: the
    composed argv's first element and the harness are different commands, and
    a rebuild of a command line around the fence is a rebuild of the harness.
    The configured lane supplies the window, the model and the effort, without
    which a stream-announced window substitutes as the utilisation's
    denominator and a resumed turn launches without its recorded model. The two
    are merged rather than one derived from the other, and a value the row
    recorded itself wins the merge: the row is the authority for what it was
    measured against, which a fresh config lookup cannot answer for a run
    already in flight.
    """
    backends = (config or {}).get("backends") or {}
    configured = backends.get(record.get("backend"))
    argv = record.get("argv")
    # The harness is read from the record's own explicit field rather than from
    # argv[0]. A placed launch prefixes the scheduler onto the argv, so the
    # first word of a placed run's argv names the scheduler, and a reader
    # taking the harness from it translates the wrong lane — the launch itself
    # succeeded, and only the identity a later reader infers is wrong. A record
    # written before the field existed falls back to argv[0], which is the
    # harness for every launch that was not placed.
    command = str(record.get("command") or "").strip()
    if not command and isinstance(argv, list) and argv:
        command = str(argv[0])
    # The field is written from the composed argv's first element, which is the
    # fence binary for every launch since the fence became the composition
    # default. Feeding that back as the harness composes a fence inside a fence
    # — bubblewrap is handed its own harness flags and the worker never starts
    # — so the fence name is refused here rather than translated. The field
    # stays authoritative for a run launched unfenced and for a placed launch,
    # where it is the only place the harness was recorded beside the scheduler.
    if _names_the_fence(command):
        command = ""
    # The record's own composition is the second authority, ahead of the
    # configured lane: the harness the argv carries behind its fence is the
    # same fact the explicit field holds for a run that was not fenced, and a
    # lane whose command has since changed must not redefine what an in-flight
    # run was launched as.
    inner_harness = _harness_behind_the_fence(argv)
    if command:
        settings: dict[str, Any] = {"launch": "cli", "command": command}
    elif inner_harness:
        settings = {"launch": "cli", "command": inner_harness}
    elif isinstance(configured, Mapping):
        settings = dict(configured)
    else:
        raise CrewError(
            f"run {record.get('run_id')!r} records no argv and its backend is not "
            "in the supplied config, so its stream cannot be read"
        )
    # The identity the launch resolved to, recorded beside the command. It is
    # consulted when the command's own stem names no dialect, which is the case
    # a placed run produces; a record naming only its lane still resolves.
    identity = str(record.get("dialect") or "").strip() or str(
        record.get("backend") or ""
    ).strip()
    if identity:
        settings.setdefault("dialect", identity)
    for key in ("usable_input_window", "model", "effort"):
        if isinstance(configured, Mapping) and configured.get(key) is not None:
            settings.setdefault(key, configured[key])
    agent = record.get("agent")
    if isinstance(agent, Mapping):
        for key in ("usable_input_window", "model", "effort"):
            if agent.get(key) is not None:
                settings[key] = agent[key]
    return settings


def _recorded_manifest_path(record: Mapping[str, Any], run_id: str) -> str:
    """The manifest path a launch rebuilt from a live pointer must carry.

    A pointer records the path twice — the top-level field a dispatch writes
    and the node definition it was written from — and a record may hold
    neither, or a value that is not absolute. Every launch composed from the
    record derives the run's directory from this path: the harness home and
    the fence roots are both built from its parent, and a path that names no
    location anchors them to the directory of whichever process rebuilt the
    launch instead of to the run. The run directory is the fallback because it
    is absolute by construction; the worker record the run's own supervisor
    writes into it is read first, since that record names the run the launch
    belongs to.
    """
    node = record.get("node")
    candidates = [record.get("manifest_path")]
    if isinstance(node, Mapping):
        candidates.append(node.get("manifest_path"))
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text and Path(text).is_absolute():
            return text
    directory = run_dir(run_id)
    try:
        worker = json.loads(
            (directory / WORKER_RECORD_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        worker = None
    if isinstance(worker, Mapping):
        text = str(worker.get("manifest_path") or "").strip()
        if text and Path(text).is_absolute():
            return text
    return str(directory / "manifest.md")


def _carry_declared_placement(
    backend: dict[str, Any],
    record: Mapping[str, Any],
    config: Mapping[str, Any] | None,
) -> None:
    """Put the lane's declared placement onto a rebuilt backend, in place.

    A resumed run is placed exactly as a dispatch is: its worker belongs in the
    one shared allocation the host holds, and the placement that says so is a
    property of the lane it resumes into rather than of the row it was recorded
    on. The recorded row stays authoritative for the command the run launched
    as, so only the placement is taken from the configured lane — a rebuilt
    backend that already carries one keeps it, and a lane declaring none adds
    nothing, which leaves an unplaced run launched as it always was.
    """
    if flight.placement_for(backend) is not None:
        return
    configured = ((config or {}).get("backends") or {}).get(
        str(record.get("backend") or "")
    )
    placement = flight.placement_for(configured)
    if placement is not None:
        backend["placement"] = placement


def _carry_declared_gate_documents(
    backend: dict[str, Any],
    record: Mapping[str, Any],
    config: Mapping[str, Any] | None,
) -> None:
    """Use the current lane's gate declarations when rebuilding a resumed run."""
    configured = ((config or {}).get("backends") or {}).get(
        str(record.get("backend") or "")
    )
    if not isinstance(configured, Mapping):
        return
    for key in ("gate_document", "lane_document"):
        if configured.get(key):
            backend[key] = configured[key]


def _carry_fence_unprotected(
    record: dict[str, Any],
    plan: _backends.LaunchPlan | None,
    config: Mapping[str, Any] | None,
) -> None:
    """Record the defaults this run's composed fence leaves writable.

    The list is written only when the fence actually composed and a layer named
    a default under ``unprotected_paths``, and removed when it did not, so a run
    carries the key exactly when its fence leaves a default out. A resume and a
    lane change recompose the fence from the resolved config, so the run's own
    record follows the fence its current attempt launched inside rather than the
    one the prior attempt did.
    """
    removed = _backends.fence_unprotected_paths(config=config)
    if removed and _plan_composed_the_fence(plan):
        record["fence_unprotected_paths"] = [str(path) for path in removed]
    else:
        record.pop("fence_unprotected_paths", None)


def resume_plan(
    run_id: str,
    advice: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> _backends.LaunchPlan:
    """Build the invocation that answers a stuck worker in its own session.

    Session reuse is load-bearing rather than an optimisation: the advice only
    makes sense to a worker that still remembers what it tried, so the resumed
    turn must carry the prior context rather than restate it.

    A resumption is judged against the full ceiling rather than the reserved
    portion, because answering a stuck worker is the expenditure the reserve was
    withheld for. It is still held at a genuinely spent quota — a resume into one
    fails anyway, and reporting the reset time is more use than the rejection.
    """
    record = read_pointer(run_id)
    if record.get("launch") != "cli":
        raise CrewError(f"run {run_id!r} is not a spawned run; resume it in-harness")
    if record_process_alive(record, process_alive) is True:
        raise CrewError(
            f"run {run_id!r} still has a live process; observe or stop it before resuming"
        )
    # The ledger and budget lookups below run against the project's own mount,
    # so a run recorded in another repository is refused here and a run whose
    # record names none falls back to the mount rather than to whatever
    # checkout the resuming session happens to stand in.
    resume_project = str(record.get("project") or "")
    resume_root = record.get("repo")
    if resume_project and project_mount_repository(resume_project) is not None:
        resume_root = resolve_project_repository(
            resume_project, resume_root, flag="the run's recorded repository"
        )
    # The pointer is a cache. A stream may already carry the captured session
    # while the next observation has not folded it into that cache yet.
    session = _current_harness_session(record, config=config)
    session_id = str(session.get("session_id") or "")
    fresh_reason = session.get("withheld")
    if not session["resolved"] and not fresh_reason:
        raise CrewError(
            f"run {run_id!r} has no session id in any authority: "
            f"{session.get('detail') or 'no session authority resolved'}"
        )
    backend = _backend_settings(record, config)
    _carry_declared_placement(backend, record, config)
    _carry_declared_gate_documents(backend, record, config)
    lane_gate = _dispatch_lane_gate(backend)
    if lane_gate["state"] in _LANE_GATE_WAITING_STATES:
        raise LanePaused(lane_gate)
    verdict = _budget_verdict(
        project=resume_project,
        root=resume_root,
        config=config,
        backend_name=str(record.get("backend") or ""),
        backend=backend,
        purpose="resume",
    )
    if verdict["held"]:
        raise _actionable_budget_hold(verdict, config=config)
    # A resumed turn re-sends the session's whole context, so a session grown
    # past the lane's input window dies at the endpoint with the attempt file
    # already open, and an attempt whose worker dies at once leaves a delivered
    # manifest reading stale to promotion. The count is the run's own last
    # recorded request input rather than an estimate, and the gate is consulted
    # before the plan is built and before anything is written, so a session the
    # lane cannot hold is refused with a fresh repair node as the remedy.
    window_refusal = resume_window_refusal(
        run_id, record, backend=backend, config=config
    )
    if window_refusal is not None:
        raise window_refusal
    # A second worker on one run is the collision this refuses, and a hand-typed
    # resume starts a worker exactly as the sweep does. The guard above refuses
    # only a process this host found alive, so a run whose end nothing observed —
    # a worker on another machine, or a pointer that never recorded a process —
    # would otherwise be resumed as though its worker were dead. The reading is
    # the sweep's own helper rather than a second composition of it, so what
    # counts as an observed end cannot drift between the two doors, and the
    # refusal names the reading it is holding. Consulted after the launcher's
    # other guards, so the reason reported for a run that fails several is the
    # one the sweep would report for it as well.
    from reckon.crew.resumption import _observed_end_refusal

    observed_end = _observed_end_refusal(record)
    if observed_end is not None:
        raise observed_end
    backend.setdefault("sandbox", record.get("sandbox"))
    # The plan is built — and its executable resolved — before anything is
    # written, so an unresolvable backend refuses a resume exactly as it
    # refuses a dispatch: no pointer field, no advice file, no stream.
    preflight_launch_command(
        str(record.get("backend") or ""), backend, fence=FENCE_WORKERS
    )
    attempt_started_at = _utc_now()
    manifest_path = _recorded_manifest_path(record, run_id)
    resumed_prompt = (
        _lane_prompt(record, advice, fresh_reason["reason"], continued=False)
        if fresh_reason
        else advice
    )
    plan = resolve_launch_executable(
        _backends.launch_plan(
            backend_name=str(record.get("backend") or ""),
            backend=backend,
            prompt=_restate_time_fence(
                resumed_prompt, record, attempt_started_at=attempt_started_at
            ),
            worktree=str(record.get("worktree") or "."),
            manifest_path=manifest_path,
            writable_directories=_fence_write_roots(
                backend=backend,
                repository=str(record.get("repo") or "."),
                run_directory=run_dir(run_id),
                manifest_path=manifest_path,
                worktree=record.get("worktree"),
                declared_write_paths=(record.get("node") or {}).get("write_paths")
                or (),
            ),
            resume_session=session_id or None,
            fence=FENCE_WORKERS,
            fence_config=config,
        )
    )
    # A resume runs where a dispatch runs. A placement-declaring backend's
    # resumed worker must be a step in the one shared allocation a dispatch
    # joins, not a child of the coordinator, so the placement is resolved
    # exactly as a dispatch resolves it — holding or adopting the reservation,
    # then prefixing the overlapping step that names its job id.
    plan = resolve_backend_placement(
        plan, backend, resume_project or None, payload=record
    )
    plan = _worker_runtime_plan(
        plan,
        run_id=run_id,
        manifest_path=manifest_path,
        attempt_started_at=attempt_started_at,
        coordinator_session=str(record.get("session") or ""),
    )

    def capture(current: dict[str, Any]) -> dict[str, Any]:
        # The held reservation's reach statement, resolved above onto the
        # pointer this attempt read, so the pointer persisted below carries it.
        if "placement_reservation" in record:
            current["placement_reservation"] = record["placement_reservation"]
        _carry_fence_unprotected(current, plan, config)
        # The pointer's fence flag describes the attempt that just launched, not
        # the one before it, so a resumed run records what this composition did
        # exactly as the primary dispatch does.
        current["fenced"] = _plan_composed_the_fence(plan)
        current["session_resumed"] = _launched_prior_session(plan) is not None
        if fresh_reason:
            current["session_id"] = None
            current["session_harness"] = None
            current["session_model"] = None
            current["session_withheld"] = fresh_reason
        # Only dispatch measures repository context fit. Resuming a session or
        # starting a replacement must not claim that measurement took place.
        current["context_fit"] = {
            "checked": False,
            "state": "unchecked",
            "window_tokens": backend.get("usable_input_window"),
            "detail": (
                "the resume request proceeds without re-verifying context fit; only "
                "dispatch performs that check against the current repository"
            ),
        }
        return current

    _mutate_pointer(run_id, capture)
    return plan


def _recorded_task_node(record: Mapping[str, Any]) -> TaskNode:
    """Rebuild the immutable dispatch request stored on a live run."""
    data = record.get("node")
    if not isinstance(data, Mapping):
        raise CrewError(f"run {record.get('run_id')!r} records no node definition")
    return TaskNode(
        id=str(data.get("id") or ""),
        goal=str(data.get("goal") or ""),
        plan=str(data.get("plan") or ""),
        section=str(data.get("section") or ""),
        brief=str(data.get("brief") or ""),
        brief_sha256=str(data.get("brief_sha256") or ""),
        brief_path=str(data.get("brief_path") or ""),
        role=str(data.get("role") or record.get("role") or "implement"),
        spec_level=str(data.get("spec_level") or ""),
        done_when=str(data.get("done_when") or ""),
        write_paths=[str(path) for path in data.get("write_paths") or ()],
        time_budget=str(data.get("time_budget") or ""),
        manifest_path=str(
            data.get("manifest_path") or record.get("manifest_path") or ""
        ),
        negative_control=str(data.get("negative_control") or ""),
        estimated_hours=data.get("estimated_hours"),
        requires_decisions=[str(key) for key in data.get("requires_decisions") or ()],
    )


def _worktree_git_read(
    worktree: Path, *arguments: str
) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
    """Run one bounded, read-only git query inside an inherited worktree."""
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        return result, detail or f"git {' '.join(arguments)} exited {result.returncode}"
    return result, None


def _inherited_worktree_reading(record: Mapping[str, Any]) -> str:
    """Describe a lane successor's retained worktree without blocking handoff."""
    taken_at = _utc_now()
    worktree_value = str(record.get("worktree") or "").strip()
    worktree = Path(worktree_value) if worktree_value else None
    lines = [
        "INHERITED WORKTREE READING (measured fact)",
        f"Reading taken at: {taken_at}",
        f"Worktree: {worktree_value or '(unset)'}",
    ]
    if worktree is None or not worktree.is_dir():
        return "\n".join(
            [
                *lines,
                "Inherited worktree could not be read.",
                "Reason: the recorded path does not exist or is not a directory.",
            ]
        )

    head_result, head_error = _worktree_git_read(
        worktree, "rev-parse", "--verify", "HEAD"
    )
    status_result, status_error = _worktree_git_read(
        worktree,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--no-renames",
    )
    summary_result, summary_error = _worktree_git_read(
        worktree,
        "diff",
        "--stat",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "HEAD",
        "--",
    )
    failure = head_error or status_error or summary_error
    if failure:
        return "\n".join(
            [
                *lines,
                "Inherited worktree could not be read.",
                f"Reason: {' '.join(str(failure).splitlines())}",
            ]
        )

    assert head_result is not None
    assert status_result is not None
    assert summary_result is not None
    head = head_result.stdout.strip()
    status = status_result.stdout.rstrip("\n")
    recorded_base = str(record.get("base_sha") or record.get("base") or "")
    lines.extend(
        [f"Head commit: {head}", f"Recorded base: {recorded_base or 'not recorded'}"]
    )
    if recorded_base:
        resolved_base = _resolve_commit(worktree, recorded_base)
        if not resolved_base:
            lines.append(
                "Head differs from recorded base: unknown; the recorded base could "
                f"not be resolved ({recorded_base!r} is not a commit)."
            )
        else:
            differs = "yes" if head != resolved_base else "no"
            lines.append(f"Head differs from recorded base: {differs}.")
    else:
        lines.append("Head differs from recorded base: unknown; no base was recorded.")

    if not status:
        lines.extend(
            [
                "Porcelain status: clean (no entries).",
                "Per-file change summary: no changes.",
            ]
        )
        return "\n".join(lines)

    lines.extend(["Porcelain status:", status, "Per-file change summary:"])
    summary = summary_result.stdout.rstrip("\n")
    if summary:
        lines.append(summary)
    untracked = [
        entry[3:]
        for entry in status.splitlines()
        if len(entry) >= 4 and entry.startswith("?? ")
    ]
    lines.extend(f"{path} | untracked" for path in untracked)
    if not summary and not untracked:
        lines.extend(
            f"{entry[3:]} | status {entry[:2]}"
            for entry in status.splitlines()
            if len(entry) >= 4
        )
    lines.append(
        "Checkpoint instruction: Commit the inherited changes before continuing; "
        "an inherited diff is the only copy of that work and a later refusal or "
        "death takes it."
    )
    return "\n".join(lines)


def _lane_prompt(
    record: Mapping[str, Any], advice: str, reason: str, *, continued: bool
) -> str:
    """Return either same-session advice or a complete fresh-start prompt."""
    if continued:
        return advice or f"Continue on the selected backend. Reason: {reason}"
    prompt_path = Path(str(record.get("prompt_path") or ""))
    if not prompt_path.is_file():
        raise CrewError(
            f"run {record.get('run_id')!r} needs a fresh session but its original "
            "prompt is unavailable"
        )
    original = prompt_path.read_text(encoding="utf-8")
    continuation = advice or "Continue the assigned work from its retained worktree."
    reading = _inherited_worktree_reading(record)
    return (
        f"{original.rstrip()}\n\n"
        "EXECUTION BACKEND CHANGED\n"
        f"Reason: {reason}\n\n"
        f"{reading}\n\n"
        "COORDINATOR ADVICE (instruction; passed through unchanged)\n"
        f"{continuation}"
    )


def _restate_time_fence(
    prompt: str, record: Mapping[str, Any], *, attempt_started_at: str
) -> str:
    """Restate the resumed attempt's own time fence on its launch prompt.

    The prompt a resumed attempt launches with — the same-session advice, or a
    fresh-start prompt — was composed for the attempt that already ended, so
    its fence names that attempt's clock. The resumed attempt is given its own
    launch instant and the deadline the recorded budget puts it under, from the
    same instant the launch records as this attempt's start.
    """
    node = record.get("node")
    budget = ""
    if isinstance(node, Mapping):
        budget = str(node.get("time_budget") or "")
    if not budget:
        return prompt
    statement = time_fence_statement(
        time_budget=budget, launch_instant=attempt_started_at
    )
    fence = f"FENCE — TIME (resumed attempt)\n  {statement}\n"
    if not prompt.strip():
        return fence
    return f"{prompt.rstrip()}\n\n{fence}"


def change_lane(
    run_id: str,
    backend_name: str,
    reason: str,
    *,
    config: Mapping[str, Any],
    advice: str = "",
    estimated_hours: float | None = None,
    launch: bool = True,
    launcher=None,
) -> dict[str, Any]:
    """Relaunch one live run elsewhere without replacing its identity.

    A blocked resumption and a working-run redispatch deliberately meet here.
    The destination is fully resolved and budget-checked before the current
    process is stopped. The existing run id, node and worktree stay in place;
    only the execution attempt changes.

    ``estimated_hours`` replaces the estimate the run carried into this attempt
    and is recorded on the run, so an orchestrator can correct an estimate the
    original estimate cannot express without editing the source plan.
    """
    destination = str(backend_name).strip()
    explanation = str(reason).strip()
    if not destination:
        raise CrewError("changing a run's backend requires a destination backend")
    if not explanation:
        raise CrewError("changing a run's backend requires a reason")

    record = read_pointer(run_id)
    source = str(record.get("backend") or "")
    if destination == source:
        raise CrewError(
            f"run {run_id!r} already uses backend {destination!r}; resume it without "
            "a backend override"
        )
    repository = Path(str(record.get("repo") or ".")).resolve()
    node = _recorded_task_node(record)
    if estimated_hours is not None:
        node.estimated_hours = float(estimated_hours)
    resolution = plan_dispatch(
        node=node,
        config=config,
        locked_decisions=node.requires_decisions,
        run_id=run_id,
        project=str(record.get("project") or ""),
        repo=repository,
        base=str(record.get("base_sha") or record.get("base") or "HEAD"),
        execution_override=bool(
            (record.get("execution_fit") or {}).get("override")
            if isinstance(record.get("execution_fit"), Mapping)
            else False
        ),
        backend_override=destination,
        session=str(record.get("session") or ""),
        # A lane change names its destination backend, so the picker has
        # nothing to select and asking it would only be refused.
        route="deterministic",
    )
    if not resolution.validation.ok:
        raise CrewError(
            f"run {run_id!r} cannot move to backend {destination!r} — "
            + "; ".join(
                f"{finding['property']}: {finding['detail']}"
                for finding in resolution.validation.findings
            )
        )
    lane_gate = resolution.lane_gate
    if lane_gate.get("state") in _LANE_GATE_WAITING_STATES:
        raise LanePaused(lane_gate)
    competence = resolution.competence or _competence_verdict(
        resolution=resolution,
        project=str(record.get("project") or ""),
        repo=repository,
    )
    if not competence["allowed"]:
        raise CompetenceLimit(competence)
    backend = resolution.backend_settings
    verdict = _budget_verdict(
        project=str(record.get("project") or ""),
        root=resolve_dispatch_ledger_root(
            resolution.authority
            or resolve_dispatch_authority(str(record.get("project") or ""), repository)
        ),
        config=config,
        backend_name=resolution.backend,
        backend=backend,
        purpose="dispatch",
    )
    if verdict["held"]:
        raise _actionable_budget_hold(verdict, config=config)

    source_launch = str(record.get("launch") or "")
    target_launch = resolution.launch
    source_harness = source_launch
    if source_launch == "cli":
        source_harness = str(record.get("dialect") or "")
        if not source_harness:
            source_harness = _backends.dialect_for(
                _backend_settings(record, config)
            ).name
    target_harness = target_launch
    if target_launch == "cli":
        target_harness = _backends.dialect_for(backend).name
    session = _current_harness_session(record, config=config)
    session_id = str(session.get("session_id") or "")
    continued = bool(
        session["resolved"]
        and source_launch == target_launch == "cli"
        and source_harness == target_harness
    )
    prompt = _lane_prompt(record, advice, explanation, continued=continued)
    attempt = int(record.get("attempt") or 1) + 1
    directory = run_dir(run_id)
    manifest_path = _recorded_manifest_path(record, run_id)
    prompt_path = directory / f"lane-change-{attempt}-prompt.txt"
    log_path = directory / f"lane-change-{attempt}.jsonl"
    stderr_path = directory / f"lane-change-{attempt}.stderr.log"
    final_path = directory / f"lane-change-{attempt}-final.txt"
    lane_change = {
        "from_backend": source,
        "to_backend": resolution.backend,
        "reason": explanation,
        "changed_at": _utc_now(),
        "from_harness": source_harness,
        "to_harness": target_harness,
        "session": "continued" if continued else "fresh",
        "session_id": session_id or None,
        "session_source": session.get("source"),
        "detail": (
            f"continued session {session_id!r} on harness {target_harness!r}"
            if continued
            else (
                "starting fresh because the session cannot follow the move from "
                f"harness {source_harness!r} to {target_harness!r}"
            )
        ),
    }
    # An in-harness attempt is delegated, so its directive is the only place the
    # attempt identity can be carried: the harness exports this environment to
    # the task it spawns. Composed here so the preview and the persisted record
    # name the same environment the launch path attaches.
    directive_environment = _worker_runtime_environment(
        None,
        run_id=run_id,
        manifest_path=manifest_path,
        attempt_started_at=lane_change["changed_at"],
        coordinator_session=str(record.get("session") or ""),
        claude_headers=False,
    )
    target_plan: _backends.LaunchPlan | None = None
    if target_launch == "cli":
        preflight_launch_command(resolution.backend, backend, fence=FENCE_WORKERS)
        target_plan = resolve_launch_executable(
            _backends.launch_plan(
                backend_name=resolution.backend,
                backend=backend,
                prompt=prompt,
                worktree=str(record.get("worktree") or "."),
                manifest_path=manifest_path,
                writable_directories=_fence_write_roots(
                    backend=backend,
                    repository=str(record.get("repo") or "."),
                    run_directory=directory,
                    manifest_path=manifest_path,
                    worktree=record.get("worktree"),
                    declared_write_paths=(record.get("node") or {}).get("write_paths")
                    or (),
                ),
                final_message_path=str(final_path),
                resume_session=session_id if continued else None,
                fence=FENCE_WORKERS,
                fence_config=config,
            )
        )
        target_plan = _worker_runtime_plan(
            target_plan,
            run_id=run_id,
            manifest_path=manifest_path,
            attempt_started_at=lane_change["changed_at"],
            coordinator_session=str(record.get("session") or ""),
        )
    preview: dict[str, Any] = {
        "run_id": run_id,
        "node": record.get("node"),
        "worktree": record.get("worktree"),
        "backend": resolution.backend,
        "launch": target_launch,
        "lane_change": lane_change,
    }
    if target_plan is not None:
        preview.update(target_plan.as_dict())
    else:
        preview["directive"] = {
            "attach_with": f"reckon crew attach --run {run_id} --task <task-id>",
            "environment": directive_environment,
            "prompt_path": str(prompt_path),
            "worktree": str(record.get("worktree") or ""),
        }
    # The launch path's refusals are read once, ahead of the preview return, so
    # a --print-only lane change names the refusal a real call would raise. The
    # resume sweep orders its own gates the same way — both ahead of its dry-run
    # branch — because a prediction that reads differently from the thing it
    # predicts is not a prediction. The stop a launch performs is a side effect
    # and stays below, where a preview cannot reach it.
    if (
        source_launch == "in-harness"
        and record.get("task")
        and str(record.get("phase") or "") not in _TERMINAL_RUN_PHASES
    ):
        raise CrewError(
            f"run {run_id!r} is attached to live harness task {record['task']!r}; "
            "cancel it in that harness before changing backend"
        )
    source_process_alive = (
        source_launch == "cli" and record_process_alive(record, process_alive) is True
    )
    if source_launch == "cli" and not source_process_alive:
        # "Not known to be alive" is not an observed end. A worker whose pointer
        # recorded no process, or whose pid this host cannot answer for, may
        # still be writing, so a fresh worker started over it is the collision
        # the stop below prevents — the same rule the resume door applies. The
        # reading is the sweep's own helper rather than a second composition of
        # it, so what counts as an observed end cannot drift between the two
        # doors, and the refusal names the reading it holds.
        from reckon.crew.resumption import _observed_end_refusal

        refusal = _observed_end_refusal(record)
        if refusal is not None:
            raise refusal

    if not launch:
        return preview

    if source_process_alive:
        _signal_process_group(
            int(record["pid"]),
            record.get("pid_start_time"),
            run_dir=directory,
            reason="lane-change",
        )

    directory.mkdir(parents=True, exist_ok=True)
    # The lane-change prompt was composed for the attempt that already ended —
    # the bare advice for a continued session, or the original prompt with its
    # original fence for a fresh one — so restating the fence for the attempt now
    # starting is what stops the worker reading the first attempt's deadline. The
    # resume path does the same through resume_plan.
    prompt_path.write_text(
        _restate_time_fence(
            prompt, record, attempt_started_at=lane_change["changed_at"]
        ),
        encoding="utf-8",
    )
    spawned_pid: int | None = None
    if target_plan is not None:
        spawn = launcher or _spawn
        spawned_pid = spawn(
            target_plan,
            log_path=log_path,
            stderr_path=stderr_path,
            prompt_path=prompt_path,
        )

    def move(current: dict[str, Any]) -> dict[str, Any]:
        if str(current.get("worktree") or "") != str(record.get("worktree") or ""):
            raise CrewError(f"run {run_id!r} changed worktree during its lane change")
        if isinstance(current.get("node"), dict):
            current["node"]["estimated_hours"] = node.estimated_hours
        history = [dict(item) for item in current.get("lane_changes") or ()]
        history.append(lane_change)
        lineage = {
            "kind": "lane-change",
            "attempt": attempt,
            "root_run_id": run_id,
            "lanes": history,
        }
        current.update(
            {
                "backend": resolution.backend,
                "route": getattr(resolution, "route", current.get("route")),
                "route_override": getattr(
                    resolution, "route_override", current.get("route_override")
                ),
                "launch": target_launch,
                "sandbox": backend.get("sandbox"),
                "sandbox_write_roots": (
                    None
                    if resolution.sandbox_write_roots is None
                    else [str(path) for path in resolution.sandbox_write_roots]
                ),
                "session_reuse_capable": bool(backend.get("session_reuse")),
                "session_resumed": _launched_prior_session(target_plan) is not None,
                "agent": _stamp_agent_display(
                    _agent_configuration(resolution.backend, target_launch, backend),
                    backend,
                ),
                "attempt": attempt,
                "attempt_kind": "lane-change",
                "attempt_started_at": lane_change["changed_at"],
                "estimated_hours": node.estimated_hours,
                "phase": "working" if target_plan is not None else "starting",
                "session_id": session_id if continued else None,
                "session_harness": target_harness if continued else None,
                "session_model": backend.get("model") if continued else None,
                "pid": spawned_pid,
                "pid_start_time": (
                    _process_start_time(spawned_pid)
                    if spawned_pid is not None
                    else None
                ),
                "task": None,
                "prompt_path": str(prompt_path),
                "log_path": str(log_path),
                "stderr_path": str(stderr_path),
                "final_message_path": str(final_path),
                "manifest_baseline_mtime_ns": _manifest_mtime_ns(
                    current.get("manifest_path") or ""
                ),
                "budget": _backends.unknown_budget("no events yet on the new lane"),
                "lane_change": lane_change,
                "lane_changes": history,
                "lineage": lineage,
            }
        )
        _carry_fence_unprotected(current, target_plan, config)
        # The fence flag follows the attempt the lane change just launched. A
        # CLI run moved to an in-harness backend composes no fence at all, so
        # the flag must fall false rather than carry the prior attempt's true
        # beside the absent removed-defaults list.
        current["fenced"] = _plan_composed_the_fence(target_plan)
        if target_plan is not None:
            current.update(
                {
                    "argv": list(target_plan.argv),
                    "command": str(target_plan.argv[0]),
                    "dialect": target_plan.dialect,
                }
            )
            current.pop("directive", None)
        else:
            current.update(
                {
                    "argv": None,
                    "command": None,
                    "dialect": None,
                    "directive": {
                        "attach_with": (
                            f"reckon crew attach --run {run_id} --task <task-id>"
                        ),
                        "fences": {
                            "delivery": str(current.get("manifest_path") or ""),
                            "evidence": node.done_when,
                            "scope": list(node.write_paths),
                            "time": node.time_budget,
                        },
                        "environment": directive_environment,
                        "prompt_path": str(prompt_path),
                        "sandbox": {
                            "tier": backend.get("sandbox"),
                            "write_roots": current["sandbox_write_roots"],
                        },
                        "worktree": str(current.get("worktree") or ""),
                    },
                }
            )
        return current

    return _mutate_pointer(run_id, move)


def terminate(run_id: str) -> dict[str, Any]:
    """Signal a spawned run's process group to stop, and record that."""

    def stop(record: dict[str, Any]) -> dict[str, Any]:
        pid = record.get("pid")
        if not pid:
            raise CrewError(f"run {run_id!r} has no process to stop")
        try:
            _signal_process_group(
                int(pid),
                record.get("pid_start_time"),
                run_dir=run_dir(run_id),
                reason="run-stop",
            )
        except (ProcessLookupError, PermissionError, OSError) as exc:
            record["detail"] = f"could not signal pid {pid} — {exc}"
        else:
            record["detail"] = f"SIGTERM sent to process group of pid {pid}"
        record["phase"] = "stopped"
        record["stopped_at"] = _utc_now()
        return record

    return _mutate_pointer(run_id, stop)


def record_resumption(
    run_id: str,
    *,
    pid: int,
    turn: int,
    log_path: str | Path,
    stderr_path: str | Path,
    attempt_started_at: str = "",
    manifest_baseline_mtime_ns: int | None = None,
) -> dict[str, Any]:
    """Record a launched resumption without overwriting newer observations."""

    def resume(record: dict[str, Any]) -> dict[str, Any]:
        from reckon.crew.resumption import resolve_session

        prior_session_resumed = record.get("session_resumed")
        if prior_session_resumed is None:
            prior_session_resumed = resolve_session(run_id, record=record)["resolved"]
        current_attempt = bool(
            attempt_started_at or manifest_baseline_mtime_ns is not None
        )
        record.update(
            {
                "pid": pid,
                "pid_start_time": _process_start_time(pid),
                "phase": "working",
                "attempt": int(record.get("attempt") or 1) + 1,
                "attempt_kind": "resume",
                # A harness change may require a fresh session even though
                # this attempt was requested through the resume command.
                "session_resumed": bool(prior_session_resumed),
                "attempt_started_at": attempt_started_at or _utc_now(),
                "manifest_baseline_mtime_ns": (
                    _manifest_mtime_ns(record.get("manifest_path") or "")
                    if manifest_baseline_mtime_ns is None
                    else manifest_baseline_mtime_ns
                ),
                "resumed_turn": turn,
                "log_path": (
                    str(log_path) if current_attempt else record.get("log_path")
                ),
                "stderr_path": str(stderr_path),
                # A new attempt has made no observations, so it must not inherit
                # the previous attempt's folded budget observation: a refusal
                # folded from the superseded stream would otherwise short-circuit
                # the refusal classifier before it reads the live resume stream.
                # The honest unknown-headroom state keeps the reader on the
                # stream that is actually running.
                "budget": _backends.unknown_budget(
                    "no events yet on the resumed attempt"
                ),
            }
        )
        return record

    return _mutate_pointer(run_id, resume)


# The supervisor entry point. This guard sits at the end of the module because
# the entry token and the supervisor's own machinery are defined with the rest
# of the launch path below it, and a module run as the supervisor must have
# them defined before it dispatches on its argv.
if __name__ == "__main__":
    raise SystemExit(_peer_command())
