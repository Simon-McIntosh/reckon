# ruff: noqa: I001, PLC0414
from __future__ import annotations
import fcntl as fcntl
import hashlib as hashlib
import json as json
import os as os
import re as re
import shlex as shlex
import shutil as shutil
import socket as socket
import stat as stat
import subprocess as subprocess
import sys as sys
import threading as threading
import time as time
from collections.abc import Callable as Callable, Iterable as Iterable, Mapping as Mapping
from contextlib import contextmanager as contextmanager, suppress as suppress
from dataclasses import dataclass as dataclass, field as field
from datetime import UTC as UTC, datetime as datetime
from pathlib import Path as Path
from typing import Any as Any
from reckon import __version__ as __version__
from reckon._store import _config_home as _config_home, _docs_dir_for_project as _docs_dir_for_project, write_atomically as write_atomically, write_json_atomically as write_json_atomically
from reckon._timestamps import parse_utc as parse_utc
from reckon.crew.host_lease import LEASE_RENEW_SECONDS as LEASE_RENEW_SECONDS, HostLease as HostLease
from reckon.crew.node import _TERMINAL_RUN_PHASES as _TERMINAL_RUN_PHASES, DEFAULT_WATCH_STALL_WINDOW as DEFAULT_WATCH_STALL_WINDOW, RUN_DRAIN_DISPOSITIONS as RUN_DRAIN_DISPOSITIONS, CrewError as CrewError, TaskNode as TaskNode, parse_duration as parse_duration
from reckon.crew.obligation_snapshot import CLI_MODULE_FILES as CLI_MODULE_FILES


def live_dir() -> Path:
    """Directory of live pointers, one JSON file per in-flight run."""
    return crew_home() / "live"


def runs_dir() -> Path:
    """Directory holding durable per-run delivery and event artifacts."""
    return crew_home() / "runs"


def reports_dir() -> Path:
    """Directory holding durable reports that are not tied to one run."""
    return crew_home() / "reports"


def delivery_roots() -> tuple[Path, ...]:
    """Return every durable directory a node may use outside its repository."""
    from reckon.crew.review import review_store_root

    return (
        runs_dir().resolve(),
        reports_dir().resolve(),
        review_store_root().resolve(),
    )


def run_dir(run_id: str) -> Path:
    """Directory holding one run's prompt, event log and default manifest."""
    return runs_dir() / run_id


def pointer_path(run_id: str) -> Path:
    """Path of one run's live pointer."""
    return live_dir() / f"{run_id}.json"


def capture_run_session(record: dict[str, Any]) -> dict[str, Any] | None:
    """Bind a captured session to this run's harness without writing a roster.

    The caller persists the pointer under its lock. Promotion copies session
    ownership from the run into explicit fields on the committed row.
    """
    from reckon.crew.resumption import resolve_session

    session_id = str(
        resolve_session(str(record.get("run_id") or ""), record=record).get(
            "session_id"
        )
        or ""
    ).strip()
    if not session_id:
        return None
    record["session_id"] = session_id
    agent = record.get("agent") or {}
    harness = str(record.get("dialect") or agent.get("dialect") or "").strip()
    record["session_harness"] = harness or None
    record["session_model"] = agent.get("model")
    return {
        "captured": True,
        "run_id": record.get("run_id"),
        "session_id": session_id,
        "harness": harness or None,
        "detail": "session captured on its run",
    }


def _manifest_mtime_ns(path: str | Path) -> int:
    """Return the manifest generation visible before an attempt begins."""
    manifest = Path(str(path or ""))
    if not str(path or "") or not manifest.is_file():
        return 0
    return manifest.stat().st_mtime_ns


def _manifest_freshness(record: Mapping[str, Any]) -> tuple[bool, bool]:
    """Return physical presence and whether delivery belongs to this attempt."""
    manifest = Path(str(record.get("manifest_path") or ""))
    file_present = bool(str(record.get("manifest_path") or "")) and manifest.is_file()
    if not file_present:
        return False, False
    baseline = record.get("manifest_baseline_mtime_ns")
    if baseline is None:
        # Pointers written before attempt identity existed remain readable.
        return True, True
    try:
        fresh = manifest.stat().st_mtime_ns > int(baseline)
    except (OSError, TypeError, ValueError):
        fresh = False
    return True, fresh


# The sidecar that records what one session's pane was last shown, one state
# per run. It sits beside the registration rather than inside it, so writing it
# never contends with the registration lock, and it is written by whoever writes
# a row to the pane rather than by the row's producer: a row generated and then
# withheld from the reader is not something the reader saw, and a re-attach that
# treated it as delivered would replay a gap that never opened.
DELIVERED_SUFFIX = ".delivered.json"

# The record's own schema version, so a later shape change is recognised rather
# than misread as the current one.
DELIVERED_VERSION = 1


def delivered_path(project: str, session: str) -> Path:
    """The delivered-state record for one session, under its follower directory."""
    readable = re.sub(r"[^A-Za-z0-9._-]", "-", session).strip("-") or "session"
    digest = hashlib.sha256(session.encode()).hexdigest()[:12]
    return follower_dir(project) / f"{readable}-{digest}{DELIVERED_SUFFIX}"


def read_delivered(project: str, session: str | None) -> dict[str, Any]:
    """Return the delivered-state record for one session, or ``{}`` when none.

    An absent, unreadable or older-shaped record is no record: a follower then
    arms as though its session had no pane before, because the fields a
    mismatch leaves untrusted are exactly the ones the replay is built from.
    """
    if not session:
        return {}
    try:
        raw = delivered_path(project, session).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        record = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(record, dict):
        return {}
    if record.get("version") != DELIVERED_VERSION:
        return {}
    if str(record.get("project") or "") != project:
        return {}
    if str(record.get("session") or "") != session:
        return {}
    if not isinstance(record.get("states"), Mapping):
        return {}
    return record


def write_delivered(
    project: str,
    session: str | None,
    states: Mapping[str, str],
    *,
    at: str | None = None,
) -> None:
    """Record the state each run was last shown at in this session's pane.

    Written atomically, so a reader sees the previous record whole or the new
    one whole. A write that fails costs a later re-attach its diff and must
    never cost this arming its pane, so it is not raised. Entries whose run no
    longer has a live pointer are dropped: the record is the pane's memory of
    live work, and one a session leaves armed for days would otherwise carry
    every run it ever showed.
    """
    if not session:
        return
    record = {
        "version": DELIVERED_VERSION,
        "project": project,
        "session": session,
        "recorded_at": at or _utc_now(),
        "states": {
            str(run_id): str(state)
            for run_id, state in states.items()
            if pointer_path(str(run_id)).exists()
        },
    }
    try:
        from reckon._store import write_json_atomically

        write_json_atomically(
            delivered_path(project, session),
            record,
            indent=None,
            sort_keys=True,
            mode=None,
            fsync=True,
            fsync_directory=True,
            create_parents=True,
        )
    except OSError:
        return


def new_run_id(node_id: str, *, now: datetime | None = None) -> str:
    """Mint a filesystem-safe run id that sorts by dispatch time."""
    stamp = (now or datetime.now(tz=UTC)).strftime("%Y%m%dT%H%M%S%f")
    token = re.sub(r"[^A-Za-z0-9._-]", "-", node_id).strip("-") or "node"
    return f"r-{stamp}-{token}"


def _recorded_launch_host(path: Path) -> str | None:
    """Return the launching host a pointer file already names, if any.

    A file that cannot be read back, or one written with no such key, answers
    None: the caller then carries the payload unchanged rather than inventing a
    host for it.
    """
    try:
        recorded = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(recorded, Mapping):
        return None
    host = recorded.get("launcher_host")
    return str(host) if host else None


def _stamp_pointer_launch_host(
    path: Path, payload: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Carry a live pointer's launching host on every write of its file.

    A process id is meaningful only on the machine that issued it, and the
    crew configuration home is shared across login nodes, so a run records
    where it was created under the key the classifier reads (``launcher_host``),
    spelled with ``socket.gethostname()`` on both sides so the writer and the
    reader cannot disagree. The host is a property of where the process was
    created and never changes while it lives, so the file records it once and
    every later write of that file carries it forward rather than trusting the
    payload to repeat it: a dispatch publishes its claim before its launch is
    composed, and the full record that replaces the claim is built as its own
    mapping, so a payload written to an existing pointer arrives without the
    key and the file's own record of it is dropped. A pointer that predates
    this change has no recoverable launching
    host, so rewriting one never invents the rewriter's host on a file that
    already exists.
    """
    if "launcher_host" in payload:
        return payload
    if path.parent != live_dir():
        return payload
    if path.exists():
        host = _recorded_launch_host(path)
        if host is None:
            return payload
    else:
        host = socket.gethostname()
    if isinstance(payload, dict):
        payload["launcher_host"] = host
        return payload
    return {**payload, "launcher_host": host}


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON atomically, so a reader never sees a half-written record."""
    payload = _stamp_pointer_launch_host(path, payload)
    write_json_atomically(
        path,
        payload,
        indent=2,
        sort_keys=True,
        mode=0o600,
        ensure_ascii=True,
    )


@contextmanager
def _pointer_lock(run_id: str):
    """Serialise every read-modify-write cycle for one live pointer."""
    path = crew_home() / "locks" / f"{run_id}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _mutate_pointer(
    run_id: str, mutation: Callable[[dict[str, Any]], dict[str, Any]]
) -> dict[str, Any]:
    """Apply one pointer mutation while holding its per-run lock."""
    with _pointer_lock(run_id):
        previous = read_pointer(run_id)
        previous_attempt = int(previous.get("attempt") or 1)
        record = mutation(previous)
        current_attempt = int(record.get("attempt") or 1)
        if current_attempt > previous_attempt:
            record["attempt_budget_seconds"] = _attempt_budget_seconds(run_id, record)
        _write_json(pointer_path(run_id), record)
        return record


