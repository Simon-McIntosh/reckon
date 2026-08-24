from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from reckon._store import _config_home
from reckon.crew.node import CrewError, DEFAULT_WATCH_STALL_WINDOW, RUN_DRAIN_DISPOSITIONS, ScopeConflict, TaskNode, _TERMINAL_RUN_PHASES, parse_duration

# ── Run records ─────────────────────────────────────────────────────────────


def crew_home() -> Path:
    """Directory holding transient run state — never committed."""
    return _config_home() / "crew"


def live_dir() -> Path:
    """Directory of live pointers, one JSON file per in-flight run."""
    return crew_home() / "live"


def runs_dir() -> Path:
    """Directory holding durable per-run delivery and event artifacts."""
    return crew_home() / "runs"


def reports_dir() -> Path:
    """Directory holding durable reports that are not tied to one run."""
    return crew_home() / "reports"


def run_dir(run_id: str) -> Path:
    """Directory holding one run's prompt, event log and default manifest."""
    return runs_dir() / run_id


def pointer_path(run_id: str) -> Path:
    """Path of one run's live pointer."""
    return live_dir() / f"{run_id}.json"


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


def watch_lock_path(project: str) -> Path:
    """Stable advisory-lock path for one project's fleet watcher."""
    readable = re.sub(r"[^A-Za-z0-9._-]", "-", project).strip("-") or "project"
    digest = hashlib.sha256(project.encode()).hexdigest()[:12]
    return crew_home() / "watch" / f"{readable}-{digest}.lock"


def _utc_now() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def new_run_id(node_id: str, *, now: datetime | None = None) -> str:
    """Mint a filesystem-safe run id that sorts by dispatch time."""
    stamp = (now or datetime.now(tz=timezone.utc)).strftime("%Y%m%dT%H%M%S%f")
    token = re.sub(r"[^A-Za-z0-9._-]", "-", node_id).strip("-") or "node"
    return f"r-{stamp}-{token}"


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON atomically, so a reader never sees a half-written record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
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
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


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
        record = mutation(read_pointer(run_id))
        _write_json(pointer_path(run_id), record)
        return record


def read_pointer(run_id: str) -> dict[str, Any]:
    """Read one run's live pointer, or say which run is unknown."""
    path = pointer_path(run_id)
    if not path.exists():
        raise CrewError(f"no live run {run_id!r} (looked in {path})")
    try:
        data = json.loads(path.read_text())
    except ValueError as exc:
        raise CrewError(f"live pointer for {run_id!r} is not valid JSON — {exc}")
    if not isinstance(data, dict):
        raise CrewError(f"live pointer for {run_id!r} does not hold an object")
    return data


def list_live(
    *, project: str | None = None, phase: str | None = None
) -> list[dict[str, Any]]:
    """Return matching live pointers, newest run id last."""
    directory = live_dir()
    if not directory.is_dir():
        return []
    records = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text())
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


