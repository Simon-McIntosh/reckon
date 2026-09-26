from __future__ import annotations

import argparse
import ast
import ctypes
import dataclasses
import errno
import fcntl
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from reckon import _backends, _store, capability, flight, ledger
from reckon.crew import lane_document as _lane_document
from reckon.crew import summary
from reckon.crew.node import (
    BudgetHold,
    CompetenceLimit,
    CrewError,
    DEFAULT_MEMBER_IDLE_WINDOW,
    claim_disposition,
    claim_repository,
    repository_identity,
    NEEDS_HELP_MARKER,
    NodeValidation,
    ScopeConflict,
    TaskNode,
    UnreconciledRuns,
    WatcherRequired,
    _SAFE_ID,
    _TERMINAL_RUN_PHASES,
    normalize_section,
    negative_control_finding,
    gate_population_finding,
    parse_duration,
    placement_query_undeclared,
    placement_requirement_node_local,
    placement_requirement_unmet,
    refuse_member_in_flight,
    validate_node,
)
from reckon.crew.prompts import compose_prompt
from reckon.crew.refusals import format_refusal
from reckon.crew.recovery import REVIEW_NODE_PREFIX, stream_paths_newest_first
from reckon.crew.review import review_store_root
from reckon.crew.routing import (
    _agent_configuration,
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
    require_plan_section_visible,
    resolve_budget_fallback,
    resolve_dispatch_authority,
    resolve_dispatch_ledger_root,
    resolve_scope_repository,
    resolve_role,
    resolve_role_override,
    resolved_time_budget,
    resolved_time_ceiling,
    shadow_worktree_session,
)
from reckon.crew.runs import (
    _expanded_scope_paths,
    _manifest_freshness,
    _manifest_mtime_ns,
    _merge_peer_scopes,
    _mutate_pointer,
    _process_start_time,
    _project_derivations,
    _scopes_overlap,
    _shared_write_paths,
    _utc_now,
    _watch_arming_line,
    _watch_attach_line,
    _write_json,
    capture_run_session,
    delivery_roots,
    list_live,
    new_run_id,
    pointer_path,
    process_alive,
    placement_job_alive,
    read_pointer,
    record_process_alive,
    reports_dir,
    run_dir,
    scheduler_job_reason,
    scheduler_job_state,
    scheduler_kill_class,
    project_watch_visibility,
    watch_lock_path,
    watch_state,
    watch_stream_path,
)

_INOTIFY_EVENTS = 0x00000100 | 0x00000008 | 0x00000080
# Process startup and registration may receive only one scheduler slice in six
# while two CPU-bound jobs share a loaded host. Keep every watcher condition
# wait on this one six-times-unloaded bound so a red test reports a producer
# defect rather than which process won the scheduler.
WATCHER_LOAD_BOUND_SECONDS = 30.0

# Workers launch unfenced. The fence's read-only overlay seals every checkout
# under the operator's code root, and with it each worktree's git directory and
# the shared object store, so a fenced worker cannot commit; and a fenced run's
# own harness home does not yet carry the operator's hooks or instruction
# files. Turn this on only once both are granted and carried.
FENCE_WORKERS = False


# Arming spawns a detached supervisor on purpose: a coordinator's producer has
# to outlive the process that armed it. Under a test the same act is a leak —
# the test ends, its configuration home is deleted, and the producer keeps
# polling a directory nothing will ever write to again. Measured before a
# manual reap: 25 live producers for one fixture project, the oldest 204 hours
# old, 14 of them polling an already-deleted temporary home. So arming refuses
# when the resolved configuration home lies under a pytest temporary
# directory, and the refusal is raised at the caller rather than skipped
# quietly. A test whose own subject is the producer lifecycle, and which reaps
# what it starts, says so through this variable.
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


def _section_heading_matches(heading: Any, requested: str, ids: set[str]) -> bool:
    """Return whether one heading identifies the requested authored section."""
    if str(heading.get("id") or "").casefold() in ids:
        return True
    text = re.sub(r"\s+", " ", heading.get_text(" ", strip=True)).casefold()
    return text == requested or bool(
        re.match(rf"^{re.escape(requested)}(?:\s|[-—:])", text)
    )


def _plan_section_text(html_text: str, section: str) -> str | None:
    """Extract one section's visible text without including its successors."""
    from bs4 import BeautifulSoup

    requested = re.sub(r"\s+", " ", section.strip()).casefold()
    if not requested:
        return None
    ids = {requested.removeprefix("#")}
    numbered = re.fullmatch(r"§\s*([A-Za-z0-9._-]+)", requested)
    if numbered:
        ids.add(f"s{numbered.group(1)}".casefold())

    soup = BeautifulSoup(html_text, "html.parser")
    identified = next(
        (
            tag
            for tag in soup.find_all(id=True)
            if str(tag.get("id") or "").casefold() in ids
        ),
        None,
    )
    if identified is not None and not re.fullmatch(r"h[1-6]", identified.name or ""):
        return identified.get_text(" ", strip=True)

    heading = identified
    if heading is None:
        heading = next(
            (
                candidate
                for candidate in soup.find_all(re.compile(r"^h[1-6]$"))
                if _section_heading_matches(candidate, requested, ids)
            ),
            None,
        )
    if heading is None:
        return None

    level = int(str(heading.name)[1])
    pieces = [heading.get_text(" ", strip=True)]
    for sibling in heading.next_siblings:
        sibling_name = getattr(sibling, "name", None)
        if (
            isinstance(sibling_name, str)
            and re.fullmatch(r"h[1-6]", sibling_name)
            and int(sibling_name[1]) <= level
        ):
            break
        if hasattr(sibling, "get_text"):
            text = sibling.get_text(" ", strip=True)
        else:
            text = str(sibling).strip()
        if text:
            pieces.append(text)
    return " ".join(pieces)


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
        try:
            observed = datetime.fromisoformat(str(stamp))
        except (TypeError, ValueError):
            observed = None
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
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=UTC)
            moment = datetime.fromisoformat(_utc_now())
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


