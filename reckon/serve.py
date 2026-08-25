#!/usr/bin/env python3
"""reckon server — host-wide static + state backend for the plan SPA.

Serves multiple project doc roots under stable URL prefixes and provides a
small JSON state store for in-page decision capture.

The on-disk layout under ~/docs-server/ is kept for backward compatibility;
the name "reckon server" describes the process, not the filesystem path.

Mounts are configured in ~/docs-server/mounts.json (default) or via --mounts:
    {
      "imas-ambix": "/home/user/Code/imas-ambix/docs",
      "my-project":  "/home/user/Code/my-project/docs"
    }

State files land in ~/docs-server/state/<project>/<doc>.json so that
agents working anywhere on the filesystem can read and write the same
JSON the browser is interacting with.

Routes:
  GET /                         → home.html (cross-project rollup)
  GET /_shared/<file>           → docs/_shared/<file> in the reckon repo
                                  (falls back to ~/.claude/skills/html-docs/assets/)
  GET /_projects/index.json     → cross-project rollup
  GET /_projects/<file>         → ~/docs-server/<file>
  GET /crew/<project>/finished[/<plan>]
                                → committed completed runs, newest first
  GET /state/<project>/<doc>    → ~/docs-server/state/<project>/<doc>.json
  POST /state/<project>/<doc>   → write the same path (versioned)
  GET /_discover/<project>      → scan docs dir for HTML plan pages (meta tag opt-in)
  GET /<project>/<relpath>      → mount[project]/<relpath>

POST versioned write contract — see reckon/serve.py for full details.
"""

from __future__ import annotations

import html.parser
import hashlib
import json
import logging
import mimetypes
import os
import re
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from reckon import _backends, _plan_html, crew, ledger
from reckon._store import _config_home, _mounts_path, _state_root
from reckon.lifecycle import (
    effective_status,
    unresolved_dependencies,
    unpassed_gate_blockers,
)
from reckon.resources import (
    ROOT_TYPES,
    ResourceCollision,
    resource_map,
    resolve_resource,
    resolve_route,
)

HOME = Path.home()
LOGGER = logging.getLogger(__name__)

# ── Configurable paths (set via main() args or env vars) ──────────────────

_MOUNTS_FILE: Path | None = None
_STATE_ROOT: Path | None = None
_HOME_HTML: Path | None = None
_SHARED_ROOT: Path | None = None


def _resolve_paths(mounts_file: Path | None = None) -> None:
    global _MOUNTS_FILE, _STATE_ROOT, _HOME_HTML, _SHARED_ROOT
    # Config home: RECKON_HOME env → ~/.config/reckon (if present) → ~/docs-server.
    config_root = _config_home()
    _MOUNTS_FILE = mounts_file or _mounts_path()
    _STATE_ROOT = _state_root()
    # Prefer the home page bundled in the reckon docs/ directory; fall back to
    # the config-home home.html from dotfiles if it doesn't exist yet.
    reckon_home = Path(__file__).parent.parent / "docs" / "home.html"
    legacy_home = config_root / "home.html"
    _HOME_HTML = reckon_home if reckon_home.is_file() else legacy_home
    # Shared assets: prefer reckon repo's own docs/_shared, fall back to dotfiles.
    repo_shared = Path(__file__).parent.parent / "docs" / "_shared"
    dotfiles_shared = HOME / ".claude" / "skills" / "html-docs" / "assets"
    _SHARED_ROOT = repo_shared if repo_shared.is_dir() else dotfiles_shared


SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_POST_BYTES = 1_000_000
CREW_LOG_TAIL_BYTES = 64 * 1024

# Fields that are updated on every write and therefore excluded from the
# content-equality check in _content_equal.
_STAMP_FIELDS = frozenset(["version", "modified"])


def _content_equal(patched: dict, reparsed: dict, *, cur_state: dict) -> bool:
    """Return True if `reparsed` (parsed from newly rendered HTML) is semantically
    equal to `cur_state` (parsed from the current on-disk file), ignoring the
    version and modified stamp fields.

    This is the idempotency guard: when the patch carries no real content change
    the only differences between cur_state and reparsed would be version/modified,
    so we can safely skip the disk write and return the current version.
    """

    def _strip(d: dict) -> dict:
        return {k: v for k, v in d.items() if k not in _STAMP_FIELDS}

    return _strip(reparsed) == _strip(cur_state)


def load_mounts() -> dict[str, Path]:
    if _MOUNTS_FILE is None:
        _resolve_paths()
    if not _MOUNTS_FILE or not _MOUNTS_FILE.exists():
        return {}
    raw = json.loads(_MOUNTS_FILE.read_text())
    out: dict[str, Path] = {}
    for name, path in raw.items():
        if not SAFE_NAME.match(name):
            continue
        p = Path(path).expanduser().resolve()
        if p.is_dir():
            out[name] = p
    return out


def _read_log_tail(path: Path, *, byte_limit: int = CREW_LOG_TAIL_BYTES) -> list[str]:
    """Read at most ``byte_limit`` bytes from the end of an event stream."""
    try:
        size = path.stat().st_size
        start = max(0, size - byte_limit)
        with path.open("rb") as handle:
            handle.seek(start)
            payload = handle.read(byte_limit)
    except OSError:
        return []
    if start:
        _partial, separator, payload = payload.partition(b"\n")
        if not separator:
            return []
    return payload.decode("utf-8", errors="replace").splitlines()


def _stream_is_terminal(pointer: dict, lines: list[str]) -> bool:
    """Recognise a terminal event using the recorded backend dialect."""
    dialect = str(pointer.get("dialect") or "")
    argv = pointer.get("argv") or []
    command = dialect or (str(argv[0]) if isinstance(argv, list) and argv else "")
    if not command:
        return False
    try:
        observation = _backends.observe_stream(
            backend_name=str(pointer.get("backend") or dialect),
            backend={"command": command},
            lines=lines,
        )
    except _backends.BackendError:
        return False
    return observation.terminal


def _log_activity(pointer: dict) -> tuple[str | None, float | None, list[str]]:
    """Return the log modification stamp, age in seconds and bounded tail."""
    raw_path = pointer.get("log_path")
    if not raw_path:
        return None, None, []
    path = Path(str(raw_path))
    try:
        if not path.is_file():
            return None, None, []
        modified = path.stat().st_mtime
    except OSError:
        return None, None, []
    stamp = (
        datetime.fromtimestamp(modified, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    age = max(0.0, datetime.now(tz=timezone.utc).timestamp() - modified)
    return stamp, age, _read_log_tail(path)


def _elapsed_since(stamp: object) -> int | None:
    """Return whole elapsed seconds from an ISO timestamp, when available."""
    if not stamp:
        return None
    try:
        started = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(tz=timezone.utc) - started).total_seconds()))


def _crew_plan_details(docs: Path, slug: str) -> tuple[str, float | None]:
    """Read navigation and effort from one referenced plan without discovery."""
    if not slug or not SAFE_NAME.fullmatch(slug):
        return "", None
    candidates = (
        docs / "plans" / f"{slug}.html",
        docs / f"{slug}.html",
        docs / "plans" / "archive" / f"{slug}.html",
    )
    for plan_path in candidates:
        if not plan_path.is_file():
            continue
        try:
            metadata = _plan_html.parse_meta(plan_path)
            return (
                str(metadata.get("sprint") or ""),
                metadata.get("effort_hours"),
            )
        except (OSError, ValueError):
            return "", None
    return "", None


