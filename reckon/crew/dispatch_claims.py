# ruff: noqa: I001, UP035
from __future__ import annotations
import contextlib
import hashlib
import heapq
import json
import os
import re
import shutil
import sqlite3
import time
from dataclasses import (
    dataclass,
)
from datetime import (
    timedelta,
)
from pathlib import (
    Path,
)
from typing import (
    Any,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
)
from reckon import (
    _backends,
)
from reckon._timestamps import (
    parse_utc,
)
from reckon.crew.node import (
    CrewError,
    ScopeConflict,
    TaskNode,
    WAITING_REQUEST_PHASES,
    claim_disposition,
    claim_repository,
    repository_identity,
)
from reckon.crew.prompts import (
    compose_prompt,
)
from reckon.crew.refusals import (
    format_refusal,
)
from reckon.crew.review import (
    review_store_root,
)
from reckon.crew.routing import (
    mounted_repository_projects,
    resolve_scope_repository,
)
from reckon.crew.runs import (
    _expanded_scope_paths,
    _pointer_lock,
    _process_start_time,
    _project_derivations,
    _repository_relative_scope,
    _scopes_overlap,
    _shared_write_paths,
    _utc_now,
    _write_json,
    crew_home,
    list_live,
    pointer_path,
    reports_dir,
    run_dir,
    runs_dir,
)


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


# These phases mark a launch claim still being composed. An established claim
# is never outranked by a later registration. Waiting requests are judged
# nonbinding by claim_disposition before this racing order is considered.
_UNLAUNCHED_CLAIM_PHASES = frozenset(
    {"starting", "launching", "launcher", "dispatching"}
)


@dataclass(frozen=True)
class _RepositoryScopeClaim:
    """One live claim resolved to the repository that contains its path.

    ``binding`` answers whether the claim still fences its paths. It is judged
    once per pointer rather than once per path, because liveness and
    unintegrated work are properties of the run, not of the file.

    ``unread`` marks a claim whose disposition was never judged: the read named
    candidate paths and none of this pointer's entries could reach them, so no
    collision between the two is possible through a path it does not declare.
    The arbitration passes such a claim over, and never reads the ``binding`` an
    unread claim carries, which is the default rather than a verdict.
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
    unread: bool = False
    disposition_reason: str = ""
    # When the claim was published, so two dispatches racing for the same paths
    # can be ordered; and whether the run has passed its own admission and
    # written the record its worker launches from. A claim still being composed
    # carries neither, and is what the ordering rule below arbitrates.
    registered_at: str = ""
    launched: bool = False
    # What the holder's claim reads for the stacked-dispatch exception, taken
    # from the same per-pointer disposition the claim's binding comes from: the
    # work it still holds, the worktree whose head is read for the chain tip,
    # and the base its own dispatch was cut from. A claim whose holder holds no
    # unintegrated work is never a stacking candidate, so these are carried
    # together rather than derived again at the check.
    unintegrated: tuple[str, ...] = ()
    worktree: str = ""
    base_sha: str = ""


def _scope_derivation_project(
    project: str,
    repository: Path,
    preferred_projects: Iterable[str] = (),
    *,
    mounted_projects: Mapping[Path, tuple[str, ...]] | None = None,
    identity_cache: MutableMapping[str, Path | None] | None = None,
) -> str:
    """Choose the project resource that owns derivations for a repository.

    ``mounted_projects`` and ``identity_cache`` let a read that resolves many
    pointers share both the mount table and the ``git rev-parse`` behind each
    lookup, so choosing a derivation project costs no subprocess beyond the one
    each distinct repository already paid.
    """
    if mounted_projects is None:
        from reckon.crew.routing import repository_projects

        mounted = repository_projects(repository)
    else:
        identity = repository_identity(repository, cache=identity_cache)
        mounted = () if identity is None else mounted_projects.get(identity, ())
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
    preferred_projects: Iterable[str] = (),
    mounted_projects: Mapping[Path, tuple[str, ...]] | None = None,
    identity_cache: MutableMapping[str, Path | None] | None = None,
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
            preferred_projects,
            mounted_projects=mounted_projects,
            identity_cache=identity_cache,
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


def _pointer_declared_paths(pointer: Mapping[str, Any]) -> tuple[str, ...]:
    """The write paths one pointer's node declares, as recorded."""
    node = pointer.get("node")
    if not isinstance(node, Mapping):
        return ()
    return tuple(str(path) for path in node.get("write_paths") or ())


