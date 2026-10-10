from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon._store import (
    _config_home,
)

# ── Run records ─────────────────────────────────────────────────────────────


def crew_home() -> Path:
    """Directory holding transient run state — never committed."""
    return _config_home() / "crew"


def watch_lock_path(project: str) -> Path:
    """Stable advisory-lock path for one project's fleet watcher."""
    readable = re.sub(r"[^A-Za-z0-9._-]", "-", project).strip("-") or "project"
    digest = hashlib.sha256(project.encode()).hexdigest()[:12]
    return crew_home() / "watch" / f"{readable}-{digest}.lock"


def watch_stream_path(project: str) -> Path:
    """Stable append-only transition stream for one project's watcher."""
    return watch_lock_path(project).with_suffix(".events")


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


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