def _crew_rows(mounts: dict[str, Path], project: str | None = None) -> list[dict]:
    """Join mounted live pointers with roster and navigation state."""
    selected = {project} if project else set(mounts)
    pointers = [
        pointer
        for pointer in crew.list_live()
        if str(pointer.get("project") or "") in selected
        and str(pointer.get("project") or "") in mounts
    ]
    referenced_projects = {
        str(pointer.get("project") or "") for pointer in pointers
    }
    roster_by_project: dict[str, dict[str, dict]] = {}
    for name in referenced_projects:
        docs = mounts[name]
        try:
            roster, _version = ledger.load(name, docs.parent)
        except (OSError, ledger.LedgerError):
            roster = {"members": []}
        roster_by_project[name] = {
            str(member.get("id") or ""): member
            for member in roster.get("members", [])
            if isinstance(member, dict) and member.get("id")
        }

    rows: list[dict] = []
    details_by_plan: dict[tuple[str, str], tuple[str, float | None]] = {}
    for pointer in pointers:
        name = str(pointer.get("project") or "")
        node = pointer.get("node") if isinstance(pointer.get("node"), dict) else {}
        agent = pointer.get("agent") if isinstance(pointer.get("agent"), dict) else {}
        plan = str(node.get("plan") or "")
        member_id = str(pointer.get("member") or "")
        roster_member = roster_by_project.get(name, {}).get(member_id, {})
        last_activity, age, lines = _log_activity(pointer)
        terminal = _stream_is_terminal(pointer, lines)
        phase = (
            "done"
            if terminal
            else "working"
            if age is not None and age <= crew.LOG_STALE_AFTER_SECONDS
            else "idle"
        )
        plan_key = (name, plan)
        if plan_key not in details_by_plan:
            details_by_plan[plan_key] = _crew_plan_details(mounts[name], plan)
        sprint, effort_hours = details_by_plan[plan_key]
        rows.append(
            {
                "run_id": str(pointer.get("run_id") or ""),
                "project": name,
                "member": str(roster_member.get("id") or member_id),
                "role": str(
                    roster_member.get("role")
                    or pointer.get("role")
                    or node.get("role")
                    or ""
                ),
                "plan": plan,
                "section": str(node.get("section") or ""),
                "backend": str(pointer.get("backend") or ""),
                "model": agent.get("model"),
                "effort": agent.get("effort"),
                "effort_hours": effort_hours,
                "elapsed_seconds": _elapsed_since(pointer.get("created_at")),
                "phase": phase,
                "last_activity": last_activity,
                "gate": str(node.get("done_when") or ""),
                "plan_href": f"/{name}/#plan/{plan}" if plan else None,
                "sprint_href": f"/{name}/#sprint/{sprint}" if sprint else None,
            }
        )
    return rows


def _finished_crew_rows(
    mounts: dict[str, Path], project: str, plan: str | None = None
) -> list[dict]:
    """Return one project's committed runs ordered by completion time."""

    records = ledger.runs(project, mounts[project].parent, plan=plan)

    def completion_key(record: dict) -> datetime:
        for field in ("completed_at", "dispatched_at"):
            value = str(record.get(field) or "")
            if not value:
                continue
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        return datetime.min.replace(tzinfo=timezone.utc)

    return sorted(records, key=completion_key, reverse=True)


def render_index_fallback(mounts: dict[str, Path], host: str, port: int) -> bytes:
    rows = []
    for name, path in sorted(mounts.items()):
        rows.append(
            f'<tr><td><a href="/{name}/">{name}</a></td>'
            f"<td><code>{path}</code></td></tr>"
        )
    body = (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<title>reckon server</title>"
        "<style>"
        "body{font:14px/1.5 system-ui,sans-serif;max-width:780px;"
        "margin:3rem auto;padding:0 1rem;color:#222}"
        "h1{font-size:1.4rem;margin-bottom:.2rem}"
        ".meta{color:#666;margin-bottom:2rem}"
        "table{width:100%;border-collapse:collapse}"
        "td{padding:.5rem .75rem;border-bottom:1px solid #eee}"
        "td:first-child{font-weight:600}"
        "code{background:#f5f5f5;padding:.1rem .3rem;border-radius:3px;font-size:.85em}"
        "</style></head><body>"
        "<h1>reckon</h1>"
        f'<div class="meta">{host}:{port} &middot; '
        f"{len(mounts)} project(s) mounted &middot; "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')} &middot; "
        "<em>home.html missing — install reckon to get the rollup view</em>"
        "</div>"
        "<table><thead><tr><th align=left>Project</th>"
        "<th align=left>Path</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        "</body></html>"
    )
    return body.encode()


def collect_projects(mounts: dict[str, Path]) -> dict:
    out: list[dict] = []
    for name, path in sorted(mounts.items()):
        state_file = path / "state" / name / "index.json"
        proj: dict = {"project": name, "path": str(path)}
        if state_file.is_file():
            try:
                envelope = json.loads(state_file.read_text())
                proj["data"] = (
                    envelope.get("data", envelope) if isinstance(envelope, dict) else {}
                )
                if isinstance(envelope, dict) and "updated" in envelope:
                    proj["updated"] = envelope["updated"]
            except (OSError, json.JSONDecodeError) as e:
                proj["error"] = str(e)
                proj["data"] = {}
        else:
            proj["data"] = {}
        out.append(proj)
    return {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "mounts_path": str(_MOUNTS_FILE or _mounts_path()),
        "projects": out,
    }


# ── Plan discovery ────────────────────────────────────────────────────────
#
# Any HTML file under a project's docs dir (outside infra files/dirs) is a
# doc — existence is sufficient. plan-* meta tags and the data-reckon sections
# only enrich the entry; their absence never hides a doc. reckon-type=research
# marks non-actionable input docs. See PLAN-FORMAT.md for the convention.

_PLAN_META_PREFIX = "plan-"
_NON_PLAN_FILES = frozenset(
    [
        "index.html",
        "sprint.html",
        "sprints.html",
        "milestones.html",
        "decisions.html",
        "inventory.html",
        "blockers.html",
        "implementation.html",
        "questions.html",
        "home.html",
        "project.html",
        "plan.html",  # legacy static-site per-plan-detail template (not a plan)
        "README.html",  # generated index/landing page (a prose README belongs in .md)
    ]
)
_NON_PLAN_DIRS = frozenset(
    [
        "_shared",
        "ui",
        "state",
        "assets",
        "images",
        "sprints",
        "milestones",
    ]
)
# Per-stage / archival history (e.g. <plan>-…-landed.html) lives under
# archive/.  Those docs ARE discovered and served — they carry the plan
# system's landed records — but every one is stamped archived so the SPA
# keeps them behind its "Show archived" toggle instead of cluttering the
# live inventory.
_ARCHIVE_DIR = "archive"


class _HeadParser(html.parser.HTMLParser):
    """Extract <meta> tags and <title> from the HTML <head> only."""

    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False
        self._done = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if self._done:
            return
        if tag == "body":
            self._done = True
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            d = dict(attrs)
            name = (d.get("name") or "").lower()
            if name:
                self.meta[name] = d.get("content", "")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("head", "body"):
            self._done = True
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and not self._done:
            self.title += data


def _read_head_meta(path: Path) -> tuple[str, dict[str, str]]:
    """Return (title, {name: content}) from a plan HTML file's <head>."""
    try:
        raw = path.read_bytes()[:8192].decode("utf-8", errors="replace")
        p = _HeadParser()
        p.feed(raw)
        return p.title.strip(), p.meta
    except Exception:
        return "", {}


@dataclass(frozen=True)
class _DiscoveryCacheEntry:
    local_signature: tuple[int, int]
    external_projects: tuple[str, ...]
    external_signatures: tuple[tuple[str, str, tuple[int, int] | None], ...]
    result: dict


@dataclass(frozen=True)
class _GitCreationEntry:
    head: str
    times: dict[str, int]


_DISC_CACHE: dict[tuple[str, str], _DiscoveryCacheEntry] = {}
_GIT_CREATION_CACHE: dict[tuple[str, str], _GitCreationEntry] = {}
_GIT_CREATION_SCHEMA = "reckon.git-creation-map"
_GIT_CREATION_SCHEMA_VERSION = 1


def _git_creation_cache_path(cache_key: tuple[str, str]) -> Path:
    """Return the disposable persisted-map path for one repository docs root."""

    identity = "\0".join(cache_key).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return _config_home() / "cache" / "git-creation" / f"{digest}.json"


