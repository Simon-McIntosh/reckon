#!/usr/bin/env python3
"""Stop hook: refuse a crew worker's turn end while its run manifest is unfinished.

A Claude Code worker that ends its turn with a non-terminal manifest costs a
manual resume: the process ends, the record still says ``in-progress``, and a
coordinator cannot tell completion from a truncated turn. This hook makes the
manifest a precondition of the terminal stop.

The hook reads the harness's Stop payload on stdin. It resolves the run manifest
two ways, in order: from ``RECKON_MANIFEST`` (exported into every dispatched
worker's environment), else by matching the payload's working directory against
the live crew run pointers on this host. When no run resolves it writes nothing
and exits 0, so a coordinator or an interactive session is never affected.

When a run does resolve, the hook blocks while the manifest is absent or its
``status:`` line does not name a terminal value. The reason names the manifest
path and what is missing. So a worker that genuinely cannot finish is never
trapped, the hook blocks at most three times for a run, counting in a file
unlikely to collide with a manifest block key.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

TERMINAL_STATUSES = frozenset({"complete", "blocked", "failed"})
BLOCK_LIMIT = 3
COUNTER_NAME = ".worker_stop_blocks"


def _config_home() -> Path:
    """Resolve the reckon config home the way the reckon package does."""
    env = os.environ.get("RECKON_HOME")
    if env:
        return Path(env).expanduser()
    xdg = Path.home() / ".config" / "reckon"
    if xdg.exists():
        return xdg
    return Path.home() / "docs-server"


def _manifest_from_pointer(cwd: Path) -> Path | None:
    """Manifest of the live crew run whose worktree resolves to ``cwd``."""
    live_dir = _config_home() / "crew" / "live"
    if not live_dir.is_dir():
        return None
    for path in sorted(live_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        worktree = record.get("worktree")
        if not worktree:
            continue
        try:
            resolved = Path(str(worktree)).expanduser().resolve()
        except OSError:
            continue
        if resolved != cwd:
            continue
        manifest = record.get("manifest_path")
        if manifest:
            return Path(str(manifest)).expanduser()
    return None


def resolve_manifest(payload: dict[str, Any]) -> Path | None:
    """The run manifest for a Stop payload, or None when no run resolves."""
    env = os.environ.get("RECKON_MANIFEST")
    if env and env.strip():
        return Path(env).expanduser()
    cwd_raw = payload.get("cwd") or os.getcwd()
    try:
        cwd = Path(str(cwd_raw)).expanduser().resolve()
    except OSError:
        return None
    return _manifest_from_pointer(cwd)


def read_status(manifest: Path) -> str | None:
    """The manifest's declared ``status:`` value, or None."""
    if not manifest.is_file():
        return None
    try:
        text = manifest.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("status:"):
            return stripped.split(":", 1)[1].strip() or None
    return None


def decide(payload: dict[str, Any]) -> tuple[bool, str | None]:
    """Return ``(blocked, reason)`` for one Stop payload.

    ``blocked`` is True when the stop is refused. When the stop is allowed it is
    False and ``reason`` is None, and the caller writes no output.
    """
    manifest = resolve_manifest(payload)
    if manifest is None:
        return False, None

    status = read_status(manifest)
    if status in TERMINAL_STATUSES:
        return False, None

    counter = manifest.parent / COUNTER_NAME
    try:
        count = int(counter.read_text().strip() or "0")
    except (OSError, ValueError):
        count = 0
    if count >= BLOCK_LIMIT:
        return False, None
    try:
        counter.parent.mkdir(parents=True, exist_ok=True)
        counter.write_text(f"{count + 1}\n")
    except OSError:
        pass

    what = "is absent" if status is None else f"has status '{status}'"
    reason = (
        f"worker stop refused: the run manifest {manifest} {what}; it must be "
        "present with a status of complete, blocked or failed before the turn "
        f"can end. Refusal {count + 1} of {BLOCK_LIMIT}."
    )
    return True, reason


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    blocked, reason = decide(payload)
    if not blocked:
        return 0

    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
