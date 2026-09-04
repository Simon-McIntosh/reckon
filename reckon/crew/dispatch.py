from __future__ import annotations

import argparse
import ast
import ctypes
import fcntl
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from reckon import _backends, _store, capability, ledger
from reckon.calibration import agent_configuration_key
from reckon.crew.node import (
    BudgetHold,
    CompetenceLimit,
    CrewError,
    DEFAULT_MEMBER_IDLE_WINDOW,
    MemberInFlight,
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
    parse_duration,
    validate_node,
)
from reckon.crew.prompts import compose_prompt
from reckon.crew.routing import (
    _agent_configuration,
    _budget_verdict,
    _competence_verdict,
    _create_worktree,
    _register_session_member,
    _fleet_script,
    _remove_worktree,
    _repository_tree_snapshot,
    _session_member_id,
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
    _utc_now,
    _watch_arming_line,
    _watch_attach_line,
    _write_json,
    list_live,
    new_run_id,
    pointer_path,
    process_alive,
    read_pointer,
    reports_dir,
    run_dir,
    project_watch_visibility,
    runs_dir,
    watch_state,
    watch_stream_path,
)


_INOTIFY_EVENTS = 0x00000100 | 0x00000008 | 0x00000080
# Process startup and registration may receive only one scheduler slice in six
# while two CPU-bound jobs share a loaded host. Keep every watcher condition
# wait on this one six-times-unloaded bound so a red test reports a producer
# defect rather than which process won the scheduler.
WATCHER_LOAD_BOUND_SECONDS = 30.0


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
        f"refusing to arm the watch producer for {project}: the resolved "
        f"configuration home {home} lies under the throwaway test directory "
        f"{root}, so a detached producer would outlive the run that armed it "
        f"and poll a deleted home. Set {WATCH_ARMING_ENV}=on for a caller "
        "that reaps the producer it starts, or waive the watch instead."
    )


def _watch_executable() -> str:
    """Resolve the console entry point beside the running interpreter first."""
    adjacent = Path(sys.executable).with_name("reckon")
    if adjacent.is_file():
        return str(adjacent)
    executable = shutil.which("reckon")
    if executable:
        return executable
    raise CrewError("cannot start the project watcher: reckon is not on PATH")


