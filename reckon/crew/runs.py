from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import __version__
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


def follower_code_stamp() -> str:
    """Return a stamp that advances when code used by a follower changes."""
    package_dir = Path(__file__).resolve().parent.parent
    sources = [package_dir / "cli.py", *sorted((package_dir / "crew").glob("*.py"))]
    stamp = hashlib.sha256()
    for source in sources:
        try:
            metadata = source.stat()
        except OSError:
            continue
        stamp.update(str(source.relative_to(package_dir)).encode())
        stamp.update(f":{metadata.st_mtime_ns}:{metadata.st_size}\n".encode())
    return stamp.hexdigest()


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


# The pipe-chain walk scans every process's descriptors — 211 ms on a host with
# 1663 of them — so a repeated reader must not pay it repeatedly. Keyed on the
# process identity rather than the pid alone, and expiring, so a recycled pid
# and a genuinely changed descriptor are both noticed.
_DELIVERY_TRACE_TTL_SECONDS = 5.0
_DELIVERY_TRACE_CACHE: dict[tuple[int, str], tuple[float, str]] = {}


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
    kind = _descriptor_kind(info.st_mode)
    if kind != "pipe":
        # The cheap answer, and the common one: no scan is needed to see that a
        # descriptor is a socket, a terminal or a file.
        return kind
    identity = (int(pid), str(_process_start_time(pid) or ""))
    cached = _DELIVERY_TRACE_CACHE.get(identity)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _DELIVERY_TRACE_TTL_SECONDS:
        return cached[1]
    resolved = _trace_delivery(info, pid=pid, hops=hops)
    _DELIVERY_TRACE_CACHE[identity] = (now, resolved)
    if len(_DELIVERY_TRACE_CACHE) > 256:
        for key, (stamp, _) in list(_DELIVERY_TRACE_CACHE.items()):
            if now - stamp >= _DELIVERY_TRACE_TTL_SECONDS:
                _DELIVERY_TRACE_CACHE.pop(key, None)
    return resolved


# Descriptor kinds whose reader sees a line when it is written. Anything else
# holds the ticker until the command exits, and a follower does not exit.
DELIVERING_MODES = ("stream", "terminal")


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_run_id(node_id: str, *, now: datetime | None = None) -> str:
    """Mint a filesystem-safe run id that sorts by dispatch time."""
    stamp = (now or datetime.now(tz=UTC)).strftime("%Y%m%dT%H%M%S%f")
    token = re.sub(r"[^A-Za-z0-9._-]", "-", node_id).strip("-") or "node"
    return f"r-{stamp}-{token}"


def _stamp_pointer_launch_host(
    path: Path, payload: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Record a live pointer's launching host on its first write.

    A process id is meaningful only on the machine that issued it, and the
    crew configuration home is shared across login nodes, so a run records
    where it was created under the key the classifier reads (``launcher_host``),
    spelled with ``socket.gethostname()`` on both sides so the writer and the
    reader cannot disagree. The host is a property of where the process was
    created and never changes while it lives, so it is stamped exactly once,
    when the pointer file is first created. A pointer that predates this
    change has no recoverable launching host, so rewriting one never invents
    the rewriter's host on a file that already exists.
    """
    if "launcher_host" in payload:
        return payload
    if path.parent != live_dir() or path.exists():
        return payload
    host = socket.gethostname()
    if isinstance(payload, dict):
        payload["launcher_host"] = host
        return payload
    return {**payload, "launcher_host": host}


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON atomically, so a reader never sees a half-written record."""
    payload = _stamp_pointer_launch_host(path, payload)
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
            record["attempt_budget_seconds"] = _attempt_budget_seconds(run_id, record)
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
    for record in records:
        if not record.get("pid"):
            continue
        # Return the re-derived fact without mutating the pointer. A read must
        # not report a worker as live merely because the last observer did,
        # while all consumers of this one snapshot must see the same answer.
        # The accessor carries the reuse check now, so the pid-start-time
        # comparison is not repeated here.
        record["process_alive"] = record_process_alive(record)
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
    }


def _project_derivations(project: str, repo: Path) -> dict[str, list[str]]:
    """Read the repository derivation map from its project resource."""
    from reckon.project_state import read_project_derivations

    docs_dir = repo / "docs"
    if not docs_dir.is_dir():
        return {}
    return read_project_derivations(docs_dir, project)


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


