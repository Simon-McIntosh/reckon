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

Multi-worktree resolution (``root`` parameter)
----------------------------------------------
A stdio MCP server has NO access to the caller's working directory — it
resolves every project to the single FIXED path registered in mounts.json
(the canonical/main checkout).  When a sub-agent runs inside a git worktree
(a separate checkout of the same repo, e.g. ``.claude/worktrees/agent-XXX``),
a write made via the MCP lands in the MAIN checkout, not the agent's worktree.

To fix this, every read/write entry point accepts an OPTIONAL ``root`` — the
absolute path to the desired checkout's repo root (the directory that
contains ``docs/``).  When given:

  - HTML plan slugs resolve under ``<root>/docs``
  - JSON config slugs (index/project) resolve under
    ``<root>/docs/state/<project>/<slug>.json``

Resolution precedence (per resolver):
  1. explicit ``root`` argument (always wins — the multi-worktree caller)
  2. RECKON_* env vars (RECKON_STATE_ROOT / RECKON_MOUNTS_PATH)
  3. mounts.json / config-home (the registered main checkout — default)

``root`` defaults to ``None`` everywhere, so existing callers (the granular
mutators, serve.py, single-checkout agents) are completely unaffected.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any


class VersionConflict(Exception):
    """Raised when expected_version doesn't match the file's current version."""

    def __init__(self, expected: int, current: int, current_data: dict) -> None:
        self.expected = expected
        self.current = current
        self.current_data = current_data
        super().__init__(f"version conflict: expected {expected}, got {current}")


# ── Path helpers ───────────────────────────────────────────────────────────


def _config_home() -> Path:
    """Resolve the reckon config home directory (mounts.json + state/).

    Resolution order (the shared precedence used across reckon):
    1. RECKON_HOME env var (explicit override — always wins)
    2. ~/.config/reckon  (XDG location — preferred when it exists)
    3. ~/docs-server     (legacy fallback — keeps existing installs working)

    The fallback is deliberate: until the on-disk directory is migrated to
    ~/.config/reckon, every caller keeps reading ~/docs-server unchanged.
    """
    env = os.environ.get("RECKON_HOME")
    if env:
        return Path(env).expanduser().resolve()
    xdg = Path.home() / ".config" / "reckon"
    if xdg.exists():
        return xdg
    return Path.home() / "docs-server"


