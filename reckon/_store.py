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
  - read_plan reads semantic state directly from the plan HTML file
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

from contextvars import ContextVar
import json
import os
import re
import tempfile
import fcntl
import hashlib
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

from reckon.lifecycle import TERMINAL_STATUSES


PLAN_SUMMARY_MAX_LENGTH = 160


def plan_summary_length(value: Any) -> int:
    """Measure the authored summary value used by writes and audits."""
    return len(str(value or ""))


def _validate_plan_summary(value: Any) -> str:
    """Return a summary within the authored display bound or refuse it."""
    summary = str(value or "")
    measured = plan_summary_length(summary)
    if measured > PLAN_SUMMARY_MAX_LENGTH:
        raise OpError(
            f"plan summary is {measured} characters; "
            f"the maximum is {PLAN_SUMMARY_MAX_LENGTH}"
        )
    return summary


class VersionConflict(Exception):
    """Raised when expected_version doesn't match the file's current version."""

    def __init__(self, expected: int, current: int, current_data: dict) -> None:
        self.expected = expected
        self.current = current
        self.current_data = current_data
        super().__init__(f"version conflict: expected {expected}, got {current}")


class CorruptEnvelopeError(Exception):
    """Raised when an existing JSON envelope cannot be read safely."""

    def __init__(self, path: Path, failure: str) -> None:
        self.path = path
        self.failure = failure
        super().__init__(
            f"cannot read JSON envelope {path}: {failure}; fix any conflict markers "
            "or restore the file from git before retrying"
        )


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


def _is_json_slug(slug: str, artifact_type: str | None = None) -> bool:
    return slug in _JSON_SLUGS and artifact_type not in {
        "sprint",
        "milestone",
        "blocker",
        "timeline",
        "project",
    }


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
    project: str,
    slug: str,
    root: str | Path | None = None,
    artifact_type: str | None = None,
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
    from reckon.resources import resolve_resource

    resource = resolve_resource(docs_dir, project, slug, artifact_type)
    if resource is None:
        # Fall back to the archive, live copy first. Without this a retired
        # resource reads as absent rather than as archived, and a caller that
        # treats an empty read as "does not exist" then offers to create one --
        # producing a live duplicate that shadows the archived original. Live
        # precedence is preserved by only consulting the archive on a miss, and
        # a genuine live-plus-archived pair still raises a collision rather than
        # being silently resolved. serve._resolve_plan_file already does this;
        # the two resolvers disagreeing is what made an archived record
        # readable through one path and invisible through the other.
        resource = resolve_resource(
            docs_dir, project, slug, artifact_type, include_archived=True
        )
    return resource.path if resource else None


# ── JSON-backed helpers (index / project slugs) ────────────────────────────


@contextmanager
def _serialized_path_lock(path: Path, namespace: str):
    """Serialise a read-check-replace critical section for one store file.

    Two writers that both pass a version check before either replaces the file
    produce a lost update whose counter still advances: nothing raises a
    conflict, so nothing can be noticed. Holding this lock makes the check and
    the replacement one step, so the writer that arrives second is decided by
    the file rather than by scheduling.
    """
    identity = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    lock_path = _config_home() / "locks" / namespace / f"{identity}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _json_envelope_lock(path: Path):
    """Serialise the version check and replacement for one JSON envelope."""
    with _serialized_path_lock(path, "envelopes"):
        yield


