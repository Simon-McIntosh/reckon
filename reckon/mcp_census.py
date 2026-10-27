"""Census of Claude Code MCP connections, grouped by failure cause.

Claude Code writes one log per MCP server connection under
``<log_root>/<cwd-key>/mcp-logs-<server>/<timestamp>.jsonl``: the ``cwd-key``
directory holds the session's working directory, and each file is one
connection. The census reads those logs over an explicit window and reports,
per server, the connections seen, how many failed, and the failures attributed
to a cause. Each cause carries its count, its newest occurrence, one example log
path and the kind of working directory it failed in — a main checkout, a worker
worktree, the system temporary directory, or a directory that is gone.

The causes are the failure signatures the plan's first section records, matched
by the module-level table below. A failure is attributed once, to the first
cause in the table's own order that its log carries, so the cause counts sum to
the number of failures; the signatures it also carried that come later in the
table are reported beside that cause rather than counted again. A failure no
pattern matches is reported as ``unclassified`` with its first line rather than
dropped, because the next unknown cause is the one this check exists to
surface. The census also counts the ``storage-slow`` tool results per tool over
the same window, read from the Claude Code transcripts through
:func:`reckon.velocity.transcript_index`.

It reads and never writes. Its runtime is bounded by naming the log directories
it walks (``log_root``), by memoising the directory kind per unique working
directory so a tree that produced hundreds of connections is classified once,
and by skipping transcript files older than the window before they are opened.
The same skip bounds the connection walk: a working directory's log directory
whose modification time predates the window cannot hold a connection inside it,
so it is never descended, and only a log file created inside the window is
opened. Both directory levels are walked once for every tracked server under a
small thread pool, because the tree holds tens of thousands of directories and
each call is a metadata round trip on a shared filesystem.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from reckon._mcp_tools import STORAGE_SLOW
from reckon._timestamps import parse_utc
from reckon.velocity import TRANSCRIPT_ROOT, transcript_index, transcript_tool_blocks

# The root Claude Code writes its per-server connection logs under.
LOG_ROOT = Path.home() / ".cache" / "claude-cli-nodejs"

# The servers whose connections the census reports; others (imas-dd, the hosted
# integrations) are outside this check.
SERVERS = ("reckon", "imas-cx")

# The failure signatures, as patterns matched against a connection log's text.
# A connection is counted once per cause whose pattern it carries. The order is
# the order causes are reported in.
CAUSES: tuple[tuple[str, str], ...] = (
    ("CONNECTION_CLOSED", "CONNECTION_CLOSED"),
    ("CONNECT_TIMEOUT", "CONNECT_TIMEOUT"),
    ("package_metadata", "Failed to generate package metadata for"),
    ("missing_directory", "No such file or directory (os error 2)"),
    ("read_only_environment", "Read-only file system"),
    ("server_discover", "input_value='server/discover'"),
)

UNCLASSIFIED = "unclassified"

# The walk is metadata-bound on a shared filesystem, so a few threads overlap
# the per-directory round trips. The bound stays small because the census runs
# beside other agents on the same allocation.
WALK_WORKERS = 16

MAIN_CHECKOUT = "main checkout"
WORKER_WORKTREE = "worker worktree"
TEMPORARY = "temporary"
GONE = "gone"

_CONNECTION_STAMP = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})-(\d{3})Z"
)
_LABEL = re.compile(r'"label"\s*:\s*"([^"]+)"')
# The sentinel as it appears in a tool result's payload, so a transcript that
# merely mentions the phrase in prose is not counted as a storage-slow result.
_SLOW_ERROR = re.compile(r'"error":\s*"?' + re.escape(STORAGE_SLOW) + r'"?')
_TOOL_PREFIX = re.compile(r"^mcp__[^_]+__")


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat().replace("+00:00", "Z")


def _stamp_from_name(path: Path) -> float | None:
    """The epoch a connection log's filename records, or None."""

    match = _CONNECTION_STAMP.search(path.name)
    if match is None:
        return None
    year, month, day, hour, minute, second, millis = match.groups()
    text = f"{year}-{month}-{day}T{hour}:{minute}:{second}.{millis}Z"
    parsed = parse_utc(text)
    return parsed.timestamp() if parsed is not None else None


def _read_connection(path: Path) -> tuple[str | None, str | None, str]:
    """Read one connection log into (cwd, first error line, whole text)."""

    text = path.read_text(errors="replace")
    cwd = None
    first_error = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if cwd is None and record.get("cwd"):
            cwd = str(record["cwd"])
        error = record.get("error")
        if error and first_error is None:
            body = str(error).strip()
            first_error = body.splitlines()[0] if body else ""
    return cwd, first_error, text