def _start_watch_producer(project: str) -> subprocess.Popen[bytes]:
    """Start a detached supervisor that remains the watcher's live parent."""
    _refuse_arming_under_a_throwaway_home(project)
    supervisor = (
        "import subprocess, sys; "
        "producer = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True); "
        "raise SystemExit(producer.wait())"
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            supervisor,
            _watch_executable(),
            "crew",
            "watch",
            "--project",
            project,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def _ensure_watch_producer(
    project: str, *, session: str | None = None
) -> dict[str, Any]:
    """Return the live producer, starting at most one across concurrent calls."""
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
        raise WatcherRequired(project, state)


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
    return _resolved_scope_entries(
        node.write_paths,
        base_repository=repository_identity(repo) or Path(repo).resolve(),
        repositories=repositories,
        project=project,
        repository_projects=repository_projects,
        preferred_projects=tuple(str(item) for item in write.get("projects") or ()),
    )


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
    conflicts: list[dict[str, Any]] = []
    for claim in claims:
        paths = [
            {"left_path": path, "right_path": claim.path}
            for repository, path, absolute, _declared, _derived_from in candidates
            if repository == claim.repository
            and _scopes_overlap(absolute.as_posix(), claim.absolute_path.as_posix())
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
    for _repository, candidate, absolute, _declared, _derived_from in candidates:
        for claim in claims:
            if _repository != claim.repository or not _scopes_overlap(
                absolute.as_posix(), claim.absolute_path.as_posix()
            ):
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


def _peer_command(argv: list[str] | None = None) -> int:
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
    local: bool = False
    warnings: list[str] = field(default_factory=list)
    competence: dict[str, Any] | None = None
    authority: dict[str, Any] | None = None
    live_conflicts: list[dict[str, Any]] | None = None
    sandbox_write_roots: tuple[Path, ...] | None = None

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
            "execution_fit": self.execution_fit.as_dict(),
            "launch": self.launch,
            "local": self.local,
            "node": self.node.as_dict(),
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
    delivery_roots = (runs_dir().resolve(), reports_dir().resolve())
    allowed_roots = (*repository_roots, *delivery_roots)
    for declared in node.write_paths:
        raw = Path(declared).expanduser()
        resolved = (raw if raw.is_absolute() else work_repo / raw).resolve()
        if any(resolved.is_relative_to(root) for root in allowed_roots):
            continue
        repositories = ", ".join(str(root) for root in repository_roots)
        raise CrewError(
            f"write path {declared!r} resolves outside the authorised work repository "
            f"{work_repo}, every other repository registered by the dispatch authority "
            f"({repositories}), and Reckon delivery directories {delivery_roots[0]} and "
            f"{delivery_roots[1]}; the repository containing this path is missing from "
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
) -> DispatchPlan:
    """Resolve routing and defaults for one node and judge it. No side effects.

    Mutates only the node it was handed, filling the defaults a dispatch would
    fill — the time budget from the resolved fence and the manifest path from
    the run directory — so the verdict is the one a real dispatch would reach.

    ``backend_override`` re-resolves the role's own overlay against an
    explicitly named backend instead of the one the role or default_backend
    would select — the re-resolution a budget hold's declared fallback needs,
    kept here rather than duplicated so it inherits every other check this
    function already makes (execution fit, sandbox reachability, write-path
    scope) for the substituted backend rather than the original.
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
    if backend_override:
        backend_name, backend = resolve_role_override(
            config, node.role, node.spec_level, backend_override
        )
    else:
        backend_name, backend = resolve_role(config, node.role, node.spec_level)
    launch_kind = backend.get("launch")
    if launch_kind not in ("cli", "in-harness"):
        raise CrewError(
            f"backend {backend_name!r} declares launch {launch_kind!r}; "
            "expected 'cli' or 'in-harness'"
        )
    default_budget = resolved_time_budget(config, backend)
    budget_ceiling = resolved_time_ceiling(config)
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
    resolution = DispatchPlan(
        run_id=resolved_run_id,
        backend=backend_name,
        launch=str(launch_kind),
        backend_settings=backend,
        node=node,
        budget_ceiling=budget_ceiling,
        validation=verdict,
        execution_fit=execution_fit,
        local=local,
        warnings=warnings,
        authority=resolved_authority,
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
            f"run {run_id!r} is not a committed ledger record in repository {repo_root}"
        )
    if len(matches) > 1:
        raise CrewError(
            f"run {run_id!r} appears in more than one project ledger in {repo_root}"
        )
    project, primary = matches[0]
    lineage = primary.get("lineage")
    if isinstance(lineage, Mapping) and lineage.get("kind") == "shadow":
        raise CrewError(
            f"run {run_id!r} is itself a shadow and cannot be a shadow parent"
        )
    agent = primary.get("agent")
    if not isinstance(agent, Mapping) or not agent:
        raise CrewError(
            f"committed run {run_id!r} has no recorded agent configuration; "
            "the shadow cannot inherit a configuration without guessing"
        )
    definition = primary.get("node_definition")
    if not isinstance(definition, Mapping):
        raise CrewError(
            f"committed run {run_id!r} has no stored node definition and cannot "
            "be shadowed without re-authoring its contract"
        )
    required = ("id", "goal", "plan", "done_when", "write_paths")
    missing = [name for name in required if not definition.get(name)]
    if missing:
        raise CrewError(
            f"committed run {run_id!r} has an incomplete stored node definition: "
            + ", ".join(missing)
        )
    base_sha = str(primary.get("base_sha") or "")
    if not base_sha:
        raise CrewError(f"committed run {run_id!r} records no base_sha")
    node = TaskNode(
        id=str(definition["id"]),
        goal=str(definition["goal"]),
        plan=str(definition["plan"]),
        section=str(definition.get("section") or ""),
        role=str(definition.get("role") or primary.get("role") or "implement"),
        spec_level=str(definition.get("spec_level") or primary.get("spec_level") or ""),
        done_when=str(definition["done_when"]),
        write_paths=[str(path) for path in definition.get("write_paths") or ()],
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
            f"candidate backend {candidate_backend!r} resolved to "
            f"{resolved_backend!r}; route the node explicitly to the candidate"
        )

    explicit = {str(config_key) for config_key in configuration_overrides}
    effective = dict(candidate)
    if "effort" in explicit:
        backend = config.get("backends", {}).get(candidate_backend, {})
        role = config.get("roles", {}).get(node.role, {})
        # The CLI accepts both candidate-backend and direct-role spellings. Its
        # routing overlays may otherwise hide either value before this point,
        # so recover the explicitly named setting from its owning layer.
        if "effort" in role:
            effective["effort"] = role["effort"]
        elif "effort" in backend:
            effective["effort"] = backend["effort"]
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

    shadow_config = dict(config)
    backends = dict(config.get("backends") or {})
    backends[candidate_backend] = effective
    shadow_config["backends"] = backends
    roles = dict(config.get("roles") or {})
    roles[node.role] = {"backend": candidate_backend}
    shadow_config["roles"] = roles
    return shadow_config, {"substituted": substituted, "inherited": inherited}


def shadow(
    run_id: str,
    *,
    candidate_backend: str,
    config: Mapping[str, Any],
    repo: str | Path,
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
    lineage = {
        "kind": "shadow",
        "primary_run_id": run_id,
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
        session=shadow_worktree_session(run_id, _backend_name),
        base=base_sha,
        locked_decisions=node.requires_decisions,
        peer_scopes={},
        member=member,
        launcher=launcher,
        lineage_override=lineage,
    )


def dispatch(
    *,
    node: TaskNode,
    project: str,
    repo: str | Path,
    config: Mapping[str, Any],
    session: str,
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
    local: bool = False,
) -> dict[str, Any]:
    """Validate, prepare and launch one node; return its run record.

    The single branch is on launch kind. A ``cli`` backend is spawned here and
    the caller yields on the returned run id. An ``in-harness`` backend cannot
    be spawned by reckon at all, so everything a worker needs is prepared and
    returned as a directive the calling harness dispatches itself, binding its
    task back with :func:`attach`.

    Naming a roster ``member`` routes the node into that member's long-lived
    session. Omitting it provisions a private member derived from the dispatching
    session, so independent coordinators never shop from a shared free list. A
    member whose worker session is still null gets one captured on its first run.

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
    """
    repo_root = Path(repo).resolve()
    shadow_lineage = (
        dict(lineage_override)
        if isinstance(lineage_override, Mapping)
        and lineage_override.get("kind") == "shadow"
        else None
    )
    if lineage_override is not None and shadow_lineage is None:
        raise CrewError("only shadow lineage may be supplied explicitly at dispatch")
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
    from reckon.crew.recovery import overdue_unreconciled_runs

    unreconciled = overdue_unreconciled_runs(
        project=project,
        grace=unreconciled_grace,
    )
    if unreconciled and not unreconciled_override:
        raise UnreconciledRuns(unreconciled, unreconciled_grace)
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
                raise BudgetHold(verdict)
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
            )
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
                raise BudgetHold(fallback_verdict)
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
    explicitly_named_peers = set() if shadow_lineage else set(node.peer_scopes)
    peers = {} if shadow_lineage else _merge_peer_scopes(peer_claims, node.peer_scopes)
    node.peer_scopes = peers

    reap_idle_session_members(
        project,
        root=ledger_root,
        idle_window=str(fences.get("member_idle_window") or DEFAULT_MEMBER_IDLE_WINDOW),
    )
    named_member = bool(member)
    effective_member = member or _session_member_id(session)
    roster_member = ledger.member(project, effective_member, root=ledger_root)
    if named_member:
        if roster_member is None:
            raise CrewError(
                f"project {project!r} has no crew member {member!r}; register it "
                "with `reckon crew member add` before dispatching to it"
            )
    if roster_member is not None:
        for pointer in list_live(project=project):
            if (
                pointer.get("member") == effective_member
                and pointer.get("phase") not in _TERMINAL_RUN_PHASES
            ):
                raise MemberInFlight(
                    effective_member, str(pointer.get("run_id") or "unknown")
                )
    disregarded_claims: list[str] = []
    if shadow_lineage:
        adjacent_peers = []
    else:
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
    if local:
        agent["local"] = True
    committed_runs = ledger.runs(project, root=ledger_root)
    reuse_session = (
        _session_for_configuration(
            roster_member,
            agent,
            committed_runs,
            harness_default_model=_harness_default_model(
                config, str(roster_member.get("harness") or "")
            ),
        )
        if roster_member and backend.get("session_reuse")
        else None
    )
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
            raise CrewError("shadow lineage names no committed primary run")
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

    worktree = _create_worktree(repo_root, session, node.id, base)
    spawned_pid: int | None = None
    spawned_start_time: str | None = None
    wired_peer_run_ids: list[str] = []
    try:
        directory.mkdir(parents=True, exist_ok=True)
        working_directory = worktree["path"]
        if launch_kind == "cli":
            working_directory = _backends.launch_working_directory(
                backend=backend,
                worktree=worktree["path"],
                manifest_path=node.manifest_path,
            )
        prompt = compose_prompt(
            node=node,
            project=project,
            worktree=worktree["path"],
            working_directory=working_directory,
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

        record: dict[str, Any] = {
            "run_id": run_id,
            "project": project,
            "repo": str(repo_root),
            "authority": resolution.authority,
            "session": session,
            "node": node.as_dict(),
            "role": node.role,
            "backend": backend_name,
            "local": local,
            "execution_fit": resolution.execution_fit.as_dict(),
            "launch": launch_kind,
            "sandbox": backend.get("sandbox"),
            "sandbox_write_roots": (
                None
                if resolution.sandbox_write_roots is None
                else [str(path) for path in resolution.sandbox_write_roots]
            ),
            "session_reuse": bool(backend.get("session_reuse")),
            "member": effective_member,
            # The configuration that actually ran the node, recorded now because
            # a later config layer change makes it unreconstructable — and
            # without it a measured duration cannot be attributed to anything.
            "agent": agent,
            "competence": competence,
            "worktree": worktree["path"],
            "base": worktree["base"],
            "base_sha": worktree["base_sha"],
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
            "attempt_started_at": _utc_now(),
            "phase": "starting",
            "session_id": reuse_session,
            "task": None,
            "pid": None,
            "argv": None,
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

        if launch_kind == "cli":
            plan = _backends.launch_plan(
                backend_name=backend_name,
                backend=backend,
                prompt=prompt,
                worktree=worktree["path"],
                manifest_path=node.manifest_path,
                writable_directories=resolution.sandbox_write_roots or (),
                final_message_path=str(final_path),
                resume_session=reuse_session,
            )
            spawn = launcher or _spawn
            spawned_pid = spawn(
                plan,
                log_path=log_path,
                stderr_path=stderr_path,
                prompt_path=prompt_path,
            )
            spawned_start_time = _process_start_time(spawned_pid)
            record.update(
                {
                    "pid": spawned_pid,
                    "pid_start_time": spawned_start_time,
                    "argv": list(plan.argv),
                    "dialect": plan.dialect,
                }
            )
        else:
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

        # Publish the pointer before probing the watcher. Otherwise a watcher
        # could drain an empty fleet between the probe and this write, leaving
        # a new run behind a payload that incorrectly said it was watched.
        _write_json(pointer_path(run_id), record)
        record["peer_channel"] = _wire_peer_channels(record, adjacent_peers)
        wired_peer_run_ids = list(record["peer_channel"]["peers"])
        record["watch"] = watch_state(project, session=session)
        _write_json(pointer_path(run_id), record)
        if roster_member is None:
            _register_session_member(
                project,
                effective_member,
                backend=backend_name,
                role=node.role,
                root=ledger_root,
            )
        # Registration can update the repository's committed crew ledger. Take
        # the boundary baseline only after dispatch's own writes are complete.
        record["repository_tree_snapshot"] = _repository_tree_snapshot(repo_root)
        _write_json(pointer_path(run_id), record)
    except Exception:
        _unwire_peer_channels(run_id, wired_peer_run_ids)
        if spawned_pid is not None:
            try:
                _signal_process_group(spawned_pid, spawned_start_time)
            except (CrewError, OSError):
                pass
        _remove_worktree(repo_root, worktree["path"])
        shutil.rmtree(directory, ignore_errors=True)
        pointer_path(run_id).unlink(missing_ok=True)
        raise
    return record


if __name__ == "__main__":
    raise SystemExit(_peer_command())


def _spawn(
    plan: _backends.LaunchPlan,
    *,
    log_path: Path,
    stderr_path: Path,
    prompt_path: Path,
) -> int:
    """Start the backend detached, with its event stream landing on disk.

    The prompt is fed from a file rather than a pipe so the caller never blocks
    on a full pipe buffer, and so the exact prompt stays recoverable beside the
    stream it produced. ``start_new_session`` detaches the worker from the
    caller's process group: a dispatching agent that ends its turn must not take
    its workers down with it.
    """
    with (
        open(prompt_path, "rb") as stdin,
        open(log_path, "wb") as stdout,
        open(stderr_path, "wb") as stderr,
    ):
        process = subprocess.Popen(
            plan.argv,
            cwd=plan.cwd,
            env={**os.environ, **plan.environment},
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
    return process.pid


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
    from reckon.crew.recovery import _apply_budget_watchdog
    from reckon.crew.reports import parse_manifest

    """Fold a run's on-disk evidence back into its pointer and return it.

    Reads the event stream, the manifest path and process liveness, then writes
    the derived phase, session id and budget block into the record. Everything
    it reports is recoverable from disk, so a fresh session can observe a run it
    did not dispatch.
    """

    def fold(record: dict[str, Any]) -> dict[str, Any]:
        backend_name = str(record.get("backend") or "")
        manifest = Path(record.get("manifest_path") or "")
        manifest_file_present, manifest_fresh = _manifest_freshness(record)
        record["manifest_file_present"] = manifest_file_present
        record["manifest_fresh"] = manifest_fresh
        record["manifest_present"] = manifest_fresh
        record["process_alive"] = process_alive(record.get("pid"))
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
            record["phase"] = "stopped" if stopped else data["phase"]
            if (
                not stopped
                and record.get("attempt_kind") == "resume"
                and record["process_alive"] is True
                and record["phase"] in _TERMINAL_RUN_PHASES
            ):
                record["phase"] = "working"
            record["session_id"] = data["session_id"] or record.get("session_id")
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


def _harness_default_model(config: Mapping[str, Any], harness: str) -> str:
    """Return the model a member's declared harness resolves to by default.

    `member add` has no `--model` flag, so a session it records carries no
    configuration of its own — this is the value it would have recorded had
    it accepted one, read from the same backend declaration `resolve_role`
    would use absent any role overlay.
    """
    if not harness:
        return ""
    backends = config.get("backends")
    backend = backends.get(harness) if isinstance(backends, Mapping) else None
    if not isinstance(backend, Mapping):
        return ""
    return str(backend.get("model") or "").strip()


def _session_for_configuration(
    member: Mapping[str, Any],
    agent: Mapping[str, Any],
    runs: Iterable[Mapping[str, Any]] = (),
    *,
    harness_default_model: str = "",
) -> str | None:
    """Return the member session proved to belong to this configuration.

    Configuration-keyed roster entries are authoritative. Model-keyed entries
    predate that representation, so they are eligible only when the committed
    run that captured the session identifies one unambiguous matching agent
    configuration.

    A session set bare, through `member add --session` with no prior capture
    at all, carries neither a `session_model` nor a `sessions` map — there is
    no run history to disambiguate it against, because it was never dispatched
    through here before. It is resolved once against the harness's own
    configured default model instead, and only when that default matches the
    model this dispatch actually resolved to; a role overlay that moves the
    resolved model away from the harness default is exactly the mismatch that
    must start a fresh session rather than risk resuming the wrong one. Once
    resumed, the ordinary capture path records both the configuration key and
    the model onto the roster, the same way it does for any other session.
    """
    configuration_key = agent_configuration_key({"agent": agent})
    sessions = member.get("sessions")
    if isinstance(sessions, Mapping) and sessions.get(configuration_key):
        return str(sessions[configuration_key])

    model = str(agent.get("model") or "").strip()
    if not model:
        return None
    session_id = member.get("session_id")
    if not session_id:
        return None
    recorded_model = str(member.get("session_model") or "").strip()
    has_capture_history = isinstance(sessions, Mapping) and bool(sessions)

    if not recorded_model and not has_capture_history:
        if harness_default_model and harness_default_model == model:
            return str(session_id)
        return None

    legacy_session = None
    if isinstance(sessions, Mapping) and sessions.get(model):
        legacy_session = str(sessions[model])
    elif recorded_model == model:
        legacy_session = str(session_id)
    if not legacy_session:
        return None

    member_id = str(member.get("id") or "")
    captured_configurations = {
        agent_configuration_key(run)
        for run in runs
        if str(run.get("member") or "") == member_id
        and str(run.get("session_id") or "") == legacy_session
        and isinstance(run.get("agent"), Mapping)
        and run.get("agent")
    }
    return legacy_session if captured_configurations == {configuration_key} else None


def _capture_member_session(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Persist a run's session id under its exact agent configuration.

    Observation is where a backend's session id first becomes knowable, so it is
    also where the roster learns it — waiting for completion would leave a second
    node dispatched in the meantime unable to reach the same session. Keying by
    the full resolved configuration (rather than the model alone) keeps an
    effort or model change from inheriting incompatible session context while
    letting every distinct configuration reuse its own history independently.
    """
    member = record.get("member")
    session_id = record.get("session_id")
    agent = record.get("agent")
    if not member or not session_id or not isinstance(agent, Mapping) or not agent:
        return None
    configuration_key = agent_configuration_key(record)
    try:
        data, version = ledger.load(
            str(record.get("project") or ""), root=record.get("repo")
        )
        for entry in data["members"]:
            if str(entry.get("id")) != str(member):
                continue
            sessions = dict(entry.get("sessions") or {})
            current = sessions.get(configuration_key)
            if current:
                return {
                    "captured": False,
                    "member": dict(entry),
                    "detail": (
                        "unchanged"
                        if str(current) == str(session_id)
                        else (
                            f"member {member!r} already reuses session {current!r} "
                            "for this agent configuration; run reported "
                            f"{session_id!r} and it was not written over the top"
                        )
                    ),
                }
            sessions[configuration_key] = str(session_id)
            entry["sessions"] = sessions
            if not entry.get("session_id"):
                entry["session_id"] = str(session_id)
                entry["session_model"] = str(agent.get("model") or "") or None
            elif str(entry.get("session_id")) == str(session_id) and not entry.get(
                "session_model"
            ):
                entry["session_model"] = str(agent.get("model") or "") or None
            ledger.write(
                str(record.get("project") or ""),
                data,
                version,
                root=record.get("repo"),
            )
            return {
                "captured": True,
                "member": dict(entry),
                "detail": "first run for agent configuration",
            }
        return {
            "captured": False,
            "member": None,
            "detail": f"project {record.get('project')!r} has no member {member!r}",
        }
    except (ledger.LedgerError, OSError) as exc:
        # The run record retains the session id even when the roster write is
        # unavailable, so observation and promotion remain recoverable.
        return {"captured": False, "member": None, "detail": str(exc)}


def _backend_settings(
    record: Mapping[str, Any], config: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Recover the backend settings needed to read a run's stream.

    Only the command matters for reading, and the recorded argv already holds
    it, so a run stays observable after its config layer changes — which is the
    difference between a durable record and one that decays.
    """
    argv = record.get("argv")
    if isinstance(argv, list) and argv:
        return {"launch": "cli", "command": argv[0]}
    backends = (config or {}).get("backends") or {}
    backend = backends.get(record.get("backend"))
    if isinstance(backend, Mapping):
        return dict(backend)
    raise CrewError(
        f"run {record.get('run_id')!r} records no argv and its backend is not "
        "in the supplied config, so its stream cannot be read"
    )


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
    if process_alive(record.get("pid")) is True:
        raise CrewError(
            f"run {run_id!r} still has a live process; observe or stop it before resuming"
        )
    session_id = record.get("session_id")
    if not session_id:
        record = observe(run_id, config=config)
        session_id = record.get("session_id")
    if not session_id:
        raise CrewError(f"run {run_id!r} has no session id in its current stream")
    backend = _backend_settings(record, config)
    verdict = _budget_verdict(
        project=str(record.get("project") or ""),
        root=record.get("repo"),
        config=config,
        backend_name=str(record.get("backend") or ""),
        backend=backend,
        purpose="resume",
    )
    if verdict["held"]:
        raise BudgetHold(verdict)
    backend.setdefault("sandbox", record.get("sandbox"))
    return _backends.launch_plan(
        backend_name=str(record.get("backend") or ""),
        backend=backend,
        prompt=advice,
        worktree=str(record.get("worktree") or "."),
        manifest_path=str(record.get("manifest_path") or ""),
        writable_directories=record.get("sandbox_write_roots") or (),
        resume_session=str(session_id),
    )


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
        estimated_hours=data.get("estimated_hours"),
        requires_decisions=[str(key) for key in data.get("requires_decisions") or ()],
    )


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
    return (
        f"{original.rstrip()}\n\n"
        "EXECUTION BACKEND CHANGED\n"
        f"Reason: {reason}\n{continuation}\n"
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
        raise BudgetHold(verdict)

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
    session_id = str(record.get("session_id") or "")
    continued = bool(
        session_id
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
        target_plan = _backends.launch_plan(
            backend_name=resolution.backend,
            backend=backend,
            prompt=prompt,
            worktree=str(record.get("worktree") or "."),
            manifest_path=str(record.get("manifest_path") or ""),
            writable_directories=resolution.sandbox_write_roots or (),
            final_message_path=str(final_path),
            resume_session=session_id if continued else None,
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
    if source_launch == "cli" and process_alive(record.get("pid")) is True:
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
                "session_reuse": bool(backend.get("session_reuse")),
                "agent": _stamp_agent_display(
                    _agent_configuration(resolution.backend, target_launch, backend),
                    backend,
                ),
                "attempt": attempt,
                "attempt_kind": "lane-change",
                "attempt_started_at": lane_change["changed_at"],
                "phase": "working" if target_plan is not None else "starting",
                "session_id": session_id if continued else None,
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
                    "dialect": target_plan.dialect,
                }
            )
            current.pop("directive", None)
        else:
            current.update(
                {
                    "argv": None,
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
            }
        )
        return record

    return _mutate_pointer(run_id, resume)
