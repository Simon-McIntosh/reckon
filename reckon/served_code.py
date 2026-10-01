"""Tell whether the running server still runs the code on disk.

The served process imports reckon's Python once, at start, while the client's
JSX and CSS are compiled per request from the working tree. A checkout that
moves on while the process keeps running therefore serves current client code
against an older server, and nothing fails loudly: a route the client now
expects answers 404 and the client falls back to a slower path.

A snapshot records every source file of the package as the process started —
its stat identity and a hash of its bytes. A report compares that snapshot
with the files on disk: a file whose identity moved is re-hashed, so a touch
that leaves the bytes alone is not drift, and a file added or removed is. The
report is what the server serves and what the client renders; the client never
decides staleness itself.
"""

from __future__ import annotations

import hashlib
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from reckon.file_memo import file_signature

#: The command a reader is told to run; the service owns the restart.
RESTART_COMMAND = "reckon service restart"

#: How long one report is reused. Every open page polls, so without reuse each
#: poll would re-stat the whole package on a shared filesystem.
_REPORT_TTL_S = 30.0

_Signature = tuple[int, int, int, int, int]


@dataclass(frozen=True)
class SourceSnapshot:
    """The package source as the process found it at start."""

    root: Path
    #: Package-relative posix path → (stat identity, sha256 of the bytes).
    files: dict[str, tuple[_Signature, str]] = field(default_factory=dict)
    taken_at: str = ""
    revision: str | None = None


def package_root() -> Path:
    """Return the directory of the installed reckon package."""

    return Path(__file__).resolve().parent


def _source_files(root: Path) -> dict[str, Path]:
    return {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = completed.stdout.strip()
    return output if completed.returncode == 0 and output else None


def take_snapshot(root: Path | None = None) -> SourceSnapshot:
    """Record every source file under ``root`` (default: this package)."""

    root = Path(root) if root is not None else package_root()
    files: dict[str, tuple[_Signature, str]] = {}
    for relative, path in _source_files(root).items():
        try:
            files[relative] = (file_signature(path), _sha256(path))
        except OSError:
            continue
    return SourceSnapshot(
        root=root,
        files=files,
        taken_at=datetime.now(UTC).isoformat(timespec="seconds"),
        revision=_git(root, "rev-parse", "HEAD"),
    )


_HASH_MEMO: dict[tuple[str, _Signature], str] = {}
_REPORTS: dict[int, tuple[float, dict]] = {}
_LOCK = threading.Lock()


def forget_reports() -> None:
    """Drop every reused report and hash, as a fresh process would hold none."""

    with _LOCK:
        _REPORTS.clear()
        _HASH_MEMO.clear()


def _current_hash(path: Path, signature: _Signature) -> str:
    key = (str(path), signature)
    with _LOCK:
        known = _HASH_MEMO.get(key)
    if known is not None:
        return known
    digest = _sha256(path)
    with _LOCK:
        _HASH_MEMO[key] = digest
    return digest


def _compare(snapshot: SourceSnapshot) -> dict:
    current = _source_files(snapshot.root)
    changed: list[str] = []
    added: list[str] = []
    removed: list[str] = []
    for relative, path in current.items():
        recorded = snapshot.files.get(relative)
        if recorded is None:
            added.append(relative)
            continue
        try:
            signature = file_signature(path)
            if (
                signature != recorded[0]
                and _current_hash(path, signature) != recorded[1]
            ):
                changed.append(relative)
        except OSError:
            removed.append(relative)
    removed.extend(relative for relative in snapshot.files if relative not in current)
    stale = bool(changed or added or removed)

    disk_revision = _git(snapshot.root, "rev-parse", "HEAD")
    commits_behind: int | None = None
    if stale and snapshot.revision and disk_revision:
        counted = _git(
            snapshot.root,
            "rev-list",
            "--count",
            f"{snapshot.revision}..{disk_revision}",
            "--",
            ".",
        )
        commits_behind = int(counted) if counted and counted.isdigit() else None
    return {
        "stale": stale,
        "summary": _summary(
            len(changed) + len(added) + len(removed),
            commits_behind,
            snapshot.taken_at,
        )
        if stale
        else None,
        "started_at": snapshot.taken_at,
        "revision": snapshot.revision,
        "disk_revision": disk_revision,
        "commits_behind": commits_behind,
        "changed": sorted(changed),
        "added": sorted(added),
        "removed": sorted(removed),
        "restart_command": RESTART_COMMAND,
    }


def _summary(files: int, commits_behind: int | None, started_at: str) -> str:
    """One sentence a reader can act on, for every surface that shows drift."""

    commits = (
        f"{commits_behind} commit{'' if commits_behind == 1 else 's'} to reckon/, "
        if commits_behind
        else ""
    )
    try:
        started = datetime.fromisoformat(started_at).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        started = started_at or "an unknown time"
    return (
        f"The server is running older code than is on disk: {commits}"
        f"{files} file{'' if files == 1 else 's'} changed since it started at "
        f"{started}. Pages may fall back to slower paths until it restarts."
    )


def drift(snapshot: SourceSnapshot, *, max_age_s: float = 0.0) -> dict:
    """Compare ``snapshot`` with the package on disk.

    ``max_age_s`` reuses a report that young for the same snapshot, which is
    what the server passes so a page poll does not re-stat the package each
    time. The default computes afresh.
    """

    key = id(snapshot)
    now = time.monotonic()
    if max_age_s > 0:
        with _LOCK:
            reused = _REPORTS.get(key)
        if reused is not None and now - reused[0] < max_age_s:
            return dict(reused[1])
    report = _compare(snapshot)
    with _LOCK:
        _REPORTS[key] = (now, report)
    return dict(report)


def served_report(snapshot: SourceSnapshot | None) -> dict | None:
    """Return the report a server serves, reusing one for a short window."""

    if snapshot is None:
        return None
    return drift(snapshot, max_age_s=_REPORT_TTL_S)