def _under(path: Path, root: Path) -> bool:
    try:
        path = path.expanduser()
        root = root.expanduser()
    except (OSError, RuntimeError):
        return False
    return path == root or root in path.parents


def _directory_kind(cwd: Path, *, temp_root: Path) -> tuple[str, str | None]:
    """The kind of working directory a connection failed in, and its repository.

    A directory under ``temp_root`` reads as ``temporary``. Otherwise an
    existing directory is a worker worktree when
    :func:`reckon.cli_entry._in_linked_worktree` says so and a main checkout
    when it does not. A missing directory is classified by where it sat: one a
    ``.reckon-worktrees`` path names reads as a worker worktree of that
    repository, since
    :func:`reckon.hooks.coordinator_obligations._repository_named_by_worktree`
    answers from the path alone for a tree that has been reaped; any other
    missing path reads as gone, never as a main checkout.
    """

    if _under(cwd, temp_root):
        return TEMPORARY, None
    if cwd.is_dir():
        from reckon.cli_entry import _in_linked_worktree

        if _in_linked_worktree(cwd):
            return WORKER_WORKTREE, None
        return MAIN_CHECKOUT, None
    from reckon.hooks.coordinator_obligations import _repository_named_by_worktree

    repository = _repository_named_by_worktree(cwd)
    if repository is not None:
        return WORKER_WORKTREE, repository
    return GONE, None


def _kind(cwd: str | None, temp_root: Path, memo: dict) -> tuple[str, str | None]:
    if not cwd:
        return GONE, None
    if cwd not in memo:
        memo[cwd] = _directory_kind(Path(cwd).expanduser(), temp_root=temp_root)
    return memo[cwd]


def _storage_slow(transcript_root: Path, start: float, end: float) -> dict:
    index = transcript_index(transcript_root)
    counts: collections.Counter = collections.Counter()
    for paths in index.values():
        for path in paths:
            try:
                if path.stat().st_mtime < start:
                    continue
            except OSError:
                continue
            uses: dict = {}
            for block in transcript_tool_blocks(
                path, window_start=_iso(start), window_end=_iso(end)
            ):
                if block.get("type") == "tool_use":
                    name = str(block.get("name") or "")
                    uses[block.get("id")] = _TOOL_PREFIX.sub("", name) or name
                elif block.get("type") == "tool_result":
                    text = block.get("content")
                    if isinstance(text, list):
                        text = "\n".join(
                            str(part.get("text", ""))
                            for part in text
                            if isinstance(part, dict)
                        )
                    text = str(text or "")
                    if not _SLOW_ERROR.search(text):
                        continue
                    label = _LABEL.search(text)
                    tool = uses.get(block.get("tool_use_id")) or (
                        label.group(1) if label else None
                    )
                    counts[tool or "unknown"] += 1
    return dict(counts)


def _stamp(name: str) -> float | None:
    parsed = parse_utc(name)
    return parsed.timestamp() if parsed is not None else None


def census(
    *,
    window_start: str,
    window_end: str,
    log_root: Path = LOG_ROOT,
    transcript_root: Path = TRANSCRIPT_ROOT,
    temp_root: Path | None = None,
) -> dict:
    """Report MCP connections and ``storage-slow`` results over one window.

    ``window_start`` and ``window_end`` are ISO instants; ``log_root`` and
    ``transcript_root`` name the trees walked, and ``temp_root`` names the
    system temporary directory the directory-kind rule reads, defaulting to
    :func:`tempfile.gettempdir`. The return is a plain dict so an agent can
    serialise it and the doctor block can print it.
    """

    start = _stamp(window_start)
    end = _stamp(window_end)
    if start is None or end is None:
        raise ValueError("census window needs two ISO instants")
    if end < start:
        start, end = end, start
    if temp_root is None:
        temp_root = Path(tempfile.gettempdir())

    root = Path(log_root).expanduser()
    memo: dict = {}
    by_server: dict[str, list[Path]] = {server: [] for server in SERVERS}
    for server, path in _connection_log_files(root, start=start):
        by_server[server].append(path)
    servers: dict[str, dict] = {}
    for server in SERVERS:
        connections = 0
        failed = 0
        causes: dict[str, dict] = {}
        for path in sorted(by_server[server]):
            when = _stamp_from_name(path)
            if when is None or not start <= when <= end:
                continue
            connections += 1
            cwd, first_error, text = _read_connection(path)
            if first_error is None:
                continue
            failed += 1
            kind, repository = _kind(cwd, temp_root, memo)
            matched = [n for n, pattern in CAUSES if pattern in text]
            # One cause per failure: the first signature the table lists. The
            # later signatures it also carried are reported beside that cause,
            # so the counts stay a partition of ``failed``.
            primary = matched[0] if matched else UNCLASSIFIED
            entry = causes.setdefault(
                primary,
                {
                    "count": 0,
                    "newest": None,
                    "newest_epoch": None,
                    "example_path": None,
                    "directory_kind": None,
                    "repository": None,
                    "first_line": None,
                    "also_carried": {},
                },
            )
            entry["count"] += 1
            for name in matched[1:]:
                entry["also_carried"][name] = entry["also_carried"].get(name, 0) + 1
            if entry["newest_epoch"] is None or when > entry["newest_epoch"]:
                entry["newest_epoch"] = when
                entry["newest"] = _iso(when)
                entry["example_path"] = str(path)
                entry["directory_kind"] = kind
                entry["repository"] = repository
            if primary == UNCLASSIFIED and entry["first_line"] is None:
                entry["first_line"] = first_error
        servers[server] = {
            "connections": connections,
            "failed": failed,
            "succeeded": connections - failed,
            "causes": [_public(causes[n], n) for n in _order(causes)],
        }

    return {
        "window": {"start": window_start, "end": window_end},
        "servers": servers,
        "storage_slow": _storage_slow(Path(transcript_root).expanduser(), start, end),
    }