def _git_creation_payload(
    cache_key: tuple[str, str], entry: _GitCreationEntry
) -> dict:
    repo, rel_docs = cache_key
    core = {
        "schema": _GIT_CREATION_SCHEMA,
        "version": _GIT_CREATION_SCHEMA_VERSION,
        "repository": repo,
        "docs": rel_docs,
        "head": entry.head,
        "times": entry.times,
    }
    checksum = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {**core, "checksum": checksum}


def _load_git_creation_cache(
    cache_key: tuple[str, str],
) -> _GitCreationEntry | None:
    """Load a complete, current-schema persisted map or decline it entirely."""

    if not (Path(cache_key[0]) / ".git").exists():
        return None
    path = _git_creation_cache_path(cache_key)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Ignoring unreadable Git creation cache %s: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        LOGGER.warning("Ignoring invalid Git creation cache %s", path)
        return None

    times = raw.get("times")
    valid_times = isinstance(times, dict) and all(
        isinstance(name, str)
        and isinstance(timestamp, int)
        and not isinstance(timestamp, bool)
        and timestamp >= 0
        for name, timestamp in times.items()
    )
    valid_identity = (
        raw.get("schema") == _GIT_CREATION_SCHEMA
        and raw.get("version") == _GIT_CREATION_SCHEMA_VERSION
        and raw.get("repository") == cache_key[0]
        and raw.get("docs") == cache_key[1]
        and isinstance(raw.get("head"), str)
        and bool(raw["head"])
    )
    if not valid_times or not valid_identity:
        LOGGER.warning("Ignoring incompatible Git creation cache %s", path)
        return None

    core = {key: raw[key] for key in raw if key != "checksum"}
    checksum = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if raw.get("checksum") != checksum:
        LOGGER.warning("Ignoring corrupt Git creation cache %s", path)
        return None
    return _GitCreationEntry(head=raw["head"], times=dict(times))


