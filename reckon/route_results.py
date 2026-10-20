"""Stored route results, and the child process that keeps one current.

Discovery, the checks summary and the fleet rollup each hold the interpreter
deriving them for seconds, so a request answers from the last result stored for
its key instead: the store is read, the result is served with its age, and a
miss starts one child process that derives the key and stores it. At most one
child runs per key, and a waiter thread publishes what it stored — a
fleet-scoped result to every open change stream, a project-scoped one to the
tree it belongs to.

Only the served process opts in to the store, through :func:`set_enabled`. A
handler driven by a test or another caller derives its route inline and starts
no child that would write a cache outside its sandbox.

The derivations a route is computed from live in :mod:`reckon.serve`, so they
are imported at call time rather than at module level, as
:mod:`reckon.change_streams` does.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from reckon import compliance, metadata_index, served_code
from reckon._store import write_json_atomically

_REFRESH_SCHEMA = "reckon-route-result"
_REFRESH_STORE_VERSION = 2
_SOURCE_REVISION_UNSET = object()

#: Whether handlers answer from the store at all. Only the served process sets
#: this, in ``serve.main``; a test or an embedded caller derives inline.
_REFRESH_ENABLED = False
#: The child process deriving each key, so at most one runs per key.
_REFRESH_CHILDREN: dict[tuple[str, ...], subprocess.Popen] = {}
_REFRESH_LOCK = threading.Lock()

#: How long the running children are given to stop once they are asked to,
#: before they are killed. A derivation is seconds of work, so a child that has
#: not gone by then is one that would not go at all.
_STOP_TIMEOUT_S = 5.0
_VELOCITY_REFRESH_S = 15 * 60


def set_enabled(enabled: bool) -> None:
    """Set whether a route answers from its stored result."""

    global _REFRESH_ENABLED  # noqa: PLW0603 — one switch for the whole process
    _REFRESH_ENABLED = enabled


def stop_children() -> None:
    """Stop every running refresh child, so none outlives this process.

    A child is a separate process holding the interpreter and writing a stored
    result, so one left running past the server's own exit writes into a home
    its owner is removing, and under a code reload keeps deriving for an image
    nobody can reach. Each running child is asked to terminate, given a bounded
    time to go, and killed if it will not. The registry is emptied first, so a
    child started while this runs is not stopped by the same call.
    """

    with _REFRESH_LOCK:
        children = list(_REFRESH_CHILDREN.values())
        _REFRESH_CHILDREN.clear()
    for child in children:
        if child.poll() is not None:
            continue
        with contextlib.suppress(OSError):
            child.terminate()
    deadline = time.monotonic() + _STOP_TIMEOUT_S
    for child in children:
        try:
            child.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                child.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=1.0)


def _refresh_store_path(key: tuple[str, ...]) -> Path:
    from reckon.serve import _MOUNTS_FILE

    return metadata_index._persisted_cache_path(
        "route-result", _REFRESH_STORE_VERSION, *key, str(_MOUNTS_FILE or "")
    )


def _source_revision() -> str | None:
    from reckon.serve import _SOURCE_SNAPSHOT

    return _SOURCE_SNAPSHOT.revision if _SOURCE_SNAPSHOT is not None else None


def _refresh_result(key: tuple[str, ...]) -> tuple[object, float, str] | None:
    """Return ``(result, age_seconds, version)`` for a key, or None.

    Age is read from the store file's own mtime, so a route can report how
    stale the answer it serves is without recording a clock of its own.
    """

    path = _refresh_store_path(key)
    try:
        stat = path.stat()
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(raw, dict)
        or raw.get("schema") != _REFRESH_SCHEMA
        or raw.get("key") != list(key)
    ):
        return None
    fingerprints = raw.get("source_fingerprints")
    if isinstance(fingerprints, dict) and fingerprints:
        if not served_code.definitions_unchanged(
            served_code.package_root(), fingerprints
        ):
            return None
    elif (
        "source_fingerprints" in raw or raw.get("source_revision") != _source_revision()
    ):
        return None
    age = max(0.0, time.time() - stat.st_mtime)
    return raw.get("result"), age, str(raw.get("version", ""))


def _store_refresh_result(
    key: tuple[str, ...],
    result: object,
    *,
    source_revision: str | object | None = _SOURCE_REVISION_UNSET,
    source_fingerprints: dict[str, dict[str, object]] | None = None,
) -> str:
    """Write one key's stored result; return the version it was written under."""

    if source_revision is _SOURCE_REVISION_UNSET:
        source_revision = _source_revision()
    version = str(time.time_ns())
    payload = {
        "schema": _REFRESH_SCHEMA,
        "key": list(key),
        "version": version,
        "source_revision": source_revision,
        "result": result,
    }
    if source_fingerprints is not None:
        payload["source_fingerprints"] = source_fingerprints
    try:
        write_json_atomically(
            _refresh_store_path(key),
            payload,
            fsync=False,
            indent=None,
            ensure_ascii=False,
        )
    except OSError as exc:
        print(f"could not store {key} result: {exc}", flush=True)
    return version