def _order(causes: dict) -> list[str]:
    known = [name for name, _ in CAUSES]
    tail = [UNCLASSIFIED] if UNCLASSIFIED in causes else []
    return [n for n in known if n in causes] + tail


def _public(entry: dict, name: str) -> dict:
    row = {
        "cause": name,
        "count": entry["count"],
        "newest": entry["newest"],
        "example_path": entry["example_path"],
        "directory_kind": entry["directory_kind"],
        "repository": entry["repository"],
        "also_carried": {
            later: entry["also_carried"][later]
            for later, _ in CAUSES
            if later in entry["also_carried"]
        },
    }
    if name == UNCLASSIFIED:
        row["first_line"] = entry["first_line"]
    return row


def _server_log_directories(key: str, *, start: float) -> list[tuple[str, str]]:
    """The ``mcp-logs-<server>`` directories under one ``cwd-key`` that survive.

    A log directory whose modification time predates ``start`` cannot hold a
    connection created inside the window, because writing an entry into a
    directory updates that directory's own time, so it is dropped without being
    descended. ``os.scandir`` reads the entry type from the directory listing
    rather than statting every path, so the walk costs one call per directory
    on the tracked servers rather than one per entry.
    """

    wanted = {f"mcp-logs-{server}": server for server in SERVERS}
    survivors: list[tuple[str, str]] = []
    try:
        with os.scandir(key) as entries:
            for entry in entries:
                server = wanted.get(entry.name)
                if server is None or not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    if entry.stat(follow_symlinks=False).st_mtime < start:
                        continue
                except OSError:
                    continue
                survivors.append((server, entry.path))
    except OSError:
        pass
    return survivors


def _connection_files(directory: str) -> list[str]:
    """The ``.jsonl`` connection logs in one server log directory.

    A log is created when its connection starts and appended to while it runs,
    so its filename stamp is the moment it entered the window. That stamp
    filters the list before any file is read, so a file outside the window is
    never opened.
    """

    files: list[str] = []
    try:
        with os.scandir(directory) as entries:
            files = [
                entry.path
                for entry in entries
                if entry.name.endswith(".jsonl")
                and entry.is_file(follow_symlinks=False)
            ]
    except OSError:
        pass
    return files


def _connection_log_files(root: Path, *, start: float):
    """Yield ``(server, path)`` for the connection logs under ``root``.

    The tree is ``<root>/<cwd-key>/mcp-logs-<server>/<timestamp>.jsonl``. The
    two levels are walked under a bounded thread pool because each directory
    call is a metadata round trip on the shared filesystem and this tree holds
    tens of thousands of them; the results are sorted so the output never
    depends on completion order.
    """

    try:
        with os.scandir(root) as entries:
            keys = [entry.path for entry in entries if entry.is_dir()]
    except OSError:
        return
    with ThreadPoolExecutor(max_workers=WALK_WORKERS) as pool:
        nested = pool.map(lambda key: _server_log_directories(key, start=start), keys)
        directories = [item for group in nested for item in group]
        opened = pool.map(
            lambda pair: (pair[0], _connection_files(pair[1])),
            directories,
        )
        found: list[tuple[str, str]] = []
        for server, paths in opened:
            found.extend((server, path) for path in paths)
    for server, path in sorted(found):
        yield server, Path(path)