def _store_git_creation_cache(
    cache_key: tuple[str, str], entry: _GitCreationEntry
) -> None:
    """Atomically persist one validated creation map without affecting service."""

    if not (Path(cache_key[0]) / ".git").exists():
        return
    path = _git_creation_cache_path(cache_key)
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(
                _git_creation_payload(cache_key, entry),
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(path)
    except OSError as exc:
        LOGGER.warning("Could not persist Git creation cache %s: %s", path, exc)
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _run_git(
    args: list[str], repo_dir: Path, *, operation: str
) -> subprocess.CompletedProcess[str] | None:
    """Run one bounded Git query and make every failure observable."""

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            cwd=repo_dir,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        LOGGER.warning("Git %s timed out after 10 seconds in %s", operation, repo_dir)
        return None
    except OSError as exc:
        LOGGER.warning("Git %s could not start in %s: %s", operation, repo_dir, exc)
        return None
    if result.returncode != 0:
        detail = result.stderr.strip()
        suffix = f": {detail}" if detail else ""
        LOGGER.warning(
            "Git %s failed with exit code %d in %s%s",
            operation,
            result.returncode,
            repo_dir,
            suffix,
        )
        return None
    return result


def _git_head(repo_dir: Path) -> str | None:
    result = _run_git(["git", "rev-parse", "HEAD"], repo_dir, operation="HEAD lookup")
    if result is None:
        return None
    head = result.stdout.strip()
    if not head:
        LOGGER.warning("Git HEAD lookup returned no commit in %s", repo_dir)
        return None
    return head


def _parse_first_committed(output: str) -> dict[str, int]:
    times: dict[str, int] = {}
    timestamp: int | None = None
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("COMMIT "):
            try:
                timestamp = int(line[7:])
            except ValueError:
                timestamp = None
        elif line and timestamp is not None:
            # Git emits newest commits first, so the last add event is the first.
            times[line] = timestamp
    return times


def _git_first_committed(repo_dir: Path, docs_dir: Path) -> dict[str, int]:
    """Return {repo-relative-path: unix_ts} for the first commit of each HTML file."""
    try:
        rel_docs = str(docs_dir.relative_to(repo_dir))
    except ValueError:
        LOGGER.warning("Docs directory %s is outside repository %s", docs_dir, repo_dir)
        return {}

    cache_key = (str(repo_dir.resolve()), rel_docs)
    cached = _GIT_CREATION_CACHE.get(cache_key)
    if cached is None:
        cached = _load_git_creation_cache(cache_key)
        if cached is not None:
            _GIT_CREATION_CACHE[cache_key] = cached
    head = _git_head(repo_dir)
    if head is None:
        return dict(cached.times) if cached else {}
    if cached and cached.head == head:
        return dict(cached.times)

    args = ["git", "log"]
    if cached:
        args.append(f"{cached.head}..{head}")
    args.extend(
        [
            "--diff-filter=A",
            "--format=COMMIT %at",
            "--name-only",
            "--",
            rel_docs,
        ]
    )
    result = _run_git(args, repo_dir, operation="history lookup")
    if result is None:
        return dict(cached.times) if cached else {}

    times = dict(cached.times) if cached else {}
    additions = _parse_first_committed(result.stdout)
    for path, timestamp in additions.items():
        times.setdefault(path, timestamp)
    entry = _GitCreationEntry(head=head, times=times)
    _GIT_CREATION_CACHE[cache_key] = entry
    _store_git_creation_cache(cache_key, entry)
    return dict(times)


def _discovery_signature(
    docs_dir: Path, project: str, state_root: Path | None
) -> tuple[int, int]:
    html_files = list(docs_dir.rglob("*.html"))
    state_files = [
        path
        for path in (
            docs_dir / ".reckon" / "project-state-migration.json",
            docs_dir / "state" / project / "project.json",
            (
                state_root / project / "index.json"
                if state_root is not None
                else docs_dir / "state" / project / "index.json"
            ),
        )
        if path.is_file()
    ]
    signature_files = [*html_files, *state_files]
    return (
        len(signature_files),
        max((path.stat().st_mtime_ns for path in signature_files), default=0),
    )


def _external_dependency_projects(
    inventory: list[dict], project: str
) -> tuple[str, ...]:
    """Return the mounted-project names that can affect derived lifecycle state."""

    from reckon._schema import parse_plan_ref

    projects = {
        str(parsed.project)
        for item in inventory
        for ref in item.get("depends_on", [])
        if (parsed := parse_plan_ref(ref)) is not None
        and parsed.is_external(project)
        and parsed.project
    }
    return tuple(sorted(projects))


def _external_project_signatures(
    projects: tuple[str, ...], state_root: Path | None
) -> tuple[tuple[str, str, tuple[int, int] | None], ...]:
    mounts = load_mounts() if projects else {}
    signatures = []
    for project in projects:
        docs_dir = mounts.get(project)
        if docs_dir is None:
            signatures.append((project, "", None))
            continue
        signatures.append(
            (
                project,
                str(docs_dir.resolve()),
                _discovery_signature(docs_dir, project, state_root),
            )
        )
    return tuple(signatures)


def _cache_discovery_result(
    cache_key: tuple[str, str],
    local_signature: tuple[int, int],
    project: str,
    state_root: Path | None,
    result: dict,
) -> dict:
    external_projects = _external_dependency_projects(
        result.get("inventory", []), project
    )
    _DISC_CACHE[cache_key] = _DiscoveryCacheEntry(
        local_signature=local_signature,
        external_projects=external_projects,
        external_signatures=_external_project_signatures(external_projects, state_root),
        result=result,
    )
    return result


def discover_plans(docs_dir: Path, project: str, state_root: Path | None) -> dict:
    """Return {inventory, sprints, milestones} by scanning HTML doc pages.

    Any HTML file under docs_dir (outside infra dirs/files) is a doc; meta tags
    enrich it. Results are cached per project against a cheap (count, max-mtime)
    signature so an unchanged docs tree returns instantly.
    """
    sig = _discovery_signature(docs_dir, project, state_root)
    cache_key = (project, str(docs_dir.resolve()))
    cached = _DISC_CACHE.get(cache_key)
    if cached and cached.local_signature == sig:
        external_signatures = _external_project_signatures(
            cached.external_projects, state_root
        )
        if external_signatures == cached.external_signatures:
            return cached.result

    # Batch git first-commit lookup — gives true creation time for tracked files.
    # Falls back to inode ctime when a file is untracked or git is unavailable.
    repo_dir = docs_dir.parent
    git_times = _git_first_committed(repo_dir, docs_dir)

    inventory: list[dict] = []
    resources = sorted(
        resource_map(
            docs_dir,
            project,
            include_archived=True,
            ignore_invalid=True,
        ).values(),
        key=lambda item: str(item.relative_path),
    )

    for resource in resources:
        if resource.type not in {"plan", "research", "evidence"}:
            continue
        html_file = resource.path

        # Scalar inventory stays on the lightweight meta path. Named gate and
        # decision rows are the body state needed to explain roadmap blockers.
        # The SPA fetches the remaining full state from the per-document route.
        rec = _plan_html.parse_meta(html_file)
        gates, decisions = _read_readiness_state(html_file)
        slug = resource.slug
        artifact_type = resource.type
        item = {
            "slug": slug,
            "resource_id": resource.identity.key,
            "href": str(
                (
                    resource.relative_path
                    if resource.legacy
                    else resource.canonical_relative_path
                ).with_suffix("")
            ),
            "canonical_href": resource.canonical_href,
            "legacy": resource.legacy,
            "title": rec["title"],
            "type": artifact_type,
            "summary": rec.get("summary", ""),
            "owner": rec.get("owner", ""),
            "last": rec.get("modified", ""),
            "created": git_times.get(str(html_file.relative_to(repo_dir)))
            or int(
                getattr(html_file.stat(), "st_birthtime", None)
                or html_file.stat().st_ctime
            ),
            "version": rec["version"],
            "archived": rec.get("archived") or ("1" if resource.archived else ""),
            "read": rec.get("read") or "",
            "reviewed_at": rec.get("reviewed_at", ""),
            "recorded_at": rec.get("recorded_at", ""),
            "verdict": rec.get("verdict", ""),
            "environment": rec.get("environment", ""),
            "source": rec.get("source", ""),
            "source_quality": rec.get("source_quality", ""),
            "informs": rec.get("informs", []),
            "evidence_for": rec.get("evidence_for", []),
            "verifies": rec.get("verifies", []),
            "supersedes": rec.get("supersedes", []),
            "commits": rec.get("commits", []),
            "artifacts": rec.get("artifacts", []),
        }
        if artifact_type == "plan":
            item.update(
                {
                    "status": rec["status"],
                    "ms": rec.get("milestone", "—"),
                    "roi": rec.get("roi", "mid"),
                    "effort": rec.get("effort", "M"),
                    # Authored hours must reach the roadmap: dropping them here
                    # silently substitutes the legacy letter's default, so a
                    # 12-hour plan reports as its size letter's 2.
                    "effort_hours": rec.get("effort_hours"),
                    "wall_clock_hours": rec.get("wall_clock_hours"),
                    "effort_calibrated": rec.get("effort_calibrated"),
                    "sprint": rec.get("sprint") or None,
                    "capability": rec.get("capability"),
                    "tier": rec.get("tier"),
                    "impl": rec["impl"],
                    "dec_open": rec["dec_open"],
                    "decisions": decisions,
                    "blockers": rec["blockers"],
                    "gates": gates,
                    "depends_on": rec.get("depends_on", []),
                    "blocks": rec.get("blocks", []),
                }
            )
            if rec.get("north_star"):
                item["north_star"] = rec["north_star"]
        inventory.append(item)

    # ── Distributed project state ──────────────────────────────────────────
    from reckon.project_state import compose_project_state, project_state_mode

    mode = project_state_mode(docs_dir)
    if mode.format == "distributed":
        composed = compose_project_state(docs_dir, project)
        inventory, sprints = _derive_lifecycle(
            project,
            inventory,
            composed.get("sprints", []),
        )
        result = {
            "inventory": inventory,
            "sprints": sprints,
            "milestones": composed.get("milestones", []),
            "blockers": composed.get("blockers", []),
            "timeline": composed.get("timeline", []),
            "active_sprint_id": composed.get("active_sprint_id"),
            "north_stars": composed.get("north_stars", []),
            "source_format": "distributed",
            "resource_versions": composed.get("resource_versions", {}),
        }
        return _cache_discovery_result(cache_key, sig, project, state_root, result)

    # ── Legacy project state ───────────────────────────────────────────────
    # Marker absence/staging means the JSON index is the only canonical store.
    # Ignore any typed destinations left by an interrupted migration; consuming
    # them here would expose a partially installed distributed state.
    sprints: list = []
    milestones: list = []
    blockers: list = []
    timeline: list = []
    active_sprint_id = None
    north_stars: list = []
    if state_root is not None:
        sf = state_root / project / "index.json"
        if sf.is_file():
            try:
                env = json.loads(sf.read_text())
                data = env.get("data", {}) if isinstance(env, dict) else {}
                sprints = data.get("sprints", [])
                milestones = data.get("milestones", [])
                blockers = data.get("blockers", [])
                timeline = data.get("timeline", [])
                active_sprint_id = data.get("active_sprint_id")
                north_stars = data.get("north_stars", [])
            except (OSError, json.JSONDecodeError):
                pass

    # Auto-synthesize stub sprint entries for any sprint ID referenced in plan
    # inventory items that isn't already represented in the sprints list.
    existing_sprint_ids = {s.get("id") for s in sprints if s.get("id")}
    referenced_sprint_ids = {item["sprint"] for item in inventory if item.get("sprint")}
    missing_sprint_ids = referenced_sprint_ids - existing_sprint_ids
    for sid in sorted(missing_sprint_ids):
        sprints.append(
            {
                "id": sid,
                "theme": f"Sprint {sid}",
                "description": "Auto-synthesized from plan inventory",
                "status": "planned",
                "items": [],
            }
        )

    inventory, sprints = _derive_lifecycle(project, inventory, sprints)
    result = {
        "inventory": inventory,
        "sprints": sprints,
        "milestones": milestones,
        "blockers": blockers,
        "timeline": timeline,
        "active_sprint_id": active_sprint_id,
        "north_stars": north_stars,
        "source_format": "legacy-index",
    }
    return _cache_discovery_result(cache_key, sig, project, state_root, result)


def _read_readiness_state(path: Path) -> tuple[list[dict], list[dict]]:
    """Read named gate and decision state used to explain readiness."""

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], []
    has_gates = 'data-reckon="gates"' in text or "data-reckon='gates'" in text
    has_decisions = (
        'data-reckon="decisions"' in text or "data-reckon='decisions'" in text
    )
    if not has_gates and not has_decisions:
        return [], []
    state = _plan_html.read_state(text)
    gates = list(state.get("gates") or []) if has_gates else []
    decisions = state.get("decisions") or {}
    decision_rows = [
        {"key": key, **(decision if isinstance(decision, dict) else {})}
        for key, decision in decisions.items()
    ]
    return gates, decision_rows