def _state_root() -> Path:
    """Resolve the state root directory for JSON-backed slugs (index/project).

    Priority:
    1. RECKON_STATE_ROOT env var
    2. <config-home>/state  (see _config_home: ~/.config/reckon then ~/docs-server)
    """
    env = os.environ.get("RECKON_STATE_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return _config_home() / "state"


def _mounts_path() -> Path:
    """Return the canonical path to mounts.json.

    Priority:
    1. RECKON_MOUNTS_PATH env var (always wins)
    2. <config-home>/mounts.json (see _config_home)
    """
    env = os.environ.get("RECKON_MOUNTS_PATH")
    if env:
        return Path(env).expanduser().resolve()
    return _config_home() / "mounts.json"


def state_path(project: str, slug: str, root: str | Path | None = None) -> Path:
    """Return the Path for a given project/slug JSON state file (index/project only).

    When ``root`` is given (a checkout's repo root), the JSON state file is
    resolved under ``<root>/docs/state/<project>/<slug>.json`` — this redirects
    index/project config writes into a specific worktree instead of the
    config-home state root (which is symlinked to the MAIN checkout).
    ``root=None`` keeps the default config-home behaviour unchanged.
    """
    if root is not None:
        return (
            Path(root).expanduser().resolve()
            / "docs"
            / "state"
            / project
            / f"{slug}.json"
        )
    return _state_root() / project / f"{slug}.json"


# ── Slug routing ────────────────────────────────────────────────────────────

#: Slugs that remain JSON-backed (project-level config, not per-plan state)
_JSON_SLUGS = frozenset(["index", "project"])


def _is_json_slug(slug: str) -> bool:
    return slug in _JSON_SLUGS


def _docs_dir_for_project(project: str, root: str | Path | None = None) -> Path | None:
    """Return the docs dir for a project, or None if unavailable.

    When ``root`` is given (a checkout's repo root), the docs dir is
    ``<root>/docs`` — bypassing mounts.json so a multi-worktree caller can
    target its own checkout.  ``root=None`` falls back to mounts.json (the
    registered MAIN checkout) — the default, unchanged behaviour.
    """
    if root is not None:
        p = Path(root).expanduser().resolve() / "docs"
        return p if p.is_dir() else None
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


def _resolve_html_file(
    project: str, slug: str, root: str | Path | None = None
) -> Path | None:
    """Locate the HTML file for a plan slug, using mounts.json + _resolve_plan_file.

    ``root`` (a checkout repo root) targets ``<root>/docs`` instead of the
    mounts-registered docs dir; defaults to mounts.json.
    """
    docs_dir = _docs_dir_for_project(project, root)
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


def _read_state(
    project: str, slug: str, root: str | Path | None = None
) -> tuple[dict, int]:
    """Read the semantic HTML state for a plan slug.

    ``root`` (a checkout repo root) targets that checkout's ``docs`` dir;
    defaults to the mounts-registered (main) checkout.

    Returns:
        (state_dict, current_version) where version = state.get("version", 0).
        Returns ({}, 0) if the HTML file or state is absent.
    """
    from reckon import _plan_html

    html_file = _resolve_html_file(project, slug, root)
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
    root: str | Path | None = None,
) -> int:
    """Atomically rewrite the semantic HTML state for a plan slug.

    ``root`` (a checkout repo root) targets that checkout's ``docs`` dir;
    defaults to the mounts-registered (main) checkout.

    Raises VersionConflict on mismatch.
    Returns the new version.
    """
    from reckon import _plan_html

    html_file = _resolve_html_file(project, slug, root)
    if html_file is None or not html_file.is_file():
        # Cannot write to a non-existent HTML file; create one only if the
        # docs dir exists and expected_version==0 (first write).
        if expected_version != 0:
            raise VersionConflict(expected_version, 0, {})
        docs_dir = _docs_dir_for_project(project, root)
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
                f"<title>{slug}</title></head>\n"
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
    new_data["modified"] = date.today().isoformat()
    new_data["version"] = cur_version + 1

    text = html_file.read_text(encoding="utf-8", errors="replace")
    new_text = _plan_html.write_state(text, new_data)

    # Idempotency guard: if the patch carries no real content change (e.g. a
    # no-op edit or a round-trip through BeautifulSoup entity-normalisation),
    # skip the disk write and return the current version unchanged.  We detect
    # a no-op by comparing the *parsed* state dicts (excluding version/modified
    # stamps) of the rendered text vs the current on-disk text.
    _STAMP = frozenset(["version", "modified"])
    cur_parsed = _plan_html.read_state(text)
    new_parsed = _plan_html.read_state(new_text)
    legacy_alias = bool(
        re.search(
            r'<meta\b(?=[^>]*\bname=["\']reckon-type["\'])(?=[^>]*\bcontent=["\']doc["\'])[^>]*>',
            text,
            re.IGNORECASE,
        )
    )
    if not legacy_alias and {k: v for k, v in new_parsed.items() if k not in _STAMP} == {
        k: v for k, v in cur_parsed.items() if k not in _STAMP
    }:
        return cur_version

    tmp = html_file.with_suffix(".html.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(html_file)
    return new_data["version"]


# ── Public API ─────────────────────────────────────────────────────────────


def read_plan(
    project: str, slug: str, root: str | Path | None = None
) -> tuple[dict, int]:
    """Read the data blob and version for a plan (or JSON config doc).

    For plan slugs: reads the semantic HTML state; version = state["version"].
    For JSON slugs (index/project): reads the JSON envelope; version = data["_version"].

    ``root`` (a checkout repo root) targets that checkout's ``docs`` tree for
    BOTH plan HTML and JSON config (index/project) — the multi-worktree path.
    Defaults to the mounts-registered / config-home (main) checkout.

    Returns:
        (data, version) — returns ({}, 0) if absent/unparseable.
    """
    if _is_json_slug(slug):
        return _load_json_envelope(state_path(project, slug, root))
    return _read_state(project, slug, root)


def write_plan(
    project: str,
    slug: str,
    data: dict,
    expected_version: int,
    root: str | Path | None = None,
) -> int:
    """Write a full data blob back with version check.

    For plan slugs: rewrites the semantic HTML state atomically.
    For JSON slugs: rewrites the JSON envelope atomically.

    ``root`` (a checkout repo root) targets that checkout's ``docs`` tree for
    BOTH plan HTML and JSON config (index/project) — the multi-worktree path.
    Defaults to the mounts-registered / config-home (main) checkout.

    Raises VersionConflict if expected_version does not match current.
    Returns the new version.
    """
    if _is_json_slug(slug):
        return _write_json_envelope(
            state_path(project, slug, root), project, slug, data, expected_version
        )
    return _write_state(project, slug, data, expected_version, root)


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


def list_followups_across(
    project: str,
    unresolved_only: bool = True,
    root: str | Path | None = None,
) -> list[dict]:
    """Return followups from all plan HTML files in a project.

    Scans the project docs dir via _plan_html.parse_plan, collecting
    followups with plan_slug and plan_title.  Skips infrastructure
    files/dirs per PLAN-FORMAT.md.
    """
    from reckon import _plan_html
    from reckon.serve import _NON_PLAN_DIRS, _NON_PLAN_FILES

    docs_dir = _docs_dir_for_project(project, root)
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


def list_questions_across(
    project: str,
    unresolved_only: bool = True,
    root: str | Path | None = None,
) -> list[dict]:
    """Return questions from all plan HTML files in a project.

    Adds plan_slug and plan_title to each entry.
    """
    from reckon import _plan_html
    from reckon.serve import _NON_PLAN_DIRS, _NON_PLAN_FILES

    docs_dir = _docs_dir_for_project(project, root)
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


# ── Op-application engine (edit_plan) ───────────────────────────────────────
#
# A single, pure, version-free op applier shared by the collapsed edit_plan
# tool. It MUTATES a working DICT in place (the read_state / index-data shape)
# and returns a list of non-fatal warnings. On a structurally invalid op it
# raises OpError — edit_plan catches that and returns ok:false WITHOUT writing.
# Schema validation (PlanState/IndexState) and the version-checked atomic write
# are the caller's job — this helper never touches disk.

from datetime import datetime as _dt  # noqa: E402


class OpError(Exception):
    """Raised by apply_ops on a structurally invalid op (bad verb, dup id,
    move-not-found, …). Carries a human-readable message; edit_plan turns it
    into {ok: false, error: "op_error", detail: <message>} and writes nothing.
    """


def _utc_ts() -> str:
    """Server UTC timestamp (seconds precision) for resolved_at/when fields."""
    from datetime import timezone

    return _dt.now(tz=timezone.utc).isoformat(timespec="seconds")


def _gen_id(prefix: str) -> str:
    """Generate a server-side id like ``c-20260529T101112123456`` (UTC, µs)."""
    from datetime import timezone

    return f"{prefix}-{_dt.now(tz=timezone.utc):%Y%m%dT%H%M%S%f}"


# Top-level plan scalar fields a `set` op may target directly (everything else
# routes through dotted handling for decisions.<key>.<field>).
_PLAN_SET_TOP = frozenset(
    {
        "status",
        "impl",
        "roi",
        "effort",
        "milestone",
        "sprint",
        "tier",
        "owner",
        "summary",
        "title",
        "type",
        "archived",
        "read",
        "reviewed_at",
        "recorded_at",
        "verdict",
        "environment",
        "source",
        "source_quality",
        "slug",
        "depends_on",
        "blocks",
        "informs",
        "evidence_for",
        "verifies",
        "supersedes",
        "commits",
        "artifacts",
    }
)

# Index top-level scalar fields a `set` op may target directly.
_INDEX_SET_TOP = frozenset({"active_sprint_id"})


def _find_by_id(lst: list, ident: str, id_field: str = "id") -> tuple[int, dict] | None:
    for i, el in enumerate(lst):
        if isinstance(el, dict) and el.get(id_field) == ident:
            return i, el
    return None


def _item_slug(it: Any) -> str:
    return (
        it
        if isinstance(it, str)
        else (it.get("slug", "") if isinstance(it, dict) else "")
    )


def _apply_set(working: dict, op: dict, is_index: bool, warnings: list[str]) -> None:
    path = op.get("path")
    if not path or not isinstance(path, str):
        raise OpError("set op requires a non-empty 'path'")
    if "value" not in op:
        raise OpError(f"set op for {path!r} requires a 'value'")
    value = op["value"]
    parts = path.split(".")
    head = parts[0]

    if is_index:
        if head == "active_sprint_id" and len(parts) == 1:
            working["active_sprint_id"] = value
            return
        if head in ("sprints", "milestones") and len(parts) >= 3:
            # list-by-id dotted path: sprints.<id>.<field>[.<sub>...]
            ident = parts[1]
            lst = working.get(head)
            if not isinstance(lst, list):
                raise OpError(f"index has no {head} list")
            hit = _find_by_id(lst, ident)
            if hit is None:
                raise OpError(f"{head[:-1]} {ident!r} not found")
            idx, el = hit
            new_el = dict(el)
            field = parts[2]
            if len(parts) == 3:
                if head == "sprints" and field == "status":
                    _apply_sprint_status(
                        working, lst, idx, new_el, ident, value, warnings
                    )
                    return
                new_el[field] = value
            else:
                # deeper nesting — build dotted into the element
                cur = new_el
                for p in parts[2:-1]:
                    nxt = cur.get(p)
                    if not isinstance(nxt, dict):
                        nxt = {}
                        cur[p] = nxt
                    cur = nxt
                cur[parts[-1]] = value
            lst[idx] = new_el
            return
        if head in _INDEX_SET_TOP and len(parts) == 1:
            working[head] = value
            return
        if head == "inventory":
            # inventory[] is SYNTHESISED live by discover_plans and never
            # persisted — a set here is accepted (folds update_inventory_item)
            # but is a durable no-op. Mutate the working copy so the op "applies",
            # knowing _write_json_envelope's data is overwritten by discovery on
            # the next GET. Honours the frozen contract: "keep it accepted but
            # document it does nothing lasting."
            inv = working.setdefault("inventory", [])
            if len(parts) >= 3 and isinstance(inv, list):
                hit = _find_by_id(inv, parts[1], id_field="slug")
                if hit is not None:
                    idx, el = hit
                    inv[idx] = {**el, parts[2]: value}
            return
        raise OpError(f"unsupported index set path {path!r}")

    # ── plan set ──
    if head == "decisions" and len(parts) >= 3:
        decisions = working.setdefault("decisions", {})
        if not isinstance(decisions, dict):
            raise OpError("plan has no decisions map")
        key = parts[1]
        dec = dict(decisions.get(key, {}))
        cur = dec
        for p in parts[2:-1]:
            nxt = cur.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[p] = nxt
            cur = nxt
        cur[parts[-1]] = value
        decisions[key] = dec
        return
    if len(parts) != 1 or head not in _PLAN_SET_TOP:
        raise OpError(f"unsupported plan set path {path!r}")
    if head == "impl":
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise OpError(f"impl must be a number, got {value!r}") from None
        working["impl"] = max(0.0, min(1.0, f))  # clamp 0..1 (was reject)
        return
    working[head] = value


def _apply_sprint_status(
    working: dict,
    sprints: list,
    idx: int,
    new_el: dict,
    sprint_id: str,
    value: Any,
    warnings: list[str],
) -> None:
    """Mirror update_sprint's active_sprint_id side-effects for a status set."""
    new_el["status"] = value
    sprints[idx] = new_el
    active_id = working.get("active_sprint_id")
    if value == "active":
        already = next(
            (
                x
                for x in sprints
                if isinstance(x, dict)
                and x.get("status") == "active"
                and x.get("id") != sprint_id
            ),
            None,
        )
        if already:
            warnings.append(
                f"sprint {already['id']} is already active — consider closing it first"
            )
        working["active_sprint_id"] = sprint_id
    elif value == "done" and active_id == sprint_id:
        working["active_sprint_id"] = None


def _apply_append(working: dict, op: dict, is_index: bool, warnings: list[str]) -> None:
    target = op.get("target")
    if not target or not isinstance(target, str):
        raise OpError("append op requires a 'target' collection")
    item = op.get("item")

    if is_index:
        if target == "sprints":
            if not isinstance(item, dict) or not item.get("id"):
                raise OpError("append sprints requires an item object with an 'id'")
            sprints = working.setdefault("sprints", [])
            if _find_by_id(sprints, item["id"]) is not None:
                raise OpError(f"sprint {item['id']!r} already exists")
            new_sprint = {
                "id": item["id"],
                "status": item.get("status", "planned"),
                "theme": item.get("theme", ""),
                "items": list(item.get("items", [])),
            }
            for k in ("starts", "ends", "description", "summary"):
                if k in item:
                    new_sprint[k] = item[k]
            sprints.append(new_sprint)
            if new_sprint["status"] == "active":
                prev = working.get("active_sprint_id")
                if prev and prev != new_sprint["id"]:
                    warnings.append(f"sprint {prev} was active — consider closing it")
                working["active_sprint_id"] = new_sprint["id"]
            return
        if target.startswith("sprints.") and target.endswith(".items"):
            sprint_id = target[len("sprints.") : -len(".items")]
            sprints = working.get("sprints", [])
            hit = _find_by_id(sprints, sprint_id)
            if hit is None:
                raise OpError(f"sprint {sprint_id!r} not found")
            idx, el = hit
            slug = _item_slug(item)
            if not slug:
                raise OpError("sprint item must have a slug")
            items = list(el.get("items", []))
            if slug in {_item_slug(x) for x in items}:
                raise OpError(f"{slug!r} already in sprint {sprint_id}")
            items.append(item)
            sprints[idx] = {**el, "items": items}
            return
        if target == "milestones":
            if not isinstance(item, dict) or not item.get("id"):
                raise OpError("append milestones requires an item object with an 'id'")
            milestones = working.setdefault("milestones", [])
            if _find_by_id(milestones, item["id"]) is not None:
                raise OpError(f"milestone {item['id']!r} already exists")
            milestones.append(item)
            return
        if target in ("timeline", "blockers"):
            lst = working.setdefault(target, [])
            lst.append(item)
            return
        raise OpError(f"unsupported index append target {target!r}")

    # ── plan append ──
    if target == "followups":
        if not isinstance(item, dict):
            raise OpError("append followups requires an item object")
        required = {"id", "written_by", "written_at", "title", "body", "prompt"}
        fu = dict(item)
        if not fu.get("id"):
            fu["id"] = _gen_id("f")
        missing = [k for k in sorted(required) if not str(fu.get(k, "")).strip()]
        if missing:
            raise OpError(f"followup missing required fields: {missing}")
        working.setdefault("followups", []).append(fu)
        return
    if target == "research":
        if not isinstance(item, dict):
            raise OpError("append research requires an item object")
        r = dict(item)
        if not r.get("id"):
            r["id"] = _gen_id("r")
        working.setdefault("research", []).append(r)
        return
    if target == "questions":
        if not isinstance(item, dict):
            raise OpError("append questions requires an item object")
        q = dict(item)
        if not q.get("id"):
            q["id"] = _gen_id("q")
        working.setdefault("questions", []).append(q)
        return
    if target == "comments":
        if not isinstance(item, dict):
            raise OpError("append comments requires an item object")
        section = op.get("section") or "_top"
        c = dict(item)
        if not c.get("id"):
            c["id"] = _gen_id("c")
        comments = working.setdefault("comments", {})
        comments.setdefault(section, []).append(c)
        return
    if target == "decisions":
        key = op.get("key")
        if not key:
            raise OpError("append decisions requires a 'key'")
        decisions = working.setdefault("decisions", {})
        if key in decisions:
            raise OpError(f"decision {key!r} already exists")
        decisions[key] = item if isinstance(item, dict) else {}
        return
    raise OpError(f"unsupported plan append target {target!r}")


def _apply_resolve(
    working: dict, op: dict, is_index: bool, warnings: list[str]
) -> None:
    if is_index:
        raise OpError("resolve op is plan-only")
    target = op.get("target")
    ident = op.get("id")
    if target not in ("followups", "questions"):
        raise OpError("resolve target must be 'followups' or 'questions'")
    if not ident:
        raise OpError("resolve op requires an 'id'")
    lst = working.get(target, [])
    hit = _find_by_id(lst, ident)
    if hit is None:
        raise OpError(f"{ident!r} not found in {target}")
    idx, el = hit
    updates: dict[str, Any] = {
        "resolved_at": _utc_ts(),
        "resolved_by": op.get("by", ""),
    }
    if target == "followups":
        updates["outcome"] = op.get("outcome", "")
        updates["status"] = "resolved"
    else:
        updates["resolution"] = op.get("resolution", "")
    lst[idx] = {**el, **updates}


def _apply_lock(working: dict, op: dict, is_index: bool, warnings: list[str]) -> None:
    if is_index:
        raise OpError("lock op is plan-only")
    key = op.get("key")
    if not key:
        raise OpError("lock op requires a 'key'")
    decisions = working.setdefault("decisions", {})
    existing = decisions.get(key)
    merged = {
        "choice": op.get("choice", ""),
        "rationale": op.get("rationale", ""),
        "when": _utc_ts(),
        "by": op.get("by", ""),
    }
    # Preserve authored title/context/choices/option_labels (merge semantics).
    if isinstance(existing, dict):
        decisions[key] = {**existing, **merged}
    else:
        decisions[key] = merged


def _apply_move(working: dict, op: dict, is_index: bool, warnings: list[str]) -> None:
    if not is_index:
        raise OpError("move op is index-only")
    if op.get("target") != "sprint_item":
        raise OpError("move target must be 'sprint_item'")
    slug = op.get("slug")
    frm = op.get("from")
    to = op.get("to")
    if not (slug and frm and to):
        raise OpError("move op requires slug, from, and to")
    sprints = working.get("sprints", [])
    fhit = _find_by_id(sprints, frm)
    thit = _find_by_id(sprints, to)
    if fhit is None:
        raise OpError(f"from sprint {frm!r} not found")
    if thit is None:
        raise OpError(f"to sprint {to!r} not found")
    fi, fs = fhit
    ti, ts = thit
    from_items = list(fs.get("items", []))
    moved = None
    new_from: list = []
    for it in from_items:
        if _item_slug(it) == slug:
            moved = it
        else:
            new_from.append(it)
    if moved is None:
        raise OpError(f"{slug!r} not found in sprint {frm}")
    to_items = list(ts.get("items", []))
    if slug in {_item_slug(x) for x in to_items}:
        raise OpError(f"{slug!r} already in sprint {to}")
    to_items.append(moved)
    sprints[fi] = {**fs, "items": new_from}
    sprints[ti] = {**ts, "items": to_items}


_OP_DISPATCH = {
    "set": _apply_set,
    "append": _apply_append,
    "resolve": _apply_resolve,
    "lock": _apply_lock,
    "move": _apply_move,
}


def apply_ops(working: dict, ops: list[dict], is_index: bool) -> list[str]:
    """Apply ``ops`` IN ORDER to the working DICT in place.

    ``working`` is the read_state dict (plan) or the index ``data`` sub-object.
    Returns accumulated non-fatal warnings (e.g. double-active-sprint).
    Raises :class:`OpError` on any structurally invalid op — the caller then
    rejects WITHOUT writing. Pure: never touches disk or version fields.
    """
    if not isinstance(ops, list):
        raise OpError("ops must be a list")
    warnings: list[str] = []
    for n, op in enumerate(ops):
        if not isinstance(op, dict):
            raise OpError(f"op #{n} is not an object")
        verb = op.get("op")
        handler = _OP_DISPATCH.get(verb)
        if handler is None:
            raise OpError(f"op #{n}: unknown verb {verb!r}")
        handler(working, op, is_index, warnings)
    return warnings


# ── New-plan HTML template (create=True) ────────────────────────────────────


def new_plan_html(project: str, slug: str, title: str | None = None) -> str:
    """Return a minimal, schema-valid plan HTML head for a brand-new plan.

    Carries docs-project + plan-slug metas, a <title>, reckon-type=plan, the two
    shared CSS links, and empty decisions/followups sections so a freshly created
    plan validates and round-trips. edit_plan applies ops on top of this.
    """
    t = title or slug
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f'<meta name="docs-project" content="{project}">\n'
        f'<meta name="plan-slug" content="{slug}">\n'
        '<meta name="reckon-type" content="plan">\n'
        f"<title>{t}</title>\n"
        '<link rel="stylesheet" href="/_shared/foundation.css">\n'
        '<link rel="stylesheet" href="/_shared/dashboard.css">\n'
        "</head>\n"
        '<body>\n<main class="plan-doc">\n'
        '<section data-reckon="decisions" id="decisions" class="r-decisions">'
        '\n<h2><span class="sec">§</span> Decisions</h2>\n</section>\n'
        '<section data-reckon="followups" id="followups" class="r-followups">'
        '\n<h2><span class="sec">§</span> Followups</h2>\n</section>\n'
        "</main>\n</body>\n</html>\n"
    )