def _pointer_preferred_projects(pointer: Mapping[str, Any]) -> tuple[str, ...]:
    """The projects a pointer's grant prefers when a repository hosts several."""
    authority = pointer.get("authority")
    authority = authority if isinstance(authority, Mapping) else {}
    write = authority.get("write")
    write = write if isinstance(write, Mapping) else {}
    return tuple(str(item) for item in write.get("projects") or ())


def _pointer_can_meet(
    pointer: Mapping[str, Any],
    *,
    candidate_paths: tuple[str, ...],
    candidate_repositories: Iterable[Path],
    repository_projects: Mapping[Path, tuple[str, ...]],
    derivation_cache: MutableMapping[tuple[str, str], Any],
) -> bool:
    """Whether any path a live pointer declares could meet a candidate path.

    The question is answered from strings and from the projects' recorded
    derivations, so a pointer that answers no costs no ``git rev-parse`` in its
    checkout. It is an over-approximation of the test the arbitration makes, and
    a safe one: a claim refuses a dispatch only through a path both declare, and
    the two are in one repository, so resolving the pointer's declared paths
    against each candidate repository root reaches every base its own repository
    could turn out to be — an authority root and a mounted root the read already
    holds alike. The derivation project that owns a candidate root is chosen the
    way :func:`_resolved_scope_entries` chooses it, without the identity probe
    that lookup would otherwise spend, and the expansion it produces is cached
    for the one read.
    """
    declared = _pointer_declared_paths(pointer)
    if not declared:
        return False
    project = str(pointer.get("project") or "")
    preferred = _pointer_preferred_projects(pointer)
    for repository in candidate_repositories:
        mounted = tuple(repository_projects.get(repository, ()))
        if project in mounted:
            derivation_project = project
        else:
            derivation_project = next(
                (name for name in preferred if name in mounted),
                mounted[0] if mounted else project,
            )
        key = (derivation_project, str(repository))
        if key not in derivation_cache:
            derivation_cache[key] = _project_derivations(derivation_project, repository)
        for path, _normalized, _derived_from in _expanded_scope_paths(
            declared, repository, derivation_cache[key]
        ):
            absolute = (repository / path).resolve().as_posix()
            if any(
                _scopes_overlap(absolute, candidate) for candidate in candidate_paths
            ):
                return True
    return False


def _pointer_scope_entries(
    pointer: Mapping[str, Any],
    *,
    repository_projects: Mapping[Path, tuple[str, ...]] | None = None,
    identity_cache: MutableMapping[str, Path | None] | None = None,
    resolve_checkout: bool = True,
) -> list[tuple[Path | None, str, Path, str, str | None]]:
    """The write paths one live pointer declares, resolved to their owning scope.

    Every reader of a pointer's claims resolves them here, so the claim registry
    and the admission pre-check agree on what an overlap is. The base repository
    comes from the checkout the worker writes in: ``repo`` is resolved from the
    run's project mount, so a run carrying one project's plan into another
    project's checkout records a ``repo`` holding none of its declared paths —
    and the paths then resolve into a repository that no other claim on the same
    file can intersect.

    ``identity_cache`` is shared across the pointers of one read, so the
    ``git rev-parse`` behind :func:`claim_repository` is paid once per distinct
    checkout rather than once per pointer.

    ``resolve_checkout`` is turned off for a pointer :func:`_pointer_can_meet`
    has already excluded. Its entries are still returned — the registry and the
    scopes view report every claim's paths — but no consumer reads a verdict or
    an arbitration match from an unread claim, so the probe that would place its
    base exactly is spent on a row nobody matches against. Its base is then the
    recorded ``repo``, which is the checkout its declared paths resolve in
    unless its worktree belongs to a different repository than the one it
    records.
    """
    node = pointer.get("node")
    if not isinstance(node, Mapping):
        return []
    pointer_repo_value = str(pointer.get("repo") or "")
    if not pointer_repo_value:
        return []
    if repository_projects is None:
        repository_projects = mounted_repository_projects()
    pointer_repo = (
        claim_repository(pointer, cache=identity_cache) if resolve_checkout else None
    ) or Path(pointer_repo_value).expanduser().resolve()
    authority = pointer.get("authority")
    authority = authority if isinstance(authority, Mapping) else {}
    authority_roots = {
        Path(str(root)).expanduser().resolve()
        for root in authority.get("repositories") or ()
    }
    roots = {*repository_projects, *authority_roots, pointer_repo}
    return _resolved_scope_entries(
        _pointer_declared_paths(pointer),
        base_repository=pointer_repo,
        repositories=roots,
        project=str(pointer.get("project") or ""),
        preferred_projects=_pointer_preferred_projects(pointer),
        mounted_projects=repository_projects,
        identity_cache=identity_cache,
    )


