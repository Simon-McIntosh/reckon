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

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

# Bumped when the key's shape changes in a way a stored snapshot cannot survive:
# a record written by an older shape would be compared field by field against a
# key that no longer has the same fields.
_ROADMAP_SNAPSHOT_VERSION = 2
# A read whose key matches recomputes nothing, so a write that neither reckon
# nor git made — an editor saving a plan in place — would be answered from a
# stale snapshot until something else moved. A full revalidation at most once
# per this window bounds how long that can last.
_ROADMAP_REVALIDATE_DEFAULT_S = 120.0
_ROADMAP_SNAPSHOT_LOCK = threading.Lock()
# In-process reuse, so a hit costs no open beyond the snapshot file itself.
#: Keyed on the project, the docs root and the crew home: a read is answered
#: from one tree and one home's live pointers, so a record made under any other
#: of them is a different answer and must not match.
_ROADMAP_SNAPSHOTS: dict[tuple[str, str, str], tuple[str, dict[str, Any]]] = {}


class _MountsMemo:
    """The mounts last resolved, as one ``(stamp, mounts)`` pair.

    The pair is a single value stored with one assignment, because the server
    answers requests on several threads at once: two stores would let a reader
    see the new stamp beside the mounts resolved under the old one and return a
    set of mounts the stamp it just read no longer describes.
    """

    pair: tuple[int | None, dict[str, Path]] | None = None


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

    A reader takes the stored pair once and uses both halves from that one
    value, so a resolution landing between its two checks cannot hand back
    mounts belonging to a stamp other than the one it matched.
    """
    pair = _MOUNTS_MEMO.pair
    if pair is not None and pair[0] == stamp:
        return pair[1]
    resolved = resolve()
    _MOUNTS_MEMO.pair = (stamp, resolved)
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


def _covered_directory_shape(docs_dir: Path, max_dirs: int = 64, depth: int = 2) -> str:
    """Return the stat identity of the directories a plan read walks.

    A document added to the tree is not a write reckon or git made: it moves no
    staging index, no HEAD and no write counter, so a key built only from those
    would keep answering from a composition that predates the new document.
    What a document's arrival does move is the identity of the directory it was
    written into, so the key carries one stat per covered directory — the docs
    root, and the subdirectories a plan read descends into, bounded in both
    depth and count so a deep figure tree cannot turn the key into a walk.

    The result is a string rather than a list so it compares as one key field
    and serialises into the key's json as a single value.
    """
    from reckon.metadata_index import _dir_identity

    readings: list[str] = []
    pending: list[tuple[Path, str, int]] = [(docs_dir, ".", 0)]
    while pending and len(readings) < max_dirs:
        directory, relative, level = pending.pop()
        identity = _dir_identity(directory)
        readings.append(f"{relative}={identity}")
        if level >= depth:
            continue
        try:
            with os.scandir(directory) as scanned:
                children = sorted(
                    entry.path
                    for entry in scanned
                    if entry.is_dir(follow_symlinks=False)
                )
        except OSError:
            continue
        pending.extend(
            (Path(child), f"{relative}/{os.path.basename(child)}", level + 1)
            for child in reversed(children)
        )
    return "|".join(sorted(readings))


def crew_home_identity() -> str:
    """Return a short digest of the crew home this read's live rows came from.

    A live pointer, a run record and a run's stream all live under the crew
    home, which ``RECKON_HOME`` resolves and a caller may move between reads.
    Two reads of one project and one docs tree under different homes are
    answered from different pointers, so a snapshot composed under one home is
    not a valid answer under another and neither the key nor the in-process
    memo may match across them.
    """
    from reckon.crew import runs as crew_runs

    try:
        home = crew_runs.crew_home()
    except OSError:
        return "none"
    return roadmap_docs_identity(home)


def roadmap_docs_identity(docs_dir: Path | None) -> str:
    """Return a short digest of the absolute path a project's docs live at.

    A project name alone does not name a tree: two checkouts, and two temporary
    trees under one test run, routinely declare the same project out of
    different directories. A snapshot keyed and stored on the name alone would
    be served across them, so the location and the key both carry this digest
    and two trees with one name never open each other's record.
    """
    if docs_dir is None:
        return "none"
    try:
        resolved = str(Path(docs_dir).resolve())
    except OSError:
        resolved = str(docs_dir)
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]


def roadmap_project_generation(project: str, docs_dir: Path | None) -> list[str]:
    """Return the facts one mounted project's plan state is keyed on."""
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
    shape = _covered_directory_shape(docs_dir) if docs_dir is not None else "none"
    return [index_identity, head, read_write_generation(project), shape]


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

    # Every project's pointer is read and this project's kept here: the
    # filter is the same set as a project-scoped listing, and taking it in the
    # snapshot's own module leaves no caller-supplied argument between the
    # listing and the rows the key is built from.
    try:
        pointers = crew_runs.list_live()
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