def _derive_lifecycle(
    project: str,
    inventory: list[dict],
    sprints: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Attach derived blockers/effective status and hydrate sprint items."""

    from copy import deepcopy

    from reckon._schema import parse_plan_ref

    plans = deepcopy(inventory)
    plan_by_slug = {
        str(plan.get("slug")): plan
        for plan in plans
        if plan.get("type", "plan") == "plan" and plan.get("slug")
    }
    explicit_by_slug: dict[str, list[str]] = {}
    for sprint in sprints:
        for raw in sprint.get("items", []):
            if not isinstance(raw, dict):
                continue
            slug = str(raw.get("slug") or "")
            if not slug:
                continue
            explicit_by_slug.setdefault(slug, []).extend(
                str(blocker_id) for blocker_id in raw.get("blocked_by", [])
            )

    external_cache: dict[tuple[str, str], dict] = {}

    def resolve(ref: str) -> dict:
        parsed = parse_plan_ref(ref)
        if parsed is None:
            return {"ref": ref, "scope": "invalid", "found": False}
        external = parsed.is_external(project)
        target_project = parsed.project if external else project
        row = {
            "ref": ref,
            "scope": "external" if external else "local",
            "project": target_project,
            "slug": parsed.slug,
            "found": False,
        }
        if parsed.stage:
            row["stage"] = parsed.stage
        if external:
            key = (target_project, parsed.slug)
            target = external_cache.get(key)
            if target is None:
                target = _mounted_plan_record(target_project, parsed.slug)
                external_cache[key] = target
        else:
            target = plan_by_slug.get(parsed.slug, {})
        if not target:
            return row
        return {
            **row,
            "found": True,
            "status": target.get("status", ""),
            "impl": target.get("impl", 0),
            "title": target.get("title", ""),
        }

    for plan in plan_by_slug.values():
        dependencies = [resolve(ref) for ref in plan.get("depends_on", [])]
        blocking = unresolved_dependencies(dependencies)
        blocking.extend(
            {"kind": "explicit", "id": blocker_id}
            for blocker_id in dict.fromkeys(
                explicit_by_slug.get(str(plan.get("slug")), [])
            )
        )
        blocking.extend(unpassed_gate_blockers(plan.get("gates") or []))
        workflow_status = str(plan.get("status") or "draft")
        plan["workflow_status"] = workflow_status
        plan["effective_status"] = effective_status(workflow_status, blocking)
        plan["blocking"] = blocking
        plan["blockers"] = len(blocking)

    hydrated_sprints = deepcopy(sprints)
    for sprint in hydrated_sprints:
        items = []
        for raw in sprint.get("items", []):
            item = {"slug": raw} if isinstance(raw, str) else dict(raw)
            plan = plan_by_slug.get(str(item.get("slug") or ""))
            if plan:
                for key in (
                    "title",
                    "status",
                    "effective_status",
                    "impl",
                    "blocking",
                ):
                    if key in plan:
                        item[key] = plan[key]
            items.append(item)
        sprint["items"] = items
    return plans, hydrated_sprints


def _mounted_plan_record(project: str, slug: str) -> dict:
    """Read one external plan's lightweight record from its registered mount."""

    docs_dir = load_mounts().get(project)
    if docs_dir is None:
        return {}
    for resource in resource_map(
        docs_dir,
        project,
        include_archived=False,
        ignore_invalid=True,
    ).values():
        if resource.type == "plan" and resource.slug == slug:
            return _plan_html.parse_meta(resource.path)
    return {}


def _render_spa_html(project: str) -> str:
    """Generate a complete index.html for the given project."""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="docs-project" content="{project}">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>reckon · {project}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/_shared/foundation.css">
  <link rel="stylesheet" href="/_shared/dashboard.css">
  <link rel="stylesheet" href="/_ui/project.css">
  <link rel="stylesheet" href="/_ui/styles-base.css">
  <link rel="stylesheet" href="/_ui/styles.css">
  <link rel="stylesheet" href="/_ui/topbar.css">
  <link rel="stylesheet" href="/_ui/plans.css">
  <link rel="stylesheet" href="/_ui/reader.css">
  <link rel="stylesheet" href="/_ui/overview.css">
  <link rel="stylesheet" href="/_ui/sprints.css">
  <link rel="stylesheet" href="/_ui/crew.css">
  <link rel="stylesheet" href="/_ui/graph.css">
  <script src="https://unpkg.com/react@18.3.1/umd/react.development.js" integrity="sha384-hD6/rw4ppMLGNu3tX5cjIb+uRZ7UkRJ6BPkLpg4hAu/6onKUg4lLsHAs9EBPT82L" crossorigin="anonymous"></script>
  <script src="https://unpkg.com/react-dom@18.3.1/umd/react-dom.development.js" integrity="sha384-u6aeetuaXnQ38mYT8rp6sbXaQe3NL9t+IBXmnYxwkUI2Hw4bsp2Wvmx4yRQF1uAm" crossorigin="anonymous"></script>
  <script src="https://unpkg.com/@babel/standalone@7.29.0/babel.min.js" integrity="sha384-m08KidiNqLdpJqLq95G/LEi8Qvjl/xUYll3QILypMoQ65QorJ9Lvtp2RXYGBFj1y" crossorigin="anonymous"></script>
</head>
<body>
  <div id="root"></div>
  <script src="/_ui/state-loader.js"></script>
  <script type="text/babel" src="/_ui/glyphs.jsx"></script>
  <script type="text/babel" src="/_ui/_shared.jsx"></script>
  <script src="/_ui/prompts.js"></script>
  <script type="text/babel" src="/_ui/ui.jsx"></script>
  <script type="text/babel" src="/_ui/bits.jsx"></script>
  <script type="text/babel" src="/_ui/decision.jsx"></script>
  <script type="text/babel" src="/_ui/cockpit.jsx"></script>
  <script type="text/babel" src="/_ui/plan.jsx"></script>
  <script type="text/babel" src="/_ui/sprint.jsx"></script>
  <script type="text/babel" src="/_ui/graph.jsx"></script>
  <script type="text/babel" src="/_ui/crew.jsx"></script>
  <script type="text/babel" src="/_ui/shell.jsx"></script>
</body>
</html>
"""


def safe_join(root: Path, rel: str) -> Path | None:
    try:
        target = (root / rel.lstrip("/")).resolve()
    except (OSError, ValueError):
        return None
    if root not in target.parents and target != root:
        return None
    return target


def _patch_into(target: dict, patch: dict) -> dict:
    """Merge a flat dotted-key patch into nested dict `target`, in place.

    Keys may be dotted (e.g. {"decisions.scan.choice": "..."}); intermediate
    objects are created as needed. A non-dict value blocking a dotted path is
    overwritten with a fresh object.
    """
    for k, v in patch.items():
        parts = str(k).split(".")
        cur = target
        for p in parts[:-1]:
            nxt = cur.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[p] = nxt
            cur = nxt
        cur[parts[-1]] = v
    return target


def _project_for_root(root: Path) -> str:
    for project, mounted in load_mounts().items():
        if mounted.resolve() == root.resolve():
            return project
    return root.parent.name


def _resolve_plan_file(
    root: Path,
    slug: str,
    artifact_type: str | None = None,
    *,
    project: str | None = None,
) -> Path | None:
    """Find an HTML resource by stable typed identity."""
    resource = resolve_resource(
        root,
        project or _project_for_root(root),
        slug,
        artifact_type,
    )
    if resource is None:
        resource = resolve_resource(
            root,
            project or _project_for_root(root),
            slug,
            artifact_type,
            include_archived=True,
        )
    return resource.path if resource else None


class Handler(BaseHTTPRequestHandler):
    server_version = "reckon-docs/1.0"
    _host: str = "127.0.0.1"
    _port: int = 8765

    def log_message(self, fmt: str, *args) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, status: int, body: bytes, ctype: str = "text/html") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj, indent=2).encode(), "application/json")

    def _send_redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.PERMANENT_REDIRECT)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlsplit(self.path).path)

        if path == "/favicon.ico":
            # Browsers auto-request this; answer cleanly instead of 404-ing
            # through the project router.
            self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            return

        if path in ("/", ""):
            if _HOME_HTML and _HOME_HTML.is_file():
                ctype, _ = mimetypes.guess_type(str(_HOME_HTML))
                try:
                    self._send(
                        HTTPStatus.OK, _HOME_HTML.read_bytes(), ctype or "text/html"
                    )
                except OSError as e:
                    self._send(HTTPStatus.INTERNAL_SERVER_ERROR, str(e).encode())
            else:
                self._send(
                    HTTPStatus.OK,
                    render_index_fallback(load_mounts(), self._host, self._port),
                )
            return

        if path.startswith("/_shared/"):
            rel = path[len("/_shared/") :]
            fname = rel.lstrip("/")
            if not SAFE_NAME.match(fname):
                self._send(HTTPStatus.BAD_REQUEST, b"bad shared filename")
                return
            target = (_SHARED_ROOT or Path("/dev/null")) / fname
            if not target.is_file():
                self._send(HTTPStatus.NOT_FOUND, b"shared asset not found")
                return
            ctype, _ = mimetypes.guess_type(str(target))
            try:
                self._send(
                    HTTPStatus.OK,
                    target.read_bytes(),
                    ctype or "application/octet-stream",
                )
            except OSError as e:
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, str(e).encode())
            return

        if path.startswith("/_ui/"):
            rel = path[len("/_ui/") :]
            fname = rel.lstrip("/")
            if not SAFE_NAME.match(fname):
                self._send(HTTPStatus.BAD_REQUEST, b"bad ui filename")
                return
            target = Path(__file__).parent.parent / "docs" / "ui" / fname
            if not target.is_file():
                self._send(HTTPStatus.NOT_FOUND, b"ui asset not found")
                return
            ext = target.suffix.lower()
            if ext == ".css":
                ctype = "text/css"
            elif ext in (".js", ".jsx"):
                ctype = "application/javascript"
            else:
                ctype, _ = mimetypes.guess_type(str(target))
                ctype = ctype or "application/octet-stream"
            try:
                self._send(HTTPStatus.OK, target.read_bytes(), ctype)
            except OSError as e:
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, str(e).encode())
            return

        if path == "/_projects/index.json":
            self._send_json(HTTPStatus.OK, collect_projects(load_mounts()))
            return

        if path.startswith("/_projects/"):
            rel = path[len("/_projects/") :]
            fname = rel.lstrip("/")
            if not SAFE_NAME.match(fname):
                self._send(HTTPStatus.BAD_REQUEST, b"bad projects filename")
                return
            target = _config_home() / fname
            if not target.is_file():
                self._send(HTTPStatus.NOT_FOUND, b"not found")
                return
            ctype, _ = mimetypes.guess_type(str(target))
            try:
                self._send(
                    HTTPStatus.OK,
                    target.read_bytes(),
                    ctype or "application/octet-stream",
                )
            except OSError as e:
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, str(e).encode())
            return

        if path == "/crew" or path.startswith("/crew/"):
            parts = path.strip("/").split("/")
            project = parts[1] if len(parts) >= 2 else None
            finished = len(parts) in (3, 4) and parts[2] == "finished"
            plan = parts[3] if len(parts) == 4 and finished else None
            if len(parts) > 2 and not finished:
                self._send(HTTPStatus.BAD_REQUEST, b"bad crew path")
                return
            if project is not None and not SAFE_NAME.fullmatch(project):
                self._send(HTTPStatus.BAD_REQUEST, b"bad project name")
                return
            if plan is not None and not SAFE_NAME.fullmatch(plan):
                self._send(HTTPStatus.BAD_REQUEST, b"bad plan name")
                return
            mounts = load_mounts()
            if project is not None and project not in mounts:
                self._send(HTTPStatus.NOT_FOUND, b"project not found")
                return
            if finished:
                try:
                    records = _finished_crew_rows(mounts, project, plan)
                except (OSError, ledger.LedgerError) as exc:
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": "ledger_error", "detail": str(exc)},
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {"project": project, "plan": plan, "runs": records},
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {"project": project, "runs": _crew_rows(mounts, project)},
            )
            return

        if path.startswith("/plan/"):
            # GET /plan/<project>/<slug> — embedded semantic state for one plan
            # (raw, with version), for clients that need the current version
            # before a write. {} if the plan/state is absent.
            parts = path[len("/plan/") :].strip("/").split("/")
            if len(parts) not in (2, 3) or not SAFE_NAME.match(parts[0]):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad path"})
                return
            project = parts[0]
            http_roots = {
                **ROOT_TYPES,
                "timeline": "timeline",
                "project": "project",
            }
            artifact_type = http_roots.get(parts[1]) if len(parts) == 3 else None
            slug = parts[-1]
            if len(parts) == 3 and artifact_type is None:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad resource type"})
                return
            slug = slug.removesuffix(".html").removesuffix(".json")
            mts = load_mounts()
            if project not in mts:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown project"})
                return
            from reckon.project_state import (
                RESOURCE_TYPES as PROJECT_RESOURCE_TYPES,
                ProjectStateError,
                read_resource,
            )

            if artifact_type in PROJECT_RESOURCE_TYPES:
                try:
                    data, version = read_resource(
                        Path(mts[project]), project, artifact_type, slug
                    )
                except FileNotFoundError:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown resource"})
                    return
                except ProjectStateError as exc:
                    self._send_json(
                        HTTPStatus.CONFLICT,
                        {"error": "project_state_error", "detail": str(exc)},
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "project": project,
                        "slug": slug,
                        "doc_type": artifact_type,
                        "version": version,
                        "data": data,
                    },
                )
                return
            try:
                pf = _resolve_plan_file(
                    mts[project], slug, artifact_type, project=project
                )
            except ResourceCollision as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            rec = _plan_html.parse_plan(pf) if pf else {}
            self._send_json(HTTPStatus.OK, rec)
            return

        if path.startswith("/state/"):
            parts = path[len("/state/") :].strip("/").split("/", 1)
            if len(parts) != 2 or not SAFE_NAME.match(parts[0]):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad path"})
                return
            project, doc = parts
            doc_stem = doc.removesuffix(".json")
            if not SAFE_NAME.match(doc_stem):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad doc"})
                return
            state_file = (
                (_STATE_ROOT or Path("/dev/null"))
                / project
                / (doc if doc.endswith(".json") else f"{doc}.json")
            )

            # index.json: serve live inventory merged with static structure.
            # Scanning HTML for <meta name="plan-*"> tags on every read means
            # new plan pages appear immediately without running reckon sync.
            if doc_stem == "index":
                envelope: dict = {}
                if state_file.is_file():
                    try:
                        envelope = json.loads(state_file.read_bytes())
                    except (OSError, json.JSONDecodeError):
                        pass
                mts = load_mounts()
                if project in mts:
                    from reckon.project_state import (
                        ProjectStateError,
                        project_state_mode,
                    )

                    try:
                        distributed = (
                            project_state_mode(Path(mts[project])).format
                            == "distributed"
                        )
                    except ProjectStateError as exc:
                        self._send_json(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            {
                                "error": "distributed_project_state_invalid",
                                "detail": str(exc),
                            },
                        )
                        return
                    try:
                        disc = discover_plans(mts[project], project, _STATE_ROOT)
                        data = dict(envelope.get("data") or {})
                        data["inventory"] = disc.get("inventory", [])
                        if disc.get("source_format") == "distributed":
                            for field in (
                                "sprints",
                                "milestones",
                                "blockers",
                                "timeline",
                                "active_sprint_id",
                                "north_stars",
                                "source_format",
                                "resource_versions",
                            ):
                                data[field] = disc.get(field)
                            data["_version"] = 0
                        elif not data.get("sprints") and disc.get("sprints"):
                            data["sprints"] = disc["sprints"]
                        if (
                            disc.get("source_format") != "distributed"
                            and not data.get("milestones")
                            and disc.get("milestones")
                        ):
                            data["milestones"] = disc["milestones"]
                        self._send_json(HTTPStatus.OK, {**envelope, "data": data})
                        return
                    except ProjectStateError as exc:
                        if distributed:
                            self._send_json(
                                HTTPStatus.INTERNAL_SERVER_ERROR,
                                {
                                    "error": "distributed_project_state_invalid",
                                    "detail": str(exc),
                                },
                            )
                            return
                    except Exception as exc:  # noqa: BLE001
                        if distributed:
                            self._send_json(
                                HTTPStatus.INTERNAL_SERVER_ERROR,
                                {
                                    "error": "distributed_project_state_invalid",
                                    "detail": str(exc),
                                },
                            )
                            return
                if not state_file.is_file():
                    self._send_json(HTTPStatus.OK, {})
                    return
                try:
                    self._send(
                        HTTPStatus.OK, state_file.read_bytes(), "application/json"
                    )
                except OSError as e:
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
                return

            if not state_file.exists():
                self._send_json(HTTPStatus.OK, {})
                return
            try:
                self._send(HTTPStatus.OK, state_file.read_bytes(), "application/json")
            except OSError as e:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
            return

        if path.startswith("/_discover/"):
            project = path[len("/_discover/") :].strip("/")
            if not project or not SAFE_NAME.match(project):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad project name"})
                return
            disc_mounts = load_mounts()
            if project not in disc_mounts:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown project"})
                return
            try:
                result = discover_plans(disc_mounts[project], project, _STATE_ROOT)
            except Exception as exc:  # noqa: BLE001
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "error": "distributed_project_state_invalid",
                        "detail": str(exc),
                    },
                )
                return
            self._send_json(HTTPStatus.OK, result)
            return

        parts = path.lstrip("/").split("/", 1)
        project = parts[0]
        rel = parts[1] if len(parts) == 2 else ""
        mounts = load_mounts()
        if project not in mounts:
            self._send(HTTPStatus.NOT_FOUND, b"unknown project")
            return

        root = mounts[project]
        if rel in ("", "/"):
            # Serve the dynamically generated SPA shell for both /<project> and
            # /<project>/. No redirect: the shell links assets via absolute
            # /_shared and /_ui routes, so it does not depend on a trailing
            # slash for relative resolution.
            self._send(HTTPStatus.OK, _render_spa_html(project).encode(), "text/html")
            return
        if rel == "index.html":
            # Also intercept direct requests to /<project>/index.html.
            self._send(HTTPStatus.OK, _render_spa_html(project).encode(), "text/html")
            return

        # Intercept /<project>/state/<subproject>/index.json — state-loader.js
        # uses a relative state URL that resolves here instead of to /state/.
        # Apply the same live-discovery logic so the SPA always gets fresh inventory.
        rel_parts = rel.split("/")
        if (
            len(rel_parts) == 3
            and rel_parts[0] == "state"
            and rel_parts[2] == "index.json"
            and SAFE_NAME.match(rel_parts[1])
        ):
            sub_project = rel_parts[1]
            sf = (_STATE_ROOT or Path("/dev/null")) / sub_project / "index.json"
            envelope: dict = {}
            if sf.is_file():
                try:
                    envelope = json.loads(sf.read_bytes())
                except (OSError, json.JSONDecodeError):
                    pass
            mts = load_mounts()
            if sub_project in mts:
                from reckon.project_state import (
                    ProjectStateError,
                    project_state_mode,
                )

                try:
                    distributed = (
                        project_state_mode(Path(mts[sub_project])).format
                        == "distributed"
                    )
                except ProjectStateError as exc:
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {
                            "error": "distributed_project_state_invalid",
                            "detail": str(exc),
                        },
                    )
                    return
                try:
                    disc = discover_plans(
                        Path(mts[sub_project]), sub_project, _STATE_ROOT
                    )
                    data = dict(envelope.get("data") or {})
                    data["inventory"] = disc.get("inventory", [])
                    if disc.get("source_format") == "distributed":
                        for field in (
                            "sprints",
                            "milestones",
                            "blockers",
                            "timeline",
                            "active_sprint_id",
                            "north_stars",
                            "source_format",
                            "resource_versions",
                        ):
                            data[field] = disc.get(field)
                        data["_version"] = 0
                    elif not data.get("sprints") and disc.get("sprints"):
                        data["sprints"] = disc["sprints"]
                    if (
                        disc.get("source_format") != "distributed"
                        and not data.get("milestones")
                        and disc.get("milestones")
                    ):
                        data["milestones"] = disc["milestones"]
                    self._send_json(HTTPStatus.OK, {**envelope, "data": data})
                    return
                except ProjectStateError as exc:
                    if distributed:
                        self._send_json(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            {
                                "error": "distributed_project_state_invalid",
                                "detail": str(exc),
                            },
                        )
                        return
                except Exception as exc:  # noqa: BLE001
                    if distributed:
                        self._send_json(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            {
                                "error": "distributed_project_state_invalid",
                                "detail": str(exc),
                            },
                        )
                        return
            if sf.is_file():
                self._send(HTTPStatus.OK, sf.read_bytes(), "application/json")
            else:
                self._send_json(HTTPStatus.OK, {})
            return

        try:
            resource, legacy_alias = resolve_route(root, project, rel)
        except ResourceCollision as exc:
            self._send(HTTPStatus.CONFLICT, str(exc).encode())
            return
        if resource is not None:
            if legacy_alias:
                self._send_redirect(resource.canonical_href)
                return
            target = resource.path
        else:
            target = safe_join(root, rel)
        if target is None:
            self._send(HTTPStatus.FORBIDDEN, b"forbidden")
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file() and not target.suffix:
            # Extensionless plan URL (/<project>/<slug>) — resolve to the
            # .html document so bare links and typed URLs render instead of
            # 404ing. Mirrors the /plan/ endpoint's slug semantics.
            html_target = target.with_suffix(".html")
            if html_target.is_file():
                target = html_target
        if not target.is_file():
            self._send(HTTPStatus.NOT_FOUND, b"not found")
            return

        ctype, _ = mimetypes.guess_type(str(target))
        try:
            self._send(
                HTTPStatus.OK, target.read_bytes(), ctype or "application/octet-stream"
            )
        except OSError as e:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, str(e).encode())

    def _read_body(self) -> tuple[bool, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_POST_BYTES:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": f"body > {MAX_POST_BYTES} bytes"},
            )
            return False, None
        raw = self.rfile.read(length) if length else b""
        try:
            return True, (json.loads(raw) if raw else {})
        except json.JSONDecodeError as e:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"json: {e}"})
            return False, None

    def _handle_plan_write(self, path: str) -> None:
        """POST /plan/<project>/<slug> — merge a dotted patch into the plan
        embedded semantic state and rewrite the HTML file in place. The plan HTML
        is the sole store; there is no sidecar state JSON.

        Optimistic concurrency: send `If-Match: <version>`; a mismatch returns
        412 with the current state so the client can rebase and retry.
        """
        parts = path[len("/plan/") :].strip("/").split("/")
        if len(parts) not in (2, 3) or not SAFE_NAME.match(parts[0]):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad path"})
            return
        project = parts[0]
        http_roots = {
            **ROOT_TYPES,
            "timeline": "timeline",
            "project": "project",
        }
        artifact_type = http_roots.get(parts[1]) if len(parts) == 3 else None
        if len(parts) == 3 and artifact_type is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad resource type"})
            return
        slug = parts[-1]
        slug = slug.removesuffix(".html")
        if not SAFE_NAME.match(slug):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad slug"})
            return
        mounts = load_mounts()
        if project not in mounts:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown project"})
            return
        from reckon.project_state import RESOURCE_TYPES as PROJECT_RESOURCE_TYPES

        if artifact_type in PROJECT_RESOURCE_TYPES:
            self._handle_project_resource_write(
                project,
                slug,
                artifact_type,
                Path(mounts[project]),
            )
            return
        plan_file = _resolve_plan_file(
            mounts[project], slug, artifact_type, project=project
        )
        if plan_file is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown plan"})
            return

        ok, patch = self._read_body()
        if not ok:
            return
        if not isinstance(patch, dict):
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "patch must be an object"}
            )
            return

        text = plan_file.read_text(encoding="utf-8", errors="replace")
        state = _plan_html.read_state(text)
        cur_version = int(state.get("version", 0) or 0)

        if_match = self.headers.get("If-Match")
        if if_match is None:
            self._send_json(
                HTTPStatus.PRECONDITION_FAILED,
                {
                    "error": "version_mismatch",
                    "current_version": cur_version,
                    "expected_version": None,
                    "current_data": state,
                },
            )
            return
        try:
            expected = int(if_match.strip().strip('"'))
        except (ValueError, TypeError):
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "If-Match must be an integer"}
            )
            return
        if expected != cur_version:
            self._send_json(
                HTTPStatus.PRECONDITION_FAILED,
                {
                    "error": "version_mismatch",
                    "current_version": cur_version,
                    "expected_version": expected,
                    "current_data": state,
                },
            )
            return

        patch.pop("version", None)
        patch.pop("_version", None)
        _patch_into(state, patch)
        state.setdefault("slug", slug)
        # The continuation rule has to hold on every write path, or an agent can
        # mark a plan landed here and tell nobody what comes next.
        try:
            from reckon._store import OpError, validate_landing_patch

            validate_landing_patch(state, patch)
        except OpError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "no_continuation", "detail": str(exc)},
            )
            return
        try:
            from reckon._schema import PlanState

            validated = PlanState.model_validate(state).validate_for_write()
        except ValueError as exc:
            details = [
                line.strip(" -")
                for line in str(exc).splitlines()
                if line.strip() and not line.rstrip().endswith("failed:")
            ]
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "schema_validation", "details": details},
            )
            return
        state = validated.canonical_dump()
        state["modified"] = datetime.now().strftime("%Y-%m-%d")
        state["version"] = cur_version + 1

        new_text = _plan_html.write_state(text, state)

        # Idempotency guard: if the only difference between the new and current
        # file would be the version/modified stamps (i.e. the patch carried no
        # real content change), skip the disk write and return the current version
        # unchanged.  This prevents churn from no-op edits or round-trips through
        # BeautifulSoup's entity-normalisation pass.
        #
        # We detect a no-op by comparing the parsed state dicts with version and
        # modified stripped out.  A string comparison would fail for trivially
        # equivalent HTML (different entity encoding, whitespace) but a state-dict
        # comparison correctly reflects semantic content equality.
        new_state_parsed = _plan_html.read_state(new_text)
        if _content_equal(
            state, new_state_parsed, cur_state=_plan_html.read_state(text)
        ):
            self._send_json(
                HTTPStatus.OK, {"ok": True, "slug": slug, "version": cur_version}
            )
            return

        tmp = plan_file.with_suffix(".html.tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(plan_file)
        self._send_json(
            HTTPStatus.OK, {"ok": True, "slug": slug, "version": state["version"]}
        )

    def _handle_project_resource_write(
        self,
        project: str,
        slug: str,
        resource_type: str,
        docs_dir: Path,
    ) -> None:
        """Version-check and patch one distributed project-state resource."""
        from reckon.project_state import (
            ProjectStateConflict,
            ProjectStateError,
            read_resource,
            write_resource,
        )

        ok, patch = self._read_body()
        if not ok:
            return
        if not isinstance(patch, dict):
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "patch must be an object"}
            )
            return
        try:
            state, current_version = read_resource(
                docs_dir, project, resource_type, slug
            )
        except ProjectStateError as exc:
            self._send_json(
                HTTPStatus.CONFLICT,
                {"error": "project_state_error", "detail": str(exc)},
            )
            return
        if_match = self.headers.get("If-Match")
        if if_match is None:
            self._send_json(
                HTTPStatus.PRECONDITION_FAILED,
                {
                    "error": "version_mismatch",
                    "current_version": current_version,
                    "expected_version": None,
                    "current_data": state,
                },
            )
            return
        try:
            expected = int(if_match.strip().strip('"'))
        except (ValueError, TypeError):
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "If-Match must be an integer"}
            )
            return
        working = dict(state)
        patch.pop("version", None)
        patch.pop("type", None)
        patch.pop("id", None)
        _patch_into(working, patch)
        try:
            new_version = write_resource(
                docs_dir,
                project,
                resource_type,
                slug,
                working,
                expected,
            )
        except ProjectStateConflict as exc:
            self._send_json(
                HTTPStatus.PRECONDITION_FAILED,
                {
                    "error": "version_mismatch",
                    "current_version": exc.current,
                    "expected_version": exc.expected,
                    "current_data": exc.current_data,
                },
            )
            return
        except (ProjectStateError, ValueError, FileNotFoundError) as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "resource_validation", "detail": str(exc)},
            )
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "slug": slug,
                "doc_type": resource_type,
                "version": new_version,
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlsplit(self.path).path)
        if path.startswith("/plan/"):
            self._handle_plan_write(path)
            return
        if not path.startswith("/state/"):
            self._send_json(
                HTTPStatus.NOT_FOUND, {"error": "POST to /plan/<project>/<slug>"}
            )
            return
        parts = path[len("/state/") :].strip("/").split("/", 1)
        if len(parts) != 2 or not SAFE_NAME.match(parts[0]):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad path"})
            return
        project, doc = parts
        doc_stem = doc.removesuffix(".json")
        if not SAFE_NAME.match(doc_stem):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad doc"})
            return
        if doc_stem == "index":
            from reckon.project_state import ProjectStateError, project_state_mode

            mounts = load_mounts()
            if project in mounts:
                try:
                    distributed = (
                        project_state_mode(Path(mounts[project])).format
                        == "distributed"
                    )
                except ProjectStateError as exc:
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {
                            "error": "distributed_project_state_invalid",
                            "detail": str(exc),
                        },
                    )
                    return
                if distributed:
                    self._send_json(
                        HTTPStatus.CONFLICT,
                        {
                            "error": "legacy_index_read_only",
                            "hint": (
                                "Edit a named sprint, milestone, blocker, "
                                "timeline, or project resource."
                            ),
                        },
                    )
                    return

        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_POST_BYTES:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": f"body > {MAX_POST_BYTES} bytes"},
            )
            return
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError as e:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"json: {e}"})
            return

        out_dir = (_STATE_ROOT or Path("/dev/null")) / project
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{doc_stem}.json"

        cur_data: dict = {}
        cur_version: int = 0
        if out_file.exists():
            try:
                envelope = json.loads(out_file.read_text())
                cur_data = (
                    envelope.get("data", {}) if isinstance(envelope, dict) else {}
                )
                cur_version = int(cur_data.get("_version", 0))
            except (OSError, json.JSONDecodeError, ValueError):
                cur_data = {}
                cur_version = 0

        if_match_raw = self.headers.get("If-Match")
        if if_match_raw is None:
            self._send_json(
                HTTPStatus.PRECONDITION_FAILED,
                {
                    "error": "version_mismatch",
                    "current_version": cur_version,
                    "expected_version": None,
                    "current_data": cur_data,
                },
            )
            return

        try:
            expected_version = int(if_match_raw.strip().strip('"'))
        except (ValueError, TypeError):
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "If-Match must be an integer"}
            )
            return

        if expected_version != cur_version:
            self._send_json(
                HTTPStatus.PRECONDITION_FAILED,
                {
                    "error": "version_mismatch",
                    "current_version": cur_version,
                    "expected_version": expected_version,
                    "current_data": cur_data,
                },
            )
            return

        new_data = dict(payload)
        new_data.pop("_version", None)
        new_data["_version"] = cur_version + 1

        envelope = {
            "updated": datetime.now().isoformat(timespec="seconds"),
            "project": project,
            "doc": doc_stem,
            "data": new_data,
        }
        tmp = out_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(envelope, indent=2) + "\n")
        tmp.replace(out_file)
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "path": str(out_file), "version": new_data["_version"]},
        )


def main(
    port: int = 8765, host: str | None = None, mounts_file: Path | None = None
) -> None:
    _resolve_paths(mounts_file)
    _host = host or os.environ.get("DOCS_SERVER_BIND", "127.0.0.1")
    _port = port or int(os.environ.get("DOCS_SERVER_PORT", "8765"))

    # Patch Handler class attributes so do_GET can use them for fallback page.
    Handler._host = _host
    Handler._port = _port

    if _STATE_ROOT:
        _STATE_ROOT.mkdir(parents=True, exist_ok=True)
    if _MOUNTS_FILE and not _MOUNTS_FILE.exists():
        _MOUNTS_FILE.write_text("{}\n")

    fqdn = socket.getfqdn()
    print(f"reckon server listening on http://{_host}:{_port}/", flush=True)
    if _host == "0.0.0.0":  # noqa: S104
        print(f"  team URL:  http://{fqdn}:{_port}/", flush=True)
    else:
        print(
            f"  reach from a laptop: ssh -L {_port}:localhost:{_port} <user>@{fqdn}",
            flush=True,
        )
    print(f"  mounts:  {_MOUNTS_FILE}", flush=True)
    print(f"  state:   {_STATE_ROOT}", flush=True)
    print(f"  shared:  {_SHARED_ROOT}", flush=True)
    ThreadingHTTPServer((_host, _port), Handler).serve_forever()


if __name__ == "__main__":
    main()