def _is_fleet_key(key: tuple[str, ...]) -> bool:
    return key[0] == "rollup" or (key[0] == "velocity" and key[1] == "*")


def velocity_key(
    project: str,
    since: str | None,
    until: str | None,
    fields: str | None,
    limit: int | None,
    cursor: str | None,
) -> tuple[str, ...]:
    """Key a velocity answer by scope and its normalised request."""

    from reckon import velocity

    if since is None and until is None:
        return "velocity", project, "default"
    start = velocity.stamp(since)
    end = velocity.stamp(until)
    selected = ",".join(
        sorted({part.strip() for part in (fields or "").split(",") if part.strip()})
    )
    return (
        "velocity",
        project,
        "window",
        velocity.iso(start) if start is not None else (since or ""),
        velocity.iso(end) if end is not None else (until or ""),
        selected,
        str(limit) if limit is not None else "",
        cursor or "",
    )


def velocity_default_response(
    project: str,
    stored: object,
    fields: str | None,
    limit: int | None,
    cursor: str | None,
) -> dict:
    """Select a caller's blocks from the unpaged stored default report."""

    from reckon import velocity

    if not isinstance(stored, dict) or stored.get("ok") is False:
        return stored
    return velocity.select_view_blocks(
        project, stored, fields=fields, limit=limit, cursor=cursor
    )


def velocity_result(key: tuple[str, ...]) -> tuple[object, float] | None:
    """Read a velocity answer and refresh a stale moving window."""

    if not _REFRESH_ENABLED:
        return _route_compute(key), 0.0
    stored = _refresh_result(key)
    if stored is None:
        if not _start_refresh(key):
            raise OSError(f"velocity refresh did not start for {key[1]}")
        return None
    result, age, _version = stored
    if key[2] == "default":
        from reckon import velocity

        end = result.get("window", {}).get("end") if isinstance(result, dict) else None
        end_epoch = velocity.stamp(end)
        if end_epoch is None or time.time() - end_epoch > _VELOCITY_REFRESH_S:
            _start_refresh(key)
    return result, age


def _compute_refresh(key: tuple[str, ...]) -> object:
    """Derive one key's result — the child's entry point, and a cold route's."""

    from reckon.serve import (
        _STATE_ROOT,
        _attach_schedule,
        collect_projects,
        discover_plans,
        load_mounts,
    )

    mounts = load_mounts()
    kind = key[0]
    if kind == "discover":
        project = key[1]
        result = discover_plans(mounts[project], project, _STATE_ROOT)
        return _attach_schedule(result, project, mounts, reference=datetime.now(UTC))
    if kind == "checks":
        project = key[1]
        docs_dir = Path(mounts[project])
        compliance.refresh(docs_dir, project)
        return compliance.project_checks(docs_dir, project)
    if kind == "rollup":
        return collect_projects(mounts)
    if kind == "velocity":
        from reckon import velocity

        project = key[1]
        if key[2] == "default":
            from reckon.serve import _velocity_default_window

            since, until = _velocity_default_window()
            checkouts = (
                {name: str(Path(docs).parent) for name, docs in mounts.items()}
                if project == "*"
                else {project: str(Path(mounts[project]).parent)}
            )
            return velocity.report(checkouts, start=since, end=until)
        since, until = key[3], key[4] or None
        fields = key[5] or None
        limit = int(key[6]) if key[6] else None
        cursor = key[7] or None
        checkout = str(Path(mounts[project]).parent) if project != "*" else None
        return velocity.view(
            project,
            since=since,
            until=until,
            checkout_path=checkout,
            fields=fields,
            limit=limit,
            cursor=cursor,
        )
    raise ValueError(f"unknown refresh key: {key!r}")


