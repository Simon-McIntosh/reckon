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

import fcntl
import hashlib
import json
import os
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, date, datetime
from pathlib import Path
from typing import IO, Any, TextIO

from reckon._schema import is_section_identity
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


if "VersionConflict" not in globals():
    # The class object stays the same across a reload of this module: callers
    # bind it by name and match it in ``except`` clauses, so a reload that
    # minted a fresh class would leave their handlers missing the conflict
    # raised below.
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


def cache_root(kind: str, override: str | Path | None = None) -> Path:
    """Resolve a cache kind's directory without creating it.

    A caller override wins over the kind's environment variable. The table
    keeps each kind's leaf and home precedence together; only an explicitly
    configured reckon home participates, read through the config-home owner.
    """
    kinds = {
        "velocity": ("RECKON_VELOCITY_CACHE", "velocity", "after"),
        "clones": ("RECKON_CLONE_CACHE", "clones", "after"),
        # Child stderr for a live session host: shared-home like the other
        # fleet caches, with no isolation home of its own.
        "session-host": ("RECKON_SESSION_HOST_CACHE", "session-host", "after"),
        # An isolated configuration must not read or write the live user cache.
        "pick-input": ("RECKON_PICK_CACHE", "", "before"),
        "run-time-profile": (
            "RECKON_RUN_TIME_PROFILE_CACHE",
            "run-time-profile",
            "before",
        ),
        # Client assets are shared independently of the configuration home.
        "client": ("RECKON_CLIENT_CACHE", "client", None),
    }
    if kind not in kinds:
        raise ValueError(f"unknown cache kind: {kind!r}")
    variable, leaf, home_order = kinds[kind]
    if override is not None:
        return Path(override)
    configured = os.environ.get(variable)
    if configured:
        root = Path(configured).expanduser()
        return root.resolve() if home_order is None else root
    has_reckon_home = home_order is not None and bool(os.environ.get("RECKON_HOME"))
    if home_order == "before" and has_reckon_home:
        return _config_home() / "cache" / leaf
    cache_home = os.environ.get("XDG_CACHE_HOME")
    # An explicitly empty client cache home denotes the current directory.
    if cache_home or (home_order is None and cache_home is not None):
        return Path(cache_home) / "reckon" / leaf
    if has_reckon_home:
        return _config_home() / "cache" / leaf
    return Path.home() / ".cache" / "reckon" / leaf


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

        def render(handle: TextIO) -> None:
            json.dump(envelope, handle, indent=2)
            handle.write("\n")

        write_atomically(path, render, mode=0o600)
        return new_data["_version"]


