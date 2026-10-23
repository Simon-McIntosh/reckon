"""The key a roadmap answer is valid under, and the node-local snapshot it opens.

A roadmap read composes a whole project, and re-establishing on the next call
that nothing it read has moved must cost less than the composition itself. This
module owns both halves of that: the key, a set of cheap facts that move exactly
when a covered input does, and the snapshot kept under that key.

The key is taken in three parts, each cheap enough to read on every call:

- a mounted project's plan state, from three facts rather than a walk: the
  staging index git moves on every commit, merge and checkout; the commit HEAD
  resolves to; and the store's write counter, which moves on every plan write
  whether or not it committed.
- the live runs of the project being answered, whose classification is a
  function of files the crew owns, of the process table, and of the clock.
- the request the answer was composed for, and the code that composed it.

The snapshot is held in process and on node-local disk. The disk copy is what
lets a fresh process, another session's server and the current-code child
answer their first call from a composition one of them already made.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Bumped when the key's shape changes in a way a stored snapshot cannot survive:
# a record written by an older shape would be compared field by field against a
# key that no longer has the same fields.
_ROADMAP_SNAPSHOT_VERSION = 1
# A read whose key matches recomputes nothing, so a write that neither reckon
# nor git made — an editor saving a plan in place — would be answered from a
# stale snapshot until something else moved. A full revalidation at most once
# per this window bounds how long that can last.
_ROADMAP_REVALIDATE_DEFAULT_S = 120.0
_ROADMAP_SNAPSHOT_LOCK = threading.Lock()
# In-process reuse, so a hit costs no open beyond the snapshot file itself.
_ROADMAP_SNAPSHOTS: dict[str, tuple[str, dict[str, Any]]] = {}


class _MountsMemo:
    """The mounts last resolved for a stamp, held until the stamp moves."""

    stamp: int | None = None
    mounts: dict[str, Path] | None = None


_MOUNTS_MEMO = _MountsMemo()


def remembered_mounts(
    stamp: int | None, resolve: Callable[[], dict[str, Path]]
) -> dict[str, Path]:
    """Return the mounts resolved for ``stamp``, resolving them if it has moved.

    ``resolve`` establishes that each mount's docs tree exists, and that stat
    lands on a path inside the covered tree — the one place a key would
    otherwise touch it on every call, to learn nothing that changed. Holding the
    answer until the mounts file's own stamp moves is the only way a mount can
    be added, removed or repointed under a running process.
    """
    memo = _MOUNTS_MEMO
    if memo.mounts is not None and memo.stamp == stamp:
        return memo.mounts
    resolved = resolve()
    memo.stamp, memo.mounts = stamp, resolved
    return resolved


def roadmap_revalidate_window() -> float:
    """Seconds a snapshot may be served before a full revalidation is forced."""
    raw = str(os.environ.get("RECKON_ROADMAP_REVALIDATE_S") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return _ROADMAP_REVALIDATE_DEFAULT_S


def _git_dir_for_docs(docs_dir: Path) -> Path | None:
    """Resolve a docs tree's git directory, following a worktree's pointer file."""
    git = docs_dir.parent / ".git"
    try:
        if git.is_dir():
            return git
        text = git.read_text().strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    target = Path(text.split(":", 1)[1].strip())
    return target if target.is_absolute() else (git.parent / target)


def _head_commit_of(git_dir: Path) -> str:
    """Resolve the commit HEAD names, reading the ref rather than asking git.

    Asking git is a subprocess, and the whole point of the key is that a read
    that finds nothing changed pays stat and open calls and nothing else.
    """
    try:
        head = (git_dir / "HEAD").read_text().strip()
    except OSError:
        return ""
    if not head.startswith("ref:"):
        return head
    ref = head.split(":", 1)[1].strip()
    try:
        return (git_dir / ref).read_text().strip()
    except OSError:
        pass
    try:
        lines = (git_dir / "packed-refs").read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        if line.startswith(("#", "^")) or " " not in line:
            continue
        sha, name = line.split(" ", 1)
        if name.strip() == ref:
            return sha.strip()
    return ""


def roadmap_project_generation(project: str, docs_dir: Path | None) -> list[str]:
    """Return the three facts one mounted project's plan state is keyed on."""
    from reckon._store import read_write_generation

    index_identity = "none"
    head = ""
    git_dir = _git_dir_for_docs(docs_dir) if docs_dir is not None else None
    if git_dir is not None:
        try:
            index_identity = str((git_dir / "index").stat().st_mtime_ns)
        except OSError:
            index_identity = "none"
        head = _head_commit_of(git_dir)
    return [index_identity, head, read_write_generation(project)]


