"""State IO for reckon MCP server — semantic-HTML-backed plan store.

Architecture
------------
The plan HTML file is the sole store for plan state.  Each plan page
embeds a <script type="application/json" id="reckon-owned sections in that
holds all mutable data (status, decisions, followups, comments, …).

Two slugs are special and remain JSON-backed:

  - "index"   — project-level config: sprints, milestones, active_sprint_id,
                 plus the auto-discovered inventory array (owned by serve.py)
  - "project" — legacy project config (kept for back-compat)

All other slugs are PLAN slugs.  For plan slugs:
  - read_plan reads the semantic HTML state directly from the plan's .html file
  - write_plan rewrites the semantic HTML state atomically
  - version field is "version" (not "_version") inside the state

Version-write contract mirrors POST /plan/<project>/<slug> in
reckon/serve.py._handle_plan_write:
  - Read the current state → cur_version = state.get("version", 0)
  - Raise VersionConflict if expected_version != cur_version
  - Set state["version"] = cur_version + 1
  - Set state["modified"] to today's date
  - Write atomically: .html.tmp → .html

JSON slugs (index/project) keep the old _version counter inside the envelope
unchanged — they are used by sprint/milestone tooling.

Slug routing uses mounts.json to find the project docs dir, then
_resolve_plan_file to locate the HTML file by stem.  RECKON_MOUNTS_PATH
env var overrides the default mounts path (mirrors RECKON_STATE_ROOT).
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any


class VersionConflict(Exception):
    """Raised when expected_version doesn't match the file's current version."""

    def __init__(self, expected: int, current: int, current_data: dict) -> None:
        self.expected = expected
        self.current = current
        self.current_data = current_data
        super().__init__(
            f"version conflict: expected {expected}, got {current}"
        )


# ── Path helpers ───────────────────────────────────────────────────────────

def _state_root() -> Path:
    """Resolve the state root directory for JSON-backed slugs (index/project).

    Priority:
    1. RECKON_STATE_ROOT env var
    2. ~/docs-server/state (same default as reckon/serve.py)
    """
    env = os.environ.get("RECKON_STATE_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / "docs-server" / "state"


def _mounts_path() -> Path:
    """Return the canonical path to mounts.json.

    Override with RECKON_MOUNTS_PATH env var.
    """
    env = os.environ.get("RECKON_MOUNTS_PATH")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / "docs-server" / "mounts.json"


def state_path(project: str, slug: str) -> Path:
    """Return the Path for a given project/slug JSON state file (index/project only)."""
    return _state_root() / project / f"{slug}.json"


# ── Slug routing ────────────────────────────────────────────────────────────

#: Slugs that remain JSON-backed (project-level config, not per-plan state)
_JSON_SLUGS = frozenset(["index", "project"])


def _is_json_slug(slug: str) -> bool:
    return slug in _JSON_SLUGS


def _docs_dir_for_project(project: str) -> Path | None:
    """Return the docs dir for a project from mounts.json, or None if unavailable."""
    mp = _mounts_path()
    if not mp.exists():
        return None
    try:
        mounts = json.loads(mp.read_text())
        raw = mounts.get(project)
        if not raw:
            return None
        p = Path(raw).expanduser().resolve()
        return p if p.is_dir() else None
    except (OSError, json.JSONDecodeError):
        return None


def _resolve_html_file(project: str, slug: str) -> Path | None:
    """Locate the HTML file for a plan slug, using mounts.json + _resolve_plan_file."""
    docs_dir = _docs_dir_for_project(project)
    if docs_dir is None:
        return None
    # Import lazily to avoid circular issues at module load time; serve.py has
    # no import side-effects and this call is cheap.
    from reckon.serve import _resolve_plan_file
    return _resolve_plan_file(docs_dir, slug)


# ── JSON-backed helpers (index / project slugs) ────────────────────────────

def _load_json_envelope(path: Path) -> tuple[dict, int]:
    """Load the JSON envelope from disk.

    Returns:
        (data_dict, current_version) — data_dict is the "data" sub-object;
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