def _load_json_envelope(path: Path) -> tuple[dict, int]:
    """Load the JSON envelope from disk.

    Returns:
        (data_dict, current_version) — data_dict is the "data" sub-object;
        current_version is data._version (0 only when the file is absent).

    Raises:
        CorruptEnvelopeError: The file exists but is not a valid envelope.
    """
    if not path.exists():
        return {}, 0
    try:
        envelope = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise CorruptEnvelopeError(path, str(exc)) from exc
    if not isinstance(envelope, dict):
        raise CorruptEnvelopeError(path, "top-level value is not an object")
    data = envelope.get("data", {})
    if not isinstance(data, dict):
        raise CorruptEnvelopeError(path, "data value is not an object")
    try:
        version = int(data.get("_version", 0))
    except (TypeError, ValueError) as exc:
        raise CorruptEnvelopeError(path, "data._version is not an integer") from exc
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

    path.parent.mkdir(parents=True, exist_ok=True)
    with _json_envelope_lock(path):
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
        tmp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                tmp = Path(handle.name)
                json.dump(envelope, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(path)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        return new_data["_version"]


# ── HTML-state helpers ────────────────────────────────────────────────────


def _unparsed_section_warnings(html_text: str, state: dict[str, Any]) -> list[str]:
    """Warn about data-reckon sections holding authored children the parser
    did not recognise.

    The parser reports such a section as an empty collection — written exactly
    like "nothing was authored" — and a read-modify-write then splices that
    empty collection over the section, silently deleting the authored items.
    The write path refuses the splice; this read-time warning is its partner,
    so a caller that reads the version before an edit sees the hazard in the
    state it already gets back instead of meeting it only as a write refusal.
    Sections absent from ``state`` are covered too: an absent section has no
    authored children either, so nothing is warned about.
    """
    from reckon import _plan_html

    out: list[str] = []
    for reckon_id in _plan_html.SECTION_IDS:
        if state.get(reckon_id):
            continue  # parsed content is present — nothing unrecognised
        count = _plan_html._authored_child_count(html_text, reckon_id)
        if count == 0:
            continue  # missing or genuinely empty section — nothing at risk
        spelling = _plan_html._SECTION_RECOGNISED_SPELLING.get(reckon_id)
        expected = f" Expected spelling: {spelling}" if spelling else ""
        out.append(
            f'plan read: section data-reckon="{reckon_id}" holds {count} '
            "authored child element(s) the parser did not recognise — the "
            f"parsed collection is empty.{expected}"
        )
    return out


def _state_from_text(
    project: str,
    text: str,
    root: str | Path | None = None,
) -> dict:
    """Parse plan HTML into the state dict both readers and writers see.

    The writer re-reads through here rather than through a bare parse, so the
    dict it compares against the caller's is shaped exactly like the dict that
    caller read: a comparison of the two would otherwise always disagree on the
    derived diagnostics one side carries and the other does not.
    """
    from reckon import _plan_html

    state = _plan_html.read_state(text)
    _add_north_star_diagnostic(project, state, root)
    for warning in _unparsed_section_warnings(text, state):
        compat = list(state.get("compatibility_warnings") or [])
        if warning not in compat:
            compat.append(warning)
            state["compatibility_warnings"] = compat
    if state.get("type", "plan") == "plan" and state.get("status") == "blocked":
        warning = (
            "status: persisted 'blocked' is legacy compatibility input; "
            "effective status is derived from current blockers"
        )
        warnings = list(state.get("compatibility_warnings") or [])
        if warning not in warnings:
            warnings.append(warning)
        state["compatibility_warnings"] = warnings
    return state


def _read_state(
    project: str,
    slug: str,
    root: str | Path | None = None,
    artifact_type: str | None = None,
) -> tuple[dict, int]:
    """Read the semantic HTML state for a plan slug.

    ``root`` (a checkout repo root) targets that checkout's ``docs`` dir;
    defaults to the mounts-registered (main) checkout.

    Returns:
        (state_dict, current_version) where version = state.get("version", 0).
        Returns ({}, 0) if the HTML file or state is absent.
    """
    html_file = _resolve_html_file(project, slug, root, artifact_type)
    if html_file is None or not html_file.is_file():
        return {}, 0
    text = html_file.read_text(encoding="utf-8", errors="replace")
    state = _state_from_text(project, text, root)
    version = int(state.get("version", 0) or 0)
    return state, version


def _add_north_star_diagnostic(
    project: str,
    state: dict,
    root: str | Path | None = None,
) -> None:
    """Report a plan label that is absent from its project's directions."""
    north_star = str(state.get("north_star") or "").strip()
    if not north_star or state.get("type", "plan") != "plan":
        return
    docs_dir = _docs_dir_for_project(project, root)
    if docs_dir is None:
        return
    from reckon.project_state import ProjectStateError, compose_project_state

    try:
        project_state = compose_project_state(docs_dir, project)
    except ProjectStateError:
        return
    declared = {
        str(item.get("id") or "")
        for item in project_state.get("north_stars", [])
        if isinstance(item, dict)
    }
    if north_star in declared:
        return
    state["validation_diagnostics"] = [
        *state.get("validation_diagnostics", []),
        {
            "code": "undeclared-north-star",
            "severity": "warning",
            "message": (
                f"plan north-star {north_star!r} is not declared by project {project!r}"
            ),
        },
    ]


#: Keys a plan write restates rather than authors, so two writers always agree
#: about them and they must not decide whether the stamps make two writers
#: comparable at all.
_STATE_STAMPS = frozenset(["version", "modified"])

# Authored HTML insertions are write effects, not plan state. ``apply_ops`` and
# ``write_plan`` run in the same request context, while ContextVar keeps
# concurrent requests isolated. Holding the working dict by identity also means
# an effect can only be consumed by the exact validated object that produced it.
_SECTION_INSERTIONS: ContextVar[tuple[dict[str, Any], list[dict[str, str]]] | None] = (
    ContextVar("reckon_section_insertions", default=None)
)

#: Evidence appends are the same kind of write effect, but their destination is
#: the plan's cumulative landing record rather than its own HTML. Kept in its own
#: collection so an evidence beat never forces a plan-file write.
_EVIDENCE_APPENDS: ContextVar[tuple[dict[str, Any], list[dict[str, str]]] | None] = (
    ContextVar("reckon_evidence_appends", default=None)
)


def _begin_write_effects(working: dict[str, Any]) -> None:
    """Start empty out-of-band effect collections for one op batch."""
    _SECTION_INSERTIONS.set((working, []))
    _EVIDENCE_APPENDS.set((working, []))


def _queue_section_insertion(working: dict[str, Any], request: dict[str, str]) -> None:
    """Attach one insertion to the current batch without changing plan state."""
    pending = _SECTION_INSERTIONS.get()
    if pending is None or pending[0] is not working:
        raise OpError("insert_section has no active write-effect collection")
    pending[1].append(request)


def _consume_section_insertions(data: dict[str, Any]) -> list[dict[str, str]]:
    """Return effects produced by this exact state object, then clear them."""
    pending = _SECTION_INSERTIONS.get()
    if pending is None or pending[0] is not data:
        return []
    _SECTION_INSERTIONS.set(None)
    return list(pending[1])


def _queue_evidence_append(working: dict[str, Any], request: dict[str, str]) -> None:
    """Attach one evidence-record append to the current batch."""
    pending = _EVIDENCE_APPENDS.get()
    if pending is None or pending[0] is not working:
        raise OpError("append_evidence has no active write-effect collection")
    pending[1].append(request)


def _consume_evidence_appends(data: dict[str, Any]) -> list[dict[str, str]]:
    """Return evidence appends produced by this exact state object, then clear."""
    pending = _EVIDENCE_APPENDS.get()
    if pending is None or pending[0] is not data:
        return []
    _EVIDENCE_APPENDS.set(None)
    return list(pending[1])


def _insert_authored_section(html_text: str, request: dict[str, str]) -> str:
    """Insert an h2 before structured plan state, or at the end of main prose."""
    from html import escape

    from bs4 import BeautifulSoup

    section_id = request["id"]
    title = request["title"]
    body = request["body"]
    soup = BeautifulSoup(html_text, "html.parser")
    if soup.find(id=section_id) is not None:
        raise OpError(f"section id {section_id!r} already exists")

    body_soup = BeautifulSoup(body, "html.parser")
    if body_soup.find("h2") is not None:
        raise OpError("insert_section body must not contain another h2")
    if body_soup.select_one("section[data-reckon]") is not None:
        raise OpError("insert_section body must not contain structured plan state")
    if body_soup.select_one('meta[name^="plan-"]') is not None:
        raise OpError("insert_section body must not contain plan metadata")

    boundary = None
    for candidate in re.finditer(r"<section\b[^>]*>", html_text, re.IGNORECASE):
        element = BeautifulSoup(candidate.group(), "html.parser").find("section")
        if element is not None and element.get("data-reckon") not in {None, "section"}:
            boundary = candidate
            break
    if boundary is None:
        boundary = re.search(r"</main\s*>", html_text, re.IGNORECASE)
    if boundary is None:
        raise OpError(
            "insert_section requires a structured-state region or main element"
        )

    line_start = html_text.rfind("\n", 0, boundary.start()) + 1
    indentation = html_text[line_start : boundary.start()]
    if indentation.strip():
        indentation = ""
    fragment = f'<h2 id="{escape(section_id, quote=True)}">{escape(title)}</h2>\n'
    if body.strip():
        fragment += body.strip() + "\n"
    fragment += "\n" + indentation
    return html_text[: boundary.start()] + fragment + html_text[boundary.start() :]


def _evidence_record_path(docs_dir: Path, plan_slug: str) -> Path:
    """The cumulative landing record one plan's evidence appends land in."""
    return docs_dir / "evidence" / "archive" / f"{plan_slug}-landed.html"


def _evidence_plan_title(project: str, plan_slug: str, root: str | Path | None) -> str:
    """The title an op-created landing record names, from the plan it documents."""
    state, _ = read_plan(project, plan_slug, root)
    title = str(state.get("title") or "").strip()
    return title or plan_slug


def _landed_record_shell(project: str, plan_slug: str, plan_title: str) -> str:
    """The document a missing landing record is created as.

    Delegates to the synthesis renderer with no runs or comments attached, so an
    op-created record and a synthesized one are the same document with the same
    metas — the anchored sections are what the two writers add afterwards.
    """
    from reckon import evidence

    plan = {"slug": plan_slug, "title": plan_title, "comments": {}}
    return evidence._render_document(project, plan, "", [], Path("."))


def _evidence_section_html(request: dict[str, str]) -> str:
    """One anchored section element carrying an append's authored content."""
    from html import escape

    from bs4 import BeautifulSoup

    body = request["body"]
    body_soup = BeautifulSoup(body, "html.parser")
    if body_soup.find("h2") is not None:
        raise OpError("append_evidence body must not contain another h2")
    if body_soup.select_one("section[data-reckon]") is not None:
        raise OpError("append_evidence body must not contain structured state")
    if body_soup.select_one('meta[name^="plan-"]') is not None:
        raise OpError("append_evidence body must not contain plan metadata")

    anchor = escape(request["anchor"], quote=True)
    parts = [f'  <section id="{anchor}">', f"    <h2>{escape(request['title'])}</h2>"]
    if body.strip():
        parts.append("    " + body.strip())
    parts.append("  </section>")
    return "\n".join(parts)


def _append_evidence_to_text(html_text: str, request: dict[str, str]) -> str:
    """Insert one anchored section before the record's closing main element."""
    from bs4 import BeautifulSoup

    anchor = request["anchor"]
    if BeautifulSoup(html_text, "html.parser").find(id=anchor) is not None:
        raise OpError(
            f"evidence anchor {anchor!r} already exists in the landing record"
        )
    boundary = re.search(r"</main\s*>", html_text, re.IGNORECASE)
    if boundary is None:
        raise OpError(
            "append_evidence requires a landing record with a closing main element"
        )
    fragment = _evidence_section_html(request) + "\n\n"
    return html_text[: boundary.start()] + fragment + html_text[boundary.start() :]


def _apply_evidence_appends(
    docs_dir: Path,
    project: str,
    requests: list[dict[str, str]],
    root: str | Path | None,
) -> list[Path]:
    """Validate every append, then write each landing record atomically.

    The duplicate-anchor refusal runs over the whole batch before any file is
    written, so a refused batch leaves every record untouched.
    """
    planned: list[tuple[Path, str]] = []
    for request in requests:
        path = _evidence_record_path(docs_dir, request["plan"])
        if path.is_file():
            current = path.read_text(encoding="utf-8", errors="replace")
        else:
            current = _landed_record_shell(
                project,
                request["plan"],
                _evidence_plan_title(project, request["plan"], root),
            )
        planned.append((path, _append_evidence_to_text(current, request)))

    written: list[Path] = []
    for path, new_text in planned:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".html.tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(path)
        written.append(path)
    return written


def _apply_append_evidence(
    working: dict, op: dict, is_index: bool, warnings: list[str]
) -> None:
    """Queue one anchored append to a plan's cumulative landing record."""
    if is_index or str(working.get("type", "plan") or "plan") != "plan":
        raise OpError("append_evidence op is plan-only")
    plan = op.get("plan")
    if not isinstance(plan, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", plan
    ):
        raise OpError(
            "append_evidence op requires a 'plan' slug matching "
            "[A-Za-z0-9][A-Za-z0-9._-]*"
        )
    anchor = op.get("anchor")
    if not isinstance(anchor, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", anchor
    ):
        raise OpError(
            "append_evidence op requires an 'anchor' id matching "
            "[A-Za-z0-9][A-Za-z0-9._-]*"
        )
    title = op.get("title")
    if not isinstance(title, str) or not title.strip():
        raise OpError("append_evidence op requires a non-empty string 'title'")
    body = op.get("body")
    if not isinstance(body, str):
        raise OpError("append_evidence op requires a string 'body'")
    request = {
        "plan": plan,
        "anchor": anchor,
        "title": title.strip(),
        "body": body,
    }
    _evidence_section_html(request)  # refuse a malformed body before any write
    _queue_evidence_append(working, request)


def _plan_write_target(
    project: str,
    slug: str,
    root: str | Path | None,
    artifact_type: str | None,
) -> Path:
    """The file a plan write will reach, whether or not it exists yet."""
    html_file = _resolve_html_file(project, slug, root, artifact_type)
    if html_file is not None:
        return html_file
    docs_dir = _docs_dir_for_project(project, root)
    if docs_dir is None:
        return _config_home() / "locks" / "unresolved" / f"{project}-{slug}.html"
    return docs_dir / "plans" / f"{slug}.html"


def _comment_append_onto_current(data: dict, current: dict) -> dict | None:
    """Merge a comment append whose base revision moved under it.

    A comment append is the one write shape that is commutative: comments are
    append-only, and a write carrying entries the file does not hold has added
    them rather than replaced the collection. When the file's comments and the
    write's other fields agree, the two collections are merged so both writers
    survive the meeting. ``None`` means the writers disagree about the document
    itself — a real conflict, for the caller to report rather than resolve;
    merging it would silently revert the other writer's change.
    """
    from reckon import _plan_html

    incoming = {k: v for k, v in dict(data).items() if k not in _STATE_STAMPS}
    held = {k: v for k, v in dict(current).items() if k not in _STATE_STAMPS}
    incoming_comments = incoming.pop("comments", None)
    held_comments = held.pop("comments", None)
    if incoming != held:
        return None
    if not isinstance(incoming_comments, dict) or not incoming_comments:
        return None
    merged = _plan_html.merge_comment_collections(held_comments, incoming_comments)
    if merged == held_comments:
        return None
    return merged


def _write_state(
    project: str,
    slug: str,
    data: dict,
    expected_version: int,
    root: str | Path | None = None,
    artifact_type: str | None = None,
    retire_preimages: list[str] | None = None,
) -> int:
    """Atomically rewrite the semantic HTML state for a plan slug.

    ``root`` (a checkout repo root) targets that checkout's ``docs`` dir;
    defaults to the mounts-registered (main) checkout.

    Raises VersionConflict on mismatch.
    Returns the new version.
    """
    with _serialized_path_lock(
        _plan_write_target(project, slug, root, artifact_type), "plans"
    ):
        return _write_state_locked(
            project, slug, data, expected_version, root, artifact_type, retire_preimages
        )


def _write_state_locked(
    project: str,
    slug: str,
    data: dict,
    expected_version: int,
    root: str | Path | None = None,
    artifact_type: str | None = None,
    retire_preimages: list[str] | None = None,
) -> int:
    """The version check and the replacement of one plan HTML file.

    Runs with the plan's write lock held, so the file the check sees is the file
    the replacement overwrites.
    """
    from reckon import _plan_html

    from reckon._schema import TYPE_ENUM
    from reckon.resources import canonical_type, resolve_resource

    docs_dir = _docs_dir_for_project(project, root)
    selected_type = canonical_type(artifact_type) if artifact_type else None
    if docs_dir is None:
        html_file = None
        selected_resource_type = selected_type
    else:
        matches = []
        candidate_types = [selected_type] if selected_type is not None else TYPE_ENUM
        for candidate_type in candidate_types:
            resource = resolve_resource(
                docs_dir,
                project,
                slug,
                candidate_type,
                include_archived=True,
            )
            if resource is not None:
                matches.append(resource)
        if selected_type is None and len(matches) > 1:
            kinds = ", ".join(sorted(resource.type for resource in matches))
            raise ValueError(
                f"resource slug {slug!r} is ambiguous across types: {kinds}; "
                "supply artifact_type"
            )
        selected = matches[0] if matches else None
        if selected is not None:
            _refuse_immutable_snapshot(selected)
        html_file = selected.path if selected else None
        selected_resource_type = selected.type if selected else selected_type
    if html_file is None or not html_file.is_file():
        # Cannot write to a non-existent HTML file; create one only if the
        # docs dir exists and expected_version==0 (first write).
        if expected_version != 0:
            raise VersionConflict(expected_version, 0, {})
        if docs_dir is None:
            raise FileNotFoundError(
                f"No docs dir found for project {project!r} — "
                "check mounts.json or RECKON_MOUNTS_PATH"
            )
        if selected_type not in {None, "plan"}:
            raise FileNotFoundError(
                f"{selected_type} resource {slug!r} does not exist; "
                "typed creation is not supported"
            )
        selected_resource_type = "plan"
        html_file = docs_dir / "plans" / f"{slug}.html"
        html_file.parent.mkdir(parents=True, exist_ok=True)
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
        text = html_file.read_text(encoding="utf-8", errors="replace")
    else:
        text = html_file.read_text(encoding="utf-8", errors="replace")
        cur_state = _state_from_text(project, text, root)
        cur_version = int(cur_state.get("version", 0) or 0)

    # Consume against the exact op-working object before a commutative comment
    # merge can replace ``data`` with a fresh mapping.
    section_insertions = _consume_section_insertions(data)
    evidence_appends = _consume_evidence_appends(data)

    if expected_version != cur_version:
        merged_comments = _comment_append_onto_current(data, cur_state)
        if merged_comments is None:
            raise VersionConflict(expected_version, cur_version, cur_state)
        data = {**dict(data), "comments": merged_comments}

    new_data = dict(data)
    state_type = canonical_type(new_data.get("type"))
    if selected_resource_type and state_type != selected_resource_type:
        raise ValueError(
            f"state type {state_type!r} does not match selected resource type "
            f"{selected_resource_type!r}"
        )
    current_status = str(cur_state.get("status") or "").strip().lower()
    requested_status = str(new_data.get("status") or "").strip().lower()
    current_summary = str(cur_state.get("summary") or "")
    requested_summary = str(new_data.get("summary") or "")
    if requested_summary != current_summary:
        _validate_plan_summary(requested_summary)
    if (
        state_type == "plan"
        and current_status not in TERMINAL_STATUSES
        and requested_status in TERMINAL_STATUSES
    ):
        _require_transition_verdict(new_data, "plan-terminal")
        _require_terminal_evidence(project, slug, requested_status, root)
    if state_type == "plan":
        _validate_decision_transitions(new_data, cur_state)
    new_data.pop("_version", None)  # never allow the old JSON key in the state
    new_data["modified"] = date.today().isoformat()
    new_data["version"] = cur_version + 1

    source_text = text
    try:
        for preimage in retire_preimages or []:
            source_text = _replace_authored_html(
                source_text,
                preimage,
                "",
                selector_name="retire_prose preimage",
            )
        for request in section_insertions:
            source_text = _insert_authored_section(source_text, request)
    except ValueError as exc:
        raise OpError(str(exc)) from exc
    if evidence_appends:
        if docs_dir is None:
            raise OpError(
                f"append_evidence: no docs dir for project {project!r}"
            )
        _apply_evidence_appends(docs_dir, project, evidence_appends, root)
    authored_text_changed = source_text != text
    new_text = _plan_html.write_state(source_text, new_data)

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
    if (
        not legacy_alias
        and not authored_text_changed
        and {k: v for k, v in new_parsed.items() if k not in _STAMP}
        == {k: v for k, v in cur_parsed.items() if k not in _STAMP}
    ):
        return cur_version

    tmp = html_file.with_suffix(".html.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(html_file)
    return new_data["version"]


# ── Public API ─────────────────────────────────────────────────────────────


def read_plan(
    project: str,
    slug: str,
    root: str | Path | None = None,
    artifact_type: str | None = None,
) -> tuple[dict, int]:
    """Read the data blob and version for a plan (or JSON config doc).

    For plan slugs: reads the semantic HTML state; version = state["version"].
    For JSON slugs (index/project): reads the JSON envelope; version = data["_version"].

    ``root`` (a checkout repo root) targets that checkout's ``docs`` tree for
    BOTH plan HTML and JSON config (index/project) — the multi-worktree path.
    Defaults to the mounts-registered / config-home (main) checkout.

    Returns:
        (data, version) — returns ({}, 0) if absent and refuses corrupt input.
    """
    from reckon.project_state import (
        RESOURCE_TYPES as PROJECT_RESOURCE_TYPES,
        compose_project_state,
        project_state_mode,
        read_resource,
    )

    docs_dir = _docs_dir_for_project(project, root)
    if artifact_type in PROJECT_RESOURCE_TYPES:
        if docs_dir is None:
            return {}, 0
        return read_resource(docs_dir, project, artifact_type, slug)
    if _is_json_slug(slug, artifact_type):
        if slug == "index" and docs_dir is not None:
            mode = project_state_mode(docs_dir)
            if mode.format == "distributed":
                data = compose_project_state(docs_dir, project)
                return data, 0
        return _load_json_envelope(state_path(project, slug, root))
    return _read_state(project, slug, root, artifact_type)


def write_plan(
    project: str,
    slug: str,
    data: dict,
    expected_version: int,
    root: str | Path | None = None,
    artifact_type: str | None = None,
    retire_preimages: list[str] | None = None,
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
    from reckon.project_state import (
        RESOURCE_TYPES as PROJECT_RESOURCE_TYPES,
        LegacyIndexReadOnly,
        project_state_mode,
        write_resource,
    )

    docs_dir = _docs_dir_for_project(project, root)
    if artifact_type in PROJECT_RESOURCE_TYPES:
        if docs_dir is None:
            raise FileNotFoundError(f"No docs dir found for project {project!r}")
        return write_resource(
            docs_dir,
            project,
            artifact_type,
            slug,
            data,
            expected_version,
        )
    if _is_json_slug(slug, artifact_type):
        if slug == "index" and docs_dir is not None:
            mode = project_state_mode(docs_dir)
            if mode.format == "distributed":
                raise LegacyIndexReadOnly(
                    "legacy_index_read_only: aggregate index writes are disabled; "
                    "read resource_versions and edit the named sprint, milestone, "
                    "blocker, timeline, project, or review resource with doc_type"
                )
        return _write_json_envelope(
            state_path(project, slug, root), project, slug, data, expected_version
        )
    return _write_state(
        project,
        slug,
        data,
        expected_version,
        root,
        artifact_type,
        retire_preimages,
    )


def _replace_authored_html(
    html_text: str,
    preimage: str,
    replacement: str,
    *,
    selector_name: str,
) -> str:
    """Replace one exact authored fragment without touching structured state."""

    from reckon import _plan_html

    occurrences = html_text.count(preimage)
    if occurrences != 1:
        raise ValueError(
            f"{selector_name} must match exactly once; found {occurrences} occurrences"
        )
    start = html_text.index(preimage)
    end = start + len(preimage)
    if any(
        start < protected_end and end > protected_start
        for protected_start, protected_end in _plan_html.structured_section_spans(
            html_text
        )
    ):
        raise ValueError(
            f"{selector_name} overlaps a section[data-reckon] structured-state region"
        )

    replaced = html_text[:start] + replacement + html_text[end:]
    stamps = frozenset({"version", "modified"})
    before_state = _plan_html.read_state(html_text)
    if before_state.get("type", "plan") == "plan":
        _require_new_section_contracts(html_text, replaced)
    after_state = _plan_html.read_state(replaced)
    before = {key: value for key, value in before_state.items() if key not in stamps}
    after = {key: value for key, value in after_state.items() if key not in stamps}
    if before != after:
        raise ValueError(
            "authored HTML replacement changes structured plan state; use "
            "edit_plan structured ops for metadata or data-reckon sections"
        )
    return replaced


def _section_contract_refusal(detail: str) -> str:
    """Give authors a complete state-mode append they can adapt and submit."""
    example = {
        "op": "append",
        "target": "sections",
        "item": {
            "id": "s2",
            "title": "Implement and verify",
            "body": "<p>Describe the work and its acceptance check.</p>",
            "effort_hours": 1.25,
            "capability": {
                "version": "1.0",
                "class": "general",
                "requirements": {
                    "reasoning": "standard",
                    "verification": "strict",
                    "risk": "low",
                },
            },
            "links": [],
        },
    }
    return f"{detail}. Use edit_plan mode=state. Example: {json.dumps(example)}"


def _new_section_record(working: dict, section: dict) -> dict:
    """Validate a new section's typed record, refusing with the worked example."""
    from pydantic import ValidationError

    from reckon._schema import SectionRecord

    fields = {"id", "title", "body", "effort_hours", "capability", "links"}
    extra = section.keys() - fields
    if extra:
        raise OpError(
            _section_contract_refusal(f"unsupported section fields: {sorted(extra)}")
        )
    declarations = working.get("section_declarations") or {}
    record_fields = {
        key: value for key, value in section.items() if key not in {"title", "body"}
    }
    try:
        return SectionRecord.model_validate(
            {
                **record_fields,
                "attempts": 0,
                "status": (
                    declarations.get(section["id"], "implementable")
                    if isinstance(section.get("id"), str)
                    else "implementable"
                ),
            }
        ).model_dump(by_alias=True, exclude_none=True)
    except ValidationError as exc:
        raise OpError(_section_contract_refusal(str(exc))) from exc


def _require_new_section_contracts(before_html: str, after_html: str) -> None:
    """Require records for newly introduced numbered plan headings only."""
    from bs4 import BeautifulSoup

    from reckon import _plan_html

    before = BeautifulSoup(before_html, "html.parser")
    after = BeautifulSoup(after_html, "html.parser")
    old_ids = {heading.get("id") for heading in before.find_all("h2", id=True)}
    added = {
        heading["id"]
        for heading in after.find_all("h2", id=re.compile(r"^s[0-9]+$"))
        if heading["id"] not in old_ids
    }
    if not added:
        return
    try:
        records = _plan_html.read_state(after_html).get("sections", [])
    except ValueError as exc:
        raise ValueError(_section_contract_refusal(str(exc))) from exc
    missing = added - {record["id"] for record in records}
    if missing:
        raise ValueError(
            _section_contract_refusal(
                f"new sections {sorted(missing)!r} missing effort_hours and capability: "
                "no typed section record"
            )
        )


# An archived EVIDENCE record stays writable; an archived plan or research
# snapshot does not. The distinction is the documented contract rather than a
# convenience: a frozen snapshot is a record of what a plan said at a moment and
# rewriting it destroys the thing it exists to preserve, while a cumulative
# execution record is appendable by design and outlives the plan that prompted
# it. Measured 2026-09-14: a withdrawn measurement in an archived evidence
# record still carried the sizing conclusions drawn from it, and correcting them
# through this path was refused, so the correction was made by hand outside the
# version check that exists to make such edits safe.
_ARCHIVED_WRITABLE_TYPES = frozenset({"evidence"})


def _refuse_immutable_snapshot(resource) -> None:
    """Refuse a write to a frozen snapshot, naming why rather than 'not found'."""
    if resource.archived and resource.type not in _ARCHIVED_WRITABLE_TYPES:
        raise ValueError(
            f"archived {resource.type} {resource.slug!r} is a frozen snapshot "
            "and is immutable; edit the live resource, or append to its "
            "evidence record"
        )


def replace_plan_text(
    project: str,
    slug: str,
    old_html: str,
    new_html: str,
    expected_version: int,
    root: str | Path | None = None,
    artifact_type: str | None = None,
) -> tuple[int, Path]:
    """Replace one exact authored HTML fragment and advance the plan version.

    Structured metadata and ``data-reckon`` sections are deliberately outside
    this operation.  Their parsed state must remain identical, so callers use
    :func:`write_plan` or the MCP ``edit_plan`` tool for those fields.
    """

    from reckon import _plan_html
    from reckon._schema import TYPE_ENUM
    from reckon.resources import canonical_type, resolve_resource

    if not old_html:
        raise ValueError("old_html must be non-empty")
    if old_html == new_html:
        raise ValueError("old_html and new_html are identical")

    docs_dir = _docs_dir_for_project(project, root)
    if docs_dir is None:
        raise FileNotFoundError(f"No docs dir found for project {project!r}")
    selected_type = canonical_type(artifact_type) if artifact_type else None
    matches = []
    for candidate_type in [selected_type] if selected_type else TYPE_ENUM:
        resource = resolve_resource(
            docs_dir,
            project,
            slug,
            candidate_type,
            include_archived=True,
        )
        if resource is not None:
            matches.append(resource)
    if not matches:
        raise FileNotFoundError(f"resource {slug!r} does not exist")
    if len(matches) > 1:
        kinds = ", ".join(sorted(resource.type for resource in matches))
        raise ValueError(
            f"resource slug {slug!r} is ambiguous across types: {kinds}; "
            "supply artifact_type"
        )
    resource = matches[0]
    _refuse_immutable_snapshot(resource)
    html_file = resource.path
    with _serialized_path_lock(html_file, "plans"):
        text = html_file.read_text(encoding="utf-8", errors="strict")
        current_state = _plan_html.read_state(text)
        current_version = int(current_state.get("version", 0) or 0)
        if expected_version != current_version:
            raise VersionConflict(expected_version, current_version, current_state)

        replaced = _replace_authored_html(
            text,
            old_html,
            new_html,
            selector_name="old_html",
        )

        stamped_state = dict(current_state)
        stamped_state["modified"] = date.today().isoformat()
        stamped_state["version"] = current_version + 1
        rendered = _plan_html.write_state(replaced, stamped_state)
        tmp = html_file.with_suffix(".html.tmp")
        tmp.write_text(rendered, encoding="utf-8")
        tmp.replace(html_file)
        return current_version + 1, html_file


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

    if field == "followups":
        if not isinstance(item, dict):
            raise OpError("followup must be an object")
        item = {
            **item,
            "prompt": _validate_new_followup_prompt(item.get("prompt", "")),
        }
    lst = list(cur_data.get(field, []))
    if isinstance(item, dict) and item.get("id"):
        _refuse_duplicate_id(lst, field, str(item["id"]))
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
    hit = _find_open_by_id(lst, item_id)
    if hit is None:
        raise KeyError(_missing_open_entry_detail(lst, field, item_id))
    idx, item = hit
    lst[idx] = {**item, **updates}

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
    from reckon.resources import resource_map

    docs_dir = _docs_dir_for_project(project, root)
    if docs_dir is None:
        return []

    results: list[dict] = []
    for resource in resource_map(
        docs_dir,
        project,
        include_archived=False,
        ignore_invalid=True,
    ).values():
        if resource.type != "plan":
            continue
        html_file = resource.path
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
    from reckon.resources import resource_map

    docs_dir = _docs_dir_for_project(project, root)
    if docs_dir is None:
        return []

    results: list[dict] = []
    for resource in resource_map(
        docs_dir,
        project,
        include_archived=False,
        ignore_invalid=True,
    ).values():
        if resource.type != "plan":
            continue
        html_file = resource.path
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


def _validate_new_followup_prompt(value: Any) -> str:
    """Return one canonical session invocation or raise at the write boundary."""
    prompt = str(value)
    if (
        prompt != prompt.strip()
        or len(prompt.splitlines()) != 1
        or not prompt.startswith("/reckon-build ")
    ):
        raise OpError(
            "followup prompt must be one /reckon-build invocation line; "
            "store guidance in the plan"
        )
    return prompt


# Top-level plan scalar fields a `set` op may target directly (everything else
# routes through dotted handling for decisions.<key>.<field>).
_PLAN_SET_TOP = frozenset(
    {
        "status",
        "impl",
        "standalone",
        "section_declarations",
        "roi",
        "effort_hours",
        "effort",
        "milestone",
        "sprint",
        "graph_handle",
        "north_star",
        "capability",
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

# Index top-level fields a `set` op may target directly.
_INDEX_SET_TOP = frozenset({"active_sprint_id", "north_stars"})


def _find_by_id(lst: list, ident: str, id_field: str = "id") -> tuple[int, dict] | None:
    for i, el in enumerate(lst):
        if isinstance(el, dict) and el.get(id_field) == ident:
            return i, el
    return None


def _check_north_star_ids(entries: Any, warnings: list[str]) -> None:
    """Refuse duplicate direction ids and report the advisory collection cap."""
    if not isinstance(entries, list):
        return
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        ident = entry["id"]
        if ident in seen:
            raise OpError(f"duplicate north-star id {ident!r}")
        seen.add(ident)

    from reckon.project_state import NORTH_STAR_ADVISORY_CAP

    if len(entries) > NORTH_STAR_ADVISORY_CAP:
        warning = (
            f"project declares {len(entries)} north-stars; "
            f"the advisory cap is {NORTH_STAR_ADVISORY_CAP}"
        )
        if warning not in warnings:
            warnings.append(warning)


def _entry_is_open(entry: dict[str, Any]) -> bool:
    """Whether an id-addressed workflow entry is still open."""
    if entry.get("resolved_at"):
        return False
    status = str(entry.get("status", "") or "").lower()
    return not status or status == "open"


def _find_open_by_id(lst: list, ident: str) -> tuple[int, dict] | None:
    """Return the first open entry with ``ident``, ignoring closed matches."""
    for i, entry in enumerate(lst):
        if (
            isinstance(entry, dict)
            and entry.get("id") == ident
            and _entry_is_open(entry)
        ):
            return i, entry
    return None


def _collection_label(collection: str) -> str:
    return {
        "followups": "followup",
        "questions": "question",
        "research": "research entry",
        "comments": "comment",
    }.get(collection, collection.rstrip("s") or "entry")


def _refuse_duplicate_id(entries: list, collection: str, ident: str) -> None:
    """Refuse an append whose id already addresses an existing entry."""
    if _find_by_id(entries, ident) is not None:
        raise OpError(f"{_collection_label(collection)} {ident!r} already exists")


def _missing_open_entry_detail(entries: list, collection: str, ident: str) -> str:
    """Describe whether a resolve missed entirely or found only closed entries."""
    label = _collection_label(collection)
    if _find_by_id(entries, ident) is not None:
        return f"{label} {ident!r} has no open entry"
    return f"{label} {ident!r} not found"


def _comment_entries(comments: Any) -> list:
    """Flatten section-addressed comments for document-wide id checks."""
    if not isinstance(comments, dict):
        return []
    return [
        comment
        for section_comments in comments.values()
        if isinstance(section_comments, list)
        for comment in section_comments
    ]


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
        if head in ("sprints", "milestones", "north_stars") and len(parts) >= 3:
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
            if head == "north_stars":
                _check_north_star_ids(lst, warnings)
            return
        if head in _INDEX_SET_TOP and len(parts) == 1:
            working[head] = value
            if head == "north_stars":
                _check_north_star_ids(value, warnings)
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
    if head == "section_declarations" and len(parts) == 2:
        from reckon._schema import SECTION_DECLARATION_ENUM

        section_id = parts[1]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", section_id):
            raise OpError(
                "section declaration id must match "
                f"[A-Za-z0-9][A-Za-z0-9._-]*; got {section_id!r}"
            )
        if value not in SECTION_DECLARATION_ENUM:
            raise OpError(
                f"section declaration must be one of {SECTION_DECLARATION_ENUM}; "
                f"got {value!r}"
            )
        declarations = working.setdefault("section_declarations", {})
        if not isinstance(declarations, dict):
            raise OpError("plan has no section declarations map")
        declarations[section_id] = value
        sections = working.get("sections")
        if isinstance(sections, list):
            for section in sections:
                if isinstance(section, dict) and section.get("id") == section_id:
                    section["status"] = value
                    break
        return
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
    if head == "followups" and len(parts) == 3 and parts[2] == "prompt":
        followups = working.setdefault("followups", [])
        if not isinstance(followups, list):
            raise OpError("plan has no followups list")
        hit = _find_by_id(followups, parts[1])
        if hit is None:
            raise OpError(f"followup {parts[1]!r} not found")
        idx, followup = hit
        followups[idx] = {
            **followup,
            "prompt": _validate_new_followup_prompt(value),
        }
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
    if head == "effort_hours":
        working["effort_hours"] = value
        working["effort_calibrated"] = True
        if working.get("effort"):
            warnings.append(
                "legacy effort letter is redundant because explicit worker-hours win"
            )
        return
    if head == "capability":
        working["capability"] = value
        if working.pop("tier", None):
            warnings.append("legacy tier removed because capability was set explicitly")
        return
    if head == "summary":
        working[head] = _validate_plan_summary(value)
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


def _decision_options_as_choices(options: Any) -> tuple[list[str], dict[str, str]]:
    """Normalise a caller's option spelling to ``(values, value -> label)``.

    Accepts the shapes a JSON caller produces: a list of strings, a list of
    objects carrying ``value`` (optionally ``label``), and a mapping of value
    to label. An unrecognised shape is refused rather than dropped, because a
    silently discarded option is the same defect this normalisation closes.
    A JSON null is refused in the same breath: reading it as absent and then
    stringifying it made ``str(None)`` a valid option literally spelled
    ``None``, which the emptiness check below cannot see.
    """
    values: list[str] = []
    labels: dict[str, str] = {}
    if isinstance(options, list):
        for entry in options:
            if isinstance(entry, dict):
                # A missing value key is refused before it can be confused with
                # a null one: ``entry.get("value", entry.get("label"))`` reads
                # both alike, so an option carrying only a label was accepted
                # with the label as its value, and an option carrying neither
                # was refused as a null when no key was present at all.
                if "value" not in entry:
                    raise OpError(f"decision option {entry!r} carries no 'value' key")
                value = entry["value"]
                label = entry.get("label", value)
            elif isinstance(entry, str):
                value = entry
                label = entry
            else:
                raise OpError(
                    f"decision option {entry!r} is neither a string nor an object"
                )
            if value is None:
                raise OpError(f"decision option {entry!r} carries a null 'value'")
            value = str(value)
            if not value:
                raise OpError("decision option carries no 'value'")
            values.append(value)
            labels[value] = str(label)
        return values, labels
    if isinstance(options, dict):
        for value, label in options.items():
            if value is None:
                raise OpError(f"decision option {value!r} carries a null 'value'")
            values.append(str(value))
            labels[str(value)] = str(value if label is None else label)
        return values, labels
    raise OpError(
        "decision 'options' must be a list of values or a mapping of value to label"
    )


def _decision_as_stored(item: Any) -> dict:
    """Fold the read view's decision spelling onto the stored block.

    The open-decisions view hands a caller ``question`` and ``options``, while
    the stored block speaks ``title`` and ``choices``. A caller echoing back the
    decision it just read therefore used to land a block whose question
    paragraph fell back to the key and which rendered no options at all: the
    append returned ok and the decision was unanswerable. Both spellings are
    accepted here so a round trip through the view preserves the decision.
    """
    if not isinstance(item, dict):
        return {}
    stored = dict(item)
    question = stored.pop("question", None)
    if not stored.get("title") and question:
        stored["title"] = question
    options = stored.pop("options", None)
    if options is not None and not stored.get("choices"):
        values, labels = _decision_options_as_choices(options)
        stored["choices"] = values
        if labels:
            stored["option_labels"] = {
                **labels,
                **(stored.get("option_labels") or {}),
            }
    return stored


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
            if (
                isinstance(item, dict)
                and item.get("tier")
                and not item.get("capability")
            ):
                raise OpError(
                    "new sprint items must use capability instead of legacy tier"
                )
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
    if target == "sections":
        if str(working.get("type", "plan") or "plan") != "plan":
            raise OpError("append sections is plan-only")
        if not isinstance(item, dict):
            raise OpError(
                _section_contract_refusal("append sections requires an item object")
            )
        record = _new_section_record(working, item)
        sections = working.setdefault("sections", [])
        _refuse_duplicate_id(sections, target, record["id"])
        try:
            _queue_authored_section(working, item)
        except OpError as exc:
            raise OpError(_section_contract_refusal(str(exc))) from exc
        sections.append(record)
        working.setdefault("section_declarations", {})[record["id"]] = record["status"]
        return
    if target == "followups":
        if not isinstance(item, dict):
            raise OpError("append followups requires an item object")
        required = {"id", "written_by", "written_at", "title", "body", "prompt"}
        fu = dict(item)
        if fu.get("tier") and not fu.get("capability"):
            raise OpError("new followups must use capability instead of legacy tier")
        if not fu.get("id"):
            fu["id"] = _gen_id("f")
        missing = [k for k in sorted(required) if not str(fu.get(k, "")).strip()]
        if missing:
            raise OpError(f"followup missing required fields: {missing}")
        fu["prompt"] = _validate_new_followup_prompt(fu["prompt"])
        followups = working.setdefault("followups", [])
        _refuse_duplicate_id(followups, target, fu["id"])
        followups.append(fu)
        return
    if target == "research":
        if not isinstance(item, dict):
            raise OpError("append research requires an item object")
        r = dict(item)
        if not r.get("id"):
            r["id"] = _gen_id("r")
        research = working.setdefault("research", [])
        _refuse_duplicate_id(research, target, r["id"])
        research.append(r)
        return
    if target == "questions":
        if not isinstance(item, dict):
            raise OpError("append questions requires an item object")
        q = dict(item)
        if not q.get("id"):
            q["id"] = _gen_id("q")
        questions = working.setdefault("questions", [])
        _refuse_duplicate_id(questions, target, q["id"])
        questions.append(q)
        return
    if target == "comments":
        if not isinstance(item, dict):
            raise OpError("append comments requires an item object")
        section = op.get("section") or "_top"
        c = dict(item)
        if not c.get("id"):
            c["id"] = _gen_id("c")
        comments = working.setdefault("comments", {})
        _refuse_duplicate_id(_comment_entries(comments), target, c["id"])
        comments.setdefault(section, []).append(c)
        return
    if target == "decisions":
        key = op.get("key")
        if not key:
            raise OpError("append decisions requires a 'key'")
        decisions = working.setdefault("decisions", {})
        if key in decisions:
            raise OpError(f"decision {key!r} already exists")
        decisions[key] = _decision_as_stored(item)
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
    hit = _find_open_by_id(lst, ident)
    if hit is None:
        raise OpError(_missing_open_entry_detail(lst, target, ident))
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
    if op.get("choice"):
        _require_transition_verdict(working, "decision-lockable", decision=key)
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


def _apply_gate(working: dict, op: dict, is_index: bool, warnings: list[str]) -> None:
    """Declare one evidence gate with a stable identity."""
    if is_index:
        raise OpError("gate op is plan-only")
    ident = str(op.get("id", "")).strip()
    measure = str(op.get("measure", "")).strip()
    if not ident:
        raise OpError("gate op requires an 'id'")
    if not measure:
        raise OpError(f"gate {ident!r} requires a non-empty 'measure'")
    gates = working.setdefault("gates", [])
    if not isinstance(gates, list):
        raise OpError("plan has no gates list")
    if _find_by_id(gates, ident) is not None:
        raise OpError(f"gate {ident!r} already exists")
    gated_sections = op.get("gated_sections", [])
    if not isinstance(gated_sections, list) or not all(
        isinstance(section, str) for section in gated_sections
    ):
        raise OpError("gate op 'gated_sections' must be a list of strings")
    declared = {
        "id": ident,
        "section": str(op.get("section", "")),
        "gated_sections": gated_sections,
        "status": str(op.get("status", "open")),
        "measure": measure,
        "required_evidence": str(op.get("required_evidence", "")),
        "verdict": "",
        "evidence": "",
        **{
            field: op[field]
            for field in ("transition", "gating_plan", "decision")
            if field in op
        },
    }
    from reckon._schema import Gate

    try:
        Gate.model_validate(declared).validate_for_write()
    except ValueError as exc:
        raise OpError(str(exc)) from exc
    gates.append(declared)


def _gate_requires_evidence(working: dict) -> bool:
    """Read the resolved evidence requirement for the owning project."""
    from reckon.flight import FlightConfigError, resolve

    project = str(working.get("project", "") or "")
    try:
        config = resolve(project=project or None).config
    except FlightConfigError as exc:
        raise OpError(f"cannot resolve gates.require_evidence: {exc}") from exc
    gates = config.get("gates") or {}
    return bool(gates.get("require_evidence", False))


def _apply_gate_verdict(
    working: dict, op: dict, is_index: bool, warnings: list[str]
) -> None:
    """Close one declared gate with the verdict named by the op verb."""
    if is_index:
        raise OpError(f"{op.get('op')} op is plan-only")
    ident = str(op.get("id", "")).strip()
    if not ident:
        raise OpError(f"{op.get('op')} op requires an 'id'")
    gates = working.get("gates", [])
    if not isinstance(gates, list):
        raise OpError("plan has no gates list")
    hit = _find_by_id(gates, ident)
    if hit is None:
        raise OpError(f"gate {ident!r} not found")
    idx, gate = hit
    evidence = str(op.get("evidence", "")).strip()
    verdict = "passed" if op.get("op") == "pass" else "failed"
    if verdict == "passed" and _gate_requires_evidence(working) and not evidence:
        raise OpError(
            f"gate {ident!r} cannot pass without evidence while "
            "gates.require_evidence is enabled"
        )
    gates[idx] = {
        **gate,
        "status": "closed",
        "verdict": verdict,
        "evidence": evidence,
    }


def _apply_retire_prose(
    working: dict, op: dict, is_index: bool, warnings: list[str]
) -> None:
    """Validate an ephemeral exact-preimage retirement directive."""

    if is_index or str(working.get("type", "plan") or "plan") != "plan":
        raise OpError("retire_prose op is plan-only")
    preimage = op.get("preimage")
    if not isinstance(preimage, str) or not preimage:
        raise OpError("retire_prose op requires a non-empty string 'preimage'")


def _require_authored_section_fields(section: dict) -> None:
    """Refuse an authored h2 request whose id, title or body is unusable."""
    section_id = section.get("id")
    title = section.get("title")
    body = section.get("body")
    if not isinstance(section_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", section_id
    ):
        raise OpError(
            "insert_section op requires an 'id' matching [A-Za-z0-9][A-Za-z0-9._-]*"
        )
    if not isinstance(title, str) or not title.strip():
        raise OpError("insert_section op requires a non-empty string 'title'")
    if not isinstance(body, str):
        raise OpError("insert_section op requires a string 'body'")


def _queue_authored_section(working: dict, section: dict) -> None:
    """Queue one validated authored h2 block for the atomic HTML write."""
    _require_authored_section_fields(section)
    _queue_section_insertion(
        working,
        {
            "id": section["id"],
            "title": section["title"].strip(),
            "body": section["body"],
        },
    )


def _apply_insert_section(
    working: dict, op: dict, is_index: bool, warnings: list[str]
) -> None:
    """Insert one authored h2 block carrying its typed section record."""
    if is_index or str(working.get("type", "plan") or "plan") != "plan":
        raise OpError("insert_section op is plan-only")
    _require_authored_section_fields(op)
    record = _new_section_record(
        working, {key: value for key, value in op.items() if key != "op"}
    )
    _queue_authored_section(working, op)
    working.setdefault("sections", []).append(record)
    working.setdefault("section_declarations", {})[record["id"]] = record["status"]


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
    "gate": _apply_gate,
    "pass": _apply_gate_verdict,
    "fail": _apply_gate_verdict,
    "retire_prose": _apply_retire_prose,
    "insert_section": _apply_insert_section,
    "append_evidence": _apply_append_evidence,
    "move": _apply_move,
}

# A plan reaching one of these has landed, so its writeback owes an answer about
# what comes next.
_LANDED_STATUSES = frozenset({"shipped", "done"})

# How a followup outcome says the chain deliberately ends here. Recognised in
# text because that is the form the skills already write.
_CHAIN_CLOSED_MARKERS = ("no followup", "no follow-up", "no-followup")


def _chain_closed(text: Any) -> bool:
    """Whether an outcome explicitly records that the chain ends here."""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _CHAIN_CLOSED_MARKERS)


CONTINUATION_REQUIRED = (
    "plan landing leaves no continuation: append a followup whose prompt is "
    "the next '/reckon-build <slug> [§N]' invocation, or resolve with an "
    "outcome recording that the chain closes (e.g. 'done — no followup')"
)


def _followup_is_open(followup: dict[str, Any]) -> bool:
    """Whether a followup is still carrying the chain.

    Mirrors how the schema derives the field: a resolved timestamp means
    resolved whatever the literal status says, and an absent status on an
    unresolved followup means open. Deriving it here too keeps the rule correct
    on a raw dict that has not been through the model.
    """
    return _entry_is_open(followup)


def _plan_is_in_progress(state: dict[str, Any]) -> bool:
    """Whether lifecycle state already names ongoing work."""
    status = str(state.get("status", "draft") or "draft").strip().lower()
    try:
        implementation = float(state.get("impl", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    return status not in TERMINAL_STATUSES and implementation < 1.0


def continuation_present(state: dict[str, Any]) -> bool:
    """Whether a plan's state names what comes next, or says nothing does.

    One definition shared by both write paths, so the ops writer and the HTTP
    patch writer cannot disagree about what a closed chain looks like.
    """
    if _plan_is_in_progress(state):
        return True
    followups = [f for f in (state.get("followups") or []) if isinstance(f, dict)]
    if any(_followup_is_open(f) for f in followups):
        return True
    return any(_chain_closed(f.get("outcome")) for f in followups)


def validate_landing_patch(state: dict[str, Any], patch: dict[str, Any]) -> None:
    """Refuse a merge patch that lands a plan without naming a continuation.

    Deliberately keyed to the *write* rather than to the resulting state. A
    state-level invariant would retroactively lock every plan already recorded
    as shipped without a followup — measured at 155 of 202 across the mounted
    projects — so history stays editable and only a new landing owes an answer.
    """
    if str(state.get("type", "plan") or "plan") != "plan":
        return
    requested_status = str(patch.get("status", "")).lower()
    if requested_status in TERMINAL_STATUSES:
        _require_transition_verdict(state, "plan-terminal")
        project = str(state.get("project") or "")
        slug = str(state.get("slug") or "")
        if project and slug:
            _require_terminal_evidence(project, slug, requested_status)
    if requested_status not in _LANDED_STATUSES:
        return
    if not continuation_present(state):
        raise OpError(CONTINUATION_REQUIRED)


def _require_terminal_evidence(
    project: str,
    slug: str,
    status: str,
    root: str | Path | None = None,
) -> None:
    """Refuse closure until a typed evidence resource claims the plan."""
    from reckon import ledger

    if ledger.evidence_records_for_plan(project, slug, root):
        return
    expected = f"docs/evidence/archive/{slug}-landed.html"
    raise OpError(
        f"terminal status {status!r} refused for plan {slug!r}: missing "
        f"evidence record {expected}; add "
        f'<meta name="plan-evidence-for" content="{slug}">'
    )


def _require_transition_verdict(
    state: dict[str, Any], transition: str, *, decision: str | None = None
) -> None:
    """Refuse a gated transition while its outcome remains unrecorded."""
    from reckon._schema import pending_transition_gates

    for gate in pending_transition_gates(state.get("gates") or [], transition):
        if decision is not None and gate.get("decision") != decision:
            continue
        raise OpError(
            f"gate {gate.get('id')!r} awaiting verdict from "
            f"{gate.get('gating_plan')!r}: {transition} refused"
        )


def _validate_decision_transitions(working: dict, previous: dict) -> None:
    """Apply the same decision gate to locks and direct state replacements."""
    decisions = working.get("decisions") or {}
    before = previous.get("decisions") or {}
    for key, decision in decisions.items():
        choice = decision.get("choice")
        if choice and choice != (before.get(key) or {}).get("choice"):
            _require_transition_verdict(working, "decision-lockable", decision=key)


def _validate_continuation(working: dict, ops: list[dict]) -> None:
    """Refuse a plan landing that names neither a next step nor an end.

    A batch resolving a followup or setting a terminal status must preserve an
    open continuation or explicitly record that the chain closes. Otherwise a
    landing would discard the next action without leaving a durable outcome.
    """
    resolved = [
        op
        for op in ops
        if op.get("op") == "resolve" and op.get("target") == "followups"
    ]
    landed = any(
        op.get("op") == "set"
        and op.get("path") == "status"
        and str(op.get("value", "")).lower() in _LANDED_STATUSES
        for op in ops
    )
    if not resolved and not landed:
        return
    if continuation_present(working):
        return
    if any(_chain_closed(op.get("outcome")) for op in resolved):
        return
    raise OpError(CONTINUATION_REQUIRED)


def apply_ops(working: dict, ops: list[dict], is_index: bool) -> list[str]:
    """Apply ``ops`` IN ORDER to the working DICT in place.

    ``working`` is the read_state dict (plan) or the index ``data`` sub-object.
    Returns accumulated non-fatal warnings (e.g. double-active-sprint).
    Raises :class:`OpError` on any structurally invalid op — the caller then
    rejects WITHOUT writing. Pure: never touches disk or version fields.
    """
    if not isinstance(ops, list):
        raise OpError("ops must be a list")
    from copy import deepcopy

    previous_decisions = deepcopy(working.get("decisions") or {})
    warnings: list[str] = []
    _begin_write_effects(working)
    for n, op in enumerate(ops):
        if not isinstance(op, dict):
            raise OpError(f"op #{n} is not an object")
        verb = op.get("op")
        handler = _OP_DISPATCH.get(verb)
        if handler is None:
            raise OpError(f"op #{n}: unknown verb {verb!r}")
        handler(working, op, is_index, warnings)
    if not is_index and str(working.get("type", "plan") or "plan") == "plan":
        if any(
            op.get("op") == "set"
            and op.get("path") == "status"
            and str(op.get("value") or "").strip().lower() in TERMINAL_STATUSES
            for op in ops
        ):
            _require_transition_verdict(working, "plan-terminal")
        _validate_decision_transitions(working, {"decisions": previous_decisions})
        _validate_continuation(working, ops)
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