def _roadmap_live_readings(project: str) -> tuple[list[list[str]], dict[str, Any]]:
    """Return this project's live-run key rows and the states they were read at.

    Only this project's pointers enter the key: a peer project's run is
    filtered out of this project's rows by the readers above, so letting it
    move the key would recompute a composition it cannot change. Each row is
    the crew watcher's own per-run reduction, which already folds the clock in
    — the same reduction a ticker consumes, so a run whose stream goes silent
    past the clock threshold moves this key exactly as it moves the watch.

    The row also carries the watcher's fresh liveness reading. Liveness is not
    a file: a worker can die with every file untouched, and the reduction's
    state is recomputed from those files, so a run that died silently would
    leave the state — and with it the key — exactly where it was. The reading
    is taken here for the same reason the watcher takes it on every poll, and
    it is a signal-0 probe rather than a storage read, so it costs the key no
    covered-file visit.

    The third element beside them is the watcher's own reuse key: the identity
    of every file a run's classification is a function of. The state word is a
    coarser summary than that composition — a manifest, a worker or attempt
    record, a stream, a review filed against the run, or the head of the tree
    the run is reviewed against can each move while the state and the liveness
    reading stay exactly as they were. The reuse key is the same value the
    watcher itself compares before it serves a stored snapshot, so this key
    moves whenever a tick would have recomputed, and a read answers from a
    composition no input of which has moved: the reuse path drops it. It is a
    set of stats over the run's own records, none of them inside a covered
    tree.

    The fourth element is the clock's own contribution, the run's declared wait
    read as overdue or not. Liveness and the composition are both facts about
    files; a wait ages with nothing on disk moving at all, and the word a
    reader sees moves with it — a run waiting at one moment is overdue a
    quarter of an hour later, with no record rewritten. The reduction already
    reads it at this moment, so the row carries the reading rather than
    leaving the answer to stand past the horizon it was composed under.
    """
    from reckon.crew import recovery_watch as watch
    from reckon.crew import runs as crew_runs

    try:
        pointers = crew_runs.list_live(project=project)
    except OSError:
        pointers = []
    moment = time.time()
    stall_seconds = watch.parse_duration(watch.DEFAULT_WATCH_STALL_WINDOW)
    # The reduction is taken for this read alone: its own store would serve the
    # snapshot, and a snapshot's silence is refreshed for the moment but not the
    # rest of what the moment decides — a declared wait that has since passed
    # its horizon stays waiting until something on disk moves, and the key built
    # on the served reading matches. A read whose key is taken at the moment it
    # answers has to reduce at that moment.
    reduction: dict[str, Any] = {}
    rows: list[list[str]] = []
    states: dict[str, Any] = {}
    for pointer in pointers:
        if not isinstance(pointer, dict) or pointer.get("project") != project:
            continue
        run_id = str(pointer.get("run_id") or "")
        if not run_id:
            continue
        try:
            snapshot = watch._watch_snapshot(
                pointer, moment=moment, stall_seconds=stall_seconds, cache=reduction
            )
        except Exception:  # noqa: BLE001 — a key never becomes the read's failure
            snapshot = {}
        state = str(snapshot.get("state") or "")
        try:
            alive, proven, descendant = watch._fresh_liveness(pointer)
        except Exception:  # noqa: BLE001 — a key never becomes the read's failure
            alive = proven = descendant = None
        # The reduction above takes the same key itself and stores it beside the
        # snapshot it just built, so taking it a second time here would stat
        # every file the run's classification reads twice on every read. The
        # store is the reduction's own, and the entry is always the one this
        # call wrote or served; a direct computation remains as the fallback for
        # the one case the store cannot answer, a run the reduction refused.
        remembered = reduction.get(run_id)
        reuse = remembered[0] if remembered else None
        if reuse is None:
            try:
                reuse = watch._snapshot_reuse_key(pointer)
            except Exception:  # noqa: BLE001 — a key never becomes the read's failure
                reuse = None
        rows.append(
            [
                run_id,
                state,
                repr(alive),
                repr(proven),
                repr(descendant),
                repr(snapshot.get("wait_overdue")),
                reuse or "",
            ]
        )
        states[run_id] = snapshot
    rows.sort()
    return rows, states


