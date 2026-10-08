# ruff: noqa: I001, UP035
from __future__ import annotations
import contextlib
import hashlib
import json
import os
import re
import shutil
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
# to launch or withdraw before refusing. The wait is not fixed: the winner
# published its claim and now composes and spawns a worker, and how long that
# takes varies with the fleet, so the bound is derived from the observed
# launch-to-claim time rather than a magic number. A wait holds the losing
# dispatch's whole turn, so the bound still has to be short.
#
# The launch-to-claim time is measured on the fleet node over recent runs —
# the interval from a claim's registration to the instant its worker record
# carries ``launched_at``. Measured 2026-10-08 over 397 recent dispatches, in
# seconds: min 1, median 10, p90 22, max 41.
_CLAIM_LAUNCH_OBSERVED_SECONDS = (
    1,
    2,
    3,
    4,
    5,
    5,
    6,
    6,
    7,
    7,
    8,
    9,
    10,
    11,
    12,
    16,
    20,
    22,
    25,
    41,
)
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


RACING_WINNER_WAIT_SECONDS = claim_grace_seconds(_CLAIM_LAUNCH_OBSERVED_SECONDS)
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
