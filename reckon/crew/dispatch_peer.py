# ruff: noqa: I001, UP035
from __future__ import annotations
import argparse
import ast
import ctypes
import fcntl
import json
import os
import select
import sys
import time
import uuid
from pathlib import (
    Path,
)
from typing import (
    Any,
    Iterable,
    Mapping,
)
from reckon.crew.node import (
    _SAFE_ID,
    _TERMINAL_RUN_PHASES,
    NEEDS_HELP_MARKER,
    CrewError,
    TaskNode,
    parse_duration,
)
from reckon.crew.runs import (
    _utc_now,
    _write_json,
    list_live,
    read_pointer,
    run_dir,
)

_INOTIFY_EVENTS = 0x00000100 | 0x00000008 | 0x00000080


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
    exclude_run_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Find live node pairs that receive a durable peer channel.

    ``exclude_run_ids`` drops runs by identity, and a dispatch that has already
    published its own claim must pass its run id: its claim declares the same
    write paths this node does, so adjacency on those paths would make the run
    its own peer and wire a channel to the worker it has not spawned yet.
    """
    excluded = set(exclude_run_ids)
    adjacent: list[dict[str, Any]] = []
    for pointer in list_live(project=project):
        if str(pointer.get("run_id") or "") in excluded:
            continue
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


def _supervisor_command(argv: list[str]) -> int:
    """Entry point for the detached per-run supervisor process."""
    parser = argparse.ArgumentParser(prog="reckon-supervisor")
    parser.add_argument("--spec", required=True)
    arguments = parser.parse_args(argv)
    return _run_supervisor(Path(arguments.spec))


def _peer_command(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == SUPERVISOR_ENTRY:
        _require_fleet_gate_open()
        return _supervisor_command(arguments[1:])
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

from .dispatch_admission import (  # noqa: E402
    _require_fleet_gate_open,
)

from .dispatch_launch import (  # noqa: E402
    SUPERVISOR_ENTRY,
    _run_supervisor,
)