def _route_compute(key: tuple[str, ...]) -> object:
    """A route's own derivation, which the refresh child's differs from.

    The checks child checks every pending document before storing the summary,
    so the summary it stores is the checked one; a route that found nothing
    stored still has to report how many documents are pending, which is the
    count its own derivation produces. Every other key derives the same
    payload in both places.
    """

    if key[0] == "checks":
        from reckon.serve import load_mounts

        project = key[1]
        return compliance.project_checks(Path(load_mounts()[project]), project)
    return _compute_refresh(key)


def _spawn_refresh(argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL)


def _publish_refresh(key: tuple[str, ...]) -> None:
    """Wake the streams a finished refresh belongs to.

    A fleet-scoped result goes to every open stream, recorded by key so each
    stream writes one event for a key whose version moved. A project-scoped
    result wakes that project's stream, which re-reads it.
    """

    from reckon.serve import _FLEET_WATCH

    watch = _FLEET_WATCH
    if watch is None or not watch.running:
        return
    stored = _refresh_result(key)
    if stored is None:
        return
    if _is_fleet_key(key):
        watch.publish_fleet(key, stored[2])
        return
    root = _refresh_key_root(key)
    if root is not None:
        watch.notify(root)


def _refresh_key_root(key: tuple[str, ...]) -> Path | None:
    """The docs tree a project-scoped key belongs to, or None."""

    from reckon.serve import load_mounts

    if len(key) < 2:
        return None
    try:
        mounts = load_mounts()
    except Exception:  # noqa: BLE001 — a mount fault must not kill the waiter
        return None
    path = mounts.get(key[1])
    return Path(path) if path is not None else None


def _refresh_waiter(key: tuple[str, ...], child: subprocess.Popen) -> None:
    """Wait for one refresh child, then publish what it stored.

    A child lost before it stores is a miss, not a crash: the key keeps
    whatever it last stored and the next request starts a fresh child.
    """

    with contextlib.suppress(Exception):
        child.wait()
    with _REFRESH_LOCK:
        if _REFRESH_CHILDREN.get(key) is child:
            _REFRESH_CHILDREN.pop(key, None)
    _publish_refresh(key)


def _start_refresh(key: tuple[str, ...]) -> bool:
    """Derive one key's result in a child process; True while one is running.

    A cold derivation holds the interpreter for seconds — a rollup of every
    mount, a compliance pass over a large project — so it runs out of process,
    at lower priority, and writes the store the route reads. At most one child
    runs per key, and a parent waiter thread waits on it so a fleet-scoped
    result reaches every open stream.
    """

    from reckon.serve import _MOUNTS_FILE

    if not _REFRESH_ENABLED:
        return False
    with _REFRESH_LOCK:
        scope = key[:2] if key[0] == "velocity" else key
        if any(
            (child_key[:2] if child_key[0] == "velocity" else child_key) == scope
            and child.poll() is None
            for child_key, child in _REFRESH_CHILDREN.items()
        ):
            return True
        argv = [
            sys.executable,
            "-m",
            "reckon.route_results",
        ]
        argv += (
            ["--key-json", json.dumps(key)]
            if key[0] == "velocity"
            else ["--key", ":".join(key)]
        )
        if _MOUNTS_FILE is not None:
            argv += ["--mounts", str(_MOUNTS_FILE)]
        source_revision = _source_revision()
        if source_revision is not None:
            argv += ["--source-revision", source_revision]
        nice = shutil.which("nice")
        if nice:
            argv = [nice, "-n", "10", *argv]
        try:
            child = _spawn_refresh(argv)
        except OSError as exc:
            print(f"refresh for {key} did not start: {exc}", flush=True)
            return False
        if child is None:
            return False
        _REFRESH_CHILDREN[key] = child
        threading.Thread(
            target=_refresh_waiter,
            args=(key, child),
            name=f"reckon-refresh-wait-{key[0]}",
            daemon=True,
        ).start()
        return True