def roadmap_snapshot_key(
    project: str,
    mounts: Mapping[str, Path],
    code_stamp: str,
    *,
    shape: Mapping[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return the key a roadmap answer is valid under, and its live-run states.

    ``shape`` names the request the answer belongs to — the view, the sprint,
    the path budget, the cursor. Two calls that differ in any of them compose
    different payloads from the same unchanged project, so an answer is only
    valid for the request it was composed for.
    """
    generations = {
        name: roadmap_project_generation(name, docs_dir)
        for name, docs_dir in sorted(mounts.items())
    }
    live_rows, live_states = _roadmap_live_readings(project)
    key = json.dumps(
        {
            "version": _ROADMAP_SNAPSHOT_VERSION,
            "code": code_stamp,
            "project": project,
            "shape": dict(shape or {}),
            "projects": generations,
            "live": live_rows,
        },
        sort_keys=True,
    )
    return key, live_states


def roadmap_snapshot_path(project: str) -> Path:
    """Return the node-local file one project's roadmap snapshot lives in."""
    from reckon.crew.review_records import node_local_cache_dir

    return node_local_cache_dir(project) / "roadmap-snapshot.json"


def roadmap_snapshot_load(project: str, key: str) -> dict[str, Any] | None:
    """Return the stored snapshot when the key matches and it is still fresh.

    The persisted file is read on every call: a fresh process, another
    session's server and the current-code child all answer a first call from
    the snapshot one of them computed, which is the whole reason the snapshot
    is on disk rather than in a module global.
    """
    window = roadmap_revalidate_window()
    moment = time.time()
    candidates: list[dict[str, Any]] = []
    with _ROADMAP_SNAPSHOT_LOCK:
        served = _ROADMAP_SNAPSHOTS.get(project)
    if served is not None and served[0] == key:
        candidates.append(served[1])
    try:
        raw = roadmap_snapshot_path(project).read_text()
        persisted = json.loads(raw)
    except (OSError, ValueError):
        persisted = None
    if isinstance(persisted, dict) and persisted.get("key") == key:
        candidates.append(persisted)
    for record in candidates:
        stored_at = record.get("stored_at")
        if not isinstance(stored_at, (int, float)):
            continue
        if window <= 0 or moment - float(stored_at) > window:
            continue
        payload = record.get("payload")
        if isinstance(payload, dict):
            return payload
    return None


def roadmap_snapshot_store(project: str, key: str, payload: dict[str, Any]) -> None:
    """Store a snapshot under its key, in process and on node-local disk."""
    record = {
        "version": _ROADMAP_SNAPSHOT_VERSION,
        "key": key,
        "stored_at": time.time(),
        "payload": payload,
    }
    with _ROADMAP_SNAPSHOT_LOCK:
        _ROADMAP_SNAPSHOTS[project] = (key, record)
    path = roadmap_snapshot_path(project)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record))
        tmp.replace(path)
    except (OSError, TypeError, ValueError):
        # A snapshot that cannot be persisted costs the next process a cold
        # read rather than an incorrect one: the answer already served is the
        # composition just computed, and only the cache of it is lost.
        return