def _watch_stream_identity(project: str) -> str:
    """Return the identity of the watch stream this project's read consumes.

    A roadmap read derives sprint liveness and live-run classification from the
    project's append-only watch stream. Appending a line to it is not a plan
    write: it moves no staging index, no HEAD and no write counter, and a run
    with no live pointer has no row in the key that could carry it — so a key
    built without the stream would answer a read from the composition made
    before the appended transitions. Size and mtime together move on an append
    and on a truncation, and one stat is the whole cost.
    """
    from reckon.crew import runs as crew_runs

    try:
        observed = crew_runs.watch_stream_path(project).stat()
    except OSError:
        return "none"
    return f"{observed.st_size}:{observed.st_mtime_ns}"


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
    roots = {
        name: roadmap_docs_identity(docs_dir)
        for name, docs_dir in sorted(mounts.items())
    }
    live_rows, live_states = _roadmap_live_readings(project)
    key = json.dumps(
        {
            "version": _ROADMAP_SNAPSHOT_VERSION,
            "code": code_stamp,
            "project": project,
            "roots": roots,
            "home": crew_home_identity(),
            "shape": dict(shape or {}),
            "projects": generations,
            "watch": _watch_stream_identity(project),
            "live": live_rows,
        },
        sort_keys=True,
    )
    return key, live_states


def roadmap_snapshot_path(project: str, docs_dir: Path | None) -> Path:
    """Return the node-local file one tree's roadmap snapshot lives in.

    The file is filed under the filesystem identity of the project's docs root
    as well as its name. The node-local cache directory is shared by every tree
    on the node, so a name alone would let a read of one tree's docs open the
    composition another tree with the same project name made.
    """
    from reckon.crew.review_records import node_local_cache_dir

    identity = roadmap_docs_identity(docs_dir)
    return node_local_cache_dir(project) / f"roadmap-snapshot-{identity}.json"


def roadmap_snapshot_load(
    project: str, docs_dir: Path | None, key: str
) -> dict[str, Any] | None:
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
        served = _ROADMAP_SNAPSHOTS.get(
            (project, roadmap_docs_identity(docs_dir), crew_home_identity())
        )
    if served is not None and served[0] == key:
        candidates.append(served[1])
    try:
        raw = roadmap_snapshot_path(project, docs_dir).read_text()
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


def roadmap_snapshot_store(
    project: str, docs_dir: Path | None, key: str, payload: dict[str, Any]
) -> None:
    """Store a snapshot under its key, in process and on node-local disk."""
    record = {
        "version": _ROADMAP_SNAPSHOT_VERSION,
        "key": key,
        "stored_at": time.time(),
        "payload": payload,
    }
    with _ROADMAP_SNAPSHOT_LOCK:
        _ROADMAP_SNAPSHOTS[
            (project, roadmap_docs_identity(docs_dir), crew_home_identity())
        ] = (key, record)
    from reckon._store import write_atomically

    path = roadmap_snapshot_path(project, docs_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # The snapshot is a disposable cache, so its bytes need not be durable:
        # the rename is what a concurrent reader needs, not a fsync before it.
        write_atomically(
            path,
            lambda handle: handle.write(json.dumps(record)),
            fsync=False,
        )
    except (OSError, TypeError, ValueError):
        # A snapshot that cannot be persisted costs the next process a cold
        # read rather than an incorrect one: the answer already served is the
        # composition just computed, and only the cache of it is lost.
        return