def _live_runs_on_backend(backend_name: str) -> list[dict[str, Any]]:
    """Return non-terminal live pointers claiming a backend, newest run id last."""
    return [
        pointer
        for pointer in list_live()
        if str(pointer.get("backend") or "") == backend_name
        and str(pointer.get("phase") or "") not in _TERMINAL_RUN_PHASES
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

    Both the record and the count are the PROJECT's. The reservation a project
    holds admits that project's workers, so only those occupy its roster, and
    a project holding none is unaffected by one another project holds. The lane
    bound above this counts across projects and should, because a served lane
    is genuinely shared; an allocation is not.
    """
    from reckon import flight
    from reckon.crew import placement as placement_module

    if flight.placement_for(backend) is None:
        return
    if not placement_module.read_reservation(project):
        return
    occupants = placement_module.occupying_the_reservation(occupying, project)
    refusal = placement_module.reservation_roster_refusal(len(occupants))
    if refusal is None:
        return
    occupying_ids = [
        str(pointer.get("run_id") or "unknown") for pointer in occupying
    ]
    raise CrewError(f"{refusal} Occupying runs: {', '.join(occupying_ids) or 'none'}.")


def _refuse_over_concurrency_ceiling(
    backend_name: str, backend: Mapping[str, Any], project: str | None = None
) -> None:
    """Refuse a dispatch that would exceed whichever bound is actually binding.

    A lane already carrying its ceiling of live runs must not be asked to carry
    one more: the harness retry budget is fixed and reckon passes no retry
    configuration, so once an overloaded lane refuses long enough a 429 turns
    from a pause at the protocol into a dead print-mode worker — measured, a
    sixth concurrent worker on the local lane killed two already-running runs
    after ten 429 retries. Adding work destroyed work, so the only reliable
    remedy is not to create the overload.

    Which resource bounds the lane is read rather than assumed. The roster
    ceiling is one candidate; the cores a placement's reservation admits and
    the login memory slice the coordinator still lives inside are the others,
    and the refusal names the one that ran out with its measured value. The
    check happens before any worktree or worker exists and never touches a run
    already in flight — a finished run holds no slot (its phase is terminal),
    and terminating one to admit a new one would reproduce the harm this exists
    to prevent.

    Every bound is user data or a host reading. A bound that cannot be read
    admits: an unknown ceiling, an unstated reservation and an unreadable
    cgroup can none of them justify refusing work.
    """
    occupying = _live_runs_on_backend(backend_name)
    bounds = summary.concurrency_bounds(
        backend, occupancy=len(occupying), login_slice=summary.read_login_slice()
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
    # A placed backend's workers run inside the one reservation, so the roster
    # cap is a second ceiling and the only one nothing enforces on our behalf:
    # under --overlap the scheduler admits whatever is asked, which makes the
    # cap a real limit rather than a formality.
    _refuse_over_reservation_roster(backend, occupying, project)


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


def _temporary_home_root(home: Path) -> Path | None:
    """Return the throwaway test root containing ``home``, if there is one."""
    for candidate in (home, *home.parents, *home.resolve().parents):
        if _PYTEST_TEMPORARY_ROOT.match(candidate.name):
            return candidate
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


def _watch_producer_argv(project: str, supervisor: str) -> list[str]:
    return [
        sys.executable,
        "-c",
        supervisor,
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
    supervisor = (
        "import subprocess, sys; "
        "producer = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True); "
        "raise SystemExit(producer.wait())"
    )
    argv = _watch_producer_argv(project, supervisor)
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
    with arming_lock.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        state = watch_state(project, session=session)
        if state["watcher_live"]:
            # A producer whose supervisor died keeps appending real transitions,
            # so it stays readable — but nothing will ever replace it, and it
            # holds the seat lock, so every later arming returns here and the
            # stale seat outlives every session that cared. Measured: one held
            # for four days, and another had to be cleared by hand. Replace it
            # rather than refusing a dispatch over it: refusing would block work
            # on account of a producer that is streaming perfectly, while
            # accepting it silently keeps the seat unreplaceable. Admission is
            # still decided by `session_attached` below.
            from reckon.crew.recovery import unwatch

            if project_watch_visibility(project)["observer_alive"] is False:
                unwatch(project)
            else:
                return state

        supervisor = _start_watch_producer(project)
        deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
        while time.monotonic() < deadline:
            # Poll producer liveness only. Resolving this session's delivery
            # costs a descriptor trace, and a trace on a loop that runs twenty
            # times a second spent the whole arming budget on measurement — the
            # session's attachment is read once, after the producer is up.
            if watch_state(project)["watcher_live"]:
                return watch_state(project, session=session)
            if supervisor.poll() is not None:
                break
            time.sleep(0.05)
        return watch_state(project, session=session)


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


def _repository_scope_claims() -> list[_RepositoryScopeClaim]:
    """Read live claims globally and group their paths by repository root."""
    repository_projects = mounted_repository_projects()
    claims: list[_RepositoryScopeClaim] = []
    for pointer in list_live():
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
                )
            )
    return sorted(
        claims,
        key=lambda claim: (claim.run_id, claim.node_id, claim.absolute_path.as_posix()),
    )


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
    """Return the plan file, evidence record and figure topic for this node.

    Every node on a plan appends its landing record to its plan section
    and its evidence anchor to the cumulative evidence record. The plan-owned
    figure topic is shared for the same reason: each node may illustrate its
    landing record without needing a scope outside its dispatch grant. Files
    within that directory remain exclusive claims, so two nodes cannot replace
    the same rendered artifact. These three repository paths are shared by all
    plan nodes rather than owned by any one.
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


def _grant_landing_write_paths(
    node: TaskNode,
    *,
    project: str,
    authority: Mapping[str, Any],
) -> None:
    """Declare the shared landing paths in the node's write scope."""
    plan = authority.get("plan")
    if not isinstance(plan, Mapping):
        return
    try:
        plan_repo = Path(str(plan["repository"])).expanduser().resolve()
    except (KeyError, TypeError, ValueError):
        return
    shared = sorted(_shared_landing_paths(node, project=project, authority=authority))
    for absolute in shared:
        try:
            relative = absolute.relative_to(plan_repo)
        except ValueError:
            continue
        declared = relative.as_posix()
        if declared not in node.write_paths:
            node.write_paths.append(declared)


def _candidate_scope_entries(
    node: TaskNode,
    *,
    project: str,
    repo: Path,
    authority: Mapping[str, Any],
) -> list[tuple[Path | None, str, Path, str, str | None]]:
    repository_projects = mounted_repository_projects()
    repositories = tuple(
        repository_identity(root) or Path(str(root)).expanduser().resolve()
        for root in authority.get("repositories") or (repo,)
    )
    write = authority.get("write")
    write = write if isinstance(write, Mapping) else {}
    entries = _resolved_scope_entries(
        node.write_paths,
        base_repository=repository_identity(repo) or Path(repo).resolve(),
        repositories=repositories,
        project=project,
        repository_projects=repository_projects,
        preferred_projects=tuple(str(item) for item in write.get("projects") or ()),
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
) -> list[dict[str, Any]]:
    candidates = _candidate_scope_entries(
        node, project=project, repo=repo, authority=authority
    )
    shared = _shared_landing_paths(node, project=project, authority=authority)
    shared_files = _shared_write_paths(project, repo)
    conflicts: list[dict[str, Any]] = []
    for claim in claims:
        if claim.absolute_path.resolve() in shared:
            continue
        paths = [
            {"left_path": path, "right_path": claim.path}
            for repository, path, absolute, _declared, _derived_from in candidates
            if repository == claim.repository
            and _scopes_overlap(absolute.as_posix(), claim.absolute_path.as_posix())
            and not (path in shared_files and path == claim.path)
        ]
        if not paths:
            continue
        if not claim.binding:
            if disregarded is not None and claim.disposition_reason not in disregarded:
                disregarded.append(claim.disposition_reason)
            continue
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


def _raise_repository_scope_conflict(
    node: TaskNode,
    *,
    project: str,
    repo: Path,
    authority: Mapping[str, Any],
    claims: Iterable[_RepositoryScopeClaim],
    disregarded: list[str] | None = None,
) -> None:
    candidates = _candidate_scope_entries(
        node, project=project, repo=repo, authority=authority
    )
    shared = _shared_landing_paths(node, project=project, authority=authority)
    shared_files = _shared_write_paths(project, repo)
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
            if claim.disposition_reason:
                message = f"{message}; {claim.disposition_reason}"
            refusal.args = (message,)
            raise refusal


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
) -> list[dict[str, Any]]:
    """Find live node pairs that receive a durable peer channel."""
    adjacent: list[dict[str, Any]] = []
    for pointer in list_live(project=project):
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
    competence: dict[str, Any] | None = None
    authority: dict[str, Any] | None = None
    live_conflicts: list[dict[str, Any]] | None = None
    sandbox_write_roots: tuple[Path, ...] | None = None
    requested_backend: str | None = None
    default_backend: str | None = None
    lane_declaration: dict[str, Any] | None = None
    lane_reading: dict[str, Any] | None = None
    lane_advisory: dict[str, Any] | None = None

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
            "node": self.node.as_dict(),
            "requested_backend": self.requested_backend,
            "run_id": self.run_id,
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
        }
        if self.competence is not None:
            payload["competence"] = dict(self.competence)
        if self.authority is not None:
            payload["authority"] = dict(self.authority)
        if self.live_conflicts is not None:
            payload["live_conflicts"] = [dict(item) for item in self.live_conflicts]
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
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
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
    """
    if document is None:
        return _lane_reading_unknown(detail="no lane document")
    if not isinstance(document, Mapping):
        return _lane_reading_unknown(
            detail=f"lane document is {type(document).__name__}, not a JSON object"
        )
    stamp = document.get("observed_at")
    if not isinstance(stamp, str):
        return _lane_reading_unknown(
            detail="lane document carries no parseable 'observed_at' timestamp"
        )
    try:
        observed = datetime.fromisoformat(stamp)
    except ValueError as exc:
        return _lane_reading_unknown(
            detail=f"'observed_at' {stamp!r} is not an ISO-8601 timestamp: {exc}"
        )
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
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
    mean_context = _metric_number(document.get("mean_context"))
    binding = document.get("binding_observed")
    if isinstance(binding, str) and not binding.strip():
        binding = None
    shelf = _metric_number(document.get("suggested_shelf_life_seconds"))
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
    return {
        "state": "fresh",
        "headroom": "unknown" if headroom is None else headroom,
        # The field names WHICH constraint binds, and a lane with no such
        # constraint has nothing to name rather than nothing to report.
        "binding_observed": "unknown" if binding is None else binding,
        "mean_context": "unknown" if mean_context is None else mean_context,
        "observed_at": stamp,
        "age_seconds": int(age.total_seconds()),
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
    authority: Mapping[str, Any] | None = None,
    report_live_conflicts: bool = False,
    local: bool = False,
    backend_override: str | None = None,
    default_backend_override: str | None = None,
    declared_backend: str | None = None,
    member: str = "",
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
    requested_backend = str(backend_override or default_backend_override or "").strip()
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
        if repo is None:
            raise CrewError(
                f"crew member {member!r} cannot be resolved without a repository"
            )
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
    if requested_backend:
        backend_name, backend = resolve_role_override(
            config, node.role, node.spec_level, requested_backend
        )
    else:
        backend_name, backend = resolve_role(config, node.role, node.spec_level)
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
    resolved_authority: dict[str, Any] | None = None
    sandbox_write_roots: tuple[Path, ...] | None = None
    if verdict.ok and repo is not None:
        resolved_authority = dict(
            authority or resolve_dispatch_authority(project, repo)
        )
        if _can_write_worktree(
            backend,
            repository=Path(repo).resolve(),
            run_directory=run_dir(resolved_run_id),
        ):
            _grant_landing_write_paths(
                node, project=project, authority=resolved_authority
            )
        _require_write_paths_in_authority(node, resolved_authority)
        plan_commit = require_plan_section_visible(
            node=node,
            project=project,
            repo=repo,
            base=base,
            authority=resolved_authority,
        )
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
        authority=resolved_authority,
        requested_backend=requested_backend or None,
        default_backend=str(config.get("default_backend") or "") or None,
        lane_declaration=lane_declaration,
        lane_reading=lane_reading,
        lane_advisory=lane_advisory,
    )
    if verdict.ok and repo is not None:
        resolution.competence = _competence_verdict(
            resolution=resolution, project=project, repo=Path(repo).resolve()
        )
        if report_live_conflicts:
            repo_root = Path(repo).resolve()
            resolution.live_conflicts = _live_conflict_rows(
                node,
                project=project,
                repo=repo_root,
                authority=resolved_authority,
                claims=_repository_scope_claims(),
                disregarded=resolution.warnings,
            )
    resolution.sandbox_write_roots = sandbox_write_roots
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
    unreconciled_override: bool = False,
    watch_required: bool = False,
    watch_override: bool = False,
    lineage_override: Mapping[str, Any] | None = None,
    worktree_session: str | None = None,
    local: bool = False,
    backend_override: str | None = None,
    default_backend_override: str | None = None,
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
    the member another run in flight happens to hold.

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
    resolution = plan_dispatch(
        node=node,
        config=config,
        locked_decisions=locked_decisions,
        peer_scopes=peer_scopes,
        project=project,
        repo=repo_root,
        base=base,
        execution_override=execution_override,
        authority=authority,
        local=local,
        backend_override=backend_override,
        default_backend_override=default_backend_override,
        member=member,
    )
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
            "grace": unreconciled_grace,
            "waived_runs": unreconciled,
        }
        if unreconciled_override
        else None
    )

    budget_warnings: list[str] = []
    budget_fallback: dict[str, Any] | None = None
    requested_backend = resolution.requested_backend
    if check_budget:
        # Before the worktree, not after: a hold that had already cut a worktree
        # would leave write scope claimed by a node nobody is running.
        requested_backend_name = resolution.backend
        verdict = _budget_verdict(
            project=project,
            root=ledger_root,
            config=config,
            backend_name=resolution.backend,
            backend=resolution.backend_settings,
            purpose="dispatch",
            budget_state=budget_state,
        )
        budget_warnings.extend(verdict.get("warnings") or ())
        if verdict["held"]:
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
                authority=authority,
                local=local,
                run_id=resolution.run_id,
                backend_override=fallback_name,
                declared_backend=(
                    str(resolution.lane_declaration.get("backend") or "")
                    if resolution.lane_declaration is not None
                    else ""
                ),
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
    directory = run_dir(run_id)
    # A backend at its declared concurrency ceiling refuses a new dispatch
    # before anything is created or spawned. A fallback backend resolved above
    # gets the same ceiling as a directly chosen one, so a held lane never
    # reroutes onto an already-saturated lane.
    _refuse_over_concurrency_ceiling(backend_name, backend, project)
    explicitly_named_peers = set() if shadow_lineage else set(node.peer_scopes)
    peers = {} if shadow_lineage else _merge_peer_scopes(peer_claims, node.peer_scopes)
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
        idle_window=str(fences.get("member_idle_window") or DEFAULT_MEMBER_IDLE_WINDOW),
    )
    named_member = bool(member)
    # An unnamed dispatch is disposable: it carries a per-run identity instead
    # of the dispatching session's shared one, and it gets no roster row. So
    # two unnamed dispatches of one coordinator — every reflex review among
    # them — are never serialised against each other, and one run in flight
    # cannot refuse an unrelated task with `member-in-flight`. A named member
    # remains a deliberate route to a durable worker, so it keeps the roster
    # lookup, the D14 check and the refusal.
    effective_member = member or _disposable_member_id(run_id)
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
    live_pointers = list_live(project=project)
    if roster_member is not None:
        for pointer in live_pointers:
            if pointer.get("member") == effective_member:
                refuse_member_in_flight(effective_member, pointer)
    disregarded_claims: list[str] = []
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
        )
        adjacent_peers = _adjacent_live_peers(
            node,
            project=project,
            repo=repo_root,
            explicitly_named=explicitly_named_peers,
        )
    agent = _stamp_agent_display(
        _agent_configuration(backend_name, launch_kind, backend), backend
    )
    if resolution.local:
        agent["local"] = True
    committed_runs = ledger.runs(project, root=ledger_root)
    session_resolution = (
        _task_session_resolution(
            node,
            project,
            committed_runs=committed_runs,
            live_pointers=live_pointers,
            harness=_backends.dialect_for(backend).name if launch_kind == "cli" else "",
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
                format_refusal("D20", "shadow lineage names no committed primary run")
            )
        attempt = int(primary.get("attempt") or 1)
    elif prior_node_runs:
        previous = prior_node_runs[-1]
        previous_lineage = previous.get("lineage") or {}
        previous_attempt = previous.get("attempt") or previous_lineage.get("attempt")
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
    if watch_required and not watch_override and watch_arming_suppressed():
        # Opting in is the caller's act. An environment that forbids arming
        # turns the requirement into the recorded waiver below rather than
        # into a producer nobody will reap.
        watch_override = True
    if watch_required and not watch_override:
        dispatch_watch = _ensure_watch_producer(project, session=session)
        # The watcher requirement is answered by the process, read from the
        # watcher's own state — never by a session's follower, which is how a
        # project with no watcher process at all kept admitting dispatches. A
        # refusal here teaches the command that starts a durable watcher, which
        # is idempotent, so it is safe to run against one already up.
        if not dispatch_watch["watcher_live"]:
            raise WatcherRequired(project, dispatch_watch)
        # A producer exists now. Whether this session hears what it writes is a
        # separate fact, and the only one that decides if the finished run gets
        # noticed, so it is checked before a worktree exists.
        if launch_kind == "cli" and not dispatch_watch["session_attached"]:
            raise WatcherRequired(project, dispatch_watch, session=session)
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

    worktree = _create_worktree(repo_root, worktree_identity, node.id, base)
    spawned_pid: int | None = None
    spawned_start_time: str | None = None
    wired_peer_run_ids: list[str] = []
    try:
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
        dispatch_host = _current_host_facts()
        prompt = compose_prompt(
            node=node,
            project=project,
            worktree=worktree["path"],
            working_directory=working_directory,
            can_write_worktree=_can_write_worktree(
                backend,
                repository=repo_root,
                run_directory=directory,
            ),
            manifest_path=node.manifest_path,
            time_budget=node.time_budget,
            needs_help_after_failures=int(fences.get("needs_help_after_failures", 2)),
            peer_scopes=peers,
            run_id=run_id,
            peer_channels={
                str(peer["node"]): {"run_id": str(peer["run_id"])}
                for peer in adjacent_peers
            },
            peer_channel_path=str(_channel_root(run_id)),
            host_line=_worker_host_line(dispatch_host, directory),
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

        attempt_started_at = _utc_now()
        record: dict[str, Any] = {
            "run_id": run_id,
            "project": project,
            "repo": str(repo_root),
            "authority": resolution.authority,
            "session": session,
            "wave": wave_id,
            "coordinator": coordinator,
            "node": node_definition,
            "role": node.role,
            "backend": backend_name,
            "requested_backend": resolution.requested_backend,
            "lane_declaration": resolution.lane_declaration,
            "lane_reading": resolution.lane_reading,
            "local": resolution.local,
            "execution_fit": resolution.execution_fit.as_dict(),
            "launch": launch_kind,
            "sandbox": backend.get("sandbox"),
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
            "warnings": [
                *resolution.warnings,
                *budget_warnings,
                *disregarded_claims,
            ],
            "lineage": lineage,
            "unreconciled_override": waiver,
            "watch_override": watcher_waiver,
            "watch": {
                "arming_line": _watch_arming_line(project),
                "attach_line": _watch_attach_line(project, session=session),
                "watcher_live": False,
                "session": session,
                "session_attached": False,
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

        if launch_kind == "cli":
            try:
                plan = resolve_launch_executable(
                    _backends.launch_plan(
                        backend_name=backend_name,
                        backend=backend,
                        prompt=prompt,
                        worktree=worktree["path"],
                        manifest_path=node.manifest_path,
                        writable_directories=resolution.sandbox_write_roots or (),
                        final_message_path=str(final_path),
                        resume_session=reuse_session,
                        fence=FENCE_WORKERS,
                    ),
                    facts=dispatch_host,
                )
                # Read before the placement wraps the plan: the harness is
                # argv[0] here, and after the wrap argv[0] is the scheduler.
                harness_command = str(plan.argv[0]) if plan.argv else None
                record["session_harness"] = plan.dialect if reuse_session else None
                plan = apply_backend_placement(plan, backend, project)
            except (_backends.BackendError, flight.FlightConfigError, OSError) as exc:
                raise CrewError(format_refusal("D22", str(exc))) from exc
            placement = flight.placement_for(backend)
            job_id, job_id_status = placement_job_id(placement, run_id=run_id)
            record.update(
                {
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
            plan = None
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

        # Publish the pointer before probing the watcher. Otherwise a watcher
        # could drain an empty fleet between the probe and this write, leaving
        # a new run behind a payload that incorrectly said it was watched.
        _write_json(pointer_path(run_id), record)
        record["peer_channel"] = _wire_peer_channels(record, adjacent_peers)
        wired_peer_run_ids = list(record["peer_channel"]["peers"])
        record["watch"] = watch_state(project, session=session)
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
                            prompt_path=prompt_path,
                            log_path=log_path,
                            stderr_path=stderr_path,
                            facts=dispatch_host,
                            attempt=int(record["attempt"]),
                            attempt_kind=str(record["attempt_kind"]),
                            attempt_started_at=str(record["attempt_started_at"]),
                        ),
                    )
                    spawned_pid = _start_supervisor(spec_path, directory, run_id)
                else:
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
                    repo_root
                )
            spawned_start_time = _process_start_time(spawned_pid)
            record["pid"] = spawned_pid
            record["pid_start_time"] = spawned_start_time
            _write_json(pointer_path(run_id), record)
        else:
            # A delegated launch spawns no process, so there is no supervisor to
            # take the boundary baseline after dispatch's writes. Dispatch takes
            # it here instead, in the same last repository-facing step, so the
            # baseline still predates every write this run's worker will make.
            _write_boundary_tree_snapshot(directory, repo_root)
    except Exception:
        _unwire_peer_channels(run_id, wired_peer_run_ids)
        if spawned_pid is not None:
            try:
                _signal_process_group(spawned_pid, spawned_start_time)
            except (CrewError, OSError):
                pass
        # The pointer goes first. The worktree remover refuses a worktree that a
        # live pointer still claims, and until this run's own pointer is gone it
        # is that claim — so removing the worktree first raised, and the unlink
        # and the run-directory removal below never ran. A refusal reached after
        # the pointer write therefore left a live pointer behind, which a reader
        # takes for a run whose process died without a manifest. Unlinking first
        # clears the claim, and the run directory follows, so a refusal is
        # indistinguishable from a dispatch that never ran.
        pointer_path(run_id).unlink(missing_ok=True)
        shutil.rmtree(directory, ignore_errors=True)
        _remove_worktree(repo_root, worktree["path"])
        raise
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
        f"HOST — ALLOCATION: node {node}; job {job}; {tmp_clause}; do not use "
        "srun, sbatch or salloc because the worker already runs on the node; "
        f"logs a later reader needs go under {run_directory}."
    )


def launch_search_path(
    environment: Mapping[str, str] | None = None,
    *,
    facts: Any | None = None,
) -> str:
    """Return the effective worker PATH, adding scheduler shims on compute."""
    merged = {**os.environ, **(environment or {})}
    inherited = str(merged.get("PATH") or os.defpath)
    placement = _current_host_facts() if facts is None else facts
    if not placement.in_allocation:
        return inherited
    shim_directory = _worker_shim_directory()
    resolved_shim = os.path.realpath(shim_directory)
    inherited_entries = [
        entry
        for entry in inherited.split(os.pathsep)
        if entry and os.path.realpath(entry) != resolved_shim
    ]
    return os.pathsep.join([str(shim_directory), *inherited_entries])


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
    if placement.in_allocation:
        persisted["PATH"] = launch_search_path(environment, facts=placement)
    return persisted


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


def resolve_launch_executable(
    plan: _backends.LaunchPlan,
    *,
    environment: Mapping[str, str] | None = None,
    facts: Any | None = None,
) -> _backends.LaunchPlan:
    """Return the plan with argv[0] replaced by an absolute executable path.

    The launch inherits the PATH of whoever started it, so a watcher armed
    without the backend directory execs a bare name, dies at exec and leaves an
    empty stream that reads as a worker turn. Resolving at plan construction
    makes the launch either runnable or an explicit refusal, and the refusal
    names the binary and the PATH that was searched so the repair is a command
    rather than an investigation.

    ``environment`` is the overlay the launch will run with; absent, the launch
    own environment is used, which is what every construction site passes.
    """
    selected_environment = plan.environment if environment is None else environment
    searched = launch_search_path(selected_environment, facts=facts)
    binary = str(plan.argv[0]) if plan.argv else ""
    resolved = shutil.which(binary, path=searched) if binary else None
    if not resolved:
        raise LaunchResolutionError(
            f"backend command {binary!r} cannot be resolved on the PATH this "
            f"launch would search: {searched} — install it or add its directory "
            "to PATH, then retry; nothing has been launched"
        )
    # Absolute, not canonical: a launcher installed as ``bin/codex`` symlinked
    # to ``codex.js`` must still be exec'd under the name the launch was
    # configured with, because that name is how the command's dialect is
    # selected and how the run records what it ran.
    resolved = os.path.abspath(resolved)
    return dataclasses.replace(plan, argv=[resolved, *plan.argv[1:]])


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
    # The project's own reservation, never another's: a worker placed into an
    # allocation its project does not hold would run somewhere nobody sized for
    # it and be counted against a roster nobody armed on its behalf.
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
    with (
        open(prompt_path, "rb") as stdin,
        open(log_path, "wb") as stdout,
        open(stderr_path, "wb") as stderr,
    ):
        process = subprocess.Popen(
            plan.argv,
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
    with _LAUNCHED_WORKERS_LOCK:
        _LAUNCHED_WORKERS.add(process.pid)
        launched = _launched_worker_record(plan, log_path, stderr_path)
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
    parent = path.parent
    if not parent.is_dir():
        return False
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except OSError:
        return False
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
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


def _supervisor_spec(
    *,
    run_id: str,
    run_directory: Path,
    repo_root: Path,
    worktree: Path,
    plan: _backends.LaunchPlan,
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
        "worktree": str(worktree),
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
    """
    return [
        sys.executable,
        "-m",
        "reckon.crew.dispatch",
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


def _write_fleet_request(fifo: Path, line: bytes, deadline: float) -> None:
    """Write one request line to the batch step's FIFO within the deadline.

    The FIFO is opened non-blocking because opening a FIFO for writing blocks
    until a reader holds the other end, and a dispatch that hung there would
    hang for as long as the batch step was down rather than refusing at its
    bound. No reader within the deadline is a refusal, not a wait.
    """
    while True:
        try:
            descriptor = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno == errno.ENXIO and time.monotonic() < deadline:
                time.sleep(FLEET_REQUEST_POLL_SECONDS)
                continue
            raise CrewError(
                f"the fleet's request FIFO {fifo} could not be written: {exc}"
            ) from exc
        try:
            os.write(descriptor, line)
        except OSError as exc:
            raise CrewError(
                f"the fleet's request FIFO {fifo} refused the spawn request: {exc}"
            ) from exc
        finally:
            os.close(descriptor)
        return


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

    Off the fleet this forks the supervisor in a session of its own — what
    makes the pointer's pid a process group ``crew stop`` can signal, since the
    worker is spawned inside that group. On the fleet node the fork would be
    reaped with the session step that made it, so the same argv is handed to
    the allocation's batch step instead and the caller waits, bounded, for the
    pid the step acknowledges.
    """
    fleet = _read_fleet_record()
    if (
        _fleet_spawn_enabled()
        and fleet is not None
        and _runs_inside_fleet_allocation(fleet)
    ):
        runtime_dir = _fleet_runtime_dir(fleet)
        if runtime_dir is not None:
            return _spawn_through_fleet(
                runtime_dir,
                run_id,
                spec_path,
                run_directory / FLEET_SPAWN_ACK_NAME,
            )
    argv = _supervisor_argv(spec_path=spec_path)
    with open(run_directory / "supervisor.stderr.log", "ab") as errors:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            start_new_session=True,
        )
    return process.pid


def _worker_default_signals() -> None:
    """Give the worker the default signal dispositions the supervisor changed.

    The supervisor installs its own handlers for SIGTERM and SIGHUP, and the
    worker is spawned into the supervisor's signal environment. The worker must
    end on the group signal that stops it rather than carry a handler the
    supervisor needed for itself, so it starts with the defaults its launch
    would otherwise have had.
    """
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGHUP, signal.SIG_DFL)


def _supervisor_spawn_worker(spec: Mapping[str, Any]) -> int:
    """Spawn the worker inside the supervisor's own process group."""
    plan = spec["plan"]
    record = read_pointer(str(spec["run_id"]))
    environment = _worker_runtime_environment(
        plan.get("environment") or {},
        run_id=str(spec["run_id"]),
        manifest_path=str(record.get("manifest_path") or ""),
        attempt_started_at=_utc_now(),
        coordinator_session=str(record.get("session") or ""),
        claude_headers=str(plan.get("dialect") or "") == "claude",
    )
    with (
        open(str(spec["prompt_path"]), "rb") as stdin,
        open(str(spec["log_path"]), "wb") as stdout,
        open(str(spec["stderr_path"]), "wb") as stderr,
    ):
        process = subprocess.Popen(
            list(plan["argv"]),
            cwd=plan.get("cwd"),
            env=_worker_process_environment(
                environment,
                dialect=str(plan.get("dialect") or ""),
            ),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=False,
            # The worker must start with the default signal dispositions rather
            # than the supervisor's own handlers, so a stop aimed at the group
            # ends the worker. This runs in the supervisor's single-threaded
            # child as it starts.
            preexec_fn=_worker_default_signals,  # noqa: PLW1509
        )
    return process.pid


def _write_boundary_tree_snapshot(
    run_directory: Path, repo_root: Path
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
    """
    try:
        snapshot: dict[str, Any] = _repository_tree_snapshot(repo_root)
    except Exception as exc:  # noqa: BLE001 - a scan failure never kills a launch
        snapshot = {"available": False, "detail": f"{type(exc).__name__}: {exc}"}
    _supervisor_write(run_directory / TREE_SNAPSHOT_NAME, snapshot)
    return snapshot


def _supervisor_tree_snapshot(spec: Mapping[str, Any]) -> None:
    """Write the boundary snapshot, or its failure, into the run directory."""
    _write_boundary_tree_snapshot(
        Path(str(spec["run_directory"])), Path(str(spec["repo"]))
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
    try:
        attempt = int(spec.get("attempt") or 1)
    except (TypeError, ValueError):
        attempt = 1
    stop_requested = _record_stop_before_spawn()
    _supervisor_tree_snapshot(spec)
    launched_at = _utc_now()
    if stop_requested.is_set():
        # The stop arrived while the snapshot ran, before any worker existed,
        # so none is spawned and the launch is over before it began. The one
        # launch-failure record is still written, because the run directory
        # outlives the pointer a discard removes.
        _write_attempt_artifact(
            run_directory,
            EXIT_RECORD_NAME,
            _supervisor_exit_record(
                run_id=str(spec.get("run_id") or ""),
                attempt=attempt,
                worker_pid=None,
                launched_at=launched_at,
                status=None,
                run_directory=run_directory,
            )
            | {"detail": "crew stop arrived before the worker was spawned"},
            attempt=attempt,
        )
        return 0
    try:
        pid = _supervisor_spawn_worker(spec)
    except (OSError, ValueError, KeyError, CrewError) as exc:
        _write_attempt_artifact(
            run_directory,
            EXIT_RECORD_NAME,
            _supervisor_exit_record(
                run_id=str(spec.get("run_id") or ""),
                attempt=attempt,
                worker_pid=None,
                launched_at=launched_at,
                status=None,
                run_directory=run_directory,
            )
            | {"detail": f"worker did not spawn: {type(exc).__name__}: {exc}"},
            attempt=attempt,
        )
        return 0
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
    try:
        _, status = os.waitpid(pid, 0)
    except ChildProcessError:
        status = None
    except OSError:
        status = None
    _write_attempt_artifact(
        run_directory,
        EXIT_RECORD_NAME,
        _supervisor_exit_record(
            run_id=str(spec.get("run_id") or ""),
            attempt=attempt,
            worker_pid=pid,
            launched_at=launched_at,
            status=status,
            run_directory=run_directory,
        ),
        attempt=attempt,
    )
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
        stopped = record.get("phase") == "stopped"

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
            record["phase"] = "stopped" if stopped else data["phase"]
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
            ):
                # A dead process with no terminal event is a recoverable orphan,
                # not a finished run. An empty log counts because argument
                # failures can exit before the first event is written.
                record["phase"] = "orphaned"
                record["detail"] = (
                    "process exited without a terminal event in its log; "
                    f"check {record.get('stderr_path')}"
                )
        elif record.get("task") and record["manifest_present"] and not stopped:
            manifest_status = str(
                parse_manifest(manifest.read_text()).get("status") or ""
            ).strip()
            if manifest_status:
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
    return str(record.get("role") or "") == "review" or _record_node_id(
        record
    ).startswith(REVIEW_NODE_PREFIX)


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
    runs = list(records)
    candidates = []
    for record in runs:
        lineage = record.get("lineage")
        if isinstance(lineage, Mapping) and lineage.get("kind") == "shadow":
            continue
        if _task_identity(record, project, runs) != identity:
            continue
        if not str(record.get("session_id") or "").strip():
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
    rather than a bare absence.
    """
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
    session_id = str(prior.get("session_id") or "").strip()
    owner = str(
        prior.get("session_harness")
        or prior.get("dialect")
        or (prior.get("agent") or {}).get("dialect")
        or ""
    )
    disqualifier = (
        f"its session belongs to harness {owner or 'unknown'!r}, not {harness!r}"
        if harness and owner != harness
        else _session_too_large_to_continue(prior)
    )
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
    if record.get("launch") != "cli":
        return resolve_session(run_id, record=record)
    owner = str(record.get("session_harness") or "")
    boundary = record.get("lane_change") or {}
    changed_harness = boundary.get("session") == "fresh" and boundary.get(
        "from_harness"
    ) != boundary.get("to_harness")
    if not owner and not changed_harness:
        return resolve_session(run_id, record=record)
    backend = _backend_settings(record, config)
    harness = _backends.dialect_for(backend).name
    if (not changed_harness and (not owner or owner == harness)) or (
        record.get("session_id") and owner == harness
    ):
        return resolve_session(run_id, record=record)
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
            "session_id": record.get("session_id") or boundary.get("session_id"),
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
    backend.setdefault("sandbox", record.get("sandbox"))
    # The plan is built — and its executable resolved — before anything is
    # written, so an unresolvable backend refuses a resume exactly as it
    # refuses a dispatch: no pointer field, no advice file, no stream.
    attempt_started_at = _utc_now()
    plan = resolve_launch_executable(
        _backends.launch_plan(
            backend_name=str(record.get("backend") or ""),
            backend=backend,
            prompt=(
                _lane_prompt(record, advice, fresh_reason["reason"], continued=False)
                if fresh_reason
                else advice
            ),
            worktree=str(record.get("worktree") or "."),
            manifest_path=str(record.get("manifest_path") or ""),
            writable_directories=record.get("sandbox_write_roots") or (),
            resume_session=session_id or None,
            fence=FENCE_WORKERS,
        )
    )
    plan = _worker_runtime_plan(
        plan,
        run_id=run_id,
        manifest_path=str(record.get("manifest_path") or ""),
        attempt_started_at=attempt_started_at,
        coordinator_session=str(record.get("session") or ""),
    )

    def capture(current: dict[str, Any]) -> dict[str, Any]:
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
    worktree = Path(str(record.get("worktree") or ""))
    lines = [
        "INHERITED WORKTREE READING (measured fact)",
        f"Reading taken at: {taken_at}",
        f"Worktree: {worktree}",
    ]
    if not worktree.is_dir():
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
        base_result, base_error = _worktree_git_read(
            worktree,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{recorded_base}^{{commit}}",
        )
        if base_error or base_result is None:
            lines.append(
                "Head differs from recorded base: unknown; the recorded base could "
                f"not be resolved ({' '.join(str(base_error).splitlines())})."
            )
        else:
            differs = "yes" if head != base_result.stdout.strip() else "no"
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


def change_lane(
    run_id: str,
    backend_name: str,
    reason: str,
    *,
    config: Mapping[str, Any],
    advice: str = "",
    launch: bool = True,
    launcher=None,
) -> dict[str, Any]:
    """Relaunch one live run elsewhere without replacing its identity.

    A blocked resumption and a working-run redispatch deliberately meet here.
    The destination is fully resolved and budget-checked before the current
    process is stopped. The existing run id, node and worktree stay in place;
    only the execution attempt changes.
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
    )
    if not resolution.validation.ok:
        raise CrewError(
            f"run {run_id!r} cannot move to backend {destination!r} — "
            + "; ".join(
                f"{finding['property']}: {finding['detail']}"
                for finding in resolution.validation.findings
            )
        )
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
    target_plan: _backends.LaunchPlan | None = None
    if target_launch == "cli":
        target_plan = resolve_launch_executable(
            _backends.launch_plan(
                backend_name=resolution.backend,
                backend=backend,
                prompt=prompt,
                worktree=str(record.get("worktree") or "."),
                manifest_path=str(record.get("manifest_path") or ""),
                writable_directories=resolution.sandbox_write_roots or (),
                final_message_path=str(final_path),
                resume_session=session_id if continued else None,
                fence=FENCE_WORKERS,
            )
        )
        target_plan = _worker_runtime_plan(
            target_plan,
            run_id=run_id,
            manifest_path=str(record.get("manifest_path") or ""),
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
            "prompt_path": str(prompt_path),
            "worktree": str(record.get("worktree") or ""),
        }
    if not launch:
        return preview

    if (
        source_launch == "in-harness"
        and record.get("task")
        and str(record.get("phase") or "") not in _TERMINAL_RUN_PHASES
    ):
        raise CrewError(
            f"run {run_id!r} is attached to live harness task {record['task']!r}; "
            "cancel it in that harness before changing backend"
        )
    if source_launch == "cli" and record_process_alive(record, process_alive) is True:
        _signal_process_group(int(record["pid"]), record.get("pid_start_time"))

    directory.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
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
            _signal_process_group(int(pid), record.get("pid_start_time"))
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
                "session_resumed": bool(
                    record.get("session_resumed", record.get("session_id"))
                ),
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
