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
  GET /state/<project>/<doc>    → ~/docs-server/state/<project>/<doc>.json
  POST /state/<project>/<doc>   → write the same path (versioned)
  GET /_discover/<project>      → scan docs dir for HTML plan pages (meta tag opt-in)
  GET /<project>/<relpath>      → mount[project]/<relpath>

POST versioned write contract — see reckon/serve.py for full details.
"""

from __future__ import annotations

import html.parser
import json
import mimetypes
import os
import re
import socket
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from reckon import _plan_html

HOME = Path.home()

# ── Configurable paths (set via main() args or env vars) ──────────────────

_MOUNTS_FILE: Path | None = None
_STATE_ROOT: Path | None = None
_HOME_HTML: Path | None = None
_SHARED_ROOT: Path | None = None


def _resolve_paths(mounts_file: Path | None = None) -> None:
    global _MOUNTS_FILE, _STATE_ROOT, _HOME_HTML, _SHARED_ROOT
    legacy_root = HOME / "docs-server"
    _MOUNTS_FILE = mounts_file or (legacy_root / "mounts.json")
    _STATE_ROOT = legacy_root / "state"
    # Prefer the home page bundled in the reckon docs/ directory; fall back to
    # the legacy ~/docs-server/home.html from dotfiles if it doesn't exist yet.
    reckon_home = Path(__file__).parent.parent / "docs" / "home.html"
    legacy_home = legacy_root / "home.html"
    _HOME_HTML = reckon_home if reckon_home.is_file() else legacy_home
    # Shared assets: prefer reckon repo's own docs/_shared, fall back to dotfiles.
    repo_shared = Path(__file__).parent.parent / "docs" / "_shared"
    dotfiles_shared = HOME / ".claude" / "skills" / "html-docs" / "assets"
    _SHARED_ROOT = repo_shared if repo_shared.is_dir() else dotfiles_shared


SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_POST_BYTES = 1_000_000


def load_mounts() -> dict[str, Path]:
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


def render_index_fallback(mounts: dict[str, Path], host: str, port: int) -> bytes:
    rows = []
    for name, path in sorted(mounts.items()):
        rows.append(
            f'<tr><td><a href="/{name}/">{name}</a></td>'
            f'<td><code>{path}</code></td></tr>'
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
        f'{len(mounts)} project(s) mounted &middot; '
        f'{datetime.now().strftime("%Y-%m-%d %H:%M")} &middot; '
        "<em>home.html missing — install reckon to get the rollup view</em>"
        "</div>"
        '<table><thead><tr><th align=left>Project</th>'
        '<th align=left>Path</th></tr></thead><tbody>'
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
                proj["data"] = envelope.get("data", envelope) if isinstance(envelope, dict) else {}
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
        "projects": out,
    }


# ── Plan discovery ────────────────────────────────────────────────────────
#
# A plan HTML page opts in to discovery by carrying at least one
#   <meta name="plan-status" content="active|pending|blocked|shipped">
# tag in its <head>.  Other plan-* meta tags are optional but encouraged.
# See the reckon-sync SKILL.md style guide for the full convention.

_PLAN_META_PREFIX = "plan-"
_NON_PLAN_FILES = frozenset([
    "index.html", "sprint.html", "sprints.html", "milestones.html",
    "decisions.html", "inventory.html", "blockers.html",
    "implementation.html", "questions.html", "home.html",
    "project.html",
])
_NON_PLAN_DIRS = frozenset([
    "_shared", "ui", "state", "assets", "images",
    # Per-stage / archival history (e.g. <plan>-shipped.html, *-locked.html)
    # lives under archive/ so it does not clutter the live inventory.
    "archive",
])


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


def discover_plans(docs_dir: Path, project: str, state_root: Path | None) -> dict:
    """Return {inventory, sprints, milestones} by scanning HTML plan pages.

    Only HTML files that carry <meta name="plan-status"> are included —
    this is the explicit opt-in that distinguishes plan pages from
    infrastructure pages (sprint boards, decisions aggregators, etc.).
    """
    inventory: list[dict] = []

    for html_file in sorted(docs_dir.rglob("*.html")):
        rel = html_file.relative_to(docs_dir)
        # Skip infrastructure directories
        if any(part in _NON_PLAN_DIRS for part in rel.parts[:-1]):
            continue
        if html_file.name in _NON_PLAN_FILES:
            continue

        # The plan HTML is the sole store: parse_plan reads the embedded
        # <script id="reckon-state"> island (status, decisions, followups, …),
        # falling back to <meta> tags then sensible defaults. A bare page with
        # no island still yields a valid record (status=draft, title=<title>).
        rec = _plan_html.parse_plan(html_file)
        slug = rec["slug"]

        # href is the URL path under /<project>/ used by plan.jsx to fetch the
        # HTML. Root-level plans: href == slug. Subdirectory plans (e.g.
        # curated/X.html): href includes the subdir so the fetch resolves.
        rel_no_ext = str(rel.with_suffix(""))
        href = rel_no_ext if rel_no_ext != slug else slug

        inventory.append({
            "slug":       slug,
            "href":       href,
            "title":      rec["title"],
            "type":       rec.get("type", "plan"),
            "informs":    rec.get("informs", []),
            "status":     rec["status"],
            "ms":         rec.get("milestone", "—"),
            "roi":        rec.get("roi", "mid"),
            "effort":     rec.get("effort", "M"),
            "sprint":     rec.get("sprint") or None,
            "summary":    rec.get("summary", ""),
            "tier":       rec.get("tier", "sonnet"),
            "owner":      rec.get("owner", ""),
            "impl":       rec["impl"],
            "dec_open":   rec["dec_open"],
            "blockers":   rec["blockers"],
            "last":       rec.get("modified", ""),
            "version":    rec["version"],
            "depends_on": rec.get("depends_on", []),
            "blocks":     rec.get("blocks", []),
            # Full per-plan state travels in the inventory so the SPA needs no
            # per-plan fetch — the plan page itself is the source of truth.
            "decisions":  rec["decisions"],
            "followups":  rec["followups"],
            "comments":   rec["comments"],
            "questions":  rec["questions"],
        })

    # ── Sprint / milestone discovery from HTML files ──────────────────────
    # docs/sprints/<id>.html and docs/milestones/<id>.html carry sprint/milestone
    # meta tags — zero-wiring alternative to project.json entries.
    sprints: list = []
    milestones: list = []

    sprints_dir = docs_dir / "sprints"
    if sprints_dir.is_dir():
        for sf in sorted(sprints_dir.glob("*.html")):
            _, meta = _read_head_meta(sf)
            sid = meta.get("sprint-id")
            if not sid:
                continue
            sprints.append({
                "id":          sid,
                "theme":       meta.get("sprint-theme", f"Sprint {sid}"),
                "description": meta.get("sprint-description", ""),
                "status":      meta.get("sprint-status", "planned"),
                "starts":      meta.get("sprint-starts", ""),
                "ends":        meta.get("sprint-ends", ""),
                "items":       [],
            })

    milestones_dir = docs_dir / "milestones"
    if milestones_dir.is_dir():
        for mf in sorted(milestones_dir.glob("*.html")):
            _, meta = _read_head_meta(mf)
            mid = meta.get("milestone-id")
            if not mid:
                continue
            milestones.append({
                "id":     mid,
                "name":   meta.get("milestone-name", mid),
                "status": meta.get("milestone-status", "planned"),
                "pct":    int(meta.get("milestone-pct", "0")),
            })

    # Fall back to state/project.json or index.json if no HTML sprint files found
    if not sprints and not milestones and state_root is not None:
        for cand in ("project.json", "index.json"):
            sf = state_root / project / cand
            if not sf.is_file():
                continue
            try:
                env = json.loads(sf.read_text())
                data = env.get("data", {}) if isinstance(env, dict) else {}
                sprints = data.get("sprints", [])
                milestones = data.get("milestones", [])
                if sprints or milestones:
                    break
            except (OSError, json.JSONDecodeError):
                pass

    # Auto-synthesize stub sprint entries for any sprint ID referenced in plan
    # inventory items that isn't already represented in the sprints list.
    existing_sprint_ids = {s.get("id") for s in sprints if s.get("id")}
    referenced_sprint_ids = {item["sprint"] for item in inventory if item.get("sprint")}
    missing_sprint_ids = referenced_sprint_ids - existing_sprint_ids
    for sid in sorted(missing_sprint_ids):
        sprints.append({
            "id": sid,
            "theme": f"Sprint {sid}",
            "description": "Auto-synthesized from plan inventory",
            "status": "planned",
            "items": [],
        })

    return {"inventory": inventory, "sprints": sprints, "milestones": milestones}


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
  <script src="https://unpkg.com/react@18.3.1/umd/react.development.js" integrity="sha384-hD6/rw4ppMLGNu3tX5cjIb+uRZ7UkRJ6BPkLpg4hAu/6onKUg4lLsHAs9EBPT82L" crossorigin="anonymous"></script>
  <script src="https://unpkg.com/react-dom@18.3.1/umd/react-dom.development.js" integrity="sha384-u6aeetuaXnQ38mYT8rp6sbXaQe3NL9t+IBXmnYxwkUI2Hw4bsp2Wvmx4yRQF1uAm" crossorigin="anonymous"></script>
  <script src="https://unpkg.com/@babel/standalone@7.29.0/babel.min.js" integrity="sha384-m08KidiNqLdpJqLq95G/LEi8Qvjl/xUYll3QILypMoQ65QorJ9Lvtp2RXYGBFj1y" crossorigin="anonymous"></script>
</head>
<body>
  <div id="root"></div>
  <script src="/_ui/state-loader.js"></script>
  <script type="text/babel" src="/_ui/ui.jsx"></script>
  <script type="text/babel" src="/_ui/bits.jsx"></script>
  <script type="text/babel" src="/_ui/decision.jsx"></script>
  <script type="text/babel" src="/_ui/cockpit.jsx"></script>
  <script type="text/babel" src="/_ui/plan.jsx"></script>
  <script type="text/babel" src="/_ui/sprint.jsx"></script>
  <script type="text/babel" src="/_ui/graph.jsx"></script>
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


def _resolve_plan_file(root: Path, slug: str) -> Path | None:
    """Find the HTML file for a plan slug under a project docs root."""
    direct = root / f"{slug}.html"
    if direct.is_file():
        return direct
    for cand in sorted(root.rglob("*.html")):
        rel = cand.relative_to(root)
        if any(part in _NON_PLAN_DIRS for part in rel.parts[:-1]):
            continue
        if cand.name in _NON_PLAN_FILES:
            continue
        if cand.stem == slug:
            return cand
        try:
            if _plan_html.parse_plan(cand)["slug"] == slug:
                return cand
        except Exception:
            pass
    return None


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
                    self._send(HTTPStatus.OK, _HOME_HTML.read_bytes(), ctype or "text/html")
                except OSError as e:
                    self._send(HTTPStatus.INTERNAL_SERVER_ERROR, str(e).encode())
            else:
                self._send(HTTPStatus.OK, render_index_fallback(load_mounts(), self._host, self._port))
            return

        if path.startswith("/_shared/"):
            rel = path[len("/_shared/"):]
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
                self._send(HTTPStatus.OK, target.read_bytes(), ctype or "application/octet-stream")
            except OSError as e:
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, str(e).encode())
            return

        if path.startswith("/_ui/"):
            rel = path[len("/_ui/"):]
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
            rel = path[len("/_projects/"):]
            fname = rel.lstrip("/")
            if not SAFE_NAME.match(fname):
                self._send(HTTPStatus.BAD_REQUEST, b"bad projects filename")
                return
            target = (HOME / "docs-server") / fname
            if not target.is_file():
                self._send(HTTPStatus.NOT_FOUND, b"not found")
                return
            ctype, _ = mimetypes.guess_type(str(target))
            try:
                self._send(HTTPStatus.OK, target.read_bytes(), ctype or "application/octet-stream")
            except OSError as e:
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, str(e).encode())
            return

        if path.startswith("/plan/"):
            # GET /plan/<project>/<slug> — the plan's embedded state island
            # (raw, with version), for clients that need the current version
            # before a write. {} if the plan/island is absent.
            parts = path[len("/plan/"):].strip("/").split("/", 1)
            if len(parts) != 2 or not SAFE_NAME.match(parts[0]):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad path"})
                return
            project, slug = parts
            slug = slug.removesuffix(".html").removesuffix(".json")
            mts = load_mounts()
            if project not in mts:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown project"})
                return
            pf = _resolve_plan_file(mts[project], slug)
            island = _plan_html.read_state(pf.read_text(errors="replace")) if pf else {}
            self._send_json(HTTPStatus.OK, island)
            return

        if path.startswith("/state/"):
            parts = path[len("/state/"):].strip("/").split("/", 1)
            if len(parts) != 2 or not SAFE_NAME.match(parts[0]):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad path"})
                return
            project, doc = parts
            doc_stem = doc.removesuffix(".json")
            if not SAFE_NAME.match(doc_stem):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad doc"})
                return
            state_file = (_STATE_ROOT or Path("/dev/null")) / project / (doc if doc.endswith(".json") else f"{doc}.json")

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
                    try:
                        disc = discover_plans(mts[project], project, _STATE_ROOT)
                        data = dict(envelope.get("data") or {})
                        data["inventory"] = disc.get("inventory", [])
                        if not data.get("sprints") and disc.get("sprints"):
                            data["sprints"] = disc["sprints"]
                        if not data.get("milestones") and disc.get("milestones"):
                            data["milestones"] = disc["milestones"]
                        self._send_json(HTTPStatus.OK, {**envelope, "data": data})
                        return
                    except Exception:
                        pass  # fall through to plain file read
                if not state_file.is_file():
                    self._send_json(HTTPStatus.OK, {})
                    return
                try:
                    self._send(HTTPStatus.OK, state_file.read_bytes(), "application/json")
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
            project = path[len("/_discover/"):].strip("/")
            if not project or not SAFE_NAME.match(project):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad project name"})
                return
            disc_mounts = load_mounts()
            if project not in disc_mounts:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown project"})
                return
            result = discover_plans(disc_mounts[project], project, _STATE_ROOT)
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
        if (len(rel_parts) == 3 and rel_parts[0] == "state"
                and rel_parts[2] == "index.json"
                and SAFE_NAME.match(rel_parts[1])):
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
                try:
                    disc = discover_plans(Path(mts[sub_project]), sub_project, _STATE_ROOT)
                    data = dict(envelope.get("data") or {})
                    data["inventory"] = disc.get("inventory", [])
                    if not data.get("sprints") and disc.get("sprints"):
                        data["sprints"] = disc["sprints"]
                    if not data.get("milestones") and disc.get("milestones"):
                        data["milestones"] = disc["milestones"]
                    self._send_json(HTTPStatus.OK, {**envelope, "data": data})
                    return
                except Exception:
                    pass
            if sf.is_file():
                self._send(HTTPStatus.OK, sf.read_bytes(), "application/json")
            else:
                self._send_json(HTTPStatus.OK, {})
            return

        target = safe_join(root, rel)
        if target is None:
            self._send(HTTPStatus.FORBIDDEN, b"forbidden")
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._send(HTTPStatus.NOT_FOUND, b"not found")
            return

        ctype, _ = mimetypes.guess_type(str(target))
        try:
            self._send(HTTPStatus.OK, target.read_bytes(), ctype or "application/octet-stream")
        except OSError as e:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, str(e).encode())

    def _read_body(self) -> tuple[bool, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_POST_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": f"body > {MAX_POST_BYTES} bytes"})
            return False, None
        raw = self.rfile.read(length) if length else b""
        try:
            return True, (json.loads(raw) if raw else {})
        except json.JSONDecodeError as e:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"json: {e}"})
            return False, None

    def _handle_plan_write(self, path: str) -> None:
        """POST /plan/<project>/<slug> — merge a dotted patch into the plan's
        embedded state island and rewrite the HTML file in place. The plan HTML
        is the sole store; there is no sidecar state JSON.

        Optimistic concurrency: send `If-Match: <version>`; a mismatch returns
        412 with the current island so the client can rebase and retry.
        """
        parts = path[len("/plan/"):].strip("/").split("/", 1)
        if len(parts) != 2 or not SAFE_NAME.match(parts[0]):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad path"})
            return
        project, slug = parts
        slug = slug.removesuffix(".html")
        if not SAFE_NAME.match(slug):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad slug"})
            return
        mounts = load_mounts()
        if project not in mounts:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown project"})
            return
        plan_file = _resolve_plan_file(mounts[project], slug)
        if plan_file is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown plan"})
            return

        ok, patch = self._read_body()
        if not ok:
            return
        if not isinstance(patch, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "patch must be an object"})
            return

        text = plan_file.read_text(encoding="utf-8", errors="replace")
        island = _plan_html.read_state(text)
        cur_version = int(island.get("version", 0) or 0)

        if_match = self.headers.get("If-Match")
        if if_match is None:
            self._send_json(HTTPStatus.PRECONDITION_FAILED, {
                "error": "version_mismatch", "current_version": cur_version,
                "expected_version": None, "current_data": island})
            return
        try:
            expected = int(if_match.strip().strip('"'))
        except (ValueError, TypeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "If-Match must be an integer"})
            return
        if expected != cur_version:
            self._send_json(HTTPStatus.PRECONDITION_FAILED, {
                "error": "version_mismatch", "current_version": cur_version,
                "expected_version": expected, "current_data": island})
            return

        patch.pop("version", None)
        patch.pop("_version", None)
        _patch_into(island, patch)
        island.setdefault("slug", slug)
        island["version"] = cur_version + 1
        island["modified"] = datetime.now().strftime("%Y-%m-%d")

        new_text = _plan_html.write_state(text, island)
        tmp = plan_file.with_suffix(".html.tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(plan_file)
        self._send_json(HTTPStatus.OK, {"ok": True, "slug": slug, "version": island["version"]})

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlsplit(self.path).path)
        if path.startswith("/plan/"):
            self._handle_plan_write(path)
            return
        if not path.startswith("/state/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "POST to /plan/<project>/<slug>"})
            return
        parts = path[len("/state/"):].strip("/").split("/", 1)
        if len(parts) != 2 or not SAFE_NAME.match(parts[0]):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad path"})
            return
        project, doc = parts
        doc_stem = doc.removesuffix(".json")
        if not SAFE_NAME.match(doc_stem):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad doc"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_POST_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": f"body > {MAX_POST_BYTES} bytes"})
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
                cur_data = envelope.get("data", {}) if isinstance(envelope, dict) else {}
                cur_version = int(cur_data.get("_version", 0))
            except (OSError, json.JSONDecodeError, ValueError):
                cur_data = {}
                cur_version = 0

        if_match_raw = self.headers.get("If-Match")
        if if_match_raw is None:
            self._send_json(HTTPStatus.PRECONDITION_FAILED, {
                "error": "version_mismatch",
                "current_version": cur_version,
                "expected_version": None,
                "current_data": cur_data,
            })
            return

        try:
            expected_version = int(if_match_raw.strip().strip('"'))
        except (ValueError, TypeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "If-Match must be an integer"})
            return

        if expected_version != cur_version:
            self._send_json(HTTPStatus.PRECONDITION_FAILED, {
                "error": "version_mismatch",
                "current_version": cur_version,
                "expected_version": expected_version,
                "current_data": cur_data,
            })
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
        self._send_json(HTTPStatus.OK, {"ok": True, "path": str(out_file), "version": new_data["_version"]})


def main(port: int = 8765, host: str | None = None, mounts_file: Path | None = None) -> None:
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
        print(f"  reach from a laptop: ssh -L {_port}:localhost:{_port} <user>@{fqdn}", flush=True)
    print(f"  mounts:  {_MOUNTS_FILE}", flush=True)
    print(f"  state:   {_STATE_ROOT}", flush=True)
    print(f"  shared:  {_SHARED_ROOT}", flush=True)
    ThreadingHTTPServer((_host, _port), Handler).serve_forever()


if __name__ == "__main__":
    main()