def _served_result(
    key: tuple[str, ...], *, spawn_on_miss: bool = True
) -> tuple[object, float]:
    """Answer one route from its stored result; derive a cold miss inline.

    A stored result is returned at once with the age a route reports in a
    response header. Only when nothing is stored does the request pay for the
    derivation, and the route also starts the child that will store a fresh
    one, so the next request answers from the store.

    A handler that is not the served process keeps its old behaviour: it
    derives the route inline and starts nothing, so a test or an embedded
    caller neither reads a result another process left nor writes a store.
    A route whose child differs from its own derivation asks for the cold
    derivation without a spawn, and owns the decision to start one.
    """

    if not _REFRESH_ENABLED:
        return _route_compute(key), 0.0
    stored = _refresh_result(key)
    if stored is not None:
        return stored[0], stored[1]
    result = _route_compute(key)
    if spawn_on_miss:
        _start_refresh(key)
    return result, 0.0


def _stored_discovery(project: str, docs_dir: Path) -> object | None:
    """Read a project's stored discovery, spawning a change-free refresh on a miss.

    The change stream reads discovery through here rather than deriving it: a
    change-driven recompute is the fleet watch's job, so a stream that finds
    nothing stored starts one and answers from what it has. A handler that is
    not the served process derives it inline, as it did before there was a
    store to read.
    """

    from reckon.serve import _STATE_ROOT, discover_plans

    if not _REFRESH_ENABLED:
        return discover_plans(docs_dir, project, _STATE_ROOT)
    stored = _refresh_result(("discover", project))
    if stored is None:
        _start_refresh(("discover", project))
        return None
    return stored[0]


def _discovery_digest(result: object) -> str:
    if not isinstance(result, dict):
        return ""
    provenance = result.get("provenance")
    if not isinstance(provenance, dict):
        return ""
    return str(provenance.get("content_digest", ""))


def _fleet_versions() -> dict[str, str]:
    """The fleet-scoped result versions the parent has published, by key."""

    from reckon.serve import _FLEET_WATCH

    watch = _FLEET_WATCH
    if watch is None or not watch.running:
        return {}
    return watch.fleet_versions()


def _refresh_main(argv: list[str]) -> int:
    """Child entry point: derive one key's result and store it."""

    from reckon.serve import _resolve_paths

    parser = argparse.ArgumentParser(prog="python -m reckon.route_results")
    key_args = parser.add_mutually_exclusive_group(required=True)
    key_args.add_argument("--key")
    key_args.add_argument("--key-json")
    parser.add_argument("--mounts", default=None)
    parser.add_argument("--source-revision")
    args = parser.parse_args(argv)
    key = (
        tuple(json.loads(args.key_json))
        if args.key_json
        else tuple(args.key.split(":"))
    )
    if args.mounts:
        os.environ["RECKON_MOUNTS_PATH"] = args.mounts
    _resolve_paths(Path(args.mounts) if args.mounts else None)
    snapshot = served_code.take_snapshot()
    executed = served_code.ExecutedSource(snapshot.root)
    if not executed.start():
        raise RuntimeError("cannot track refresh definitions")
    try:
        result = _compute_refresh(key)
    finally:
        executed.stop()
    fingerprints = served_code.definition_fingerprints(snapshot, executed.definitions())
    if not fingerprints:
        raise RuntimeError("refresh recorded no package definitions")
    _store_refresh_result(
        key,
        result,
        source_revision=args.source_revision,
        source_fingerprints=fingerprints,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_refresh_main(sys.argv[1:]))