def _fsync_directory(directory: Path) -> None:
    """Flush a directory entry, so a rename into it survives a crash."""
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_sibling_temporary(
    path: Path, create_mode: int, exact_mode: int | None, *, binary: bool = False
) -> tuple[IO[Any], Path]:
    """Open a fresh, uniquely named sibling of ``path`` at ``create_mode``.

    The creation mode is handed to the open rather than applied afterwards, so a
    caller that must hold its file private is private from the first byte rather
    than only after a chmod. ``O_EXCL`` refuses a name that already exists rather
    than reusing a sibling another writer is mid-way through, and the random
    suffix makes that refusal vanishingly rare. ``create_mode`` is filtered by
    the umask exactly as an ordinary file creation would be, which is how a
    caller asking for the process default gets it.

    ``exact_mode`` is the mode the caller asked for, applied with ``os.chmod``
    immediately after creation when it is not ``None``. The open's own mode is
    filtered by the umask, which can only narrow it, so a file the caller asked
    to be ``0o600`` would otherwise land ``0o400`` under a umask such as
    ``0o277``; the chmod restores exactly the requested bits without ever
    widening the file past them at any instant.

    ``binary`` opens the handle in binary mode so a caller whose payload is
    bytes rather than text writes them without a decode or encode round trip.
    """
    for _ in range(64):
        candidate = (
            path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        try:
            descriptor = os.open(
                candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, create_mode
            )
        except FileExistsError:
            continue
        try:
            if exact_mode is not None:
                os.chmod(candidate, exact_mode)
            handle = (
                os.fdopen(descriptor, "wb")
                if binary
                else os.fdopen(descriptor, "w", encoding="utf-8")
            )
        except BaseException:
            with suppress(OSError):
                os.close(descriptor)
            candidate.unlink(missing_ok=True)
            raise
        return handle, candidate
    raise FileExistsError(f"could not create a temporary sibling for ``{path}``")


def write_atomically(
    path: Path,
    render: Callable[[IO[Any]], Any],
    *,
    fsync: bool = True,
    mode: int | None = None,
    fsync_directory: bool = False,
    binary: bool = False,
) -> None:
    """Write through a unique sibling temporary and rename it into place.

    The bytes go to a fresh temporary beside the destination, which is then
    renamed over it. Renaming within one directory is the atomic step, so a
    reader sees the file as it was or as it now is, never a half-written one.
    The temporary is removed on any failure, so a refused write leaves neither a
    partial destination nor a stray sibling. ``fsync`` decides whether the bytes
    are made durable before the rename: a caller whose file must survive a crash
    leaves it on, while a caller rewriting a disposable cache may turn it off.
    ``mode`` is the permission bits the final file must carry. The temporary is
    created at that mode and then set to it exactly with ``os.chmod``, because
    the open's own mode is filtered by the umask and a umask such as ``0o277``
    would otherwise leave a caller asking for ``0o600`` with ``0o400``. ``None``
    takes the process's default creation mode instead, umask and all, which is
    what an ordinary whole-file write would have left. ``fsync_directory``
    flushes the parent directory entry after the rename, which is what makes the
    rename itself durable across a crash. ``binary`` opens the temporary in
    binary mode and hands the render callback a binary handle, so a caller whose
    payload is bytes writes them without a decode or encode round trip.

    This is the one place a file is published through a sibling temporary: every
    writer that needs that guarantee renders through this callback rather than
    keeping a private temporary-and-rename beside it.
    """
    temporary: Path | None = None
    try:
        handle, temporary = _open_sibling_temporary(
            path, 0o666 if mode is None else mode, mode, binary=binary
        )
        with handle:
            render(handle)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        temporary.replace(path)
        if fsync_directory:
            _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_json_atomically(
    path: str | Path,
    payload: Any,
    *,
    fsync: bool = True,
    indent: int | None = 2,
    sort_keys: bool = True,
    mode: int | None = 0o600,
    fsync_directory: bool = False,
    create_parents: bool = True,
    ensure_ascii: bool = True,
) -> Path:
    """Write ``payload`` as JSON so a reader never observes a partial file.

    The serialised bytes go to a unique sibling temporary which is renamed over
    the destination, so the destination is only ever the whole previous file or
    the whole new one. A failure removes the temporary and leaves the
    destination untouched. ``fsync`` is on by default and makes the bytes
    durable before the rename; a caller rewriting a disposable cache may turn it
    off.

    The remaining keyword arguments exist so a caller keeps the behaviour it had
    before it moved onto this writer. ``indent`` and ``sort_keys`` are the
    serialisation, defaulting to this writer's own; ``ensure_ascii`` defaults on
    and escapes non-ASCII characters, so a caller whose file must hold a path or
    string literally passes ``False``. ``mode`` is the temporary's
    permission bits and defaults to ``0o600``, the private mode this writer has
    always produced; a caller whose file was an ordinary whole-file write passes
    ``None`` to take the process's default creation mode instead.
    ``fsync_directory`` flushes the parent directory entry after the rename, and
    ``create_parents`` is turned off by a caller for which materialising a
    missing parent would resurrect a directory something else deliberately
    removed.
    """
    target = Path(path)
    if create_parents:
        target.parent.mkdir(parents=True, exist_ok=True)

    def render(handle: TextIO) -> None:
        json.dump(
            payload,
            handle,
            indent=indent,
            sort_keys=sort_keys,
            ensure_ascii=ensure_ascii,
        )
        handle.write("\n")

    write_atomically(
        target,
        render,
        fsync=fsync,
        mode=mode,
        fsync_directory=fsync_directory,
    )
    return target


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

    return _state_with_diagnostics(project, text, _plan_html.read_state(text), root)


def _state_with_diagnostics(
    project: str,
    text: str,
    state: dict,
    root: str | Path | None = None,
) -> dict:
    """Add diagnostics that depend on more than one plan file."""
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
    from reckon import _plan_html

    state, text = _plan_html.read_state_and_text_file(html_file)
    state = _state_with_diagnostics(project, text, state, root)
    state["todos"] = _plan_html.derive_section_todos(text, state)
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

#: Section collapses are the same kind of write effect as insertions: each
#: replaces one authored h2's body with its landed card. Kept in its own
#: collection so a collapse and an insertion in one batch apply in order and
#: neither is confused for the other.
_SECTION_COLLAPSES: ContextVar[tuple[dict[str, Any], list[dict[str, str]]] | None] = (
    ContextVar("reckon_section_collapses", default=None)
)


def _begin_write_effects(working: dict[str, Any]) -> None:
    """Start empty out-of-band effect collections for one op batch."""
    _SECTION_INSERTIONS.set((working, []))
    _EVIDENCE_APPENDS.set((working, []))
    _SECTION_COLLAPSES.set((working, []))


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


def _queue_section_collapse(working: dict[str, Any], request: dict[str, str]) -> None:
    """Attach one collapse to the current batch without changing plan state."""
    pending = _SECTION_COLLAPSES.get()
    if pending is None or pending[0] is not working:
        raise OpError("collapse_section has no active write-effect collection")
    pending[1].append(request)


def _consume_section_collapses(data: dict[str, Any]) -> list[dict[str, str]]:
    """Return collapses produced by this exact state object, then clear."""
    pending = _SECTION_COLLAPSES.get()
    if pending is None or pending[0] is not data:
        return []
    _SECTION_COLLAPSES.set(None)
    return list(pending[1])


def _insert_authored_section(html_text: str, request: dict[str, str]) -> str:
    """Insert an h2 before structured plan state, or at the end of main prose."""
    from html import escape

    from bs4 import BeautifulSoup

    from reckon import _plan_html

    section_id = request["id"]
    title = request["title"]
    body = request["body"]
    soup = BeautifulSoup(html_text, "html.parser")
    if soup.find(id=section_id) is not None:
        raise OpError(f"section id {section_id!r} already exists")

    body_soup = BeautifulSoup(body, "html.parser")
    if _plan_html.plan_headings(body):
        raise OpError("insert_section body must not contain another h2")
    if body_soup.select_one(f"section[{_plan_html.RECKON_ATTRIBUTE}]") is not None:
        raise OpError("insert_section body must not contain structured plan state")
    if body_soup.select_one('meta[name^="plan-"]') is not None:
        raise OpError("insert_section body must not contain plan metadata")

    boundary = None
    for start, end in _plan_html.structured_section_spans(html_text):
        element = BeautifulSoup(html_text[start:end], "html.parser").find(True)
        if element is not None and _plan_html.machinery_kind(element.attrs) not in {
            None,
            "section",
        }:
            boundary = start
            break
    if boundary is None:
        closing = re.search(r"</main\s*>", html_text, re.IGNORECASE)
        boundary = closing.start() if closing else None
    if boundary is None:
        raise OpError(
            "insert_section requires a structured-state region or main element"
        )

    line_start = html_text.rfind("\n", 0, boundary) + 1
    indentation = html_text[line_start:boundary]
    if indentation.strip():
        indentation = ""
    fragment = f'<h2 id="{escape(section_id, quote=True)}">{escape(title)}</h2>\n'
    if body.strip():
        fragment += body.strip() + "\n"
    fragment += "\n" + indentation
    return html_text[:boundary] + fragment + html_text[boundary:]


def _landed_card_html(open_tag: str, inner_html: str, request: dict[str, str]) -> str:
    """The landed card: the shipped badge, the summary and the evidence link.

    The heading keeps its own open tag — and therefore its id — and is carried
    inside the card's ``<header>``. That header's direct parent is a
    ``<section class="section-landed">``, which is the shape the document
    structure audit exempts as summary-card chrome rather than a second shell
    header.
    """
    from html import escape

    from reckon import _plan_html

    anchor = escape(request["evidence_anchor"], quote=True)
    return (
        f'<section class="{_plan_html.LANDED_SECTION_CLASS}">\n'
        f'  <header><span class="badge badge-shipped">&#10003; landed '
        f"{datetime.now(UTC).date().isoformat()}</span>\n"
        f"    {open_tag}{inner_html}</h2></header>\n"
        f'  <p class="landed-summary">{request["summary"]} '
        f'<a href="{anchor}">full record</a></p>\n'
        "</section>"
    )


def _collapse_authored_section(html_text: str, request: dict[str, str]) -> str:
    """Replace one section's rendered extent with its landed card.

    An authored section runs from its h2 to the next heading, landed card or
    structured-state region, or ``</main>``. A section whose id sits on a
    wrapping ``<section>`` element runs from that wrapper's opening tag to its
    matching close, and the card keeps the identity by carrying it on the
    heading. A section already rendered as a landed card runs from that card's
    opening tag to its matching close, so a repeat collapse refreshes the card
    in place: the heading and its id are kept and there is exactly one card and
    one closing tag.
    """
    from reckon._plan_html import plan_headings, section_record_id

    wanted = section_record_id(request["section"])
    heading = next(
        (
            item
            for item in plan_headings(html_text)
            if item.level == 2 and item.identity == wanted and not item.machinery
        ),
        None,
    )
    if heading is None:
        raise OpError(
            f"collapse_section: no section with id {request['section']!r} in the plan"
        )
    if heading.error:
        raise OpError(f"collapse_section: {heading.error}")
    open_tag = heading.opening_html
    inner_html = html_text[slice(*heading.inner_span)]
    extent_start, extent_end = heading.span
    replaced = html_text[extent_start:extent_end]
    trailing = re.search(r"\s*\Z", replaced)
    card = _landed_card_html(open_tag, inner_html, request)
    return (
        html_text[:extent_start]
        + card
        + (trailing.group() if trailing is not None else "")
        + html_text[extent_end:]
    )


def _evidence_record_path(docs_dir: Path, plan_slug: str) -> Path:
    """The cumulative landing record one plan's evidence appends land in."""
    return docs_dir / "evidence" / "archive" / f"{plan_slug}-landed.html"


_EVIDENCE_FRAGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _resolve_evidence_href(
    anchor: str,
    *,
    project: str,
    slug: str,
    docs_dir: Path | None,
    evidence_appends: list[dict[str, str]],
) -> str:
    """The href a landed card links its full record through.

    An anchor carrying a path or a scheme is the caller's href and is kept as
    written. A bare anchor names a section of this plan's landing record, which
    the surface serves at ``/<project>/evidence/archive/<plan>-landed.html``, so
    it is linked there with the anchor as its fragment. Left bare, the surface
    would fetch ``/<project>/<anchor>.html`` and the link would lead nowhere.

    A bare anchor must name a section the record holds once this batch lands:
    one an ``append_evidence`` in the same batch writes, or one already in the
    record. Anything else would be a dead link, so it is refused at write time
    rather than discovered by a reader.
    """
    if "/" in anchor or ":" in anchor:
        return anchor
    fragment = anchor.removeprefix("#")
    if not _EVIDENCE_FRAGMENT.fullmatch(fragment):
        raise OpError(
            f"collapse_section evidence_anchor {anchor!r} is neither an href nor "
            "a landing-record anchor"
        )
    appended = any(
        request.get("plan") == slug and request.get("anchor") == fragment
        for request in evidence_appends
    )
    if not appended:
        record = _evidence_record_path(docs_dir, slug) if docs_dir else None
        held = (
            record is not None
            and record.is_file()
            and bool(
                re.search(
                    rf"""\bid=["']{re.escape(fragment)}["']""",
                    record.read_text(encoding="utf-8"),
                )
            )
        )
        if not held:
            raise OpError(
                f"collapse_section evidence_anchor {fragment!r} names no section "
                f"of the landing record docs/evidence/archive/{slug}-landed.html; "
                "append it with append_evidence in the same batch, or pass a "
                "project-absolute href"
            )
    return f"/{project}/evidence/archive/{slug}-landed.html#{fragment}"


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

    from reckon import _plan_html

    body = request["body"]
    body_soup = BeautifulSoup(body, "html.parser")
    if _plan_html.plan_headings(body):
        raise OpError("append_evidence body must not contain another h2")
    if body_soup.select_one(f"section[{_plan_html.RECKON_ATTRIBUTE}]") is not None:
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
    """Append each request to its own record, all records or none.

    Requests are grouped by record path and applied in order, each to the text
    the previous request produced, so a batch naming one record twice appends
    twice and the duplicate-anchor refusal sees the anchors an earlier request
    added. Every named record's lock is held while the whole batch is validated
    and replaced, so a refusal leaves every record as it was and a concurrent
    writer's append interleaves with this batch instead of being overwritten by
    it. The locks are taken in sorted path order so two overlapping batches
    cannot deadlock.
    """
    grouped: dict[Path, list[dict[str, str]]] = {}
    for request in requests:
        grouped.setdefault(_evidence_record_path(docs_dir, request["plan"]), []).append(
            request
        )

    planned: list[tuple[Path, str]] = []
    written: list[Path] = []
    with ExitStack() as locks:
        for path in sorted(grouped, key=str):
            locks.enter_context(_serialized_path_lock(path, "evidence"))
        for path, batch in grouped.items():
            plan_slug = batch[0]["plan"]
            if path.is_file():
                current = path.read_text(encoding="utf-8", errors="replace")
            else:
                current = _landed_record_shell(
                    project,
                    plan_slug,
                    _evidence_plan_title(project, plan_slug, root),
                )
            for request in batch:
                current = _append_evidence_to_text(current, request)
            planned.append((path, current))
        for path, new_text in planned:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_atomically(path, lambda handle, text=new_text: handle.write(text))
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
            project,
            slug,
            data,
            expected_version,
            root,
            artifact_type,
            retire_preimages,
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
    section_collapses = _consume_section_collapses(data)

    if expected_version != cur_version:
        merged_comments = _comment_append_onto_current(data, cur_state)
        if merged_comments is None:
            raise VersionConflict(expected_version, cur_version, cur_state)
        data = {**dict(data), "comments": merged_comments}

    new_data = dict(data)
    # A read may carry the derived count and outcomes. Preserve only the
    # source attribute through a state write; no caller authors the count.
    if isinstance(new_data.get("sections"), list):
        source_attempts = {
            str(row["id"]): row["attempts"] for row in cur_state.get("sections", [])
        }
        new_data["sections"] = [
            {
                **{
                    key: value
                    for key, value in row.items()
                    if key != "attempt_outcomes"
                },
                "attempts": source_attempts.get(str(row.get("id") or ""), 0),
            }
            for row in new_data["sections"]
        ]
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
        for request in section_collapses:
            linked = {
                **request,
                "evidence_anchor": _resolve_evidence_href(
                    request["evidence_anchor"],
                    project=project,
                    slug=slug,
                    docs_dir=docs_dir,
                    evidence_appends=evidence_appends,
                ),
            }
            source_text = _collapse_authored_section(source_text, linked)
    except ValueError as exc:
        raise OpError(str(exc)) from exc
    if evidence_appends:
        if docs_dir is None:
            raise OpError(f"append_evidence: no docs dir for project {project!r}")
        _apply_evidence_appends(docs_dir, project, evidence_appends, root)
    authored_text_changed = source_text != text
    if state_type == "plan":
        _require_heading_for_section_records(source_text, new_data.get("sections"))
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

    write_atomically(html_file, lambda handle: handle.write(new_text))
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
            f"{selector_name} changes structured plan state; use "
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


def _carries_authored_prose(item: dict) -> bool:
    """Whether an append item writes a heading and body, or only a record.

    ``body`` is the discriminator: an item carrying one is authored work
    entering the file, and one that omits it is the typed record a reader
    already holds for a section whose heading is in the file.
    """
    return "body" in item


def _require_heading_for_section_records(html_text: str, sections: Any) -> None:
    """Refuse a typed record whose id has no authored h2 to attach to.

    A record rides with a ``section`` heading: the write regenerates the record
    span beside it and leaves the heading's text and the prose around it
    byte-identical, so an id with no heading has nothing to attach to. The
    refusal names the id, because the writer's next move — authoring the
    heading and prose — is a different call from the one it attempted.
    """
    from reckon import _plan_html

    if not isinstance(sections, list):
        return
    heading_ids = {
        heading.own_id
        for heading in _plan_html.plan_headings(html_text)
        if heading.level == 2 and heading.own_id is not None
    }
    missing = sorted(
        str(record.get("id"))
        for record in sections
        if isinstance(record, dict) and str(record.get("id")) not in heading_ids
    )
    if missing:
        raise OpError(
            _section_contract_refusal(
                f"no authored h2 heading with id {missing[0]!r} to attach a section "
                "record to (a record rides with an existing section heading; supply "
                "'title' and 'body' in the same append to write the heading and "
                "prose as well)"
            )
        )


def _require_new_section_contracts(before_html: str, after_html: str) -> None:
    """Require records for newly introduced numbered plan headings only."""
    from reckon import _plan_html

    old_ids = {
        heading.own_id
        for heading in _plan_html.plan_headings(before_html)
        if heading.level == 2 and heading.own_id is not None
    }
    added = {
        heading.own_id
        for heading in _plan_html.plan_headings(after_html)
        if heading.level == 2
        and re.fullmatch(rf"s{_plan_html.SECTION_NUMBER_PATTERN}", heading.own_id or "")
        and heading.own_id not in old_ids
    }
    if not added:
        return
    try:
        records = _plan_html.read_state(after_html).get("sections", [])
    except ValueError as exc:
        raise ValueError(_section_contract_refusal(str(exc))) from exc
    record_ids = {record["id"] for record in records}
    missing = {
        sid for sid in added if _plan_html.section_record_id(sid) not in record_ids
    }
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

    return _replace_plan_text(
        project,
        slug,
        [(old_html, new_html)],
        expected_version,
        root,
        artifact_type,
        indexed=False,
    )


def replace_plan_text_batch(
    project: str,
    slug: str,
    replacements: Sequence[Mapping[str, str]],
    expected_version: int,
    root: str | Path | None = None,
    artifact_type: str | None = None,
) -> tuple[int, Path]:
    """Apply an ordered batch of authored HTML replacements as one versioned write.

    Each entry carries ``old_html`` and ``new_html``, exactly as the single
    replacement does. The pairs are applied to a working copy in order, so a
    later pair sees the text an earlier one wrote, and the file is written once
    with one version advance. Every pair keeps :func:`replace_plan_text`'s
    refusals — a non-unique match, an overlap with a ``section[data-reckon]``
    region, or a change to structured plan state — and a refusal names the
    pair's index in ``replacements`` while leaving the file and version
    untouched.
    """

    if not replacements:
        raise ValueError(
            "replacements must be a non-empty list of {old_html, new_html} pairs"
        )
    pairs: list[tuple[str, str]] = []
    for index, entry in enumerate(replacements):
        selector = f"replacements[{index}]"
        if not isinstance(entry, Mapping):
            # ValueError, not TypeError: text mode's refusal channel is
            # ValueError, which the MCP handler maps to text_edit_error.
            raise ValueError(  # noqa: TRY004
                f"{selector} must be an object with old_html and new_html"
            )
        unknown = sorted(set(entry) - {"old_html", "new_html"})
        if unknown:
            raise ValueError(f"{selector} has unsupported fields: {unknown}")
        missing = [key for key in ("old_html", "new_html") if key not in entry]
        if missing:
            raise ValueError(f"{selector} is missing {missing[0]!r}")
        old_html = entry["old_html"]
        new_html = entry["new_html"]
        if not isinstance(old_html, str) or not isinstance(new_html, str):
            # ValueError for the same reason: it is the refusal the MCP
            # handler maps to text_edit_error.
            raise ValueError(  # noqa: TRY004
                f"{selector} old_html and new_html must be strings"
            )
        pairs.append((old_html, new_html))
    return _replace_plan_text(
        project,
        slug,
        pairs,
        expected_version,
        root,
        artifact_type,
        indexed=True,
    )


def _replace_plan_text(
    project: str,
    slug: str,
    pairs: Sequence[tuple[str, str]],
    expected_version: int,
    root: str | Path | None,
    artifact_type: str | None,
    *,
    indexed: bool,
) -> tuple[int, Path]:
    from reckon import _plan_html
    from reckon._schema import TYPE_ENUM
    from reckon.resources import canonical_type, resolve_resource

    checked: list[tuple[str, str, str]] = []
    for index, (old_html, new_html) in enumerate(pairs):
        selector = f"replacements[{index}].old_html" if indexed else "old_html"
        if not old_html:
            raise ValueError(f"{selector} must be non-empty")
        if old_html == new_html:
            raise ValueError(f"{selector} and new_html are identical")
        checked.append((old_html, new_html, selector))

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

        replaced = text
        for old_html, new_html, selector in checked:
            replaced = _replace_authored_html(
                replaced,
                old_html,
                new_html,
                selector_name=selector,
            )

        stamped_state = dict(current_state)
        stamped_state["modified"] = date.today().isoformat()
        stamped_state["version"] = current_version + 1
        rendered = _plan_html.write_state(replaced, stamped_state)
        write_atomically(html_file, lambda handle: handle.write(rendered))
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
        if _followup_is_open(item):
            _refuse_hiding_followup(cur_data, item, project=project)
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

    A collision — an append whose id already addresses an entry — also carries
    ``existing_item``, a one-phrase description of the entry already stored, so
    the writer can tell which item it collided with without re-reading the plan.
    """

    def __init__(self, message: str, *, existing_item: str | None = None) -> None:
        super().__init__(message)
        self.existing_item = existing_item


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


def _move_section_record(working: dict, section_id: str, status: Any) -> None:
    """Move the typed record a declaration must agree with.

    A declaration and the record beside it are one statement copied into the
    map and into the markup, so a write that moves one and not the other lands
    a plan whose records contradict its declaration map — and the parse on the
    way back in refuses the file the write just produced. A declaration with no
    record yet (a section whose record is not authored) has nothing to move.
    """
    sections = working.get("sections")
    if isinstance(sections, list):
        for section in sections:
            if isinstance(section, dict) and section.get("id") == section_id:
                section["status"] = status
                break


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


def _shorten(text: str, limit: int = 72) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _describe_entry(entry: Any) -> str:
    """One short phrase naming an existing entry inside a duplicate refusal.

    A section's authored heading and prose live in the plan file rather than in
    its typed record, so a record with no narrative field is described by the
    scalar fields it does carry (its id and status).
    """
    if isinstance(entry, dict):
        for field in ("title", "question", "measure", "body", "description", "who"):
            value = entry.get(field)
            if isinstance(value, str) and value.strip():
                return _shorten(f"{field} {value.strip()!r}")
        scalars = [
            f"{key}={value!r}"
            for key, value in entry.items()
            if isinstance(value, (str, int, float, bool)) and value != ""
        ]
        if scalars:
            return _shorten(", ".join(scalars))
    return _shorten(str(entry))


def _refuse_duplicate_id(entries: list, collection: str, ident: str) -> None:
    """Refuse an append whose id already addresses an existing entry."""
    hit = _find_by_id(entries, ident)
    if hit is not None:
        _, existing = hit
        raise OpError(
            f"{_collection_label(collection)} {ident!r} already exists",
            existing_item=_describe_entry(existing),
        )


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
        if not is_section_identity(section_id):
            raise OpError(
                "section declaration id must match "
                f"a safe section identity; got {section_id!r}"
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
        _move_section_record(working, section_id, value)
        return
    if head == "section_declarations" and len(parts) == 1:
        # The whole map writes the same statement the dotted form writes one
        # entry at a time, so every entry carries its record with it. Assigning
        # the map alone left the records contradicting it, and the plan refused
        # the file that write produced on the way back in.
        if isinstance(value, dict):
            for declared_id, declared_status in value.items():
                _move_section_record(working, declared_id, declared_status)
        working[head] = value
        return
    if head == "decisions" and len(parts) >= 3:
        decisions = working.setdefault("decisions", {})
        if not isinstance(decisions, dict):
            raise OpError("plan has no decisions map")
        key = parts[1]
        if len(parts) == 3 and parts[2] == "sections":
            value = _normalise_decision_scope(working, key, value)
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
        from reckon import _plan_html

        if (
            _plan_html.derive_impl_from_sections(
                working.get("sections"), working.get("section_declarations")
            )
            is not None
        ):
            raise OpError(
                "impl is computed from the section records on this plan: the sum of "
                "effort over sections whose status is done, over that sum plus the "
                "predicted effort of every remaining implementable section. It is "
                "not authorable — move a section's status instead."
            )
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


_FIRST_SECTION_RECORD_WARNING = (
    "first section record: impl becomes computed once the records cover "
    "every declared section"
)


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
        first_record = not sections
        # An item carrying no body attaches the typed record to a heading the
        # file already holds and writes nothing else; one carrying a body
        # authors the heading and prose too, which is the create route.
        if _carries_authored_prose(item):
            try:
                _queue_authored_section(working, item)
            except OpError as exc:
                raise OpError(_section_contract_refusal(str(exc))) from exc
        sections.append(record)
        working.setdefault("section_declarations", {})[record["id"]] = record["status"]
        if first_record:
            warnings.append(_FIRST_SECTION_RECORD_WARNING)
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
        if _followup_is_open(fu):
            _refuse_hiding_followup(
                working, fu, project=str(working.get("project") or "")
            )
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
            raise OpError(
                f"decision {key!r} already exists",
                existing_item=_describe_entry(decisions[key]),
            )
        stored = _decision_as_stored(item)
        if stored.get("sections"):
            stored["sections"] = _normalise_decision_scope(
                working, key, stored.get("sections")
            )
        decisions[key] = stored
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


def _normalise_decision_scope(working: dict, key: str, sections: Any) -> Any:
    """Refuse a decision scope naming a section this plan does not declare.

    A scoped decision is a wait on the sections it names, so a section that
    does not exist is a wait that can never be satisfied — the decision would
    read as a frozen anchor while holding nothing. The refusal names the
    offending section and the plan's own declared identities, so a typo or a
    later rename is answered with the set it should have named. The declared
    set and the refusal codes come from the schema module, so the write
    boundary and the roadmap reader judge one scope by one rule.
    """
    from reckon._schema import decision_section_refusals, declared_section_identities

    value = sections
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    declared = declared_section_identities(working)
    refusals = decision_section_refusals({key: value}, declared)
    if refusals:
        declared_list = ", ".join(sorted(declared)) or "(none)"
        raise OpError(
            f"decision {key!r}: {refusals[0]['message']} — "
            f"declared sections: {declared_list}"
        )
    return value


def _apply_accept(working: dict, op: dict, is_index: bool, warnings: list[str]) -> None:
    """Accept a decision's stored recommendation as its choice, in one action.

    A recommendation is a proposal, so the choice stays empty until someone
    accepts it; this op is that acceptance. An absent recommendation is
    refused rather than locked empty, and a gated decision is held to the same
    transition verdict a direct lock is.
    """
    if is_index:
        raise OpError("accept op is plan-only")
    key = op.get("key")
    if not key:
        raise OpError("accept op requires a 'key'")
    decisions = working.setdefault("decisions", {})
    if not isinstance(decisions, dict):
        raise OpError("plan has no decisions map")
    existing = decisions.get(key)
    if not isinstance(existing, dict):
        raise OpError(f"decision {key!r} does not exist")
    recommendation = str(existing.get("recommended") or "").strip()
    if not recommendation:
        raise OpError(f"decision {key!r} carries no recommendation to accept")
    _require_transition_verdict(working, "decision-lockable", decision=key)
    decisions[key] = {
        **existing,
        "choice": recommendation,
        "when": _utc_ts(),
        "by": op.get("by", ""),
    }


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
    hit = _find_by_id(gates, ident)
    if hit is not None:
        raise OpError(
            f"gate {ident!r} already exists",
            existing_item=_describe_entry(hit[1]),
        )
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
    if not isinstance(section_id, str) or not is_section_identity(section_id):
        raise OpError("insert_section op requires a safe section identity in 'id'")
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
    sections = working.setdefault("sections", [])
    first_record = not sections
    sections.append(record)
    working.setdefault("section_declarations", {})[record["id"]] = record["status"]
    if first_record:
        warnings.append(_FIRST_SECTION_RECORD_WARNING)


def _apply_collapse_section(
    working: dict, op: dict, is_index: bool, warnings: list[str]
) -> None:
    """Collapse one landed section to its card and declare it done.

    The HTML replacement and the declaration change ride one versioned write:
    the card is queued as a write effect and the section's declaration is set on
    the same working object, so a caller cannot observe one without the other.
    """
    if is_index or str(working.get("type", "plan") or "plan") != "plan":
        raise OpError("collapse_section op is plan-only")
    section_id = op.get("section")
    if not isinstance(section_id, str) or not is_section_identity(section_id):
        raise OpError(
            "collapse_section op requires a 'section' id matching "
            "a safe section identity"
        )
    summary = op.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise OpError("collapse_section op requires a non-empty string 'summary'")
    anchor = op.get("evidence_anchor")
    if not isinstance(anchor, str) or not anchor.strip():
        raise OpError(
            "collapse_section op requires a non-empty string 'evidence_anchor'"
        )
    _queue_section_collapse(
        working,
        {
            "section": section_id,
            "summary": summary.strip(),
            "evidence_anchor": anchor.strip(),
        },
    )
    working.setdefault("section_declarations", {})[section_id] = "done"
    sections = working.get("sections")
    if isinstance(sections, list):
        for section in sections:
            if isinstance(section, dict) and section.get("id") == section_id:
                section["status"] = "done"
                break


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
    "accept": _apply_accept,
    "gate": _apply_gate,
    "pass": _apply_gate_verdict,
    "fail": _apply_gate_verdict,
    "retire_prose": _apply_retire_prose,
    "insert_section": _apply_insert_section,
    "append_evidence": _apply_append_evidence,
    "collapse_section": _apply_collapse_section,
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


def _refuse_hiding_followup(
    plan: dict[str, Any], followup: dict[str, Any], *, project: str = ""
) -> None:
    """Refuse one open followup whose invocation hides work on its own plan.

    One definition for the ops writer, the append tool and the HTTP patch
    writer, so the three cannot disagree about what a followup does not name:
    work the roadmap can dispatch on this plan.
    """
    from reckon.followup_pointers import classify_followup

    verdict = classify_followup(
        plan,
        followup,
        project=project,
        declarations=plan.get("section_declarations"),
    )
    if not verdict.hides_work:
        return
    ident = str(followup.get("id") or "")
    raise OpError(
        f"followup {ident!r} hides work ({verdict.reason}): its invocation "
        "names no work the roadmap can dispatch — add the work as a section "
        "of this plan, or create a new plan when this one is complete and "
        "point the followup at it; a step that needs authority is recorded "
        "as an open decision instead"
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


def _followups_before_the_patch(state: dict[str, Any]) -> list[dict[str, Any]]:
    """The plan's stored followups, read before the patch is written.

    A patch carries the whole followups list, so telling an appended followup
    from one already in the plan needs the state that preceded the write.
    """
    project = str(state.get("project") or "")
    slug = str(state.get("slug") or "")
    if not project or not slug:
        return []
    try:
        data, _version = read_plan(project, slug)
    except (OSError, CorruptEnvelopeError, ValueError):
        return []
    return [f for f in (data.get("followups") or []) if isinstance(f, dict)]


def _refuse_appended_hiding_followups(
    state: dict[str, Any], patch: dict[str, Any]
) -> None:
    """Refuse the open followups a patch introduces that hide work."""
    if "followups" not in patch:
        return
    stored = {str(f.get("id") or "") for f in _followups_before_the_patch(state)}
    project = str(state.get("project") or "")
    for followup in state.get("followups") or []:
        if not isinstance(followup, dict):
            continue
        if str(followup.get("id") or "") in stored:
            continue
        if not _followup_is_open(followup):
            continue
        _refuse_hiding_followup(state, followup, project=project)


def validate_landing_patch(state: dict[str, Any], patch: dict[str, Any]) -> None:
    """Refuse a merge patch that lands a plan without naming a continuation.

    Deliberately keyed to the *write* rather than to the resulting state. A
    state-level invariant would retroactively lock every plan already recorded
    as shipped without a followup — measured at 155 of 202 across the mounted
    projects — so history stays editable and only a new landing owes an answer.
    """
    if str(state.get("type", "plan") or "plan") != "plan":
        return
    _refuse_appended_hiding_followups(state, patch)
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
