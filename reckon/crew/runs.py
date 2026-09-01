from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon._store import _config_home
from reckon.crew.node import (
    _TERMINAL_RUN_PHASES,
    DEFAULT_WATCH_STALL_WINDOW,
    RUN_DRAIN_DISPOSITIONS,
    CrewError,
    ScopeConflict,
    TaskNode,
    parse_duration,
)

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


def watch_stream_path(project: str) -> Path:
    """Stable append-only transition stream for one project's watcher."""
    return watch_lock_path(project).with_suffix(".events")


def follower_dir(project: str) -> Path:
    """Directory holding one registration per session consuming the ticker."""
    return watch_lock_path(project).with_suffix(".followers")


def follower_lock_path(project: str, session: str) -> Path:
    """Stable advisory-lock path for one session's delivery registration."""
    readable = re.sub(r"[^A-Za-z0-9._-]", "-", session).strip("-") or "session"
    digest = hashlib.sha256(session.encode()).hexdigest()[:12]
    return follower_dir(project) / f"{readable}-{digest}.lock"


def _pipe_reader_pids(inode: int, *, exclude: int) -> list[int]:
    """Return the pids holding the other end of one pipe, by its inode."""
    target = f"pipe:[{inode}]"
    readers: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == exclude:
            continue
        try:
            descriptors = list((entry / "fd").iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                if os.readlink(descriptor) == target:
                    readers.append(pid)
                    break
            except OSError:
                continue
    return readers


def _descriptor_kind(mode: int) -> str:
    if stat.S_ISFIFO(mode):
        return "pipe"
    if stat.S_ISSOCK(mode):
        return "stream"
    if stat.S_ISCHR(mode):
        return "terminal"
    if stat.S_ISREG(mode):
        return "file"
    return "unknown"


def _trace_delivery(info: os.stat_result, *, pid: int, hops: int) -> str:
    """Follow one output descriptor to whatever finally consumes its lines."""
    seen: set[int] = set()
    for _hop in range(hops):
        kind = _descriptor_kind(info.st_mode)
        if kind != "pipe":
            return kind
        if info.st_ino in seen:
            return "unknown"
        seen.add(info.st_ino)
        readers = _pipe_reader_pids(info.st_ino, exclude=pid)
        if not readers:
            # Nothing holds the read end: the lines have nowhere to go at all.
            return "file"
        pid = readers[0]
        try:
            # stat rather than open: opening a FIFO can block, and a probe that
            # blocks is a worse failure than the one being detected.
            info = os.stat(f"/proc/{pid}/fd/1")
        except OSError:
            # A reader whose own output cannot be inspected is credited as a
            # reader: refusing on an unknown would refuse the ordinary case.
            return "stream"
    return "stream"


def delivery_mode(descriptor: int = 1, *, hops: int = 4) -> str:
    """Classify what will actually consume this process's lines.

    A follower is only a wake-up if something reads its lines as they are
    written. A socket or terminal has a reader doing exactly that; a regular
    file is read by whoever opens it later, which for a command that never
    exits is nobody.

    A pipe answers neither way by itself, and that is the case that matters: a
    filter between the follower and a file looks like a live consumer at the
    first hop while the chain still ends in a file nothing reads. So the pipe
    is followed to the process on its other end and the question is asked
    again of *that* process's output. The verdict belongs to the end of the
    chain, because that is where the lines stop.
    """
    try:
        info = os.fstat(descriptor)
    except OSError:
        return "unknown"
    return _trace_delivery(info, pid=os.getpid(), hops=hops)


def delivery_mode_of(pid: int, *, hops: int = 4) -> str | None:
    """Classify what consumes another process's output, or None if unreadable.

    Read live rather than trusted from the registration, so a follower is
    judged by where its lines go *now*. A recorded verdict is a snapshot: it
    survives the consumer at the end of the chain going away, and it answers
    with whatever the check understood on the day it was written.
    """
    try:
        info = os.stat(f"/proc/{pid}/fd/1")
    except OSError:
        return None
    return _trace_delivery(info, pid=pid, hops=hops)


# Descriptor kinds whose reader sees a line when it is written. Anything else
# holds the ticker until the command exits, and a follower does not exit.
DELIVERING_MODES = ("stream", "terminal")


def _utc_now() -> str:
    return (
        datetime.now(tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def new_run_id(node_id: str, *, now: datetime | None = None) -> str:
    """Mint a filesystem-safe run id that sorts by dispatch time."""
    stamp = (now or datetime.now(tz=UTC)).strftime("%Y%m%dT%H%M%S%f")
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
        previous = read_pointer(run_id)
        previous_attempt = int(previous.get("attempt") or 1)
        record = mutation(previous)
        current_attempt = int(record.get("attempt") or 1)
        if current_attempt > previous_attempt:
            record["attempt_budget_seconds"] = _attempt_budget_seconds(
                run_id, record
            )
        _write_json(pointer_path(run_id), record)
        return record


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
    """Return matching live pointers, newest run id last."""
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


@dataclass
class _WatchStreamProducer:
    """In-process transition memory owned by the kernel-backed watcher seat."""

    path: Path
    known: dict[str, dict[str, Any]]
    stall_window: str
    fleet_seen: bool = False


_WATCH_STREAM_PRODUCERS: dict[str, _WatchStreamProducer] = {}


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


def read_stream_events(
    path: Path, *, offset: int = 0
) -> Iterable[dict[str, Any]]:
    """Yield every event a stream holds from one byte offset onward."""
    if not Path(path).is_file():
        return
    with Path(path).open(encoding="utf-8") as stream:
        stream.seek(offset)
        for line in stream:
            event = parse_stream_line(line)
            if event is not None:
                yield event


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


def _publish_watch_stream(project: str, records: Iterable[Mapping[str, Any]]) -> None:
    """Append each fleet state transition once for the active producer."""
    producer = _WATCH_STREAM_PRODUCERS.get(project)
    if producer is None:
        return

    from reckon.crew.recovery import _fleet_counts

    current = _watch_stream_snapshots(records, stall_window=producer.stall_window)
    if not current and not producer.fleet_seen:
        return

    counts = _fleet_counts(current)
    if not producer.fleet_seen:
        producer.fleet_seen = True
        producer.known = {
            run_id: dict(snapshot) for run_id, snapshot in current.items()
        }
        _append_watch_lines(
            producer.path,
            (
                _stream_transition(
                    project,
                    snapshot=snapshot,
                    previous=None,
                    current=str(snapshot["state"]),
                    counts=counts,
                )
                for snapshot in current.values()
            ),
        )
        return

    events: list[dict[str, Any]] = []
    next_known = {run_id: dict(snapshot) for run_id, snapshot in producer.known.items()}
    for run_id in (item for item in producer.known if item not in current):
        snapshot = producer.known[run_id]
        next_known.pop(run_id, None)
        events.append(
            _stream_transition(
                project,
                snapshot=snapshot,
                previous=str(snapshot["state"]),
                current="promoted",
                counts=counts,
            )
        )

    for run_id in (item for item in current if item not in producer.known):
        snapshot = current[run_id]
        dispatched = {**snapshot, "state": "dispatched", "reason": ""}
        next_known[run_id] = dispatched
        events.append(
            _stream_transition(
                project,
                snapshot=dispatched,
                previous=None,
                current="dispatched",
                counts=counts,
            )
        )

    for run_id in (item for item in current if item in producer.known):
        snapshot = current[run_id]
        previous = str(producer.known[run_id]["state"])
        current_state = str(snapshot["state"])
        if current_state == previous:
            continue
        next_known[run_id] = dict(snapshot)
        events.append(
            _stream_transition(
                project,
                snapshot=snapshot,
                previous=previous,
                current=current_state,
                counts=counts,
            )
        )

    producer.known = next_known
    _append_watch_lines(producer.path, events)


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
    try:
        offset = path.stat().st_size
    except FileNotFoundError:
        offset = 0
    return {"stream_path": str(path), "offset": offset, "baseline": baseline}


def producer_live(project: str) -> bool:
    """Report whether a project's watcher seat is held, without locking it.

    A reader must never need an exclusive lock to observe. Probing the seat
    with one makes an observer able to deny an arming for the microseconds it
    holds it, which is a producer that fails to start because something looked
    at it. The registered pid, paired with its start time so a recycled pid
    cannot impersonate it, answers the same question and touches nothing.
    """
    path = watch_lock_path(project)
    if not path.is_file():
        return False
    with path.open("rb") as handle:
        record = _read_watch_record(handle)
    pid = record.get("pid")
    if process_alive(pid) is not True:
        return False
    expected = record.get("pid_start_time")
    if expected is not None and _process_start_time(pid) != expected:
        return False
    if "parent_pid" in record:
        try:
            parent_pid = int(record.get("parent_pid") or 0)
        except (TypeError, ValueError):
            return False
        if parent_pid <= 1 or process_alive(parent_pid) is not True:
            return False
        if _process_start_time(parent_pid) != record.get("parent_start_time"):
            return False
    return True


# A seat is held for the life of its watcher, so a lock that is unavailable for
# only a moment is a passing read-only probe rather than an occupied seat.
_CLAIM_CONTENTION_SECONDS = 0.5


@contextmanager
def _project_watch_claim(project: str, stall_window: str):
    """Claim the one kernel-tracked watcher seat for a project, if free."""
    path = watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
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

        record = {
            "project": project,
            "pid": os.getpid(),
            "pid_start_time": _process_start_time(os.getpid()),
            "stall_window": stall_window,
            "started_at": _utc_now(),
            "stream_path": str(watch_stream_path(project)),
        }
        _write_watch_record(handle, record)
        producer = _WatchStreamProducer(
            path=watch_stream_path(project),
            known={},
            stall_window=stall_window,
        )
        _WATCH_STREAM_PRODUCERS[project] = producer
        _publish_watch_stream(project, _list_live_records(project=project))
        try:
            yield True, record
        finally:
            _WATCH_STREAM_PRODUCERS.pop(project, None)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def follower_claim(
    project: str,
    session: str,
    *,
    delivery: str | None = None,
    scope: Mapping[str, Any] | None = None,
):
    """Register that one session is consuming this project's ticker.

    The seat proves a producer exists. This proves a *reader* exists, for a
    named session, which is the only fact a dispatch guard can act on: a seat
    is project-global while the wake-up it feeds is session-local, so a caller
    dispatching against a peer's seat is told a watcher is live and still hears
    nothing.

    The registration is an advisory lock held by the live follower, so it is
    released by the process ending however it ends — the same property that
    keeps the seat honest.
    """
    resolved = delivery or delivery_mode()
    path = follower_lock_path(project, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False, _read_watch_record(handle)
            return
        parent = os.getppid()
        record = {
            "project": project,
            "session": session,
            "pid": os.getpid(),
            "pid_start_time": _process_start_time(os.getpid()),
            "parent_pid": parent,
            "parent_start_time": _process_start_time(parent),
            "delivery": resolved,
            "scope": dict(scope or {}),
            "started_at": _utc_now(),
        }
        _write_watch_record(handle, record)
        try:
            yield True, record
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            path.unlink(missing_ok=True)


def _follower_liveness(path: Path) -> dict[str, Any]:
    """Read one registration and decide whether it still delivers."""
    if not path.is_file():
        return {"registered": False, "live": False, "delivery": None, "follower": {}}
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            registered = True
            record = _read_watch_record(handle)
        else:
            registered = False
            record = _read_watch_record(handle)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    pid = record.get("pid")
    running = process_alive(pid) is True
    expected = record.get("pid_start_time")
    if expected is not None:
        running = running and _process_start_time(pid) == expected

    # An orphaned follower has lost the session it was reporting to, so its
    # lines go nowhere even while the process runs.
    consumer_alive = True
    if "parent_pid" in record:
        try:
            parent_pid = int(record.get("parent_pid") or 0)
        except (TypeError, ValueError):
            parent_pid = 0
        consumer_alive = parent_pid > 1 and process_alive(parent_pid) is True

    # Prefer what the descriptor says now over what registration recorded: a
    # recorded verdict survives its consumer going away, and answers with
    # whatever the check understood when it was written. The one reader that
    # keeps the declaration is the registering process itself, where the
    # declaration is a statement about its own output and deceives nobody
    # else; a dispatch guard is always a different process, which is the case
    # this measures.
    observed = (
        delivery_mode_of(pid)
        if isinstance(pid, int) and running and pid != os.getpid()
        else None
    )
    delivery = observed or str(record.get("delivery") or "unknown")
    return {
        "registered": registered,
        "live": bool(
            registered and running and consumer_alive and delivery in DELIVERING_MODES
        ),
        "delivery": delivery,
        "delivery_recorded": str(record.get("delivery") or "unknown"),
        "delivery_observed": observed,
        "consumer_alive": consumer_alive,
        "follower": record,
    }


def follower_state(project: str, session: str) -> dict[str, Any]:
    """Report whether one session will be woken by this project's ticker."""
    state = _follower_liveness(follower_lock_path(project, session))
    return {
        "project": project,
        "session": session,
        "attach_line": _watch_attach_line(project, session=session),
        **state,
    }


def list_followers(project: str) -> list[dict[str, Any]]:
    """List every registered consumer of one project's ticker."""
    directory = follower_dir(project)
    if not directory.is_dir():
        return []
    rows = []
    for path in sorted(directory.glob("*.lock")):
        state = _follower_liveness(path)
        session = str(state["follower"].get("session") or path.stem)
        rows.append({"project": project, "session": session, **state})
    return rows


def watch_state(project: str, *, session: str | None = None) -> dict[str, Any]:
    """Return the paste-ready arming line and kernel-backed watcher liveness."""
    arming_line = _watch_arming_line(project)
    attach_line = _watch_attach_line(project, session=session)
    path = watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Delivery is reported beside the seat because reading one without the
    # other is how "a watcher is live" came to mean "I will be told".
    delivery = (
        follower_state(project, session) if session is not None else None
    )
    attached = None if delivery is None else bool(delivery["live"])
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {
                "arming_line": arming_line,
                "attach_line": attach_line,
                "watcher_live": True,
                "watcher": _read_watch_record(handle),
                "session": session,
                "session_attached": attached,
                "follower": {} if delivery is None else delivery["follower"],
            }
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return {
        "arming_line": arming_line,
        "attach_line": attach_line,
        "watcher_live": False,
        "watcher": {},
        "session": session,
        "session_attached": attached,
        "follower": {} if delivery is None else delivery["follower"],
    }


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

    pid = registration.get("pid")
    expected_start = registration.get("pid_start_time")
    actual_start = _process_start_time(pid)
    registering_process_alive = process_alive(pid)
    if expected_start is not None:
        registering_process_alive = bool(
            registering_process_alive is True and actual_start == expected_start
        )

    observer_alive: bool | None = None
    if "parent_pid" in registration:
        parent_pid = registration.get("parent_pid")
        try:
            parent_pid_value = int(parent_pid)
        except (TypeError, ValueError):
            parent_pid_value = 0
        observer_alive = bool(
            process_alive(parent_pid) is True
            and _process_start_time(parent_pid)
            == registration.get("parent_start_time")
            and parent_pid_value > 1
        )

    pointer_count = len(list_live(project=project))
    watcher_live = bool(
        seat_held
        and registering_process_alive is True
        and observer_alive is not False
    )
    followers = list_followers(project)
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
        "process_alive": registering_process_alive,
        "observer_alive": observer_alive,
        "watcher_live": watcher_live,
        "watcher_required": watcher_required,
        "unwatched": unwatched,
        "pointer_count": pointer_count,
        # A seat with no reader is the state that reads as healthy and is not:
        # the producer runs, the guard passes, and every transition it writes
        # is read by nobody.
        "followers": [
            {
                "session": row["session"],
                "live": row["live"],
                "delivery": row["delivery"],
            }
            for row in followers
        ],
        "delivering_sessions": sorted(
            row["session"] for row in followers if row["live"]
        ),
        "session": session,
        "session_attached": None if delivery is None else bool(delivery["live"]),
        "arming_line": arming_line,
        "attach_line": attach_line,
        "stream_path": str(watch_stream_path(project)),
    }


# Every state `_watch_snapshot` can emit, split by whether a coordinator has to
# act on it. A ticker filter built from the first set wakes a session for work it
# did not think to name; the second set is progress and would only add noise.
WATCH_ATTENTION_STATES = (
    "complete",
    "blocked",
    "failed",
    "stalled",
    "stopped",
    "abandoned",
    "completed_unpromoted",
    "unknown",
)
WATCH_PROGRESS_STATES = ("dispatched", "working", "running", "promoted")


def _watch_arming_line(project: str) -> str:
    """Return the exact shell-safe command a dispatch payload carries."""
    return f"reckon crew watch --project {shlex.quote(project)}"


def _watch_attach_line(project: str, *, session: str | None = None) -> str:
    """Return the follower one session arms to be woken about its own runs.

    A seat existing is not the same as this session hearing about it: the seat
    is project-global and wake delivery is session-local, so a caller
    dispatching against another session's seat is told a watcher is live while
    nothing reaches it. This is the command that closes that gap.

    It is one bare command on purpose: filtering and buffering belong inside
    the follower, because a shell pipeline around it has three ways to swallow
    the ticker silently. An unbuffered stage withholds every line until the
    command exits, and this command does not exit. An unanchored pattern
    matches the summary field that trails each line, so it matches everything.
    And a trailing ``|| true`` turns the follower's own refusal into a silent
    success indistinguishable from a quiet fleet.
    """
    parts = ["reckon", "crew", "follow", "--project", shlex.quote(project)]
    if session:
        parts += ["--session", shlex.quote(session)]
    parts.append("--attention")
    return " ".join(parts)


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
                created = datetime.fromisoformat(str(record.get("created_at") or ""))
            except ValueError:
                latest = now_seconds
            else:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
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
                "stream_path": str(watch_stream_path(project)),
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