def _project_executable_remainder(project: str) -> tuple[int | None, int | None]:
    """Return a declared-scope lower bound and its uncovered plan count.

    A plan without a valid declaration cannot reduce the lower bound or make
    it unknown when another plan supplies a declared remainder.  The separate
    uncovered count makes that incomplete coverage visible to the closure
    decision.  An unreadable inventory remains entirely unknown.
    """
    from reckon import _plan_html
    from reckon._schema import plan_executable_remainder
    from reckon._store import _docs_dir_for_project
    from reckon.resources import resource_map

    docs_dir = _docs_dir_for_project(project)
    if docs_dir is None:
        return None, None

    remainders: list[int] = []
    uncovered_plans = 0
    plan_count = 0
    for resource in resource_map(
        docs_dir, project, include_archived=False, ignore_invalid=True
    ).values():
        if resource.type != "plan":
            continue
        plan_count += 1
        try:
            state = _plan_html.read_state(resource.path.read_text(encoding="utf-8"))
        except OSError:
            return None, None
        remainder = plan_executable_remainder(state)
        if remainder is None:
            uncovered_plans += 1
            continue
        remainders.append(remainder)

    if plan_count == 0:
        return None, None
    return (sum(remainders) if remainders else None), uncovered_plans


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
    from reckon.crew.recovery import (
        _partition_session_rows,
        classify_pointer,
        closure_disposition_valid,
    )

    rows: list[dict[str, Any]] = []
    for pointer in list_live(project=project):
        # ``still-working`` is a current liveness claim. Recheck it rather
        # than letting a historical ``process_alive`` field keep the closure
        # fence open after a terminal manifest arrives. A pointer with no pid
        # has no process-table evidence and therefore cannot use that stored
        # boolean to outrank delivery on disk.
        current = {**pointer, "process_alive": record_process_alive(pointer)}
        row = classify_pointer(current)
        recorded = pointer.get("closure_disposition")
        disposition = (
            str(recorded.get("kind") or "") if isinstance(recorded, Mapping) else ""
        )
        valid = closure_disposition_valid(disposition, row["classification"])
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

    from reckon.crew.recovery import _fleet_counts, fleet_transitions

    current = _watch_stream_snapshots(records, stall_window=producer.stall_window)
    if not current and not producer.fleet_seen:
        return

    if not producer.fleet_seen:
        producer.fleet_seen = True
        producer.known = {
            run_id: dict(snapshot) for run_id, snapshot in current.items()
        }
        counts = _fleet_counts(current)
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

    # The same fold the seat's own ticker uses, so a follower reading the stream
    # and a reader watching the seat's stdout cannot disagree about either the
    # transitions or their counts.
    folded, next_known = fleet_transitions(producer.known, current)
    producer.known = next_known
    _append_watch_lines(
        producer.path,
        (
            _stream_transition(
                project,
                snapshot=snapshot,
                previous=previous,
                current=state,
                counts=event_counts,
            )
            for snapshot, previous, state, event_counts in folded
        ),
    )


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
    return {
        "reckon_version": version,
        "started_at": started_at,
        "line": f"reckon {version or 'unknown'} started {started_at or 'unknown'}",
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

    Deliberately *not* the orphan check that :func:`watch_state` applies. A
    producer whose supervisor died is reparented to init and stops satisfying
    the dispatch guard, because nothing is listening to the seat it holds — but
    it is still appending real transitions to the stream, and a follower that
    refuses to read them waits forever on data that is arriving. Measured: an
    orphaned producer with 51 KB of stream and a live run left its session's
    pane empty for four minutes. Admission and readability are different
    questions about the same process.
    """
    path = watch_lock_path(project)
    if not path.is_file():
        return False
    with path.open("rb") as handle:
        record = _read_watch_record(handle)
    if not record:
        return False
    alive = record_process_alive(record) is True
    if not alive:
        _erase_confirmed_dead_seat(path, record)
    return alive


def _record_producer_running(record: Mapping[str, Any]) -> bool:
    """Report whether a seat record names a process that is running now.

    Drawn from the process table, not from the seat's held-state: a live
    producer whose record no longer holds the seat lock still reads as live,
    which is the direction a guard must not be fooled in. Deliberately not the
    start-time gate :func:`producer_live` applies — a running process is live
    whether or not its recorded start time still matches, because the
    start-time check exists for who may *signal* that process, a different
    question than whether it is running.
    """
    pid = record.get("pid")
    return bool(pid) and record_process_alive(record, match_start_time=False) is True


def _record_producer_dead(record: Mapping[str, Any]) -> bool:
    """Report whether a seat record names a process that is no longer running."""
    pid = record.get("pid")
    return (
        bool(pid) and record_process_alive(record, match_start_time=False) is not True
    )


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
    if not _record_producer_dead(record):
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
            "reckon_version": __version__,
        }
        # Which unit owns this seat, so a reader can tell a watcher a service
        # will replace from one nothing will. The unit exports its own name, and
        # a watcher started by any other route leaves the key absent rather than
        # claiming a service that does not exist.
        unit = _read_watch_record(handle).get("unit") or os.environ.get(WATCH_UNIT_ENV)
        if unit:
            record["unit"] = str(unit)
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


class _FollowerRegistration:
    """One session's delivery registration, claimable now or later.

    Registration and streaming are separable, and only registration satisfies
    the dispatch guard. A second follower for the same session therefore streams
    read-only while the first holds the lock — and if that first process then
    dies, the registration is released while the streamer keeps delivering
    lines, so every visible signal says attached and dispatch correctly refuses.
    Retrying the claim while streaming closes that gap: whoever is still
    delivering ends up holding the registration.
    """

    def __init__(
        self,
        project: str,
        session: str,
        *,
        delivery: str,
        scope: Mapping[str, Any] | None = None,
    ) -> None:
        self.project = project
        self.session = session
        self.delivery = delivery
        self.scope = dict(scope or {})
        self.held = False
        self.record: dict[str, Any] = {}
        self.blocked_by: dict[str, Any] = {}
        self._handle = None

    def _adopt_inherited(self) -> bool:
        """Keep the same advisory lock across an in-place process reload."""
        raw = os.environ.pop(_FOLLOWER_REGISTRATION_ENV, "")
        if not raw:
            return False
        try:
            inherited = json.loads(raw)
            fd = int(inherited["fd"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
        if (
            inherited.get("project") != self.project
            or inherited.get("session") != self.session
        ):
            os.close(fd)
            return False
        try:
            handle = os.fdopen(fd, "a+b")
            os.set_inheritable(handle.fileno(), False)
            record = _read_watch_record(handle)
        except OSError:
            return False
        if (
            record.get("project") != self.project
            or record.get("session") != self.session
        ):
            handle.close()
            return False
        self._handle = handle
        self.record = record
        self.held = True
        self.blocked_by = {}
        return True

    def _open(self):
        if self._handle is None:
            path = follower_lock_path(self.project, self.session)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = path.open("a+b")
        return self._handle

    def acquire(self) -> bool:
        """Take the registration if it is free, and report whether it is held."""
        if self.held:
            return True
        if self._adopt_inherited():
            return True
        handle = self._open()
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.blocked_by = _read_watch_record(handle)
            return False
        parent = os.getppid()
        self.record = {
            "project": self.project,
            "session": self.session,
            "pid": os.getpid(),
            "pid_start_time": _process_start_time(os.getpid()),
            "parent_pid": parent,
            "parent_start_time": _process_start_time(parent),
            "delivery": self.delivery,
            "scope": self.scope,
            "started_at": _utc_now(),
        }
        _write_watch_record(handle, self.record)
        self.held = True
        self.blocked_by = {}
        return True

    def prepare_reexec(self) -> None:
        """Make this registration survive replacement of the current process."""
        if not self.held or self._handle is None:
            return
        fd = self._handle.fileno()
        os.set_inheritable(fd, True)
        os.environ[_FOLLOWER_REGISTRATION_ENV] = json.dumps(
            {"fd": fd, "project": self.project, "session": self.session}
        )

    def cancel_reexec(self) -> None:
        """Undo descriptor inheritance when process replacement was refused."""
        os.environ.pop(_FOLLOWER_REGISTRATION_ENV, None)
        if self._handle is not None:
            os.set_inheritable(self._handle.fileno(), False)

    def release(self) -> None:
        """Drop the claim, leaving the file as a record rather than removing it.

        Deleting it would orphan a second follower that is holding the same
        inode read-only and about to take over: its claim would succeed on an
        unlinked file, so it would believe it was registered while every reader
        looked up a path that no longer exists. Liveness is the lock plus the
        pid, so a leftover record cannot lie about either.
        """
        if self._handle is None:
            return
        if self.held:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self.held = False
        self._handle.close()
        self._handle = None


@contextmanager
def follower_registration(
    project: str,
    session: str,
    *,
    delivery: str | None = None,
    scope: Mapping[str, Any] | None = None,
):
    """Hold one session's delivery registration for the life of a follower.

    The seat proves a producer exists. This proves a *reader* exists, for a
    named session, which is the only fact a dispatch guard can act on: a seat
    is project-global while the wake-up it feeds is session-local, so a caller
    dispatching against a peer's seat is told a watcher is live and still hears
    nothing.

    The registration is an advisory lock held by the live follower, so it is
    released by the process ending however it ends — the same property that
    keeps the seat honest. Call :meth:`_FollowerRegistration.acquire` again while
    streaming to take over a registration whose holder has since gone.
    """
    registration = _FollowerRegistration(
        project, session, delivery=delivery or delivery_mode(), scope=scope
    )
    registration.acquire()
    try:
        yield registration
    finally:
        registration.release()


@contextmanager
def follower_claim(
    project: str,
    session: str,
    *,
    delivery: str | None = None,
    scope: Mapping[str, Any] | None = None,
):
    """Register one session's delivery, reporting whether the claim succeeded."""
    with follower_registration(
        project, session, delivery=delivery, scope=scope
    ) as registration:
        yield (
            registration.held,
            (registration.record if registration.held else registration.blocked_by),
        )


# A claim takes the lock and then writes its record, so a reader can arrive
# between the two and see a held lock with nothing in it. Settling is measured in
# microseconds; treating that instant as "delivery unknown" would refuse a
# dispatch against a follower that is fine, so a reader waits out the gap.
_REGISTRATION_SETTLE_SECONDS = 0.25


FOLLOWER_FRESHNESS_SECONDS = 1.0
_FOLLOWER_REGISTRATION_ENV = "RECKON_FOLLOWER_REGISTRATION"


def _follower_liveness(path: Path) -> dict[str, Any]:
    """Read one registration and decide whether it still delivers."""
    if not path.is_file():
        return {
            "registered": False,
            "live": False,
            "not_live_because": "no registration remains",
            "delivery": None,
            "follower": {},
        }
    deadline = time.monotonic() + _REGISTRATION_SETTLE_SECONDS
    while True:
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
        if record or not registered or time.monotonic() >= deadline:
            break
        time.sleep(0.005)

    pid = record.get("pid")
    running = record_process_alive(record) is True

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
    live = bool(
        registered and running and consumer_alive and delivery in DELIVERING_MODES
    )
    # A row that is not live says which condition ended it. Its `since` records
    # when it attached, so a dead row's only timestamp makes it look older and
    # better established rather than stale — and a reader counting rows to ask
    # "is this project covered" is then answered by a registration that ended.
    reason = ""
    if not live:
        if not registered and record:
            reason = (
                f"the registration was released; its process {record.get('pid')} "
                "is gone"
                if not running
                else "the registration was released"
            )
        elif not registered:
            reason = "no registration remains"
        elif not running:
            reason = f"the registered process {record.get('pid')} is gone"
        elif not consumer_alive:
            reason = (
                f"the session consuming it (process {record.get('parent_pid')}) "
                "has exited"
            )
        else:
            reason = (
                f"its lines end in a {delivery}, which nothing reads until the "
                "command exits — and a follower does not exit"
            )
    return {
        "registered": registered,
        "live": live,
        "not_live_because": reason,
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


# A released registration keeps its file: ``release`` drops the advisory lock
# and leaves the path in place on purpose, because a second follower may hold the
# same inode read-only and take over. Nothing on a read path removes one either —
# a reader that unlinked while another process was opening the same path would
# put two inodes behind one session's name, and a claim on each would look
# successful. So the directory only grows, one file per session that has ever
# followed the project, and only an explicit maintenance call trims it. The
# window is generous by design: it has to outlast any pause between a released
# registration and the session restarting, and longer than that leaves a residue
# the caller can reason about rather than a registry that grows without bound.
FOLLOWER_REGISTRY_STALE_SECONDS = 14 * 24 * 60 * 60


def sweep_released_followers(
    project: str,
    *,
    stale_after_seconds: float = FOLLOWER_REGISTRY_STALE_SECONDS,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Remove released registrations older than ``stale_after_seconds``.

    A registration is removed only when two things hold at once: its advisory
    lock is free, so nothing is delivering from it, and its file is older than
    the threshold, so a session that released moments ago and is restarting is
    left alone. The check that keeps a delivering registration is the lock and
    not the timestamp — a follower armed in the morning and still delivering at
    night survives a sweep whose other candidates are half its age — so this is
    safe to run against a directory holding a live follower.

    The sweep takes each file's lock before unlinking it, which is the one place
    a registration file is removed. A caller must not invoke it from a read path:
    unlinking there races a process opening the same path, letting one hold the
    old inode and the other a fresh one for a single session. Even here a claim
    that arrives while the sweep holds the lock is refused by
    :meth:`_FollowerRegistration.acquire` rather than silently succeeding, so run
    it from an explicit maintenance path where no registration is in flight; the
    residual window is a claimant that has opened the path without yet taking the
    lock.

    ``now`` supplies the reference time and lets a caller reason about a fixed
    clock. Returns one entry per removed registration, so the caller reports a
    count rather than re-listing the directory to discover it.
    """
    directory = follower_dir(project)
    if not directory.is_dir():
        return []
    reference = time.time() if now is None else now
    removed: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.lock")):
        try:
            handle = path.open("a+b")
        except OSError:
            continue
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Its lock is held, so it delivers right now whatever its age.
                continue
            try:
                try:
                    age = reference - path.stat().st_mtime
                except OSError:
                    continue
                if age < stale_after_seconds:
                    continue
                session = str(_read_watch_record(handle).get("session") or path.stem)
                path.unlink()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        removed.append(
            {
                "project": project,
                "session": session,
                "path": str(path),
                "age_seconds": age,
            }
        )
    return removed


def watch_state(project: str, *, session: str | None = None) -> dict[str, Any]:
    """Return the paste-ready arming line and process-backed watcher liveness."""
    arming_line = _watch_arming_line(project)
    attach_line = _watch_attach_line(project, session=session)
    path = watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Delivery is reported beside the seat because reading one without the
    # other is how "a watcher is live" came to mean "I will be told".
    delivery = follower_state(project, session) if session is not None else None
    attached = None if delivery is None else bool(delivery["live"])
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            registration = _read_watch_record(handle)
        else:
            registration = _read_watch_record(handle)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    # Liveness is reconciled against the process table rather than read from
    # the seat's held-state: a probe that sees the lock free reads a running
    # producer as absent, and one that sees it held reads a dead process as
    # live — each the wrong way to decide a dispatch guard. The running answer
    # is the one the guard may trust.
    watcher_live = _record_producer_running(registration)
    return {
        "arming_line": arming_line,
        "attach_line": attach_line,
        "ensure_line": watcher_ensure_line(project),
        "unit": registration.get("unit"),
        "watcher_live": watcher_live,
        "watcher": dict(registration),
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

    # Reconcile-on-read: a registration whose process is gone is a disagreement
    # between the registry and the machine, and reading it without repairing it
    # leaves the next reader to find the same lie. Repair in place — never on a
    # blocking lock, so observing still cannot deny an arming — and report the
    # repaired state (an empty registration) rather than the stale one.
    if (
        registration
        and _record_producer_dead(registration)
        and _reconcile_watch_record(project, registration)
    ):
        registration = {}

    pid = registration.get("pid")
    registering_process_alive = record_process_alive(registration)

    observer_alive: bool | None = None
    if "parent_pid" in registration:
        parent_pid = registration.get("parent_pid")
        try:
            parent_pid_value = int(parent_pid)
        except (TypeError, ValueError):
            parent_pid_value = 0
        observer_alive = bool(
            process_alive(parent_pid) is True
            and _process_start_time(parent_pid) == registration.get("parent_start_time")
            and parent_pid_value > 1
        )

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


# ── Watcher user service ────────────────────────────────────────────────────


WATCH_UNIT_TEMPLATE = """\
[Unit]
Description=reckon crew watcher for {project}
After=network.target

[Service]
Type=simple
WorkingDirectory={working_directory}
Environment="PATH={path}"
Environment="{unit_variable}={unit}"
{environment}\
ExecStart={exec_start}
StandardOutput=append:{log_file}
StandardError=append:{log_file}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def _reckon_console_script() -> str:
    """Return the absolute path of the reckon console script to run.

    Both callers need the absolute path: the watcher unit runs without a shell,
    so its ExecStart cannot depend on PATH, and the follower attach line is
    armed by a shell that does not carry the interpreter's bin directory on
    PATH. The interpreter's own bin directory is preferred because it pins the
    caller to the environment the command was invoked from, and it is taken
    without resolving ``sys.executable`` because a virtualenv's interpreter is
    a symlink into the base distribution -- resolving it would leave the
    virtualenv, and the console script with it, behind.
    """
    sibling = Path(sys.executable).parent / "reckon"
    if sibling.is_file():
        return str(sibling)
    discovered = shutil.which("reckon")
    if discovered:
        return os.path.abspath(discovered)
    raise CrewError(
        "the 'reckon' console script is not beside the running interpreter "
        "and is not on PATH"
    )


def _watcher_search_path(config: Mapping[str, Any]) -> str:
    """Return the PATH a project's watcher must run with.

    The measured fault: a watcher unit ran seven hours with the backend
    directory absent from PATH, so every wait-lift it issued died at exec while
    the pointer kept reading ``working``. The PATH here is the one the launch
    would search — the resolved backend executables' own directories first —
    so a lift started by this watcher can resolve what a dispatch can.
    """
    from reckon.crew.dispatch import assert_routable_backends_resolvable

    resolved = assert_routable_backends_resolvable("<watcher>", config)
    directories = [str(Path(row["executable"]).parent) for row in resolved]
    current = (os.environ.get("PATH") or os.defpath).split(os.pathsep)
    return os.pathsep.join(
        dict.fromkeys(directory for directory in [*directories, *current] if directory)
    )


def _watcher_service_environment(config: Mapping[str, Any]) -> dict[str, str]:
    """Return the environment the watcher unit must carry."""
    environment = {"PATH": _watcher_search_path(config)}
    config_home = os.environ.get("RECKON_HOME")
    if config_home:
        # Forward the config home so the unit resolves the same mounts and run
        # pointers as the shell that ensured it, rather than the account
        # default it would otherwise fall back to.
        environment["RECKON_HOME"] = str(Path(config_home).expanduser().resolve())
    return environment


def render_watch_unit(
    project: str,
    *,
    environment: Mapping[str, str],
    executable: str | None = None,
) -> str:
    """Render the systemd user unit that runs one project's watcher."""
    command = executable or _reckon_console_script()
    argv = [command, "crew", "watch", "--project", project]
    log_file = _config_home() / "logs" / f"watch-{watch_unit_name(project)}.log"
    override = "".join(
        f'Environment="{name}={value}"\n'
        for name, value in environment.items()
        if name != "PATH"
    )
    return WATCH_UNIT_TEMPLATE.format(
        project=project,
        working_directory=Path.home(),
        path=environment.get("PATH") or os.defpath,
        unit_variable=WATCH_UNIT_ENV,
        unit=watch_unit_name(project),
        environment=override,
        exec_start=" ".join(shlex.quote(part) for part in argv),
        log_file=log_file,
    )


class SystemdUserWatchService:
    """The host's systemd user manager, as a watcher service needs it.

    Narrow on purpose: the ensure path asks four questions (what is written,
    is it active, write it, start it), so a caller can answer them from a fake
    without a systemd manager on the host — and so no unit is written to the
    real account home by a test.
    """

    def unit_path(self, project: str) -> Path:
        return Path.home() / ".config" / "systemd" / "user" / watch_unit_name(project)

    def installed(self, project: str) -> bool:
        return self.unit_path(project).is_file()

    def active(self, project: str) -> bool:
        from reckon import service

        completed = service.systemctl(
            "is-active", watch_unit_name(project), check=False
        )
        return completed.returncode == 0

    def lingering(self) -> bool:
        from reckon import service

        return service.linger_enabled()

    def enable_linger(self) -> None:
        from reckon import service

        service.enable_linger()

    def write_unit(self, project: str, content: str) -> tuple[Path, bool]:
        target = self.unit_path(project)
        target.parent.mkdir(parents=True, exist_ok=True)
        # systemd opens the log file but will not create its parent directory.
        (_config_home() / "logs").mkdir(parents=True, exist_ok=True)
        unchanged = target.is_file() and target.read_text() == content
        if not unchanged:
            target.write_text(content)
        return target, not unchanged

    def start(self, project: str, *, restart: bool) -> None:
        from reckon import service

        unit = watch_unit_name(project)
        service.systemctl("daemon-reload")
        service.systemctl("restart" if restart else "start", unit)


def _register_watch_unit(project: str, unit: str) -> dict[str, Any]:
    """Record the unit name in the project's watcher registration.

    Written only while the seat is free, and non-blocking: when the seat is held,
    the watcher holding it is authoritative and records the unit itself from
    its own environment, so a registration never ends up with no live writer
    behind it. A held seat is reported rather than overwritten.
    """
    path = watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            held = _read_watch_record(handle)
            return {
                "registered": False,
                "reason": "seat-held",
                "unit": held.get("unit") or unit,
            }
        record = _read_watch_record(handle)
        record["project"] = project
        record["unit"] = unit
        _write_watch_record(handle, record)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return {"registered": True, "reason": "written", "unit": unit}


def _service_manager_unreachable(error: BaseException) -> bool:
    """Report whether an arming failure means the service bus cannot be reached.

    Only an unreachable manager is a reason to arm the watcher another way; a
    unit the manager refused on its merits must still raise, or the fallback
    would replace a diagnosis with a process nobody asked for. The markers are
    the ones systemd and logind print when the per-user manager is gone:
    ``Failed to connect to bus: Connection refused`` after a client restart,
    ``No such file or directory`` when the bus socket has been removed, and the
    launcher's own ``is not available on this host`` when it is absent entirely.
    """
    detail = str(error).lower()
    return any(
        marker in detail
        for marker in (
            "failed to connect to bus",
            "connection refused",
            "connection reset by peer",
            "transport endpoint is not connected",
            "is not available on this host",
        )
    )


def _arm_watcher_as_process(project: str) -> Mapping[str, Any]:
    """Start the project's watcher as a plain background process.

    The same producer the dispatch path arms, so the fallback reuses one watcher
    implementation rather than adding a second: it takes the seat once, replaces
    a seat whose supervisor has died, and reports liveness rather than raising.
    Imported lazily because the dispatch path imports this module.
    """
    from reckon.crew.dispatch import _ensure_watch_producer

    return _ensure_watch_producer(project)


def ensure_watcher_service(
    project: str,
    *,
    manager: Any | None = None,
    config: Mapping[str, Any] | None = None,
    restart: bool = False,
    producer: Callable[[str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Start or restart a project's watcher as an idempotent user service.

    Idempotent in the sense that decides whether a second call disturbs a live
    watcher: the unit is rewritten only when its rendered content changed, and a
    unit already active on an unchanged definition is reported rather than
    restarted. Restarting unconditionally would drop the seat and re-take it,
    so the command a refusal tells a person to run would interrupt the watcher
    it exists to guarantee.

    Arming also survives a manager that cannot be reached. The user manager is a
    single point of failure for a command whose whole job is to leave a watcher
    behind: a watcher started as a plain process lifts on every tick exactly as
    the service would, so a bus failure falls back to that path rather than
    raising and leaving the project unwatched. The result names which path armed
    the watcher, and why, in fields a caller reads rather than in prose.

    The fallback is deliberately narrow — a unit the manager refused on its
    merits still raises — and ``producer`` is the seam that lets a caller supply
    the process arming, so a test exercises this path without starting a watcher.
    """
    from reckon import flight

    service_manager = manager if manager is not None else SystemdUserWatchService()
    if manager is None:
        # A real unit is written to the account's systemd directory, which a
        # throwaway configuration home must never cause. Imported lazily so the
        # read-only surfaces of this module do not depend on the arming path.
        from reckon.crew.dispatch import _refuse_arming_under_a_throwaway_home

        _refuse_arming_under_a_throwaway_home(project)
    resolved_config = (
        config if config is not None else flight.resolve(project=project).config
    )
    environment = _watcher_service_environment(resolved_config)
    # Rendering and writing the unit touch no bus, so a failure here is a real
    # one about the account's own filesystem and never a reason to fall back.
    content = render_watch_unit(project, environment=environment)
    path, changed = service_manager.write_unit(project, content)

    try:
        was_active = service_manager.active(project)
        start_required = bool(changed or restart or not was_active)
        if start_required:
            # 'enable --now' leaves an already-running unit on its old
            # definition, so a rewritten active unit needs an explicit restart.
            service_manager.start(
                project, restart=bool(restart or (changed and was_active))
            )

        lingering: bool | None = None
        if LINGER_IF_REQUIRED and hasattr(service_manager, "lingering"):
            lingering = bool(service_manager.lingering())
            if not lingering:
                service_manager.enable_linger()
                lingering = bool(service_manager.lingering())
    except Exception as error:
        if not _service_manager_unreachable(error):
            raise
        return _watcher_armed_as_process(
            project,
            error=error,
            unit_path=path,
            unit_changed=bool(changed),
            environment=environment,
            producer=producer or _arm_watcher_as_process,
        )

    registration = _register_watch_unit(project, watch_unit_name(project))
    if start_required:
        detail = (
            f"restarted {watch_unit_name(project)} onto a rewritten unit"
            if changed and was_active
            else f"started {watch_unit_name(project)}"
        )
    else:
        detail = (
            f"{watch_unit_name(project)} is already active on an unchanged unit; "
            "started nothing"
        )
    return {
        "project": project,
        "unit": watch_unit_name(project),
        "unit_path": str(path),
        "unit_changed": bool(changed),
        "service_active": was_active or start_required,
        "started": start_required,
        "detail": detail,
        "environment": environment,
        "lingering": lingering,
        "registration": registration,
        "watcher_live": watch_state(project)["watcher_live"],
        # Which path armed the watcher, as a field rather than prose: a caller
        # acts on a value instead of parsing a sentence out of ``detail``.
        "path": "service",
        "fallback_reason": None,
        "ensure_line": watcher_ensure_line(project),
    }


def _watcher_armed_as_process(
    project: str,
    *,
    error: BaseException,
    unit_path: Path,
    unit_changed: bool,
    environment: Mapping[str, str],
    producer: Callable[[str], Mapping[str, Any]],
) -> dict[str, Any]:
    """Arm the watcher as a plain process after the service bus refused.

    The result keeps the shape the service path returns, so a caller reads one
    result either way, and states the fallback in three fields: ``path``, the
    failure that forced it, and whether a watcher is live afterwards. The
    producer reports liveness rather than raising, so a fallback that could not
    start a watcher reads as ``watcher_live`` false with the reason beside it,
    which is the same shape the dispatch path reports.
    """
    state = producer(project)
    registration = _register_watch_unit(project, watch_unit_name(project))
    live = bool(state.get("watcher_live"))
    reason = " ".join(str(error).split())
    return {
        "project": project,
        "unit": watch_unit_name(project),
        "unit_path": str(unit_path),
        "unit_changed": bool(unit_changed),
        "service_active": False,
        "started": live,
        "detail": (
            f"the service manager could not be reached ({reason}); "
            + (
                f"armed the watcher for {project} as a plain background process"
                if live
                else f"a plain background watcher for {project} is not live either"
            )
        ),
        "environment": environment,
        "lingering": None,
        "registration": registration,
        "watcher_live": live,
        "path": "fallback",
        "fallback_reason": reason,
        "ensure_line": watcher_ensure_line(project),
    }


# Every state the watch surface can emit, split by whether a coordinator has to
# act on it. The first set is the vocabulary a reader acts on the sight of; the
# second is progress and is not news on its own. The split used to feed a
# follower state filter, which is gone — a follower now delivers every
# transition — but the vocabulary remains the one the surface knows, and tests
# still assert every emitted state lands in it.
WATCH_ATTENTION_STATES = (
    "complete",
    "blocked",
    "failed",
    "stalled",
    "stopped",
    "abandoned",
    "completed_unpromoted",
    "unknown",
    "unreadable",
    "wait-aged",
)
WATCH_PROGRESS_STATES = ("dispatched", "working", "running", "waiting", "promoted")


def _watch_arming_line(project: str) -> str:
    """Return the exact shell-safe command a dispatch payload carries."""
    return f"reckon crew watch --project {shlex.quote(project)}"


# The unit exports this into the watcher's own environment, so the seat record
# a service-armed watcher claims names the unit that will replace it. Read from
# the environment rather than passed as an argument: the watcher's argv is the
# arming contract a person copies, and a path-only flag would appear there.
WATCH_UNIT_ENV = "RECKON_WATCH_UNIT"

# A watcher holds its seat for as long as it runs, so a service that dies at
# logout is the fault this deployment exists to remove: none of the units it
# owns come back, and the project reads as watched until the next dispatch
# refuses. Lingering is what keeps a user manager alive past the last session.
LINGER_IF_REQUIRED = True


def ensure_placement_reservation(
    *,
    session: str | None = None,
    project: str | None = None,
    runner: Any | None = None,
    alive_probe: Any | None = None,
) -> dict[str, Any]:
    """Hold the placement reservation if absent, and report it if present.

    The reservation is a durable shared resource of the same kind as the model
    serve and the project watcher, so it is managed the same way: an ensure
    command that is safe to run twice, holding the resource once and reporting
    it afterwards. Its job id is published into the shared crew state rather
    than held in the session that ran the command, because a job id in one
    session's memory is invisible to every other session and each would hold
    its own reservation.
    """
    from reckon.crew import placement as placement_module

    result = placement_module.ensure_reservation(
        session=session, project=project, runner=runner, alive_probe=alive_probe
    )
    result["ensure_line"] = placement_ensure_line()
    return result


def placement_ensure_line() -> str:
    """Return the command that holds or reports the placement reservation."""
    return "reckon crew placement --ensure"


def watch_unit_name(project: str) -> str:
    """Return the systemd user unit that runs one project's watcher service."""
    readable = re.sub(r"[^A-Za-z0-9._-]", "-", project).strip("-") or "project"
    return f"reckon-watch-{readable}.service"


def watcher_ensure_line(project: str) -> str:
    """Return the command that starts or restarts a project's watcher service."""
    return f"reckon crew watch --ensure --project {shlex.quote(project)}"


def _watch_attach_line(project: str, *, session: str | None = None) -> str:
    """Return the follower one session arms to be woken about its own runs.

    A seat existing is not the same as this session hearing about it: the seat
    is project-global and wake delivery is session-local, so a caller
    dispatching against another session's seat is told a watcher is live while
    nothing reaches it. This is the command that closes that gap.

    The command's first token is the absolute path of the running reckon
    console script, because the shell that arms it need not carry the
    interpreter's bin directory on PATH.

    It is one bare command on purpose: filtering and buffering belong inside
    the follower, because a shell pipeline around it has three ways to swallow
    the ticker silently. An unbuffered stage withholds every line until the
    command exits, and this command does not exit. An unanchored pattern
    matches the summary field that trails each line, so it matches everything.
    And a trailing ``|| true`` turns the follower's own refusal into a silent
    success indistinguishable from a quiet fleet.

    It carries no state filter either. A filter that legitimately matches
    nothing produces an empty pane, which reads the same as a follower that
    never started -- and a reader watching a wave wants the starts and the
    working transitions, not only the landings. A state filter is worse than
    no filter even when it matches: it reports how a run stopped and hides how
    it recovered, which is the half of the story the reader is waiting for.
    """
    parts = [
        shlex.quote(_reckon_console_script()),
        "crew",
        "follow",
        "--project",
        shlex.quote(project),
    ]
    if session:
        parts += ["--session", shlex.quote(session)]
    return " ".join(parts)


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


def process_alive(pid: Any) -> bool | None:
    """Report whether a pid is still running; None when there is no pid.

    A dead process with no terminal event in its log is a recoverable orphan
    rather than a completed run, which is why liveness is recorded beside the
    stream rather than inferred from it. A PermissionError from a zero signal
    means the process exists but belongs to another user — proof of life, not
    death. On a shared workstation carrying several fleets that is the normal
    condition for a peer worker, so reporting it as dead would classify a live
    run as abandoned.
    """
    if not pid:
        return None
    # A zombie is a process-table entry whose process has exited and whose
    # exit status the parent has not yet collected, so the kernel accepts a
    # zero signal against it and the probe below would report the finished
    # run as running: its slot stays held and its own resume is refused while
    # it lingers. Every caller asking whether work is still running wants
    # "no" for a zombie, because the process it was launched to run has
    # finished either way. Reading the state from per-process stat is what
    # tells that apart from the liveness proof above; an unreadable record is
    # not proof of death, so it falls through to the signal probe unchanged.
    if _process_state(pid) == "Z":
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError):
        return None
    return True


# The scheduler states that mean a placed job is still in the system: the job
# exists and its work has not ended. Any other readable state means the job has
# left the queue, which is terminal whatever the scheduler calls it. A state the
# scheduler cannot be asked for is None, not False, so a silent scheduler never
# reads as a stopped worker.
_JOB_LIVE_STATES = frozenset(
    {"running", "pending", "configuring", "completing", "suspended"}
)

# A scheduler query is on the liveness path of every read, so it is bounded:
# a controller that hangs must not make a fleet listing hang with it.
_SCHEDULER_QUERY_TIMEOUT_SECONDS = 5.0

# The token a query vector carries where the job id is substituted, so a probe
# spells its own argument order rather than reckon guessing one.
_JOB_STATE_PLACEHOLDER = "{job}"


def _scheduler_query_argv(
    placement: Mapping[str, Any] | None,
    job_id: Any,
    field: str,
) -> list[str] | None:
    """The argument vector for one field of one job, or None when unknowable.

    The query is read from the placement's own declaration rather than from a
    table keyed on the wrapper's name, so which reporting verb answers a given
    scheduler is configuration. A placement that declares the wrapper without
    declaring how to ask it answers None here and falls through to the pid
    probe, having been refused before launch for exactly that omission.
    """
    if not placement or not job_id:
        return None
    query = placement.get(field)
    if not isinstance(query, Iterable) or isinstance(query, (str, bytes)) or not query:
        return None
    token = str(job_id)
    return [
        token if str(item) == _JOB_STATE_PLACEHOLDER else str(item) for item in query
    ]


def _ask_scheduler(
    argv: list[str] | None, runner: Callable[[list[str]], str | None] | None
) -> str | None:
    """One scheduler question, or None when the question could not be asked.

    The two failures are kept apart because they mean opposite things. A query
    that could not be run — no such scheduler, a non-zero exit, a timeout —
    answers None, and the caller falls back to another instrument. A query that
    ran and printed nothing answers the empty string, which for a job-state
    question is a statement in its own right: the scheduler knows no such job,
    so the job has left the queue. Collapsing the empty answer into None would
    read every ordinary completion as unreadable and send the caller back to a
    pid that belongs to another host.
    """
    if argv is None:
        return None
    probe = _run_scheduler_query if runner is None else runner
    try:
        output = probe(argv)
    except (OSError, subprocess.SubprocessError):
        return None
    if output is None:
        return None
    lines = str(output).strip().splitlines()
    return lines[-1].strip() if lines else ""


def scheduler_job_reason(
    placement: Mapping[str, Any] | None,
    job_id: Any,
    runner: Callable[[list[str]], str | None] | None = None,
) -> str | None:
    """The scheduler's own reason string for a placed job, or None.

    A job that never started reports why here rather than through an exit
    status, so a launch-failure record quotes the scheduler's reason instead
    of fabricating one.
    """
    return _ask_scheduler(
        _scheduler_query_argv(placement, job_id, "reason_query"), runner
    )


# A job the scheduler ended for its own reason is not a worker whose work
# failed: the remedy differs, since a resubmission with the same resources fails
# the same way. The two classes a placement plan already names are a time limit
# and a memory limit, matched on the scheduler's own words so a different
# spelling adds a class rather than being read as a failed worker.
_SCHEDULER_KILL_CLASSES: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"timeout", "timelimit", "time limit", "deadline"}), "job-timeout"),
    (
        frozenset(
            {
                "out_of_memory",
                "outofmemory",
                "out of memory",
                "oom",
                "memory limit",
            }
        ),
        "job-out-of-memory",
    ),
)


def scheduler_kill_class(state: Any, reason: Any = None) -> str | None:
    """Name the scheduler's own kill reason, or None when it ended for another.

    Matched against both the state and the scheduler's reason string, because a
    scheduler spells a time or memory end in either place and a reason of
    ``None`` is reported differently depending on which one fired.
    """
    haystack = " ".join(
        part.strip().casefold() for part in (state, reason) if part
    )
    if not haystack:
        return None
    for spellings, name in _SCHEDULER_KILL_CLASSES:
        if any(spelling in haystack for spelling in spellings):
            return name
    return None


def _run_scheduler_query(argv: list[str]) -> str | None:
    """Run one scheduler state query, answering None when it cannot be read.

    A query that exits non-zero — an unknown job, an unreachable controller —
    answers None rather than an empty string, because the absence of a state is
    not the statement that the job has ended.
    """
    executable = shutil.which(argv[0])
    if executable is None:
        return None
    completed = subprocess.run(
        [executable, *argv[1:]],
        capture_output=True,
        text=True,
        timeout=_SCHEDULER_QUERY_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout


def _scheduler_state_argv(
    placement: Mapping[str, Any] | None, job_id: str
) -> list[str] | None:
    """The argument vector that asks a scheduler for one job's state, or None.

    A placement declares the reporting verb that answers one job's state beside
    the wrapper it asks, so a placement that declares no query answers None and
    falls through to the pid probe instead of being read as a stopped job.
    """
    return _scheduler_query_argv(placement, job_id, "state_query")


def scheduler_job_state(
    placement: Mapping[str, Any] | None,
    job_id: Any,
    runner: Callable[[list[str]], str | None] | None = None,
) -> str | None:
    """The state a scheduler reports for a placed job, or None when unread.

    Three answers, and the caller must tell the last two apart. A state names
    the job and is read against the in-flight set. The empty string is a
    successful query that named no job: the scheduler knows it not, so it has
    left the queue. None is a question that could not be asked at all — no
    scheduler, no such wrapper, a non-zero exit — and leaves the caller to fall
    back to the pid probe rather than reporting a live run as stopped.
    """
    return _ask_scheduler(
        _scheduler_state_argv(placement, str(job_id or "")), runner
    )


def placement_job_alive(
    record: Mapping[str, Any] | None,
    runner: Callable[[list[str]], str | None] | None = None,
) -> bool | None:
    """Whether the job a placed run was charged to is still in the system.

    A placed run's recorded pid names the scheduler client, not the worker, so
    the job is the subject of a liveness read. A state the scheduler reports as
    in-flight answers True. Any other readable answer means the job has left the
    queue and answers False, whatever the scheduler calls it — including the
    empty answer of a successful query that named no job, which is how an
    ordinary completion is reported and must not fall through to the pid. A
    record carrying no placement, or one whose scheduler could not be queried at
    all, answers None so the pid probe decides as it always has.

    ``runner`` is the caller's own scheduler query, handed in the way ``alive``
    is so a test reaches this without a scheduler on the host.
    """
    if not record:
        return None
    placement = record.get("placement")
    if not isinstance(placement, Mapping) or not placement:
        return None
    state = scheduler_job_state(placement, record.get("job_id"), runner)
    if state is None:
        return None
    return state.casefold() in _JOB_LIVE_STATES


def record_process_alive(
    record: Mapping[str, Any] | None,
    alive: Callable[[Any], bool | None] | None = None,
    job_alive: Callable[[Mapping[str, Any] | None], bool | None] | None = None,
    match_start_time: bool = True,
) -> bool | None:
    """Report whether the process a run record names is still running.

    Every liveness decision about a run is taken from the run's own record, so
    the pid lookup lives here in one place and the call site never handles a
    bare pid. A record that names no process answers None, the same shape
    :func:`process_alive` already returns for a missing pid, so a caller cannot
    read "no process recorded yet" as a stopped worker.

    A pid the kernel has since handed to another process is not the one the
    record names, and a bare process-table probe cannot tell the two apart: it
    answers liveness for whatever now holds the number. When the record carries
    the kernel start tick written at registration, the probe's answer is kept
    only if the running process is the registered one, so a reused pid stops
    reading as a survivor on every read rather than only where a caller
    remembered to compare. An unreadable start tick is not proof of reuse — the
    same stance :func:`process_alive` takes toward an unreadable process record
    — so the probe's answer stands, which is also what keeps a peer's live
    process readable.

    A placed run is charged to a scheduler job rather than to the coordinator's
    own login slice, so its recorded pid names the scheduler client rather than
    the worker and a local process-table read answers a different question. The
    job is asked first, and the pid probe is the fallback for a record carrying
    no placement or a scheduler that cannot be queried — which keeps an
    unplaced run answering exactly as it always has.

    ``alive`` is the caller's own probe. A module that keeps the primitive
    bound under its own name — so a test can substitute liveness for that
    module — hands it in rather than having its substitution bypassed. The
    reuse check needs the process table, so it is taken only when that real
    primitive answered: a substituted probe is the whole answer for the read.

    ``match_start_time`` is for the one caller that asks whether the process
    itself is running rather than whether it is the registered one. A seat
    guard needs the process that holds the seat, and a running holder is a
    running holder however its recorded identity reads, so it opts out here
    rather than depending on this check being absent.
    """
    if not record:
        return None
    placed = (placement_job_alive if job_alive is None else job_alive)(record)
    if placed is not None:
        return placed
    probe = process_alive if alive is None else alive
    pid = record.get("pid")
    running = probe(pid)
    # The reuse check reads the process table, which is the only thing that can
    # say whether the pid still names the registered process. A caller that
    # substitutes its own probe owns the whole read — its probe answers for
    # liveness and there is nothing left for a second lookup to decide — so the
    # check is skipped when the real primitive is the one that answered. The
    # callers that pass this module's own ``process_alive`` through still get
    # the check on every read they make, and ``alive=None`` resolves to that
    # same function, so the default path is covered by the same identity.
    if running is True and match_start_time and probe is process_alive:
        expected = record.get("pid_start_time")
        if expected is not None:
            actual = _process_start_time(pid)
            if actual is not None:
                running = actual == expected
    return running


def _process_stat_fields(pid: Any) -> list[str]:
    """The space-separated per-process stat fields, or [] when unreadable.

    The comm field is parenthesised and may itself contain spaces and closing
    parentheses, so the split begins after the final ``)``; the fields are the
    third one (state) onwards, unrenumbered from the parenthesised form.
    """
    try:
        value = int(pid)
        stat = Path(f"/proc/{value}/stat").read_text()
    except (OSError, TypeError, ValueError):
        return []
    return stat[stat.rfind(")") + 2 :].split()


def _process_state(pid: Any) -> str | None:
    """The single-character kernel state from the per-process stat record."""
    fields = _process_stat_fields(pid)
    return fields[0] if fields else None


def _process_start_time(pid: Any) -> str | None:
    """Read the kernel start tick that distinguishes reused process ids."""
    fields = _process_stat_fields(pid)
    return fields[19] if len(fields) > 19 else None
