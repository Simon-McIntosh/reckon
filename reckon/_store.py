"""State file IO for reckon MCP server.

Mirrors the version-write contract of the docs-server POST handler at
~/Code/reckon/reckon/serve.py:do_POST:

  - State files live at $RECKON_STATE_ROOT/<project>/<slug>.json
    (default root: ~/docs-server/state, which symlinks into each project repo).
  - The JSON envelope is { "updated": <iso>, "project": str, "doc": str, "data": {...} }.
  - data._version is the optimistic-concurrency counter.
  - Writes are atomic: write to .json.tmp then os.replace() to .json.
  - Conflict (expected_version != current_version) raises VersionConflict.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


class VersionConflict(Exception):
    """Raised when expected_version doesn't match the file's current _version."""

    def __init__(self, expected: int, current: int, current_data: dict) -> None:
        self.expected = expected
        self.current = current
        self.current_data = current_data
        super().__init__(
            f"version conflict: expected {expected}, got {current}"
        )


def _state_root() -> Path:
    """Resolve the state root directory.

    Priority:
    1. RECKON_STATE_ROOT env var
    2. ~/docs-server/state (same default as reckon/serve.py)
    """
    env = os.environ.get("RECKON_STATE_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / "docs-server" / "state"


def state_path(project: str, slug: str) -> Path:
    """Return the Path for a given project/slug state file."""
    return _state_root() / project / f"{slug}.json"


def _load_envelope(path: Path) -> tuple[dict, int]:
    """Load the JSON envelope from disk.

    Returns:
        (data_dict, current_version) — data_dict is the "data" sub-object,
        current_version is data._version (0 if file absent or unparseable).
    """
    if not path.exists():
        return {}, 0
    try:
        envelope = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}, 0
    if not isinstance(envelope, dict):
        return {}, 0
    data = envelope.get("data", {})
    if not isinstance(data, dict):
        data = {}
    version = int(data.get("_version", 0))
    return data, version


def read_plan(project: str, slug: str) -> tuple[dict, int]:
    """Read the data blob and version for a plan.

    Returns:
        (data, version) — data is the full data dict (without envelope);
        version is data._version.

    Does not raise if the file is absent — returns ({}, 0).
    """
    path = state_path(project, slug)
    return _load_envelope(path)


def write_plan(
    project: str,
    slug: str,
    data: dict,
    expected_version: int,
) -> int:
    """Write a full data blob back to disk with version check.

    Mirrors the POST /state/<project>/<doc> contract exactly:
    - Reads current file to get cur_version.
    - Raises VersionConflict if expected_version != cur_version.
    - Strips _version from data, sets _version = cur_version + 1.
    - Writes atomically via .tmp rename.

    Returns:
        The new _version value.
    """
    path = state_path(project, slug)
    cur_data, cur_version = _load_envelope(path)

    if expected_version != cur_version:
        raise VersionConflict(expected_version, cur_version, cur_data)

    new_data = dict(data)
    new_data.pop("_version", None)
    new_data["_version"] = cur_version + 1

    envelope = {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "project": project,
        "doc": slug,
        "data": new_data,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(envelope, indent=2) + "\n")
    tmp.replace(path)

    return new_data["_version"]


def patch_plan(
    project: str,
    slug: str,
    patch: dict[str, Any],
    expected_version: int,
) -> int:
    """JSON merge-patch into the existing data blob.

    Reads current data, merges patch (top-level keys only — callers that
    need deep merge should read first and pass the full merged blob to
    write_plan directly).

    Returns the new _version.
    """
    cur_data, cur_version = read_plan(project, slug)

    if expected_version != cur_version:
        raise VersionConflict(expected_version, cur_version, cur_data)

    merged = {**cur_data, **patch}
    # write_plan re-reads and re-checks, so pass cur_version to it
    return write_plan(project, slug, merged, cur_version)


def append_to_list(
    project: str,
    slug: str,
    field: str,
    item: Any,
    expected_version: int,
) -> int:
    """Append item to data[field] (a list), creating it if absent.

    Returns the new _version.
    """
    cur_data, cur_version = read_plan(project, slug)

    if expected_version != cur_version:
        raise VersionConflict(expected_version, cur_version, cur_data)

    lst = list(cur_data.get(field, []))
    lst.append(item)
    merged = {**cur_data, field: lst}
    return write_plan(project, slug, merged, cur_version)


def set_nested(
    project: str,
    slug: str,
    field: str,
    key: str,
    value: Any,
    expected_version: int,
) -> int:
    """Set data[field][key] = value (creates field dict if absent).

    Used by lock_decision: data["decisions"][key] = {...}.

    Returns the new _version.
    """
    cur_data, cur_version = read_plan(project, slug)

    if expected_version != cur_version:
        raise VersionConflict(expected_version, cur_version, cur_data)

    d = dict(cur_data.get(field, {}))
    d[key] = value
    merged = {**cur_data, field: d}
    return write_plan(project, slug, merged, cur_version)


def resolve_in_list(
    project: str,
    slug: str,
    field: str,
    item_id: str,
    updates: dict[str, Any],
    expected_version: int,
) -> int:
    """Find item in data[field] where item["id"] == item_id and merge updates.

    Raises KeyError if item_id not found.
    Returns the new _version.
    """
    cur_data, cur_version = read_plan(project, slug)

    if expected_version != cur_version:
        raise VersionConflict(expected_version, cur_version, cur_data)

    lst = list(cur_data.get(field, []))
    found = False
    for i, item in enumerate(lst):
        if isinstance(item, dict) and item.get("id") == item_id:
            lst[i] = {**item, **updates}
            found = True
            break

    if not found:
        raise KeyError(f"{item_id!r} not found in data[{field!r}]")

    merged = {**cur_data, field: lst}
    return write_plan(project, slug, merged, cur_version)