@dataclass(frozen=True)
class _LiveScopeClaim:
    """One normalized repository-relative path held by a live pointer."""

    run_id: str
    node_id: str
    path: str
    declared_path: str
    derived_from: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the stable read-model representation of this claim."""
        claim = {
            "path": self.path,
            "run_id": self.run_id,
            "node": self.node_id,
            "declared_path": self.declared_path,
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
        claim.as_dict()
        for claim in _live_scope_claims(project, repo_root, derivations)
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
                neighbor
                for neighbor in adjacency[current]
                if neighbor not in component
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
    }


def _project_derivations(project: str, repo: Path) -> dict[str, list[str]]:
    """Read the repository derivation map from its project resource."""
    from reckon._store import read_plan

    index, _version = read_plan(project, "index", repo)
    projects = index.get("projects") or []
    if not projects or not isinstance(projects[0], Mapping):
        return {}
    derivations = projects[0].get("derivations") or {}
    return {
        str(source): [str(output) for output in outputs]
        for source, outputs in derivations.items()
    }


def _raise_live_scope_conflict(
    node: TaskNode,
    claims: Iterable[_LiveScopeClaim],
    repo: Path,
    derivations: Mapping[str, Iterable[str]] | None = None,
) -> None:
    """Refuse the first deterministic collision with an existing live claim."""
    candidates = [
        path
        for path, _declared, _derived_from in _expanded_scope_paths(
            node.write_paths, repo, derivations
        )
    ]
    for candidate in candidates:
        for claim in claims:
            if _scopes_overlap(candidate, claim.path):
                raise ScopeConflict(
                    run_id=claim.run_id,
                    node_id=claim.node_id,
                    candidate_path=candidate,
                    claimed_path=claim.path,
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
) -> dict[str, Any]:
    """Record why one live pointer may remain across session closure."""
    reason = str(disposition).strip()
    if reason not in RUN_DRAIN_DISPOSITIONS:
        allowed = ", ".join(RUN_DRAIN_DISPOSITIONS)
        raise CrewError(
            f"run disposition {disposition!r} is not one of {allowed}"
        )

    def record(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer_project = str(pointer.get("project") or "")
        if project is not None and pointer_project != project:
            raise CrewError(
                f"live run {run_id!r} belongs to project {pointer_project!r}, "
                f"not {project!r}"
            )
        pointer["closure_disposition"] = {
            "kind": reason,
            "recorded_at": _utc_now(),
        }
        return pointer

    return _mutate_pointer(run_id, record)


def drain(project: str) -> dict[str, Any]:
    from reckon.crew.recovery import classify_pointer

    """Return the closure drain derived from one project's live pointers.

    A handoff remains valid until the receiving session reconciles the pointer.
    ``still-working`` is narrower: it excuses only a pointer whose current
    classification remains ``running``. Any missing, malformed, unknown or
    expired disposition therefore contributes to ``unreconciled_runs``.
    """
    rows: list[dict[str, Any]] = []
    for pointer in list_live(project=project):
        row = classify_pointer(pointer)
        recorded = pointer.get("closure_disposition")
        disposition = (
            str(recorded.get("kind") or "")
            if isinstance(recorded, Mapping)
            else ""
        )
        valid = disposition == "handed-off" or (
            disposition == "still-working" and row["classification"] == "running"
        )
        rows.append(
            {
                **row,
                "disposition": dict(recorded)
                if isinstance(recorded, Mapping)
                else None,
                "disposition_valid": valid,
                "unreconciled": not valid,
            }
        )

    unreconciled = sum(1 for row in rows if row["unreconciled"])
    return {
        "project": project,
        "live_pointers": len(rows),
        "disposed_runs": len(rows) - unreconciled,
        "unreconciled_runs": unreconciled,
        "dispositions": list(RUN_DRAIN_DISPOSITIONS),
        "runs": rows,
    }


def _read_watch_record(handle) -> dict[str, Any]:
    """Read watcher metadata while preserving the handle's advisory lock."""
    handle.seek(0)
    try:
        value = json.loads(handle.read().decode() or "{}")
    except (UnicodeDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_watch_record(handle, record: Mapping[str, Any]) -> None:
    """Replace watcher metadata without replacing the inode carrying its lock."""
    payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    handle.seek(0)
    handle.truncate()
    handle.write(payload)
    handle.flush()
    os.fsync(handle.fileno())


@contextmanager
def _project_watch_claim(project: str, stall_window: str):
    """Claim the one kernel-tracked watcher seat for a project, if free."""
    path = watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False, _read_watch_record(handle)
            return

        record = {
            "project": project,
            "pid": os.getpid(),
            "pid_start_time": _process_start_time(os.getpid()),
            "stall_window": stall_window,
            "started_at": _utc_now(),
        }
        _write_watch_record(handle, record)
        try:
            yield True, record
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def watch_state(project: str) -> dict[str, Any]:
    """Return the paste-ready arming line and kernel-backed watcher liveness."""
    arming_line = _watch_arming_line(project)
    path = watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {
                "arming_line": arming_line,
                "watcher_live": True,
                "watcher": _read_watch_record(handle),
            }
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return {"arming_line": arming_line, "watcher_live": False, "watcher": {}}


def _watch_arming_line(project: str) -> str:
    """Return the exact shell-safe command a dispatch payload carries."""
    return f"reckon crew watch --project {shlex.quote(project)}"


def _stream_quiet_seconds(
    record: Mapping[str, Any], *, now_seconds: float
) -> int:
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
            try:
                created = datetime.fromisoformat(
                    str(record.get("created_at") or "").replace("Z", "+00:00")
                )
            except ValueError:
                latest = now_seconds
            else:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                latest = created.timestamp()
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
        (pointer, classify_pointer(pointer, now_seconds=moment))
        for pointer in pointers
    ]
    for _pointer, row in classified:
        if row.get("manifest_status") in {"complete", "blocked", "failed"}:
            return {"project": project, "event": "terminal", **row}

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
            }
        while True:
            event = _watch_event(project, stall_seconds=stall_seconds)
            if event is not None and (event["event"] != "empty" or exit_on_empty):
                return event
            sleeper(poll_interval)


def _pointer_claims_worktree(record: Mapping[str, Any]) -> bool:
    """Return whether a pointer must keep its worktree untouched."""
    phase = str(record.get("phase") or "")
    if phase in _TERMINAL_RUN_PHASES:
        return False
    if phase:
        return True
    return process_alive(record.get("pid")) is not False


def _live_worktree_claims() -> dict[Path, list[str]]:
    claims: dict[Path, list[str]] = {}
    for record in list_live():
        worktree = record.get("worktree")
        if not worktree or not _pointer_claims_worktree(record):
            continue
        path = Path(str(worktree)).resolve()
        claims.setdefault(path, []).append(str(record.get("run_id") or "unknown"))
    return claims


def process_alive(pid: Any) -> bool | None:
    """Report whether a pid is still running; None when there is no pid.

    A dead process with no terminal event in its log is a recoverable orphan
    rather than a completed run, which is why liveness is recorded beside the
    stream rather than inferred from it.
    """
    if not pid:
        return None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except (TypeError, ValueError):
        return None
    return True


def _process_start_time(pid: Any) -> str | None:
    """Read the kernel start tick that distinguishes reused process ids."""
    try:
        value = int(pid)
        stat = Path(f"/proc/{value}/stat").read_text()
    except (OSError, TypeError, ValueError):
        return None
    fields = stat[stat.rfind(")") + 2 :].split()
    return fields[19] if len(fields) > 19 else None