def _repository_scope_claims(
    *,
    exclude_run_ids: Iterable[str] = (),
    candidate_paths: Iterable[str | Path] | None = None,
    candidate_repositories: Iterable[str | Path] | None = None,
) -> list[_RepositoryScopeClaim]:
    """Read live claims globally and group their paths by repository root.

    ``exclude_run_ids`` drops runs by identity. A dispatch that has already
    published a claim for the run it created reads every other live claim, never
    its own: an arbitration run against a run's own claim would refuse the
    dispatch that had just made it.

    ``candidate_paths`` names the absolute paths the caller could write, and
    ``candidate_repositories`` the roots a live claim's repository would have to
    be to collide with any of them. A pointer whose declared paths reach none of
    those roots is returned with ``unread`` set and no disposition, and costs no
    git at all: neither the status and reachability query behind a disposition,
    nor the ``git rev-parse`` that places its checkout. A caller that names no
    roots offers every mounted repository as one, and a read with no root to
    test against reads every claim rather than passing one over. A caller that
    names no candidate paths — a re-read of one run's claim, and the scopes view
    that reports every claim — reads every disposition exactly as before.
    """
    excluded = set(exclude_run_ids)
    repository_projects = mounted_repository_projects()
    # One identity cache for the whole read, so the ``git rev-parse`` behind
    # each pointer's repository costs one subprocess per distinct checkout
    # rather than one per pointer.
    identity_cache: dict[str, Path | None] = {}
    candidate_roots = (
        ()
        if candidate_paths is None
        else tuple(
            sorted(
                {
                    Path(str(value)).expanduser().resolve()
                    for value in candidate_repositories or repository_projects
                },
                key=str,
            )
        )
    )
    candidates = (
        None
        if candidate_paths is None
        else tuple(
            Path(str(path)).expanduser().resolve().as_posix()
            for path in candidate_paths
        )
    )
    derivation_cache: dict[tuple[str, str], Any] = {}
    claims: list[_RepositoryScopeClaim] = []
    for pointer in list_live():
        if str(pointer.get("run_id") or "") in excluded:
            continue
        pointer_repo_value = str(pointer.get("repo") or "")
        if not pointer_repo_value:
            continue
        node = pointer.get("node")
        if not isinstance(node, Mapping):
            continue
        relevant = (
            candidates is None
            or not candidate_roots
            or _pointer_can_meet(
                pointer,
                candidate_paths=candidates,
                candidate_repositories=candidate_roots,
                repository_projects=repository_projects,
                derivation_cache=derivation_cache,
            )
        )
        entries = _pointer_scope_entries(
            pointer,
            repository_projects=repository_projects,
            identity_cache=identity_cache,
            resolve_checkout=relevant,
        )
        phase = str(pointer.get("phase") or "")
        disposition = claim_disposition(pointer) if relevant else None
        project = str(pointer.get("project") or "")
        run_id = str(pointer.get("run_id") or "unknown")
        node_id = str(node.get("id") or "unknown")
        for (
            repository,
            path,
            absolute,
            declared,
            derived_from,
        ) in entries:
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
                    binding=True if disposition is None else disposition.binding,
                    unread=disposition is None,
                    disposition_reason=(
                        "" if disposition is None else disposition.reason
                    ),
                    registered_at=str(pointer.get("created_at") or ""),
                    unintegrated=()
                    if disposition is None
                    else disposition.unintegrated,
                    worktree=str(pointer.get("worktree") or ""),
                    base_sha=str(pointer.get("base_sha") or ""),
                    # A run that has written the record it launches its worker
                    # from, whose process has started, or whose phase has moved
                    # past the pre-spawn set has already passed its own
                    # admission: the claim is no longer forming, so it refuses
                    # newcomers as it always has. The launch claim published
                    # before the checks is the only shape that is still
                    # composing, and the phase is what names it.
                    launched=phase not in WAITING_REQUEST_PHASES
                    and (
                        bool(pointer.get("worktree") or pointer.get("pid"))
                        or phase not in _UNLAUNCHED_CLAIM_PHASES
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
    _write_json(
        run_dir(run_id) / "claim.json",
        {"run_id": run_id, "claim_registered_at": record["created_at"]},
    )
    _write_json(pointer_path(run_id), record)


def _release_launch_claim(run_id: str) -> None:
    """Give up a claim this dispatch published and is not going to use.

    The pointer goes first and the run directory follows: a dispatch refusal is
    made to leave nothing of its run behind, and a reader that found the run
    directory without the pointer would take a claim that no longer exists.
    Neither removal is an argument about whose claim it is — the path is this
    run's own — so a call for a run that published nothing removes nothing.
    """
    # A discard issued while this launch was composing recorded its departure
    # in the run directory, which the removal below takes with it. Copied to
    # the shared spool first, that departure outlives the refusal that gives
    # the claim back, so a reader after the rollback still finds it instead of
    # the same absence a vanished pointer produces.
    from reckon.crew.promotion import preserve_discard_record

    preserve_discard_record(run_id)
    # Publish and release use the same lock, so a publish already in progress
    # finishes before the removal and cannot write the pointer back afterward.
    with _pointer_lock(run_id):
        pointer_path(run_id).unlink(missing_ok=True)
    shutil.rmtree(run_dir(run_id), ignore_errors=True)


def _refuse_a_discarded_launch(run_id: str, *, started: bool = False) -> None:
    """Refuse a launch whose request was discarded before its worker started.

    A discard removes the run's pointer and leaves its own record behind, and
    the admission path hands an already-selected request to dispatch, which
    publishes the claim and composes the launch over several seconds. An
    operator's discard can land inside that span, after the pass selected the
    request and before this dispatch starts the worker, and nothing else on the
    path reads that record: the launch would write the pointer back and spawn a
    worker beside the record of its own discard, so every reader that takes a
    recorded discard for an abandoned run reads the new worker as one.

    Called at the last step before the spawn, so the window left open to a
    discard is only the fork itself.
    """
    from reckon.crew.promotion import discard_recorded, preserve_discard_record

    if discard_recorded(run_id):
        # The rollback that follows this refusal removes the run directory,
        # which is where a discard issued while this launch was composing
        # recorded its departure. Copied to the shared spool first, the record
        # outlives the directory exactly as it does for a discard the admission
        # pass preserved before handing the request over.
        preserve_discard_record(run_id)
        when = (
            "while its worker was being started"
            if started
            else "before its worker was started"
        )
        raise CrewError(f"run {run_id!r} was discarded {when}; the launch is refused")


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
    suite_command: str = "",
    suite_gate: str = "",
    launch_instant: str = "",
    needs_help_after_failures: int,
    grace_multiple: float | None = None,
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
        suite_command=suite_command,
        suite_gate=suite_gate,
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
        grace_multiple=grace_multiple,
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
        if claim.unread:
            # Its disposition was never judged because no path it declares can
            # meet this node's, so there is no collision to report and no
            # verdict to read.
            continue
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
# to launch or withdraw before refusing. The wait is not fixed: the winner
# published its claim and now composes and spawns a worker, and how long that
# takes varies with the fleet, so the bound is derived from the observed
# launch-to-claim time rather than a magic number. A wait holds the losing
# dispatch's whole turn, so the bound still has to be short.
#
# The shortest grace, so a fleet with no launch to learn from still gives a
# winner a moment rather than refusing on sight.
CLAIM_GRACE_FLOOR_SECONDS = 5.0
# The grace covers this multiple of the observed figure, so a launch a little
# slower than the measured tail is still covered.
CLAIM_GRACE_MARGIN = 1.5
# The percentile of the observed distribution the grace is derived from: the
# tail, so most winners launch inside the bound rather than a rare slow launch
# stretching it.
CLAIM_GRACE_PERCENTILE = 0.9


def claim_grace_seconds(observed_launch_seconds: Iterable[float]) -> float:
    """The bounded wait for an unlaunched racing winner, from observed launches.

    ``observed_launch_seconds`` is the recent launch-to-claim distribution. The
    grace is the observed tail scaled by a margin, never below the floor, so a
    fleet that launches slowly waits longer and one that launches quickly does
    not hold a losing dispatch's turn unnecessarily. An empty observation set —
    a quiet fleet, or a reader that found nothing — falls back to the floor.
    """
    observed = sorted(
        float(value)
        for value in observed_launch_seconds
        if value is not None and float(value) >= 0.0
    )
    if not observed:
        return CLAIM_GRACE_FLOOR_SECONDS
    rank = min(len(observed) - 1, int(CLAIM_GRACE_PERCENTILE * (len(observed) - 1)))
    return max(CLAIM_GRACE_FLOOR_SECONDS, CLAIM_GRACE_MARGIN * observed[rank])


# The directory listing is filtered by run id, then only the newest bounded
# candidate set is opened. A process keeps the result briefly so repeated
# admissions do not reread run receipts on every dispatch.
CLAIM_GRACE_OBSERVATION_LIMIT = 128
_CLAIM_GRACE_CACHE_SECONDS = 30.0
CLAIM_GRACE_OBSERVATIONS_NAME = "claim-grace-observations.json"
_claim_grace_cache: dict[Path, tuple[float, float]] = {}


def _recent_run_names(directory: Path, *, indexed: bool) -> list[str]:
    """Name recent runs from the committed index and live pointers."""
    names: list[str] = []
    if indexed:
        database = crew_home() / "run_store.db"
        try:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
                names.extend(
                    row[0]
                    for row in connection.execute(
                        "SELECT run_id FROM runs WHERE run_id GLOB 'r-20*' "
                        "ORDER BY run_id DESC LIMIT ?",
                        (CLAIM_GRACE_OBSERVATION_LIMIT,),
                    )
                )
        except sqlite3.Error:
            pass
        try:
            with os.scandir(crew_home() / "live") as entries:
                names.extend(
                    entry.name[:-5]
                    for entry in entries
                    if entry.name.startswith("r-") and entry.name.endswith(".json")
                )
        except OSError:
            pass
        if names:
            return heapq.nlargest(CLAIM_GRACE_OBSERVATION_LIMIT, set(names))
    try:
        with os.scandir(directory) as entries:
            return heapq.nlargest(
                CLAIM_GRACE_OBSERVATION_LIMIT,
                (entry.name for entry in entries if entry.name.startswith("r-")),
            )
    except OSError:
        return []


def recent_claim_grace_seconds(root: Path | None = None) -> float:
    """Derive a racing wait from recent complete launch receipts."""
    directory = root if root is not None else runs_dir()
    now = time.monotonic()
    cached = _claim_grace_cache.get(directory)
    if cached is not None and now - cached[0] < _CLAIM_GRACE_CACHE_SECONDS:
        return cached[1]
    if root is None:
        try:
            summary = json.loads(
                (crew_home() / CLAIM_GRACE_OBSERVATIONS_NAME).read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError):
            summary = None
        if isinstance(summary, Mapping) and isinstance(summary.get("intervals"), list):
            intervals = [
                float(row["seconds"])
                for row in summary["intervals"]
                if isinstance(row, Mapping)
                and isinstance(row.get("seconds"), (int, float))
                and row["seconds"] >= 0
            ]
            grace = (
                claim_grace_seconds(intervals)
                if len(intervals) >= 20
                else CLAIM_GRACE_FLOOR_SECONDS
            )
            _claim_grace_cache[directory] = (now, grace)
            return grace
    latest = _recent_run_names(directory, indexed=root is None)
    intervals: list[float] = []
    for name in latest:
        try:
            record = json.loads(
                (directory / name / "attempt-1-worker.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            try:
                record = json.loads(
                    (directory / name / "worker.json").read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                continue
        if not isinstance(record, Mapping):
            continue
        try:
            attempt = int(record.get("attempt") or 1)
        except (TypeError, ValueError):
            continue
        if attempt != 1:
            continue
        registered = parse_utc(str(record.get("claim_registered_at") or ""))
        launched = parse_utc(str(record.get("launched_at") or ""))
        if registered is None or launched is None:
            continue
        interval = (launched - registered).total_seconds()
        if interval >= 0:
            intervals.append(interval)
    grace = (
        claim_grace_seconds(intervals)
        if len(intervals) >= 20
        else CLAIM_GRACE_FLOOR_SECONDS
    )
    _claim_grace_cache[directory] = (now, grace)
    return grace


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
    grace_seconds: float | None = None,
) -> str:
    """Wait, bounded, for a racing winner to launch or withdraw.

    Returns ``"proceed"`` when the winner's claim has withdrawn or disappeared —
    the paths are this dispatch's after all — ``"launched"`` when the winner has
    passed its own admission, so the refusal stands, and ``"expired"`` when the
    bound passed with the winner still unlaunched. Only a claim already known to
    be an unlaunched racing winner is ever reached here.
    """
    deadline = clock() + (
        recent_claim_grace_seconds() if grace_seconds is None else grace_seconds
    )
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
    base_revision: str | None = None,
    stacked: list[dict[str, Any]] | None = None,
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
        and not claim.unread
        and repository == claim.repository
        and _scopes_overlap(absolute.as_posix(), claim.absolute_path.as_posix())
        and _directory_claim_overlaps(absolute, claim.absolute_path)
    }
    for _repository, candidate, absolute, _declared, _derived_from in candidates:
        for claim in claims:
            if claim.unread:
                # No path this claim declares can meet this node's, so it was
                # never judged and carries no verdict for the arbitration to
                # read.
                continue
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
            if _claim_holds_the_dispatch_base(claim, base_revision=base_revision):
                # A stopped holder whose chain tip this dispatch is built on
                # holds nothing the newcomer does not carry: the worktree the
                # newcomer works in was cut at or above that tip, so the
                # holder's commits are in it already. The admission is written
                # down rather than passed over quietly, because the stacking is
                # what later names the holder's live descendants.
                if stacked is not None and claim.run_id not in {
                    str(row.get("run_id") or "") for row in stacked
                }:
                    stacked.append({"run_id": claim.run_id, "node_id": claim.node_id})
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
                grace_seconds = recent_claim_grace_seconds()
                settle = _settle_racing_winner(
                    claim,
                    own_run_id=own_run_id,
                    own_registered_at=own_registered_at,
                    reread=_racing_claim_current,
                    clock=_racing_clock,
                    pause=_racing_pause,
                    grace_seconds=grace_seconds,
                )
                if settle == "proceed":
                    continue
                if settle == "expired":
                    racing_winner_refusal = (
                        f"the earlier dispatch {claim.run_id!r} has not launched "
                        f"within {grace_seconds:g}s and its claim on "
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
                    refusal.args = (f"{message} in project {claim.project!r}",)
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


def _claim_holds_the_dispatch_base(
    claim: _RepositoryScopeClaim, *, base_revision: str | None
) -> bool:
    """Whether a stopped holder's chain tip is contained in the dispatch's base.

    A holder that has committed past its own dispatch base still fences its
    paths, and it stays fenced for every dispatch cut from the integration
    branch — that is the refusal the claim exists to give. A dispatch that names
    a revision at or above the holder's head is a different case: the worktree
    it is about to build carries the holder's commits, so the second writer
    adds to that dispatch's work rather than burying it, and refusing would
    strand the holder's own repair. The holder's head is read from its worktree
    because the pointer records only the base it was dispatched from. A holder
    with no commits past that base has no chain tip for anyone to build on, so
    only a base or an ancestor of it admits — a dispatch cut from the tip
    itself, or from a commit above it.
    """
    if not base_revision or not claim.unintegrated or not claim.worktree:
        return False
    worktree = Path(claim.worktree).expanduser()
    if not worktree.is_dir():
        return False
    from reckon.crew.promotion_scope import _head_revision, _revision_is_ancestor

    holder_head = _head_revision(worktree)
    if not holder_head or holder_head == claim.base_sha:
        return False
    if holder_head == base_revision:
        return True
    return _revision_is_ancestor(worktree, holder_head, base_revision)


def stopped_with_terminal_manifest(pointer: Mapping[str, Any]) -> bool:
    """Whether a live pointer's run has ended and delivered a terminal verdict.

    A claim protects a file only while its holder can still edit it. Once the
    holder's process has ended and its manifest records a terminal status, the
    work in its worktree is fixed: a second run landing on the same path cannot
    lose it, because merging is the orchestrator's job. Both facts are read here
    at the moment of the question, from the run's own process and its own
    manifest — never from a field kept on the pointer, which nothing updates
    when the run ends.

    Liveness is read through :func:`local_liveness`, so a pointer recorded on
    another host answers "not proven" and keeps protecting its paths rather than
    being read as dead from a process table that never issued its pid.

    The manifest is read through :func:`_manifest_freshness`, which pairs the
    file with the pointer's ``manifest_baseline_mtime_ns``: a run resumed or
    redispatched after its predecessor delivered leaves that predecessor's
    terminal manifest at the same path until it writes its own, and reading the
    status alone would treat the earlier attempt's verdict as this attempt's
    delivery. A manifest older than the current attempt's baseline is not
    terminal here.
    """
    from reckon.crew.recovery_liveness import local_liveness
    from reckon.crew.reports import TERMINAL_MANIFEST_STATUSES, parse_manifest
    from reckon.crew.runs import _manifest_freshness

    alive, proven = local_liveness(pointer)
    if not proven or alive is not False:
        return False
    present, fresh = _manifest_freshness(pointer)
    if not present or not fresh:
        return False
    manifest = Path(str(pointer.get("manifest_path") or ""))
    try:
        status = str(
            parse_manifest(
                manifest.read_text(encoding="utf-8"), path=str(manifest)
            ).get("status")
            or ""
        )
    except (OSError, ValueError):
        return False
    return status.strip().lower() in TERMINAL_MANIFEST_STATUSES


def refuse_widen_scope_conflicts(
    pointer: Mapping[str, Any],
    added_paths: Iterable[str],
    *,
    set_aside_run_ids: Iterable[str] = (),
) -> None:
    """Judge added fence paths with the same claim rule as dispatch.

    ``set_aside_run_ids`` names claims the caller has already judged to protect
    nothing — a stopped peer whose manifest is terminal, set aside by a
    promotion's acceptance. The judgement is made by the caller, because a
    dispatch onto those same paths still refuses as it always has; only the
    acceptance that names the path is allowed to walk past the claim.
    """
    set_aside = {str(run_id) for run_id in set_aside_run_ids}
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
    candidate_scope = _candidate_scope_entries(
        candidate, project=project, repo=repo, authority=authority
    )
    claims = _repository_scope_claims(
        exclude_run_ids=(str(pointer.get("run_id") or ""),),
        # The added paths are the whole question a widening asks, so only the
        # claims that can meet them need a disposition read.
        candidate_paths=(
            absolute
            for _repository, _path, absolute, _declared, _derived in candidate_scope
        ),
        candidate_repositories=(
            repository
            for repository, _path, _absolute, _declared, _derived in candidate_scope
            if repository is not None
        ),
    )
    if set_aside:
        claims = [claim for claim in claims if claim.run_id not in set_aside]
    # The run doing the widening is the one whose base decides whether a claim it
    # overlaps is one it already carries. A run stacked on a stopped holder has
    # that holder's chain tip as its base, so reading its own base is what keeps a
    # widening dispatch from being refused by the very claim it was cut from. The
    # pointer records the base it was dispatched from, and the resolved value is
    # preferred over the ref it was resolved from.
    widening_base = str(pointer.get("base_sha") or pointer.get("base") or "").strip()
    _raise_repository_scope_conflict(
        candidate,
        project=project,
        repo=repo,
        authority=authority,
        claims=claims,
        base_revision=widening_base,
        own_run_id=str(pointer.get("run_id") or ""),
        own_registered_at=_utc_now(),
    )