def _write_json_envelope(
    path: Path,
    project: str,
    slug: str,
    data: dict,
    expected_version: int,
) -> int:
    """Atomic write of a JSON envelope with optimistic-concurrency check.

    Returns the new _version.
    """
    from datetime import datetime
    cur_data, cur_version = _load_json_envelope(path)
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


# ── HTML-state helpers ────────────────────────────────────────────────────

def _read_state(project: str, slug: str) -> tuple[dict, int]:
    """Read the semantic HTML state for a plan slug.

    Returns:
        (state_dict, current_version) where version = state.get("version", 0).
        Returns ({}, 0) if the HTML file or state is absent.
    """
    from reckon import _plan_html
    html_file = _resolve_html_file(project, slug)
    if html_file is None or not html_file.is_file():
        return {}, 0
    text = html_file.read_text(encoding="utf-8", errors="replace")
    state = _plan_html.read_state(text)
    version = int(state.get("version", 0) or 0)
    return state, version


def _write_state(
    project: str,
    slug: str,
    data: dict,
    expected_version: int,
) -> int:
    """Atomically rewrite the semantic HTML state for a plan slug.

    Raises VersionConflict on mismatch.
    Returns the new version.
    """
    from reckon import _plan_html
    html_file = _resolve_html_file(project, slug)
    if html_file is None or not html_file.is_file():
        # Cannot write to a non-existent HTML file; create one only if the
        # docs dir exists and expected_version==0 (first write).
        if expected_version != 0:
            raise VersionConflict(expected_version, 0, {})
        docs_dir = _docs_dir_for_project(project)
        if docs_dir is None:
            raise FileNotFoundError(
                f"No docs dir found for project {project!r} — "
                "check mounts.json or RECKON_MOUNTS_PATH"
            )
        html_file = docs_dir / f"{slug}.html"
        if not html_file.exists():
            # Stub HTML with minimal structure for the state to be injected.
            html_file.write_text(
                f'<!doctype html>\n<html lang="en">\n<head>'
                f'<meta charset="utf-8">'
                f'<meta name="docs-project" content="{project}">'
                f'<title>{slug}</title></head>\n'
                f'<body><main class="plan-doc"></main></body>\n</html>\n',
                encoding="utf-8",
            )
        cur_state: dict = {}
        cur_version = 0
    else:
        text = html_file.read_text(encoding="utf-8", errors="replace")
        cur_state = _plan_html.read_state(text)
        cur_version = int(cur_state.get("version", 0) or 0)

    if expected_version != cur_version:
        raise VersionConflict(expected_version, cur_version, cur_state)

    new_data = dict(data)
    new_data.pop("_version", None)  # never allow the old JSON key in the state
    new_data["version"] = cur_version + 1
    new_data["modified"] = date.today().isoformat()

    text = html_file.read_text(encoding="utf-8", errors="replace")
    new_text = _plan_html.write_state(text, new_data)
    tmp = html_file.with_suffix(".html.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(html_file)
    return new_data["version"]


# ── Public API ─────────────────────────────────────────────────────────────

def read_plan(project: str, slug: str) -> tuple[dict, int]:
    """Read the data blob and version for a plan (or JSON config doc).

    For plan slugs: reads the semantic HTML state; version = state["version"].
    For JSON slugs (index/project): reads the JSON envelope; version = data["_version"].

    Returns:
        (data, version) — returns ({}, 0) if absent/unparseable.
    """
    if _is_json_slug(slug):
        return _load_json_envelope(state_path(project, slug))
    return _read_state(project, slug)


def write_plan(
    project: str,
    slug: str,
    data: dict,
    expected_version: int,
) -> int:
    """Write a full data blob back with version check.

    For plan slugs: rewrites the semantic HTML state atomically.
    For JSON slugs: rewrites the JSON envelope atomically.

    Raises VersionConflict if expected_version does not match current.
    Returns the new version.
    """
    if _is_json_slug(slug):
        return _write_json_envelope(
            state_path(project, slug), project, slug, data, expected_version
        )
    return _write_state(project, slug, data, expected_version)


def patch_plan(
    project: str,
    slug: str,
    patch: dict[str, Any],
    expected_version: int,
) -> int:
    """JSON merge-patch into the existing data blob (top-level keys only).

    Returns the new version.
    """
    cur_data, cur_version = read_plan(project, slug)
    if expected_version != cur_version:
        raise VersionConflict(expected_version, cur_version, cur_data)

    merged = {**cur_data, **patch}
    return write_plan(project, slug, merged, cur_version)


def append_to_list(
    project: str,
    slug: str,
    field: str,
    item: Any,
    expected_version: int,
) -> int:
    """Append item to data[field] (a list), creating it if absent.

    Returns the new version.
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

    IMPORTANT: if both the existing data[field][key] and `value` are dicts,
    the new value is MERGED into the existing entry (preserving authored
    fields like title/context/choices) — not replaced wholesale.  This
    ensures a lock_decision call never drops the authored decision title,
    context, or choices array.

    Returns the new version.
    """
    cur_data, cur_version = read_plan(project, slug)
    if expected_version != cur_version:
        raise VersionConflict(expected_version, cur_version, cur_data)

    d = dict(cur_data.get(field, {}))
    existing = d.get(key)
    if isinstance(existing, dict) and isinstance(value, dict):
        # Merge: authored fields (title, context, choices) survive; locked
        # fields (choice, rationale, when, by) from `value` win.
        d[key] = {**existing, **value}
    else:
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
    Returns the new version.
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


# ── Cross-plan scan ────────────────────────────────────────────────────────

def list_followups_across(project: str, unresolved_only: bool = True) -> list[dict]:
    """Return followups from all plan HTML files in a project.

    Scans the project docs dir via _plan_html.parse_plan, collecting
    followups with plan_slug and plan_title.  Skips infrastructure
    files/dirs per PLAN-FORMAT.md.
    """
    from reckon import _plan_html
    from reckon.serve import _NON_PLAN_DIRS, _NON_PLAN_FILES

    docs_dir = _docs_dir_for_project(project)
    if docs_dir is None:
        return []

    results: list[dict] = []
    for html_file in sorted(docs_dir.rglob("*.html")):
        rel = html_file.relative_to(docs_dir)
        if any(part in _NON_PLAN_DIRS for part in rel.parts[:-1]):
            continue
        if html_file.name in _NON_PLAN_FILES:
            continue
        try:
            rec = _plan_html.parse_plan(html_file)
        except Exception:
            continue
        slug = rec["slug"]
        title = rec.get("title") or slug
        # followups live in the raw state (parse_plan returns them)
        for f in rec.get("followups", []):
            if unresolved_only and f.get("resolved_at"):
                continue
            results.append({"plan_slug": slug, "plan_title": title, **f})
    return results


def list_questions_across(project: str, unresolved_only: bool = True) -> list[dict]:
    """Return questions from all plan HTML files in a project.

    Adds plan_slug and plan_title to each entry.
    """
    from reckon import _plan_html
    from reckon.serve import _NON_PLAN_DIRS, _NON_PLAN_FILES

    docs_dir = _docs_dir_for_project(project)
    if docs_dir is None:
        return []

    results: list[dict] = []
    for html_file in sorted(docs_dir.rglob("*.html")):
        rel = html_file.relative_to(docs_dir)
        if any(part in _NON_PLAN_DIRS for part in rel.parts[:-1]):
            continue
        if html_file.name in _NON_PLAN_FILES:
            continue
        try:
            rec = _plan_html.parse_plan(html_file)
        except Exception:
            continue
        slug = rec["slug"]
        title = rec.get("title") or slug
        for q in rec.get("questions", []):
            if unresolved_only and q.get("resolved_at"):
                continue
            results.append({"plan_slug": slug, "plan_title": title, **q})
    return results