def queue_dispatch(record: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Store a held local request once per project, session, section and node."""
    lock = crew_home() / "locks" / "queued-dispatch.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            live = _list_live_records(project=str(record["project"]))
            existing = next(
                (
                    pointer
                    for pointer in live
                    if pointer.get("phase") == "queued"
                    and pointer.get("session") == record["session"]
                    and (pointer.get("node") or {}).get("id") == record["node"]["id"]
                    and (pointer.get("node") or {}).get("plan")
                    == record["node"]["plan"]
                    and (pointer.get("node") or {}).get("section")
                    == record["node"]["section"]
                ),
                None,
            )
            existing_run_id = str(existing["run_id"]) if existing is not None else None
            requeued = False
            if existing_run_id is not None:
                with _pointer_lock(existing_run_id):
                    if pointer_path(existing_run_id).exists():
                        current = read_pointer(existing_run_id)
                        if current.get("phase") != "queued":
                            raise CrewError(
                                f"queued run {existing_run_id!r} changed phase; "
                                "retry dispatch"
                            )
                        record["run_id"] = existing_run_id
                        record["queued_at"] = current["queued_at"]
                        record["created_at"] = current["created_at"]
                        requeued = True
                        _write_json(pointer_path(existing_run_id), record)
                    # A pointer that vanished between the scan and this lock was
                    # discarded underneath the re-queue: fall through and queue
                    # afresh rather than reading a pointer that is gone.
            if not requeued:
                run_id = str(record["run_id"])
                with _pointer_lock(run_id):
                    _write_json(pointer_path(run_id), record)
            from reckon.crew.queue_order import admission_order

            live = _list_live_records(project=str(record["project"]))
            queued = [pointer for pointer in live if pointer.get("phase") == "queued"]
            order = admission_order(queued, live, now=datetime.now(UTC))
            position = next(
                index
                for index, pointer in enumerate(order, 1)
                if pointer["run_id"] == record["run_id"]
            )
            return record, position
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


_RESUME_BUDGET = re.compile(
    r"\b(?:time\s+)?(?:budget|fence)\s+(?:is\s+)?"
    r"(?:extended|extends?)\s+(?:to|by)\s+"
    r"(?P<amount>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>seconds?|minutes?|hours?|[smh])\b",
    re.IGNORECASE,
)


def _attempt_budget_seconds(run_id: str, record: Mapping[str, Any]) -> int | None:
    """Resolve the finite allowance stated for the newly launched attempt."""
    node = record.get("node") or {}
    try:
        default = parse_duration(str(node.get("time_budget") or ""))
    except CrewError:
        default = None
    if record.get("attempt_kind") != "resume":
        return default
    turn = record.get("resumed_turn")
    try:
        advice = (run_dir(run_id) / f"resume-{int(turn)}-advice.txt").read_text()
    except (OSError, TypeError, ValueError):
        return default
    match = _RESUME_BUDGET.search(advice)
    if match is None:
        return default
    amount = float(match.group("amount"))
    unit = match.group("unit").lower()
    multiplier = 1
    if unit.startswith("m"):
        multiplier = 60
    elif unit.startswith("h"):
        multiplier = 3600
    return int(amount * multiplier)


def read_pointer(run_id: str) -> dict[str, Any]:
    """Read one run's live pointer, or say which run is unknown."""
    path = pointer_path(run_id)
    if not path.exists():
        raise CrewError(f"no live run {run_id!r} (looked in {path})")
    try:
        data = json.loads(path.read_text())
    except ValueError as exc:
        raise CrewError(
            f"live pointer for {run_id!r} is not valid JSON — {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise CrewError(f"live pointer for {run_id!r} does not hold an object")
    return data


def _list_live_records(
    *, project: str | None = None, phase: str | None = None
) -> list[dict[str, Any]]:
    """Read matching live pointers without publishing watcher transitions."""
    directory = live_dir()
    if not directory.is_dir():
        return []
    records = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            continue
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if project is not None and str(data.get("project") or "") != project:
            continue
        if phase is not None and str(data.get("phase") or "") != phase:
            continue
        records.append(data)
    return records


def list_live(
    *, project: str | None = None, phase: str | None = None
) -> list[dict[str, Any]]:
    """Return matching live pointers, newest run id last.

    The pointers are returned as they are stored: a liveness probe taken here is
    never written into ``process_alive``. A pid answers only on the host that
    issued it, while the crew home is shared across login nodes, so a value
    written into that key would be read back by the classifier as its own
    observer's answer on a host that never issued the pid — from which a live
    worker on another machine reads dead.

    A consumer that reports a run's liveness derives it where it reads it,
    through ``recovery.local_liveness`` or ``recovery.classify_pointer`` — which
    reads it and supplies the row the fleet, the directory, the query and the
    ticker all render — the one reading that asks the process table only when
    the record's ``launcher_host`` is the reading host. ``record_process_alive``
    is the bare probe beneath that gate: it asks this host's table about
    whatever pid the record carries and performs no host check of its own, so it
    may be used only where the pid's presence on this host is already
    established — ``dispatch`` probing the process it has just spawned, say.
    """
    records = _list_live_records(project=project, phase=phase)
    if project is not None and phase is None:
        _publish_watch_stream(project, records)
    return records


@dataclass(frozen=True)
class _LiveScopeClaim:
    """One normalized repository-relative path held by a live pointer."""

    run_id: str
    node_id: str
    path: str
    declared_path: str
    derived_from: str | None = None
    # When the run published the claim, and whether it has passed its own
    # admission and launched. The dispatch scope check orders two racing claims
    # by these, so they travel with the claim into that check rather than being
    # dropped when it is rebuilt from the read model.
    registered_at: str = ""
    launched: bool = False
    # Whether this claim still fences its path, judged by the same rule the
    # dispatch scope check applies (a stopped run holding no unintegrated work
    # is walked past), so a reader of the registry reaches the enforcer's
    # disposition rather than a stricter one of its own. The reason travels
    # with the verdict so a disregarded claim does not read as a bare false.
    binding: bool = True
    disposition_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return the stable read-model representation of this claim."""
        claim = {
            "path": self.path,
            "run_id": self.run_id,
            "node": self.node_id,
            "declared_path": self.declared_path,
            "binding": self.binding,
            "disposition_reason": self.disposition_reason,
        }
        if self.derived_from is not None:
            claim["derived_from"] = self.derived_from
        return claim


def _repository_relative_scope(path: str, repo: Path) -> str | None:
    """Normalize an in-repository scope to a repository-relative POSIX path."""
    raw = Path(path).expanduser()
    resolved = (raw if raw.is_absolute() else repo / raw).resolve()
    try:
        relative = resolved.relative_to(repo)
    except ValueError:
        return None
    return relative.as_posix()


def _scopes_overlap(first: str, second: str) -> bool:
    """Return whether either normalized path contains the other by component."""
    first_parts = Path(first).parts
    second_parts = Path(second).parts
    common = min(len(first_parts), len(second_parts))
    return first_parts[:common] == second_parts[:common]


def _scope_contains(container: str, path: str) -> bool:
    """Return whether one normalized path contains another by component."""
    container_parts = Path(container).parts
    path_parts = Path(path).parts
    return path_parts[: len(container_parts)] == container_parts


def _normalized_derivations(
    derivations: Mapping[str, Iterable[str]] | None,
    repo: Path,
) -> dict[str, tuple[str, ...]]:
    """Normalize the repository's source-to-generated path relationships."""
    normalized: dict[str, tuple[str, ...]] = {}
    for source, generated in sorted((derivations or {}).items()):
        source_path = _repository_relative_scope(str(source), repo)
        if source_path is None:
            raise CrewError(
                f"project derivation source {source!r} is outside repository {repo}"
            )
        outputs: list[str] = []
        for output in generated:
            output_path = _repository_relative_scope(str(output), repo)
            if output_path is None:
                raise CrewError(
                    f"project derivation output {output!r} is outside repository {repo}"
                )
            outputs.append(output_path)
        normalized[source_path] = tuple(sorted(set(outputs)))
    return normalized


def _expanded_scope_paths(
    paths: Iterable[str],
    repo: Path,
    derivations: Mapping[str, Iterable[str]] | None,
) -> list[tuple[str, str, str | None]]:
    """Expand declared paths through transitive source-to-generated relations."""
    relationships = _normalized_derivations(derivations, repo)
    expanded: dict[str, tuple[str, str | None]] = {}
    for raw_path in paths:
        declared = _repository_relative_scope(str(raw_path), repo)
        if declared is None:
            raw = Path(str(raw_path)).expanduser()
            absolute = (raw if raw.is_absolute() else repo / raw).resolve().as_posix()
            expanded[absolute] = (absolute, None)
            continue
        expanded[declared] = (declared, None)
        pending = [declared]
        visited: set[str] = set()
        while pending:
            current = pending.pop(0)
            if current in visited:
                continue
            visited.add(current)
            for source, generated in relationships.items():
                if not _scope_contains(current, source):
                    continue
                for output in generated:
                    if output not in expanded:
                        expanded[output] = (declared, source)
                    if output not in visited:
                        pending.append(output)
    return [
        (path, declared, derived_from)
        for path, (declared, derived_from) in sorted(expanded.items())
    ]


def _live_scope_claims(
    project: str,
    repo: Path,
    derivations: Mapping[str, Iterable[str]] | None = None,
) -> list[_LiveScopeClaim]:
    """Derive this repository's claimed paths from its project live pointers."""
    from reckon.crew.dispatch import _UNLAUNCHED_CLAIM_PHASES
    from reckon.crew.node import claim_disposition

    claims: list[_LiveScopeClaim] = []
    for pointer in list_live(project=project):
        pointer_repo = str(pointer.get("repo") or "")
        if not pointer_repo or Path(pointer_repo).expanduser().resolve() != repo:
            continue
        node = pointer.get("node")
        if not isinstance(node, Mapping):
            continue
        run_id = str(pointer.get("run_id") or "unknown")
        node_id = str(node.get("id") or "unknown")
        # A claim still being composed carries no worktree or pid and sits in a
        # pre-spawn phase; anything else has passed its own admission. The
        # dispatch scope check orders racing claims by these, so they travel
        # with every claim built here.
        registered_at = str(pointer.get("created_at") or "")
        launched = bool(pointer.get("worktree") or pointer.get("pid")) or (
            str(pointer.get("phase") or "") not in _UNLAUNCHED_CLAIM_PHASES
        )
        # The same per-pointer disposition the dispatch scope check takes, so
        # the registry a worker reads carries the verdict the enforcer would
        # apply instead of leaving the reader to derive a stricter one.
        disposition = claim_disposition(pointer)
        for path, declared, derived_from in _expanded_scope_paths(
            node.get("write_paths") or (), repo, derivations
        ):
            claims.append(
                _LiveScopeClaim(
                    run_id=run_id,
                    node_id=node_id,
                    path=path,
                    declared_path=declared,
                    derived_from=derived_from,
                    registered_at=registered_at,
                    launched=launched,
                    binding=disposition.binding,
                    disposition_reason=disposition.reason,
                )
            )
    return sorted(claims, key=lambda claim: (claim.run_id, claim.node_id, claim.path))


def scope_claims(
    project: str,
    repo: str | Path,
    *,
    derivations: Mapping[str, Iterable[str]] | None = None,
) -> list[dict[str, Any]]:
    """Read this repository's live claim registry without changing pointers."""
    repo_root = Path(repo).expanduser().resolve()
    return [
        claim.as_dict() for claim in _live_scope_claims(project, repo_root, derivations)
    ]


def _candidate_nodes(
    candidates: Iterable[Mapping[str, Any]],
    repo: Path,
    derivations: Mapping[str, Iterable[str]] | None,
) -> list[dict[str, Any]]:
    """Validate and normalize an ordered candidate wave manifest."""
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, candidate in enumerate(candidates):
        node_id = str(candidate.get("id") or candidate.get("node") or "").strip()
        if not node_id:
            raise CrewError(f"candidate at index {position} has no node id")
        if node_id in seen:
            raise CrewError(f"candidate node id {node_id!r} is duplicated")
        seen.add(node_id)
        raw_paths = candidate.get("write_paths", candidate.get("paths"))
        if not isinstance(raw_paths, (list, tuple)) or not raw_paths:
            raise CrewError(
                f"candidate node {node_id!r} must declare a non-empty write_paths list"
            )
        paths = _expanded_scope_paths(raw_paths, repo, derivations)
        normalized.append(
            {
                "id": node_id,
                "position": position,
                "declared_paths": sorted(
                    {declared for _path, declared, _derived_from in paths}
                ),
                "paths": [path for path, _declared, _derived_from in paths],
                "derived_paths": [
                    {
                        "path": path,
                        "declared_path": declared,
                        "derived_from": derived_from,
                    }
                    for path, declared, derived_from in paths
                    if derived_from is not None
                ],
            }
        )
    return normalized


def _scope_intersections(
    first: Iterable[str], second: Iterable[str]
) -> list[dict[str, str]]:
    """Return every deterministic path pair that overlaps by containment."""
    return [
        {"left_path": left, "right_path": right}
        for left in sorted(set(first))
        for right in sorted(set(second))
        if _scopes_overlap(left, right)
    ]


def plan_scope_lanes(
    candidates: Iterable[Mapping[str, Any]],
    *,
    project: str,
    repo: str | Path,
    derivations: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    """Partition candidate nodes into ordered, mutually independent lanes.

    A lane is a serial sequence. Conflicting nodes therefore stay in the same
    lane, while disconnected components may run concurrently as separate lanes.
    Candidate order is retained both between lanes and within each lane.

    The lane partition, the candidate conflicts and the live-claim
    intersections are all computed against the supplied wave manifest, so with
    no candidates there is no question to answer rather than an answer of
    none. ``candidate_wave`` reports which of the two this payload carries, so
    an empty ``conflicts`` is never read as an evaluated wave that happened to
    be clean. Every listed claim carries ``binding``, judged by the dispatch
    scope check's own rule.
    """
    repo_root = Path(repo).expanduser().resolve()
    nodes = _candidate_nodes(candidates, repo_root, derivations)
    live_claims = _live_scope_claims(project, repo_root, derivations)
    adjacency = {node["id"]: set() for node in nodes}
    conflicts: list[dict[str, Any]] = []
    for index, left in enumerate(nodes):
        for right in nodes[index + 1 :]:
            intersections = _scope_intersections(left["paths"], right["paths"])
            if not intersections:
                continue
            adjacency[left["id"]].add(right["id"])
            adjacency[right["id"]].add(left["id"])
            conflicts.append(
                {
                    "left": left["id"],
                    "right": right["id"],
                    "paths": intersections,
                }
            )

    live_conflicts: list[dict[str, Any]] = []
    for node in nodes:
        for claim in live_claims:
            intersections = _scope_intersections(node["paths"], [claim.path])
            if intersections:
                live_conflicts.append(
                    {
                        "candidate": node["id"],
                        "run_id": claim.run_id,
                        "node": claim.node_id,
                        "claimed_path": claim.path,
                        "paths": intersections,
                    }
                )

    lanes: list[dict[str, Any]] = []
    assigned: set[str] = set()
    for node in nodes:
        if node["id"] in assigned:
            continue
        pending = [node["id"]]
        component: set[str] = set()
        while pending:
            current = pending.pop(0)
            if current in component:
                continue
            component.add(current)
            pending.extend(
                neighbor for neighbor in adjacency[current] if neighbor not in component
            )
        ordered = [item["id"] for item in nodes if item["id"] in component]
        assigned.update(component)
        blocked_by = sorted(
            {
                conflict["run_id"]
                for conflict in live_conflicts
                if conflict["candidate"] in component
            }
        )
        lane: dict[str, Any] = {"lane": len(lanes) + 1, "nodes": ordered}
        if blocked_by:
            lane["blocked_by_live"] = blocked_by
        lanes.append(lane)

    return {
        "candidates": [
            {key: value for key, value in node.items() if key != "position"}
            for node in nodes
        ],
        "claims": [claim.as_dict() for claim in live_claims],
        "conflict_graph": {
            node_id: sorted(neighbors) for node_id, neighbors in adjacency.items()
        },
        "conflicts": conflicts,
        "live_conflicts": live_conflicts,
        "lane_count": len(lanes),
        "lanes": lanes,
        "candidate_wave": {
            "state": "evaluated" if nodes else "unevaluated",
            "detail": (
                ""
                if nodes
                else "no candidate wave manifest was supplied, so the lane "
                "partition, candidate conflicts and live-claim intersections "
                "were not evaluated"
            ),
        },
    }


def _project_derivations(project: str, repo: Path) -> dict[str, list[str]]:
    """Read the repository derivation map from its project resource."""
    from reckon.project_state import read_project_derivations

    docs_dir = repo / "docs"
    if not docs_dir.is_dir():
        return {}
    return read_project_derivations(docs_dir, project)


SHARED_WRITE_PATHS_FILENAME = "shared-write-paths.json"


def _shared_write_paths(project: str | None, repo: Path) -> frozenset[str]:
    """Return the files this project declares safe for concurrent live claims.

    A project may list repository-relative files in
    ``docs/state/<project>/shared-write-paths.json`` whose concurrent editors
    work in different functions often enough that refusing the second claimant
    costs more than it protects. Each entry names one file and why it is
    shareable. Only the named file is shareable: a directory that merely
    contains it stays exclusive. An absent, unreadable or malformed list
    declares nothing, so the refusal is a whole-file refusal as before.
    """
    if not project:
        return frozenset()
    manifest = repo / "docs" / "state" / str(project) / SHARED_WRITE_PATHS_FILENAME
    try:
        raw = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError):
        return frozenset()
    entries = raw.get("paths") if isinstance(raw, Mapping) else raw
    if not entries:
        return frozenset()
    shared: set[str] = set()
    for entry in entries:
        path = entry.get("path") if isinstance(entry, Mapping) else entry
        if not isinstance(path, str) or not path.strip():
            continue
        normalized = _repository_relative_scope(path, repo)
        if normalized is not None:
            shared.add(normalized)
    return frozenset(shared)


def _raise_live_scope_conflict(
    node: TaskNode,
    claims: Iterable[_LiveScopeClaim],
    repo: Path,
    derivations: Mapping[str, Iterable[str]] | None = None,
    *,
    project: str | None = None,
    own_run_id: str | None = None,
    own_registered_at: str | None = None,
) -> None:
    """Delegate a scope refusal to the check every crew dispatch runs.

    The shared-path exemption and the whole-file refusal both live in
    ``dispatch._raise_repository_scope_conflict``, the check a real dispatch
    reaches. This entry is the facade's name for it, kept so the export stays
    resolvable and no second claim check can drift out of step with the one
    dispatch uses. The import is deferred because dispatch imports this module.

    ``own_run_id`` and ``own_registered_at`` name the dispatch on whose behalf
    the refusal is judged, and the converted claims carry their registration
    time and launch state through, so the racing-claims ordering applies on this
    route exactly as it does inside a real dispatch rather than refusing a
    racing arrival the dispatch placed and registered first.
    """
    from reckon.crew.dispatch import (
        _raise_repository_scope_conflict,
        _RepositoryScopeClaim,
    )

    converted = [
        _RepositoryScopeClaim(
            project=project or "",
            repository=repo,
            run_id=claim.run_id,
            node_id=claim.node_id,
            path=claim.path,
            absolute_path=(repo / claim.path).resolve(),
            declared_path=claim.declared_path,
            registered_at=claim.registered_at,
            launched=claim.launched,
        )
        for claim in claims
    ]
    _raise_repository_scope_conflict(
        node,
        project=project or "",
        repo=repo,
        authority={"repositories": [repo]},
        claims=converted,
        own_run_id=own_run_id,
        own_registered_at=own_registered_at,
    )


def _merge_peer_scopes(
    claims: Iterable[_LiveScopeClaim],
    supplied: Mapping[str, Iterable[str]] | None,
) -> dict[str, list[str]]:
    """Combine pointer-derived peer scopes with optional explicit supplements."""
    peers: dict[str, set[str]] = {}
    for claim in claims:
        peers.setdefault(claim.node_id, set()).add(claim.path)
    for node_id, paths in (supplied or {}).items():
        peers.setdefault(node_id, set()).update(str(path) for path in paths)
    return {node_id: sorted(paths) for node_id, paths in sorted(peers.items())}


def record_run_disposition(
    run_id: str,
    disposition: str,
    *,
    project: str | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    """Record why one live pointer may remain across session closure."""
    reason = str(disposition).strip()
    if reason not in RUN_DRAIN_DISPOSITIONS:
        allowed = ", ".join(RUN_DRAIN_DISPOSITIONS)
        raise CrewError(f"run disposition {disposition!r} is not one of {allowed}")

    def record(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer_project = str(pointer.get("project") or "")
        if project is not None and pointer_project != project:
            raise CrewError(
                f"live run {run_id!r} belongs to project {pointer_project!r}, "
                f"not {project!r}"
            )
        pointer_session = str(pointer.get("session") or "")
        if session is not None and pointer_session != session:
            raise CrewError(
                f"live run {run_id!r} belongs to session {pointer_session!r}, "
                f"not {session!r}"
            )
        pointer["closure_disposition"] = {
            "kind": reason,
            "recorded_at": _utc_now(),
        }
        return pointer

    return _mutate_pointer(run_id, record)


ACKNOWLEDGEMENT_FIELD = "acknowledgement"


def record_run_acknowledgement(
    run_id: str,
    reason: str,
    until: str,
    *,
    project: str | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    """Record a deliberate deferral of one run's obligations.

    For a live run the deferral is written beside the closure disposition on
    the live pointer, so it travels with the run it excuses and expires on its
    own: the obligations reader withholds a run whose deferral has not yet
    passed and returns it once it has, with no second store to reconcile. A run
    already promoted has no pointer left, and the remainder it still holds is
    a deliberate one all the same, so the deferral is written to a file of its
    own under the crew home — transient state that belongs to the run, not to
    the project's committed history, which an acknowledgement must never
    rewrite. ``until`` is normalised to UTC on write so a later comparison
    never has to know which zone it arrived in.
    """
    text = str(reason).strip()
    if not text:
        raise CrewError("an acknowledgement requires a non-empty --reason")
    deadline = parse_utc(until)
    if deadline is None:
        raise CrewError(f"acknowledgement --until {until!r} is not an ISO-8601 instant")
    deferral = {
        "reason": text,
        "until": deadline.isoformat(),
        "recorded_at": _utc_now(),
    }
    if not pointer_path(run_id).exists():
        return _record_promoted_acknowledgement(run_id, deferral, project=project)

    def record(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer_project = str(pointer.get("project") or "")
        if project is not None and pointer_project != project:
            raise CrewError(
                f"live run {run_id!r} belongs to project {pointer_project!r}, "
                f"not {project!r}"
            )
        pointer_session = str(pointer.get("session") or "")
        if session is not None and pointer_session != session:
            raise CrewError(
                f"live run {run_id!r} belongs to session {pointer_session!r}, "
                f"not {session!r}"
            )
        pointer[ACKNOWLEDGEMENT_FIELD] = deferral
        return pointer

    return _mutate_pointer(run_id, record)


def acknowledgements_dir() -> Path:
    """Directory of deferrals recorded for runs whose live pointer is gone."""
    return crew_home() / "acknowledgements"


def acknowledgement_path(run_id: str) -> Path:
    """Path of one promoted run's recorded deferral."""
    return acknowledgements_dir() / f"{run_id}.json"


def _deferral_clock() -> datetime:
    """The instant the obligations reader judges a deferral's ``until`` against.

    The sweep and the reader that honours a deferral must agree on whether a
    record has expired, or one would delete a deferral the other still
    withholds a duty for. The reader's clock is the one seam that settles it,
    read at call time so a caller that patches the reader's clock is answered
    by the same instant.
    """
    from reckon.crew import obligations as obligations_module

    return obligations_module._utc_now()


def recorded_promoted_acknowledgements(
    project: str | None = None,
) -> list[dict[str, Any]]:
    """Every deferral recorded for a promoted run, in run-id order.

    One small file per acknowledged run, so reading the directory costs what
    the acknowledgements cost and nothing of any ledger. A file that cannot be
    parsed is skipped rather than guessed at, on the same principle as an
    unreadable ``until``: absence of a readable record is not proof of one.
    A file whose ``until`` has passed is removed as it is read: the deferral
    it carried excuses nothing, and leaving it behind charges every later read
    for a record no reader can honour.
    """
    records: list[dict[str, Any]] = []
    try:
        paths = sorted(acknowledgements_dir().glob("*.json"))
    except OSError:
        return records
    now = _deferral_clock()
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        until = parse_utc(payload.get("until"))
        if until is not None and until <= now:
            # A failed removal keeps the read intact: the file stays on disk
            # for a later sweep, and the expired deferral is still not
            # returned, exactly as if it had been removed.
            with suppress(OSError):
                path.unlink()
            continue
        if project is not None and str(payload.get("project") or "") != project:
            continue
        records.append(payload)
    return records


def _mounted_project_roots(project: str | None) -> dict[str, Path]:
    """Each mounted project's checkout root, keyed by project name.

    A promoted run's record lives beside the project it was promoted for, and
    the mount registry is what resolves a project name to a checkout from
    outside it. The optional ``project`` narrows the search to one registry
    entry; an unknown name resolves to no root rather than a guess.
    """
    from reckon import flight

    try:
        mounted = flight.mounted_project_docs()
    except flight.FlightConfigError:
        return {}
    if project is not None:
        docs = mounted.get(str(project))
        return {str(project): docs.parent.resolve()} if docs is not None else {}
    return {name: docs.parent.resolve() for name, docs in mounted.items()}


def _promoted_record_project(run_id: str, project: str | None) -> str | None:
    """Name the project whose run store holds a promoted run's record.

    The record is read from the run's own file beside the project's ledger,
    which is where promotion writes it, rather than from the aggregate the
    ledger's split moves runs out of: naming one run must not load the whole
    history, and a presence check on the file is the same evidence the reader
    itself would find.
    """
    from reckon import ledger

    for name, root in sorted(_mounted_project_roots(project).items()):
        try:
            path = ledger.run_path(name, run_id, root)
        except ledger.LedgerError:
            continue
        if not path.is_file():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and str(record.get("run_id") or "") == run_id:
            return name
    return None


def _record_promoted_acknowledgement(
    run_id: str, deferral: Mapping[str, Any], *, project: str | None
) -> dict[str, Any]:
    """Record a promoted run's deferral in its own file under the crew home.

    The project's committed state is not the place for it: an acknowledgement
    is transient, expires on its own, and amending a promoted run's record
    would rewrite tracked history for a deferral the next reader may simply
    discard. Undeferring is likewise the file's own expiry, so nothing has to
    be written back when it passes.
    """
    name = _promoted_record_project(run_id, project)
    if name is None:
        looked = (
            ", ".join(sorted(_mounted_project_roots(project))) or "no mounted project"
        )
        raise CrewError(
            f"no live run {run_id!r} and no ledger record for it (looked in: {looked})"
        )
    payload = {"run_id": run_id, "project": name, **dict(deferral)}
    path = acknowledgement_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, payload)
    return {"run_id": run_id, ACKNOWLEDGEMENT_FIELD: dict(deferral)}


def run_acknowledgement(pointer: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return one pointer's recorded deferral, or None when it has none."""
    recorded = pointer.get(ACKNOWLEDGEMENT_FIELD)
    return dict(recorded) if isinstance(recorded, Mapping) else None


STATUS_REPAIR_RECORD_NAME = "status-repair.json"
AS_DELIVERED_SUFFIX = ".asdelivered"
DEFAULT_MANIFEST_NAME = "manifest.md"
# The statuses a repaired manifest may carry: exactly the terminal vocabulary
# the classifier treats as a verdict. The set is closed because the repair
# exists to resolve a record, and a replacement outside it would leave the run
# as undecided as it was.
REPAIRABLE_STATUSES = ("complete", "blocked", "failed")


def _manifest_for_repair(run_id: str) -> Path:
    """The manifest a status repair rewrites.

    The live pointer's own manifest path comes first, because that is the file
    the classifier read and the one a dispatch customised; the run directory's
    default manifest is the fallback for a run whose pointer is gone. A run
    with no readable manifest at either path is refused naming both, so the
    caller sees what the repair looked for rather than a bare absence.
    """
    candidates: list[Path] = []
    pointer = pointer_path(run_id)
    if pointer.is_file():
        try:
            record = read_pointer(run_id)
        except CrewError:
            record = {}
        named = str(record.get("manifest_path") or "").strip()
        if named:
            candidates.append(Path(named).expanduser())
    candidates.append(run_dir(run_id) / DEFAULT_MANIFEST_NAME)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    looked = ", ".join(str(candidate) for candidate in candidates)
    raise CrewError(f"run {run_id!r} has no manifest to repair (looked at {looked})")


def _status_line_replaced(text: str, verdict: str) -> tuple[str | None, str]:
    """Rewrite the first top-level ``status:`` line, or None when absent.

    Only a line starting at column zero counts, so a nested value is never
    mistaken for the manifest's own status. The returned previous value is the
    word being replaced, kept so the repair record states what changed.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("status:"):
            continue
        previous = line.split(":", 1)[1].strip()
        lines[index] = f"status: {verdict}"
        replaced = "\n".join(lines)
        if text.endswith("\n"):
            replaced += "\n"
        return replaced, previous
    return None, ""


def repair_manifest_status(run_id: str, status: str, reason: str) -> dict[str, Any]:
    """Replace a manifest's status word with a verdict, keeping what was delivered.

    A worker that exits after committing its work but before replacing its
    in-progress status leaves a delivery its own record cannot state, and the
    manifest is the worker's own file — a hand edit loses the as-delivered
    text and leaves nothing the reader can compare against. So the status line
    becomes the verdict the coordinator read from the run, the delivered file is
    kept beside it under ``manifest.md.asdelivered``, and the reason is recorded
    in the run directory. A manifest with no top-level status line, or a status
    outside the terminal vocabulary, is refused rather than half-repaired.
    """
    verdict = str(status).strip().lower()
    if verdict not in REPAIRABLE_STATUSES:
        allowed = ", ".join(REPAIRABLE_STATUSES)
        raise CrewError(
            f"a status repair names a verdict — one of {allowed} — not {status!r}"
        )
    recorded_reason = str(reason).strip()
    if not recorded_reason:
        raise CrewError("a status repair requires a non-empty --reason")

    manifest = _manifest_for_repair(run_id)
    try:
        original = manifest.read_text(encoding="utf-8")
    except OSError as exc:
        raise CrewError(f"the manifest at {manifest} could not be read: {exc}") from exc
    rewritten, previous = _status_line_replaced(original, verdict)
    if rewritten is None:
        raise CrewError(
            f"the manifest at {manifest} carries no top-level status line to repair"
        )
    # The as-delivered copy is written before the rewrite and never overwritten,
    # so it always holds the worker's own file and a repair that repeats or
    # fails still leaves the delivery recoverable beside it.
    as_delivered = manifest.with_name(manifest.name + AS_DELIVERED_SUFFIX)
    if not as_delivered.exists():
        as_delivered.write_text(original, encoding="utf-8")
    write_atomically(manifest, lambda handle: handle.write(rewritten), fsync=False)
    record = {
        "run_id": run_id,
        "status": verdict,
        "reason": recorded_reason,
        "previous_status": previous,
        "previous_manifest": str(as_delivered),
        "manifest": str(manifest),
        "repaired_at": _utc_now(),
    }
    _write_json(run_dir(run_id) / STATUS_REPAIR_RECORD_NAME, record)
    return record


def _project_executable_remainder(project: str) -> tuple[int | None, int | None]:
    """Return a declared-scope lower bound and its uncovered plan count.

    A plan without a valid declaration cannot reduce the lower bound or make
    it unknown when another plan supplies a declared remainder.  The separate
    uncovered count makes that incomplete coverage visible to the closure
    decision.  An unreadable inventory remains entirely unknown.

    The plan inventory and every plan's declared remainder come from the
    persisted metadata index, which revalidates each row by one stat of its
    plan file and parses only the files whose stat identity moved. A drain in a
    fresh process therefore re-reads no plan whose content has not changed.
    """
    from reckon import metadata_index
    from reckon._store import _docs_dir_for_project

    docs_dir = _docs_dir_for_project(project)
    if docs_dir is None:
        return None, None

    remainders: list[int] = []
    uncovered_plans = 0
    plan_count = 0
    for plan in metadata_index.plan_derivations(docs_dir, project):
        if plan["unreadable"]:
            return None, None
        plan_count += 1
        remainder = plan["implementable_sections"]
        if remainder is None:
            uncovered_plans += 1
            continue
        remainders.append(remainder)
    if plan_count == 0:
        return None, None
    return (sum(remainders) if remainders else None), uncovered_plans


def _drain_row(pointer: Mapping[str, Any]) -> dict[str, Any]:
    """One live pointer's closure row, the drain's own classification."""
    from reckon.crew.recovery import (
        classify_pointer,
        closure_disposition_valid,
        local_liveness,
    )

    alive, proven = local_liveness(pointer)
    row = classify_pointer({**pointer, "process_alive": alive if proven else None})
    recorded = pointer.get("closure_disposition")
    disposition = (
        str(recorded.get("kind") or "") if isinstance(recorded, Mapping) else ""
    )
    valid = closure_disposition_valid(disposition, row["classification"])
    return {
        **row,
        "disposition": dict(recorded) if isinstance(recorded, Mapping) else None,
        "disposition_valid": valid,
        "unreconciled": not valid,
    }


def drain(project: str, *, session: str | None = None) -> dict[str, Any]:
    """Return the closure drain derived from one project's live pointers.

    Without ``session`` every project pointer contributes, preserving the
    established project-wide view. With it, only pointers dispatched by that
    session contribute to the closure count and peer-session rows remain
    visible separately. A handoff remains valid until the receiving session
    reconciles the pointer. ``still-working`` is narrower: it excuses only a
    pointer whose current classification remains ``running``. Any missing,
    malformed, unknown or expired disposition therefore contributes to
    ``unreconciled_runs``.
    """
    from reckon.crew.recovery import _partition_session_rows

    # ``still-working`` is a current liveness claim, so the classification
    # rechecks it rather than letting a historical ``process_alive`` field
    # keep the closure fence open after a terminal manifest arrives. The
    # recheck is the host-gated reading, and only a reading this host
    # stands behind counts: this host asks the process table only for a run
    # it launched, and a pid number it happens to hold from another host's
    # run belongs to some other process. A local probe here would hand that
    # foreign run a live reading it never earned — the same borrowed life
    # the directory row and the retained-work clause refuse — and an
    # unproven answer is carried no further than the row, because the
    # closure fence turns on whether the worker lives now rather than on
    # what a pointer's writer recorded at launch.
    rows = [_drain_row(pointer) for pointer in list_live(project=project)]

    counted, peers = _partition_session_rows(rows, session)
    unreconciled = sum(1 for row in counted if row["unreconciled"])
    executable_remainder, uncovered_plans = _project_executable_remainder(project)
    result = {
        "project": project,
        "live_pointers": len(counted),
        "disposed_runs": len(counted) - unreconciled,
        "unreconciled_runs": unreconciled,
        "executable_remainder": executable_remainder,
        "uncovered_plans": uncovered_plans,
        "drained": (
            unreconciled == 0 and executable_remainder == 0 and uncovered_plans == 0
        ),
        "dispositions": list(RUN_DRAIN_DISPOSITIONS),
        "runs": counted,
    }
    if session is not None:
        result.update(
            {
                "session": session,
                "peer_pointers": len(peers),
                "peer_runs": peers,
            }
        )
    return result


def drain_unreconciled_by_session(project: str) -> dict[str, int]:
    """The drain's unreconciled count for every session, derived once.

    A sweep needs the count for each session it publishes for, and deriving the
    rows once and partitioning them per session keeps that from repeating every
    pointer's classification for each reader. Rows with no recorded owner count
    for every session, exactly as :func:`drain` counts them.
    """
    from reckon.crew.recovery import _partition_session_rows

    rows = [_drain_row(pointer) for pointer in list_live(project=project)]
    counts: dict[str, int] = {}
    for session in {str(row.get("session") or "") for row in rows} | {""}:
        counted, _peers = _partition_session_rows(rows, session)
        counts[session] = sum(1 for row in counted if row["unreconciled"])
    return counts


# The producer's lease registration. It is separate from the seat record because
# the seat is an advisory lock held on its own file's inode for the producer's
# whole life: a follower that had to renew the lease could never take that lock,
# and a write that replaced that inode by rename would detach the seat lock the
# process table checks depend on. This file carries no lock of its own for the
# same reason a replace is safe here and not there — its inode may churn — so
# writers serialise on a sibling lock file that is never renamed.
DEFAULT_PRODUCER_LEASE_SECONDS = 600.0
PRODUCER_LEASE_ENV = "RECKON_PRODUCER_LEASE_SECONDS"


def watch_registration_path(project: str) -> Path:
    """Stable JSON registration the producer's lease is read from and written to."""
    base = watch_lock_path(project)
    return base.with_name(base.name + ".registration")


def watch_registration_lock_path(project: str) -> Path:
    """Lock serialising the two writers of one project's registration."""
    base = watch_registration_path(project)
    return base.with_name(base.name + ".lock")


def _utc_epoch_seconds() -> float:
    """Current time as epoch seconds, matching a file mtime's clock."""
    return datetime.now(tz=UTC).timestamp()


def producer_lease_seconds() -> float:
    """The effective producer lease interval, in seconds.

    ``RECKON_PRODUCER_LEASE_SECONDS`` overrides the ten-minute default so a
    test can run a lease of a few seconds. A missing, unparseable or
    non-positive override falls back to the default rather than to zero, because
    a lease of zero would end every producer on its first wake-up.
    """
    raw = os.environ.get(PRODUCER_LEASE_ENV)
    if raw is None:
        return DEFAULT_PRODUCER_LEASE_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_PRODUCER_LEASE_SECONDS
    return value if value > 0 else DEFAULT_PRODUCER_LEASE_SECONDS


def read_watch_registration(project: str) -> dict[str, Any]:
    """Read a project's lease registration, tolerating absence or a torn read."""
    try:
        value = json.loads(watch_registration_path(project).read_text() or "{}")
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def watch_lease_renewed_at(project: str) -> float | None:
    """The instant a follower last renewed the project's producer lease."""
    value = read_watch_registration(project).get("lease_renewed_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def update_watch_registration(project: str, **fields: Any) -> dict[str, Any]:
    """Read-modify-write the producer's registration, atomically.

    The registration has two writers — the producer, which records its pid and
    current poll interval, and each live follower, which records
    ``lease_renewed_at`` on every renewal. A whole-record replace by either one
    would drop the other's field, so each merges its fields into what is on
    disk. The read and the write are serialised by the sibling lock, and the
    write lands by renaming a fresh sibling over the destination, so a reader
    never observes a partial record and a reader that sees the file mid-write
    sees the whole previous record instead.
    """
    path = watch_registration_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = watch_registration_lock_path(project)
    with lock.open("a+b") as guard:
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
        try:
            record = read_watch_registration(project)
            record.setdefault("project", project)
            record.update(fields)
            write_json_atomically(path, record)
        finally:
            fcntl.flock(guard.fileno(), fcntl.LOCK_UN)
    return record


def renew_producer_lease(
    project: str, *, now: float | None = None
) -> dict[str, Any] | None:
    """A live follower's lease renewal for its project's producer.

    Called on the follower's wait pass. It is throttled to half the lease
    interval so a tight poll loop does not rewrite the shared record on every
    tick — the configured cadence for "at least once per half interval" — and
    it refuses to write when no producer is live, so a follower arming an
    already-exited producer does not resurrect its registration.
    """
    if not producer_live(project):
        return None
    moment = _utc_epoch_seconds() if now is None else float(now)
    previous = watch_lease_renewed_at(project)
    if previous is not None and moment - previous < producer_lease_seconds() / 2:
        return read_watch_registration(project)
    return update_watch_registration(project, lease_renewed_at=moment)


@dataclass
class _WatchStreamProducer:
    """In-process transition memory owned by the kernel-backed watcher seat."""

    path: Path
    known: dict[str, dict[str, Any]]
    stall_window: str
    fleet_seen: bool = False
    # Whether the last tick's read was deferred because the resolved config did
    # not load. Held so the deferral is announced once per episode rather than
    # on every retry, and cleared the moment a tick reads the config cleanly.
    tick_deferred: bool = False
    # The sweep runs off the producer's transition path: a transition is
    # written the moment it is detected, and the derivation the sessions'
    # snapshots are sliced from runs behind it. The lock keeps a trigger that
    # arrives while a sweep is in flight from starting a second one — the
    # trigger is skipped, not queued — and the thread reference is what a
    # caller waits on when it needs the published state to have settled.
    sweep_lock: threading.Lock = field(default_factory=threading.Lock)
    sweep_thread: threading.Thread | None = None


_WATCH_STREAM_PRODUCERS: dict[str, _WatchStreamProducer] = {}

# The line a producer prints when a tick's read cannot resolve the config. A
# merge that lands a layer which does not validate is a transient state of the
# file tree, so the tick is deferred and retried rather than allowed to end the
# seat; the marker is what a reader greps the producer's log for.
WATCH_TICK_DEFERRAL_MARKER = "reckon crew watch deferred its tick"


def _watch_stream_snapshots(
    records: Iterable[Mapping[str, Any]], *, stall_window: str
) -> dict[str, dict[str, Any]]:
    """Reduce live pointers to the state carried by the human ticker."""
    from reckon.crew.recovery import _utc_seconds, _watch_snapshot

    moment = _utc_seconds()
    stall_seconds = parse_duration(stall_window)
    return {
        str(record.get("run_id") or ""): _watch_snapshot(
            record, moment=moment, stall_seconds=stall_seconds
        )
        for record in records
        if record.get("run_id")
    }


def parse_stream_line(line: str) -> dict[str, Any] | None:
    """Return one stream event, tolerating a line an older producer wrote.

    The durable stream carries the transition object rather than its rendered
    line, because a rendered line cannot say which session owns the run and a
    reader that cannot answer that cannot filter to its own fleet. A producer
    holds its code until it is restarted, so lines written before the format
    changed stay readable and are passed through with their ownership unknown —
    dropping them would lose exactly the signal a follower exists to carry.
    """
    text = line.strip()
    if not text:
        return None
    try:
        event = json.loads(text)
    except (TypeError, ValueError):
        return {"legacy": True, "rendered": text, "session": None, "event": "legacy"}
    if not isinstance(event, dict):
        return {"legacy": True, "rendered": text, "session": None, "event": "legacy"}
    event.setdefault("legacy", False)
    return event


def read_stream_events(path: Path, *, offset: int = 0) -> Iterable[dict[str, Any]]:
    """Yield every event a stream holds from one byte offset onward."""
    if not Path(path).is_file():
        return
    with Path(path).open(encoding="utf-8") as stream:
        stream.seek(offset)
        for line in stream:
            event = parse_stream_line(line)
            if event is not None:
                yield event


def read_whole_line(stream) -> str:
    """Read one complete line, leaving a half-written line for a later read.

    A producer appends one JSON record per line and may still be writing the
    last one when a follow loop reads: ``readline`` then returns the bytes
    written so far, without a newline. Admitting that fragment as a whole
    record would deliver the record truncated, and advancing the handle past it
    would put the recorded offset inside a line, so the completion would be
    read as a second fragment and the record would reach a request in two
    pieces or not at all. So a line that does not end in a newline is not
    returned: the handle is left where the line began, and the next read
    returns it whole and once, after the completion lands.

    The empty string is returned both at end of file and while the only bytes
    left are an unterminated line, which is the same answer the caller acts on:
    wait, then read again.
    """
    start = stream.tell()
    line = stream.readline()
    if line and not line.endswith("\n"):
        stream.seek(start)
        return ""
    return line


def line_boundary(path: Path, *, chunk: int = 64 * 1024) -> int:
    """The byte after the last newline in a file, read backward in chunks.

    A stream's size taken while a producer is mid-append falls inside a record.
    A reader that starts there or reads to there opens or stops inside a line,
    so the boundary a reader is given must be the byte after the last newline
    at or before the size. The scan reads at most ``chunk`` bytes per step from
    the end, so finding one newline no longer costs a full-file read and a
    same-size buffer: for the ordinary case, where the last record is short, it
    reads a single chunk and stops. A file with no newline at all is scanned in
    full, which is the only answer it admits.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size <= 0:
        return 0
    try:
        with path.open("rb") as stream:
            end = size
            while end > 0:
                start = max(0, end - chunk)
                stream.seek(start)
                data = stream.read(end - start)
                newline = data.rfind(b"\n")
                if newline >= 0:
                    return start + newline + 1
                end = start
    except OSError:
        return 0
    return 0


def _append_watch_lines(path: Path, events: Iterable[Mapping[str, Any]]) -> None:
    """Durably append complete transition records without replacing history."""
    payload = "".join(
        f"{json.dumps(dict(event), sort_keys=True)}\n" for event in events
    )
    if not payload:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _stream_transition(
    project: str,
    *,
    snapshot: Mapping[str, Any],
    previous: str | None,
    current: str,
    counts: Mapping[str, int],
) -> dict[str, Any]:
    """Build the transition object consumed by the established formatter."""
    from reckon.crew.recovery import _watch_transition

    return _watch_transition(
        project,
        kind="baseline" if previous is None else "transition",
        snapshot=snapshot,
        previous=previous,
        current=current,
        counts=counts,
    )


def _publish_watch_transitions(
    project: str,
    producer: _WatchStreamProducer,
    records: Iterable[Mapping[str, Any]],
) -> bool:
    """Append each fleet state transition once for the active producer.

    The tick's own read is fallible: composing a transition prices the run
    against the resolved configuration, and a merge can leave a layer that does
    not validate under a seat that is already armed. A config error is deferred
    rather than fatal — one dim line names it, and the next tick retries — so a
    file resolved a moment later costs one fleet observation, not the producer.
    Nothing is committed until the whole tick succeeds, so a deferred tick
    neither advances the fold's memory nor drops the transitions it owes.

    Reports whether this sweep appended a transition, which is the event the
    session snapshots republish on.
    """
    from reckon.crew.recovery import _fleet_counts, fleet_transitions
    from reckon.flight import FlightConfigError

    current = _watch_stream_snapshots(records, stall_window=producer.stall_window)
    if not current and not producer.fleet_seen:
        return False

    try:
        if not producer.fleet_seen:
            baseline = {run_id: dict(snapshot) for run_id, snapshot in current.items()}
            counts = _fleet_counts(current)
            lines = [
                _stream_transition(
                    project,
                    snapshot=snapshot,
                    previous=None,
                    current=str(snapshot["state"]),
                    counts=counts,
                )
                for snapshot in current.values()
            ]
            _append_watch_lines(producer.path, lines)
            producer.fleet_seen = True
            producer.known = baseline
            producer.tick_deferred = False
            return True

        # The same fold the seat's own ticker uses, so a follower reading the
        # stream and a reader watching the seat's stdout cannot disagree about
        # either the transitions or their counts.
        folded, next_known = fleet_transitions(producer.known, current)
        lines = [
            _stream_transition(
                project,
                snapshot=snapshot,
                previous=previous,
                current=state,
                counts=event_counts,
            )
            for snapshot, previous, state, event_counts in folded
        ]
        _append_watch_lines(producer.path, lines)
        producer.known = next_known
        producer.tick_deferred = False
        return bool(lines)
    except FlightConfigError as exc:
        if producer.tick_deferred:
            return False
        producer.tick_deferred = True
        _announce_watch_tick_deferral(exc)
        return False


_SWEEP_LOCAL = threading.local()


def _publish_watch_stream(project: str, records: Iterable[Mapping[str, Any]]) -> None:
    """Fold this sweep's fleet state, then publish the sessions' snapshots.

    A sweep publishes the obligations it derives from the same live pointers,
    and that derivation reads them through :func:`list_live` — one of this
    function's own callers — so a call made from inside a running sweep is left
    to that sweep rather than re-entered. The transition write never waits on
    the derivation: the sweep runs in its own thread, one at a time, and a
    trigger arriving while one is in flight is skipped.
    """
    if getattr(_SWEEP_LOCAL, "in_sweep", False):
        return
    producer = _WATCH_STREAM_PRODUCERS.get(project)
    if producer is None:
        return
    transition_fired = _publish_watch_transitions(project, producer, records)
    if not producer.sweep_lock.acquire(blocking=False):
        return
    producer.sweep_thread = threading.Thread(
        target=_run_obligation_sweep,
        args=(project, producer, transition_fired),
        name=f"reckon-obligations-sweep-{project}",
        daemon=True,
    )
    producer.sweep_thread.start()


def _run_obligation_sweep(
    project: str, producer: _WatchStreamProducer, transition_fired: bool
) -> None:
    """Run one obligations sweep on the thread its trigger started."""
    _SWEEP_LOCAL.in_sweep = True
    try:
        _publish_obligation_snapshots(project, transition_fired=transition_fired)
    finally:
        _SWEEP_LOCAL.in_sweep = False
        producer.sweep_thread = None
        producer.sweep_lock.release()


# The bound on a sweep a caller joins. A sweep runs off the producer's
# transition path and the producer never waits on it, so a caller that owns the
# in-process registry can wait here; the bound keeps a sweep wedged on an
# unresponsive input from holding the caller open, and a thread still alive
# past it is reported rather than waited on forever.
WATCH_SWEEP_JOIN_SECONDS = 10.0


def join_watch_sweeps(timeout: float = WATCH_SWEEP_JOIN_SECONDS) -> list[str]:
    """Join every registered producer's in-flight sweep, then clear the registry.

    The sweep runs on a daemon thread per transition, off the producer's own
    path, and nothing joins that thread: a sweep can still be running after
    the seat that started it is gone, and a caller running afterwards — the
    next test in a suite, which may have replaced the modules the sweep reads
    — then sees an unhandled exception from a thread it never started. A
    caller that owns the in-process registry waits for those threads here and
    drops the registry, so no later trigger starts another sweep for a
    producer the caller has ended.

    Returns the names of the threads still running when the bound expired.
    """
    stragglers: list[str] = []
    for producer in list(_WATCH_STREAM_PRODUCERS.values()):
        thread = producer.sweep_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                stragglers.append(thread.name)
    _WATCH_STREAM_PRODUCERS.clear()
    return stragglers


def _producer_snapshot_identity(project: str) -> dict[str, Any]:
    """The producer's pid, start time and code stamp, read from its registration.

    The lease registration is the only place a producer's identity is kept: it
    is written when the seat is taken, so a reader of a session's snapshot can
    say which process published it and which source that process runs. A
    registration written before the stamp existed, or one left by a producer
    superseded by this one, carries a stamp other than the one this process
    runs, so the stamp is recorded from the running source rather than assumed.
    """
    registration = read_watch_registration(project)
    stamp = follower_code_stamp()
    if registration.get("code_stamp") != stamp:
        registration = update_watch_registration(project, code_stamp=stamp)
    return {
        "pid": registration.get("pid"),
        "pid_start_time": registration.get("pid_start_time"),
        "started_at": registration.get("started_at"),
        "code_stamp": registration.get("code_stamp"),
    }


def _publish_obligation_snapshots(project: str, *, transition_fired: bool) -> list[str]:
    """Republish every live session's obligations snapshot for one sweep.

    The sessions are the ones currently delivering from a follower registration
    in the project's watch directory — the same registration the hook resolves
    its own session through. A registration keeps its file after its follower
    goes, so publishing for every registration the project has ever had would
    spend the sweep deriving for sessions nobody is coordinating under; a
    session that never registered a follower gets no snapshot at all.

    The triggers live with the snapshot module; this is the per-project sweep
    the producer already runs, so its clock and its stat reads are the ones the
    floor tick and the file-identity trigger are judged against.
    """
    from reckon.crew import obligation_snapshot
    from reckon.flight import FlightConfigError

    sessions = [
        str(row.get("session") or "")
        for row in list_followers(project)
        # Only a registration something is delivering from is a session with
        # duties to publish: a released registration keeps its file for a later
        # re-arm, and deriving for every registration the project has ever had
        # would spend the sweep on sessions nobody is coordinating under.
        if row.get("live")
    ]
    docs = _docs_dir_for_project(project)
    try:
        written = obligation_snapshot.sweep(
            project,
            sessions=[session for session in sessions if session],
            producer=_producer_snapshot_identity(project),
            stream_offset=line_boundary(watch_stream_path(project)),
            transition_fired=transition_fired,
            state_dirs=[docs / "state" / project, docs / "plans"] if docs else [],
        )
    except FlightConfigError as exc:
        # A tick whose config does not load has no fleet state to derive from,
        # so it defers exactly as the stream transition fold does: the snapshots
        # already at rest age out of freshness until a later tick resolves the
        # config, and the seat stays up meanwhile.
        producer = _WATCH_STREAM_PRODUCERS.get(project)
        if producer is not None and not producer.tick_deferred:
            producer.tick_deferred = True
            _announce_watch_tick_deferral(exc)
        return []
    return [str(path) for path in written]


def _announce_watch_tick_deferral(exc: Exception) -> None:
    """Say once, in the producer's own log, that a tick was deferred.

    The producer's stdout is redirected to its log when it takes the seat, so a
    line printed here lands where a reader looks for the seat's last words. It
    is dimmed only on a terminal; a redirected log carries the bare text, which
    is what a reader greps for.
    """
    line = (
        f"{WATCH_TICK_DEFERRAL_MARKER}: the resolved config does not load "
        f"({exc}); keeping the current image, retrying on the next tick"
    )
    stream = sys.stdout
    if stream.isatty():
        line = f"\x1b[2m{line}\x1b[0m"
    print(line, file=stream, flush=True)


def watch_stream_cursor(
    project: str, *, stall_window: str = DEFAULT_WATCH_STALL_WINDOW
) -> dict[str, Any]:
    """Return a current fleet baseline and the byte offset for future lines."""
    from reckon.crew.recovery import _fleet_counts

    producer = _WATCH_STREAM_PRODUCERS.get(project)
    effective_window = producer.stall_window if producer is not None else stall_window
    snapshots = _watch_stream_snapshots(
        _list_live_records(project=project), stall_window=effective_window
    )
    counts = _fleet_counts(snapshots)
    baseline = [
        _stream_transition(
            project,
            snapshot=snapshot,
            previous=None,
            current=str(snapshot["state"]),
            counts=counts,
        )
        for snapshot in snapshots.values()
    ]
    path = watch_stream_path(project)
    # A reader that starts at the raw size opens inside a record the producer
    # is still writing, and the fragment it then reads is delivered as a whole
    # record while the completion arrives as a second one. The offset a reader
    # is handed is therefore the byte after the last newline, so it opens at
    # the start of any unterminated line and reads that record whole and once
    # when its newline lands.
    offset = line_boundary(path)
    return {
        "stream_path": str(path),
        "offset": offset,
        "baseline": baseline,
        "producer": watch_producer_identity(project),
    }


def watch_producer_identity(project: str) -> dict[str, Any]:
    """Describe which code an armed seat is running, for the first line a
    follower reads on attach.

    A watcher imports its detection module once at startup and runs for
    hours, so a later fix is inert on a seat armed before it landed and
    nothing distinguishes that seat from a current one. This is the fact
    that answers it: the version the seat started with, plus when it
    started, sourced from the same record :func:`_project_watch_claim`
    writes rather than a separate probe that could disagree with it.
    """
    path = watch_lock_path(project)
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        record = _read_watch_record(handle)
    if not record:
        return {}
    version = record.get("reckon_version")
    started_at = record.get("started_at")
    code_stamp = record.get("code_stamp")
    # A seat that records no stamp predates the field, so it is reported as
    # stale rather than as current: absence is the older producer, which is the
    # case a reader most needs to distinguish.
    current_stamp = follower_code_stamp()
    stale = code_stamp != current_stamp
    detail = f"reckon {version or 'unknown'} started {started_at or 'unknown'}" + (
        ", code stale" if stale else ""
    )
    return {
        "reckon_version": version,
        "started_at": started_at,
        "code_stamp": code_stamp,
        "reload_started_at": record.get("reload_started_at"),
        "log_path": record.get("log_path"),
        # Both sides of the comparison, so a caller that has to say which code
        # the seat is behind does not recompute one of them.
        "current_stamp": current_stamp,
        "stale": stale,
        "line": detail,
    }


def watch_seat_version_current(project: str) -> bool:
    """Report whether an armed seat's recorded version matches the installed one.

    This names the *install* and not the code. ``__version__`` is read from the
    installed distribution's metadata, written once when the package was
    installed, so every seat this install arms records one string and every
    process it runs compares against that same string — whichever revision of
    these files each is executing. A fix that lands in the checkout after a seat
    was armed therefore leaves this answer True, which is the case the stamp was
    introduced to catch and cannot. Measured on one workstation install: the
    stamp stayed put while eighty-five commits reached the package, this module
    among them. Only a reinstall under a live seat moves it.

    Absence stays absence: a seat with no recorded version predates the stamp
    and is treated as stale rather than as current, exactly like a seat whose
    recorded version differs from what is installed now.
    """
    path = watch_lock_path(project)
    if not path.is_file():
        return False
    with path.open("rb") as handle:
        record = _read_watch_record(handle)
    recorded = record.get("reckon_version")
    return recorded is not None and recorded == __version__


def watch_seat_needs_replacement(project: str) -> bool:
    """Report whether a held seat should be replaced rather than reused.

    Two independent conditions make a seat untrustworthy: its supervisor died
    (``observer_alive`` is False), or it is running code other than what is
    installed now. Either is sufficient on its own, so this folds them into one
    answer without collapsing the death-of-supervisor signal into the version
    one — a caller that wants to know why can still read
    :func:`project_watch_visibility` and :func:`watch_seat_version_current`
    separately.

    Only the first half carries information today. The version half is a
    constant for any seat this install armed, for the reason
    :func:`watch_seat_version_current` records, so this currently answers the
    dead-supervisor question alone.
    """
    visibility = project_watch_visibility(project)
    if not visibility["seat_held"]:
        return False
    if visibility["observer_alive"] is False:
        return True
    return not watch_seat_version_current(project)


def replace_stale_watch_seat(project: str) -> dict[str, Any] | None:
    """Clear a seat judged stale, through the one existing teardown path.

    Returns the :func:`~reckon.crew.recovery.unwatch` result when a
    replacement happened, else ``None`` when the seat is current and nothing
    was touched.

    Nothing calls it. The arming path clears a dead-supervisor seat with its own
    ``observer_alive`` check and a direct ``unwatch``, so routing that case
    through here would be a refactor rather than a repair; the case that would
    be a repair — a version-stale seat — cannot arise, because the stamp
    :func:`watch_seat_version_current` compares names the install and not the
    code. A caller wired on today would gate on a condition that never becomes
    true. What has to reach this first is a staleness signal that moves when the
    code does: the seat's ``started_at``, which the record already carries,
    against the moment the module a watcher imports last changed.
    """
    if not watch_seat_needs_replacement(project):
        return None
    from reckon.crew.recovery import unwatch

    return unwatch(project)


def _seat_host_is_local(record: Mapping[str, Any]) -> bool:
    """Report whether a seat record names this host as its producer's host."""
    return _seat_host(record) == socket.gethostname()


def _erase_confirmed_dead_seat(path: Path, stale: Mapping[str, Any]) -> None:
    """Erase a seat record only if it still names the process just confirmed dead.

    Lock-free and best-effort: nothing here takes the seat lock, so a concurrent
    arming or teardown may already have replaced the file between the read that
    confirmed death and this write. Re-reading and comparing against what was
    read guards that race — when the content has moved on, this leaves the
    newer state alone rather than clobbering it. Writes the same empty payload
    an explicit :func:`~reckon.crew.recovery.unwatch` writes, so the two
    erasure paths can never leave the record disagreeing with each other.
    """
    try:
        with path.open("r+b") as handle:
            if _read_watch_record(handle) != dict(stale):
                return
            _write_watch_record(handle, {})
    except OSError:
        return


def producer_live(project: str) -> bool:
    """Report whether a project's stream is being written, without locking it.

    A reader must never need an exclusive lock to observe. Probing the seat with
    one makes an observer able to deny an arming for the microseconds it holds
    it, which is a producer that fails to start because something looked at it.
    The registered pid, paired with its start time so a recycled pid cannot
    impersonate it, answers the same question.

    A record confirmed dead — its pid is gone, or alive under a start time that
    disagrees with what was registered, the recycled-pid case — is erased here
    rather than merely reported, so the next reader finds no record instead of
    the same stale one. A live record is left untouched.

    Only a record that names *this* host is judged that way. A pid is issued by
    one kernel, so a record naming another host carries a number this host cannot
    answer for, and it is judged by the transition stream instead — the one piece
    of evidence that crosses the shared home. An erased record names no pid at
    all, and is judged the same way. Measured 2026-09-25: a producer on compute
    node 98dci4-clu-2058 held its seat and kept writing transitions while a read
    on another host, unable to find the pid locally, erased the record to ``{}``.
    Delivery and admission both follow that record, so the fleet went unwatched
    for about three hours while the stream grew.

    A record naming *no* host is a third case, and the narrowest: it is read
    against the local table, because every seat this install writes now carries a
    host and a hostless one is a legacy or hand-written record rather than proof
    of a foreign producer. But its death is confirmed only when the stream is
    quiet too, so a pid alone never erases it.

    Deliberately *not* the orphan check that :func:`watch_state` applies. A
    producer whose supervisor died is reparented to init and stops satisfying
    the dispatch guard, because nothing is listening to the seat it holds — but
    it is still appending real transitions to the stream, and a follower that
    refuses to read them waits forever on data that is arriving. Measured: an
    orphaned producer with 51 KB of stream and a live run left its session's
    pane empty for four minutes. Admission and readability are different
    questions about the same process.
    """
    lease = watch_host_lease(project)
    holder = lease.holder()
    if holder is not None and holder.host != socket.gethostname():
        return True
    path = watch_lock_path(project)
    if not path.is_file():
        if holder is not None and process_alive(holder.pid) is False:
            lease.release_holder(holder)
        return False
    with path.open("rb") as handle:
        record = _read_watch_record(handle)
    if _seat_names_a_foreign_host(record) or not record:
        if holder is not None and process_alive(holder.pid) is False:
            lease.release_holder(holder)
        # A foreign or erased seat names a pid this host cannot judge — another
        # kernel's, or none at all. Delivery must follow the stream rather than
        # such a record, so a stream that moved within the stall window reads
        # live and the record is left exactly as it was found.
        return _seat_stream_fresh(project, record)
    alive = record_process_alive(record) is True
    if alive:
        return True
    # The pid is gone from this host's table. For a record that names no host,
    # the stream is a second witness before the death is acted on: while it is
    # moving, something is still producing transitions, and the seat is left for
    # the host that issued its pid to clear. A record naming *this* host needs no
    # second witness: this host can see the process it names, and its absence is
    # the whole answer.
    if not _seat_host_is_local(record) and _seat_stream_fresh(project, record):
        return True
    _erase_confirmed_dead_seat(path, record)
    if holder is not None and (record.get("host"), record.get("pid")) == (
        holder.host,
        holder.pid,
    ):
        lease.release_holder(holder)
    return False


def _record_producer_dead(
    record: Mapping[str, Any], *, project: str | None = None
) -> bool:
    """Report whether a seat record names a process confirmed dead *here*.

    A record naming another host is never dead from here: the local process
    table cannot see the process it names, so its absence says nothing about it.
    A record naming no host is judged by pid, but confirmed dead only when its
    stream is quiet too, so a pid alone never erases it. Reconciliation and
    erasure both gate on this, so neither can clear a live producer's seat from
    a host that did not issue its pid.
    """
    if _seat_names_a_foreign_host(record):
        return False
    pid = record.get("pid")
    if not (
        bool(pid) and record_process_alive(record, match_start_time=False) is not True
    ):
        return False
    if _seat_host_is_local(record):
        return True
    return not _stream_says_alive(record, project)


def _reconcile_watch_record(project: str, record: Mapping[str, Any]) -> bool:
    """Repair a stale seat record in place, without ever blocking the seat lock.

    A record whose registered process is gone disagrees with the process table,
    and reporting the disagreement without removing it leaves the next reader to
    find the same lie. This clears the registration only when it is provably
    free: the registered process is dead (a seat lock is auto-released when its
    holder dies) and a non-blocking probe confirms nothing re-armed it in the
    interim. It never takes a blocking exclusive lock, so observing still cannot
    deny an arming. Returns True when the record was rewritten.
    """
    if not _record_producer_dead(record, project=project):
        return False
    path = watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # A live producer claimed the seat since this record was read.
            return False
        _write_watch_record(handle, {})
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True


# A seat is held for the life of its watcher, so a lock that is unavailable for
# only a moment is a passing read-only probe rather than an occupied seat.
_CLAIM_CONTENTION_SECONDS = 0.5

# The seat is an open advisory lock, and an in-place process replacement
# (``os.execve``) closes every non-inheritable descriptor — so the lock would be released
# and a fresh image would re-enter the arming race, where a peer's arming can take
# the seat between the release and the re-acquire and leave the replacement to
# report ``watcher-live`` and exit. The held handle is carried to the
# replacement instead: ``prepare_watch_seat_reexec`` makes its descriptor
# inheritable and names it, and ``_take_watch_seat_fd`` adopts it in the new
# image so the seat is never released.
_WATCH_SEAT_ENV = "RECKON_WATCH_SEAT_FD"
_WATCH_SEAT_HANDLES: dict[str, Any] = {}
_WATCH_HOST_LEASES: dict[str, Any] = {}


def renew_watch_host_lease(project: str) -> bool:
    """Keep the producer's claim fresh; stop when another host owns it."""
    lease = _WATCH_HOST_LEASES.get(project)
    return lease is not None and lease.renew()


def prepare_watch_seat_reexec(project: str) -> int | None:
    """Make a held seat survive replacement of the process image, or None.

    The descriptor is named in the environment the replacement is given, never
    in this image's ``os.environ``, so a child the producer starts inherits no
    handle to the seat.
    """
    handle = _WATCH_SEAT_HANDLES.get(project)
    if handle is None:
        return None
    record = _read_watch_record(handle)
    record["reload_started_at"] = _utc_now()
    _write_watch_record(handle, record)
    fd = handle.fileno()
    os.set_inheritable(fd, True)
    return fd


def cancel_watch_seat_reexec(project: str) -> None:
    """Undo descriptor inheritance when process replacement was refused."""
    handle = _WATCH_SEAT_HANDLES.get(project)
    if handle is not None:
        os.set_inheritable(handle.fileno(), False)
        record = _read_watch_record(handle)
        record.pop("reload_started_at", None)
        _write_watch_record(handle, record)


def _take_watch_seat_fd() -> Any:
    """Consume the seat descriptor handed across an in-place reload, or None."""
    raw = os.environ.pop(_WATCH_SEAT_ENV, "")
    if not raw:
        return None
    try:
        fd = int(raw)
    except (TypeError, ValueError):
        return None
    try:
        handle = os.fdopen(fd, "a+b")
        os.set_inheritable(handle.fileno(), False)
    except OSError:
        return None
    return handle


@contextmanager
def _project_watch_claim(project: str, stall_window: str):
    """Claim the local seat path, then the lease shared between hosts."""
    path = watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    if project in _WATCH_HOST_LEASES:
        with path.open("a+b") as occupied:
            yield False, _read_watch_record(occupied)
        return
    inherited = _take_watch_seat_fd()
    handle = inherited if inherited is not None else path.open("a+b")
    lease = None
    try:
        if inherited is None:
            deadline = time.monotonic() + _CLAIM_CONTENTION_SECONDS
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        yield False, _read_watch_record(handle)
                        return
                    time.sleep(0.01)

        lease = watch_host_lease(project)
        holder = lease.holder()
        if (
            inherited is None
            and holder is not None
            and holder.host == socket.gethostname()
        ):
            # This path's lock is ours. A local predecessor can still hold an
            # unlinked inode, but it no longer owns the seat a new arm opens.
            lease.release_holder(holder)
        if not lease.claim():
            yield False, _read_watch_record(handle)
            return
        previous = _read_watch_record(handle)

        # Adopt the log the arming named before the record is written, so a
        # reader who finds this seat dead finds the producer's own last words
        # beside it rather than a path that was named and never written.
        _adopt_watch_log()
        # The start time in the record belongs to the process that wrote it, so
        # it travels only across this producer's own image replacement. A
        # replacement keeps the pid and the process start time, so those two
        # plus the host name name one process; anything else sitting in the file
        # is a predecessor whose seat was never erased, and a fresh arming over
        # it would otherwise report the predecessor's start -- reading as a
        # producer that has run for hours when it was armed seconds ago.
        pid_start_time = _process_start_time(os.getpid())
        carried = (
            previous.get("pid"),
            previous.get("pid_start_time"),
            previous.get("host"),
        ) == (os.getpid(), pid_start_time, socket.gethostname())
        record = {
            "project": project,
            "pid": os.getpid(),
            "pid_start_time": pid_start_time,
            # The host that issued this pid. The seat lives on the shared home
            # every fleet node mounts, so without this a reader on another host
            # probes its own process table for a pid that belongs to someone
            # else and confirms a running producer dead.
            "host": socket.gethostname(),
            "stall_window": stall_window,
            "started_at": previous.get("started_at") if carried else _utc_now(),
            "stream_path": str(watch_stream_path(project)),
            # Where this producer's stdout and stderr go, so a dead seat can be
            # dated and explained from the file rather than only observed empty.
            "log_path": str(watch_log_path(project)),
            "reckon_version": __version__,
            # The code this producer is actually executing. The version names
            # the install and holds still while commits land in the checkout, so
            # a reader cannot tell a producer running stale code from a current
            # one without this: the content-hash stamp the follower reloads on,
            # recorded where the seat is read.
            "code_stamp": follower_code_stamp(),
        }
        # Which unit owns this seat, so a reader can tell a watcher a service
        # will replace from one nothing will. The unit exports its own name, and
        # a watcher started by any other route leaves the key absent rather than
        # claiming a service that does not exist.
        unit = previous.get("unit") or os.environ.get(WATCH_UNIT_ENV)
        if unit:
            record["unit"] = str(unit)
        _write_watch_record(handle, record)
        if inherited is not None:
            print(
                "reckon crew watch completed its reload; publishing continues",
                flush=True,
            )
        # The lease registration names this producer for as long as it lives and
        # starts its lease clock at the instant it took the seat: a producer
        # nobody renews therefore expires one lease interval from now, while a
        # follower's renewal pushes that instant forward.
        update_watch_registration(
            project,
            pid=os.getpid(),
            pid_start_time=pid_start_time,
            host=socket.gethostname(),
            started_at=record.get("started_at"),
            lease_renewed_at=_utc_epoch_seconds(),
        )
        producer = _WatchStreamProducer(
            path=watch_stream_path(project),
            known={},
            stall_window=stall_window,
        )
        _WATCH_STREAM_PRODUCERS[project] = producer
        _WATCH_SEAT_HANDLES[project] = handle
        _WATCH_HOST_LEASES[project] = lease
        try:
            _publish_watch_stream(project, _list_live_records(project=project))
            yield True, record
        finally:
            _WATCH_SEAT_HANDLES.pop(project, None)
            _WATCH_HOST_LEASES.pop(project, None)
            _WATCH_STREAM_PRODUCERS.pop(project, None)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
        if lease is not None:
            lease.release()


def watch_observer_alive(registration: Mapping[str, Any]) -> bool | None:
    """Check the registered supervisor without walking runs or follower pipes.

    A registration naming another host answers nothing about a process this host
    cannot see: the parent pid was issued by a kernel elsewhere, so its absence
    from the local table is not evidence of death. Returning None — unknown, not
    dead — keeps an arming on one host from stopping a live producer on another.
    """
    if _seat_names_a_foreign_host(registration):
        return None
    if "parent_pid" not in registration:
        return None
    try:
        parent_pid = int(registration.get("parent_pid") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        parent_pid > 1
        and process_alive(parent_pid) is True
        and _process_start_time(parent_pid) == registration.get("parent_start_time")
    )


def project_watch_visibility(
    project: str, *, session: str | None = None
) -> dict[str, Any]:
    """Describe whether a project's pointers have a live watcher and reader."""
    arming_line = _watch_arming_line(project)
    attach_line = _watch_attach_line(project, session=session)
    path = watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            seat_held = True
            registration = _read_watch_record(handle)
        else:
            seat_held = False
            registration = _read_watch_record(handle)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    holder = _fresh_watch_holder(project, registration)
    if holder is not None:
        seat_held = True
        if (registration.get("host"), registration.get("pid")) != (
            holder.host,
            holder.pid,
        ):
            registration = {"project": project}
        registration.update(host=holder.host, pid=holder.pid, job=holder.job)

    # Reconcile-on-read: a registration whose process is gone is a disagreement
    # between the registry and the machine, and reading it without repairing it
    # leaves the next reader to find the same lie. Repair in place — never on a
    # blocking lock, so observing still cannot deny an arming — and report the
    # repaired state (an empty registration) rather than the stale one.
    if (
        registration
        and (holder is None or holder.host == socket.gethostname())
        and _record_producer_dead(registration, project=project)
        and _reconcile_watch_record(project, registration)
    ):
        registration = {}

    pid = registration.get("pid")
    # The seat-aware judgement, not a bare pid probe: a record naming another
    # host is answered by its stream, which is the only liveness this host can
    # observe for a producer it cannot see.
    registering_process_alive = (
        _record_producer_running(registration, project=project)
        if registration
        else None
    )

    observer_alive = watch_observer_alive(registration)

    pointer_count = len(list_live(project=project))
    # Liveness follows the process, not the seat: a running producer is live
    # whether or not the seat lock reads as held, and a dead one is absent
    # whether or not a stale record claims otherwise.
    watcher_live = bool(
        registering_process_alive is True and observer_alive is not False
    )
    followers = list_followers(project)
    delivering = [row for row in followers if row["live"]]
    delivery = follower_state(project, session) if session is not None else None
    watcher_required = pointer_count > 0
    unwatched = watcher_required and not watcher_live
    if unwatched:
        status = "unwatched"
    elif watcher_live:
        status = "watched"
    else:
        status = "idle"
    return {
        "project": project,
        "status": status,
        "seat_held": seat_held,
        "pid": pid,
        "armed_at": registration.get("started_at"),
        # The code the armed seat is running, and whether it differs from this
        # reader's. A reader of the live view needs the second fact and not the
        # first: the producer runs the image it was armed with, so a fix landed
        # since then is inert on that seat, and nothing else in this block
        # distinguishes it from a current one. A record with no stamp predates
        # the field, so it reads stale -- absence is the older producer. No
        # record at all is no producer, which is not stale but absent.
        "code_stamp": registration.get("code_stamp"),
        "code_stale": bool(registration)
        and registration.get("code_stamp") != follower_code_stamp(),
        "process_alive": registering_process_alive,
        "observer_alive": observer_alive,
        "watcher_live": watcher_live,
        "watcher_required": watcher_required,
        "unwatched": unwatched,
        "pointer_count": pointer_count,
        # A seat with no reader is the state that reads as healthy and is not:
        # the producer runs, the guard passes, and every transition it writes
        # is read by nobody.
        # The pids are here because "whose follower is this" was otherwise only
        # answerable with `ps`: a peer reported one as an orphan to be reaped
        # after confirming it was not theirs, and it belonged to a live session
        # reading it. A follower's owner is whatever consumes its stdout, and
        # `consumer_pid` names that process.
        # A registration file is never unlinked when its lock is released, so
        # the directory holds one entry per session name that has ever followed
        # the project. Only the registrations that deliver are rows: a released
        # registration is not a reader, and listing it handed a reader a row to
        # add and a `not_live_because` sentence to decode before it could ask
        # whether the project is covered. The released remainder is stated as a
        # count instead, which is the fact without the rows.
        "followers": [
            {
                "session": row["session"],
                "delivery": row["delivery"],
                "pid": row["follower"].get("pid"),
                "consumer_pid": row["follower"].get("parent_pid"),
                "since": row["follower"].get("started_at"),
            }
            for row in delivering
        ],
        # Both counts are stated rather than left to be derived from the array's
        # length: the array holds nothing but delivering rows, so its length is
        # already followers_live, and the released figure has no row to live in.
        # A released count of zero is a measurement, so the key is always present.
        "followers_live": len(delivering),
        "followers_released": len(followers) - len(delivering),
        "delivering_sessions": sorted(row["session"] for row in delivering),
        "session": session,
        "session_attached": None if delivery is None else bool(delivery["live"]),
        "arming_line": arming_line,
        "attach_line": attach_line,
        "stream_path": str(watch_stream_path(project)),
    }


def _stream_quiet_seconds(record: Mapping[str, Any], *, now_seconds: float) -> int:
    """Measure quiet time from a stream, with pointer activity as the fallback."""
    stream = Path(str(record.get("log_path") or ""))
    if stream.is_file():
        latest = stream.stat().st_mtime
    else:
        run_id = str(record.get("run_id") or "")
        pointer = pointer_path(run_id) if run_id else Path()
        if run_id and pointer.is_file():
            latest = pointer.stat().st_mtime
        else:
            created = parse_utc(str(record.get("created_at") or ""))
            latest = now_seconds if created is None else created.timestamp()
    return max(0, int(now_seconds - latest))


def _watch_event(project: str, *, stall_seconds: int) -> dict[str, Any] | None:
    from reckon.crew.recovery import _utc_seconds, classify_pointer

    """Return the first terminal or stalled pointer, or the empty-fleet event."""
    pointers = list_live(project=project)
    if not pointers:
        return {
            "project": project,
            "event": "empty",
            "run_id": None,
            "classification": "no_live_pointers",
            "next_action": f"none — project {project!r} has no live pointers",
        }

    moment = _utc_seconds()
    classified = [
        (pointer, classify_pointer(pointer, now_seconds=moment)) for pointer in pointers
    ]
    for pointer, row in classified:
        manifest_status = row.get("manifest_status")
        # A resumed attempt owns evidence newer than the baseline captured at
        # resumption. Completion is never an early placeholder, so the
        # one-event watcher may finish as soon as that attempt writes it even
        # during the short interval before its process exits. Failed and
        # blocked reports remain deferred while the process lives because
        # workers can pre-arm those pessimistic statuses before doing work.
        if (
            manifest_status == "complete"
            or manifest_status in {"blocked", "failed"}
            or (
                pointer.get("attempt_kind") == "resume"
                and row.get("manifest_fresh") is True
                and row.get("manifest_reported_status") == "complete"
            )
        ):
            return {
                "project": project,
                "event": "terminal",
                **row,
                "manifest_status": manifest_status or "complete",
            }

    for pointer, row in classified:
        quiet = _stream_quiet_seconds(pointer, now_seconds=moment)
        if quiet > stall_seconds:
            return {
                "project": project,
                "event": "stalled",
                **row,
                "stalled_for_seconds": quiet,
            }
    return None


def watch(
    project: str,
    *,
    stall_window: str = DEFAULT_WATCH_STALL_WINDOW,
    exit_on_empty: bool = False,
    poll_interval: float = 1.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Block for a fleet event, optionally treating an empty fleet as a drain."""
    # The single-event arm takes the same seat as the streaming watcher, so it
    # owes the same refusal: a seat held by a watcher that cannot resolve a
    # backend it may be asked to lift reads as armed while it can lift nothing.
    from reckon import flight
    from reckon.crew.dispatch import assert_routable_backends_resolvable

    assert_routable_backends_resolvable(project, flight.resolve(project=project).config)
    stall_seconds = parse_duration(stall_window)
    with _project_watch_claim(project, stall_window) as (acquired, watcher):
        if not acquired:
            return {
                "project": project,
                "event": "watcher-live",
                "run_id": None,
                "classification": "watcher_live",
                "next_action": "wait for the live project watcher to report",
                "watcher_live": True,
                "watcher": watcher,
                "stream_path": str(watch_stream_path(project)),
            }
        while True:
            if not renew_watch_host_lease(project):
                return {
                    "project": project,
                    "event": "seat-lost",
                    "watcher_live": False,
                }
            event = _watch_event(project, stall_seconds=stall_seconds)
            if event is not None and (event["event"] != "empty" or exit_on_empty):
                return event
            remaining = poll_interval
            while remaining > 0:
                step = min(remaining, LEASE_RENEW_SECONDS)
                sleeper(step)
                if not renew_watch_host_lease(project):
                    return {
                        "project": project,
                        "event": "seat-lost",
                        "watcher_live": False,
                    }
                remaining -= step
            if poll_interval <= 0:
                sleeper(poll_interval)


def _pointer_claims_worktree(record: Mapping[str, Any]) -> bool:
    """Return whether a pointer must keep its worktree untouched."""
    phase = str(record.get("phase") or "")
    if phase in _TERMINAL_RUN_PHASES:
        return False
    if phase:
        return True
    return record_process_alive(record) is not False


def _live_worktree_claims() -> dict[Path, list[str]]:
    claims: dict[Path, list[str]] = {}
    for record in list_live():
        worktree = record.get("worktree")
        if not worktree or not _pointer_claims_worktree(record):
            continue
        path = Path(str(worktree)).resolve()
        claims.setdefault(path, []).append(str(record.get("run_id") or "unknown"))
    return claims


from .follower_registration import (  # noqa: E402
    DELIVERING_MODES as DELIVERING_MODES,
    FOLLOWER_FRESHNESS_SECONDS as FOLLOWER_FRESHNESS_SECONDS,
    FOLLOWER_REGISTRY_STALE_SECONDS as FOLLOWER_REGISTRY_STALE_SECONDS,
    _DELIVERY_TRACE_CACHE as _DELIVERY_TRACE_CACHE,
    _DELIVERY_TRACE_TTL_SECONDS as _DELIVERY_TRACE_TTL_SECONDS,
    _FOLLOWER_OWNER_ENV as _FOLLOWER_OWNER_ENV,
    _FOLLOWER_REGISTRATION_ENV as _FOLLOWER_REGISTRATION_ENV,
    _FollowerOwnerCache as _FollowerOwnerCache,
    _FollowerRegistration as _FollowerRegistration,
    _REGISTRATION_SETTLE_SECONDS as _REGISTRATION_SETTLE_SECONDS,
    _RESOLVED_FOLLOWER_OWNER as _RESOLVED_FOLLOWER_OWNER,
    _descriptor_kind as _descriptor_kind,
    _follower_liveness as _follower_liveness,
    _follower_source_digests as _follower_source_digests,
    _format_follower_owner as _format_follower_owner,
    _parse_follower_owner as _parse_follower_owner,
    _pipe_reader_pids as _pipe_reader_pids,
    _source_content_digest as _source_content_digest,
    _trace_delivery as _trace_delivery,
    delivery_mode as delivery_mode,
    delivery_mode_of as delivery_mode_of,
    follower_claim as follower_claim,
    follower_code_stamp as follower_code_stamp,
    follower_dir as follower_dir,
    follower_lock_path as follower_lock_path,
    follower_owner as follower_owner,
    follower_registration as follower_registration,
    follower_state as follower_state,
    list_followers as list_followers,
    sweep_released_followers as sweep_released_followers,
)


from .process_liveness import (  # noqa: E402
    _JOB_LIVE_STATES as _JOB_LIVE_STATES,
    _JOB_STATE_PLACEHOLDER as _JOB_STATE_PLACEHOLDER,
    _SCHEDULER_KILL_CLASSES as _SCHEDULER_KILL_CLASSES,
    _SCHEDULER_QUERY_TIMEOUT_SECONDS as _SCHEDULER_QUERY_TIMEOUT_SECONDS,
    _ask_scheduler as _ask_scheduler,
    _process_start_time as _process_start_time,
    _process_stat_fields as _process_stat_fields,
    _process_state as _process_state,
    _run_scheduler_query as _run_scheduler_query,
    _scheduler_query_argv as _scheduler_query_argv,
    _scheduler_state_argv as _scheduler_state_argv,
    placement_job_alive as placement_job_alive,
    process_alive as process_alive,
    record_process_alive as record_process_alive,
    scheduler_job_reason as scheduler_job_reason,
    scheduler_job_state as scheduler_job_state,
    scheduler_kill_class as scheduler_kill_class,
)


from .run_paths import (  # noqa: E402
    _read_watch_record as _read_watch_record,
    _utc_now as _utc_now,
    _write_watch_record as _write_watch_record,
    crew_home as crew_home,
    watch_lock_path as watch_lock_path,
    watch_stream_path as watch_stream_path,
)


from .watch_unit import (  # noqa: E402
    LINGER_IF_REQUIRED as LINGER_IF_REQUIRED,
    WATCH_ATTENTION_STATES as WATCH_ATTENTION_STATES,
    WATCH_LOG_ENV as WATCH_LOG_ENV,
    WATCH_LOG_MAX_BYTES as WATCH_LOG_MAX_BYTES,
    WATCH_PROGRESS_STATES as WATCH_PROGRESS_STATES,
    WATCH_UNIT_ENV as WATCH_UNIT_ENV,
    WATCH_UNIT_TEMPLATE as WATCH_UNIT_TEMPLATE,
    _LINGER_FALLBACK_CAUSE as _LINGER_FALLBACK_CAUSE,
    _SERVICE_UNREACHABLE_CAUSE as _SERVICE_UNREACHABLE_CAUSE,
    _WatchLogStream as _WatchLogStream,
    _adopt_watch_log as _adopt_watch_log,
    _arm_watcher_as_process as _arm_watcher_as_process,
    _fresh_watch_holder as _fresh_watch_holder,
    _reckon_console_script as _reckon_console_script,
    _record_producer_running as _record_producer_running,
    _register_watch_unit as _register_watch_unit,
    _seat_host as _seat_host,
    _seat_names_a_foreign_host as _seat_names_a_foreign_host,
    _seat_project as _seat_project,
    _seat_stream_fresh as _seat_stream_fresh,
    _service_manager_unreachable as _service_manager_unreachable,
    _stream_says_alive as _stream_says_alive,
    _watch_arming_line as _watch_arming_line,
    _watch_attach_line as _watch_attach_line,
    _watcher_armed_as_process as _watcher_armed_as_process,
    _watcher_search_path as _watcher_search_path,
    _watcher_service_environment as _watcher_service_environment,
    ensure_placement_reservation as ensure_placement_reservation,
    ensure_watcher_service as ensure_watcher_service,
    placement_ensure_line as placement_ensure_line,
    render_watch_unit as render_watch_unit,
    watch_cycle_line as watch_cycle_line,
    watch_host_lease as watch_host_lease,
    watch_log_path as watch_log_path,
    watch_state as watch_state,
    watch_unit_name as watch_unit_name,
    watcher_ensure_line as watcher_ensure_line,
)
