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
  GET /                         → first mounted project's #home route
  GET /_shared/<file>           → docs/_shared/<file> in the reckon repo
                                  (falls back to ~/.claude/skills/html-docs/assets/)
  GET /_projects/index.json     → cross-project rollup
  GET /_projects/<file>         → ~/docs-server/<file>
  GET /crew/<project>/finished[/<plan>]
                                → committed completed runs, newest first
  GET /crew/<project>/routing   → derived cross-ledger routing measurements
  GET /state/<project>/<doc>    → ~/docs-server/state/<project>/<doc>.json
  POST /state/<project>/<doc>   → write the same path (versioned)
  GET /_discover/<project>      → scan docs dir for HTML plan pages (meta tag opt-in)
  GET /_index/<project>         → the persisted metadata rows, nothing derived
  GET /<project>/<relpath>      → mount[project]/<relpath>

POST versioned write contract — see reckon/serve.py for full details.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import html.parser
import io
import json
import logging
import mimetypes
import os
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timezone
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import urlopen

from reckon import (
    _backends,
    _plan_html,
    _store,
    capabilities,
    compliance,
    crew,
    fleet_index,
    ledger,
    metadata_index,
    served_code,
)
from reckon._store import (
    _config_home,
    _mounts_path,
    _state_root,
    write_atomically,
    write_json_atomically,
)
from reckon._timestamps import parse_utc
from reckon.crew.recovery import _PRE_SPAWN_PHASES
from reckon.evidence import (
    EvidenceSynthesisError,
    compose_landed_record,
    evidence_record_plan,
)
from reckon.figures import figure_rows
from reckon.file_memo import memoized
from reckon.lifecycle import (
    effective_status,
    unpassed_gate_blockers,
    unresolved_dependencies,
)
from reckon.project_state import ProjectStateError
from reckon.reader_pdf import (
    FontFace,
    ReaderPdfError,
    ReaderPdfUnavailableError,
    reader_hash,
    render_reader_pdf,
)
from reckon.resources import (
    ROOT_TYPES,
    ResourceCollision,
    composed_provenance,
    resolve_resource,
    resolve_route,
    resource_map,
    resource_scan_scope,
)
from reckon.service import ServiceError, node_executable

HOME = Path.home()
LOGGER = logging.getLogger(__name__)

# ── Configurable paths (set via main() args or env vars) ──────────────────

_MOUNTS_FILE: Path | None = None
_STATE_ROOT: Path | None = None
_SHARED_ROOT: Path | None = None

_INOTIFY_EVENT = struct.Struct("iIII")
_INOTIFY_CHANGE_MASK = 0x00000FCC
_INOTIFY_DIRECTORY = 0x40000000
_CHANGE_SETTLE_S = 0.25
_GZIP_MIN_BYTES = 64 * 1024


class _ProjectChangeWatch:
    """Block on kernel filesystem notifications for one mounted docs tree."""

    def __init__(self, root: Path) -> None:
        import ctypes

        self.root = root.resolve()
        self._libc = ctypes.CDLL(None, use_errno=True)
        initializer = getattr(self._libc, "inotify_init1", None)
        add_watch = getattr(self._libc, "inotify_add_watch", None)
        if initializer is None or add_watch is None:
            raise OSError("filesystem change notifications are unavailable")
        self.fd = initializer(os.O_CLOEXEC)
        if self.fd < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        self._add_watch = add_watch
        self._directories: dict[int, Path] = {}
        try:
            self._watch_tree(self.root)
        except Exception:
            os.close(self.fd)
            raise

    def _watch_tree(self, root: Path) -> None:
        for directory, subdirectories, _files in os.walk(root):
            self._watch(Path(directory))
            subdirectories[:] = [
                name for name in subdirectories if name not in {".git", "node_modules"}
            ]

    def _watch(self, directory: Path) -> None:
        encoded = os.fsencode(directory)
        descriptor = self._add_watch(self.fd, encoded, _INOTIFY_CHANGE_MASK)
        if descriptor < 0:
            import ctypes

            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(directory))
        self._directories[descriptor] = directory

    def consume(self) -> bool:
        """Read pending notifications, arming a watch on any new directory."""
        events = os.read(self.fd, 64 * 1024)
        offset = 0
        while offset + _INOTIFY_EVENT.size <= len(events):
            descriptor, mask, _cookie, name_length = _INOTIFY_EVENT.unpack_from(
                events, offset
            )
            offset += _INOTIFY_EVENT.size
            name = events[offset : offset + name_length].rstrip(b"\0")
            offset += name_length
            if mask & _INOTIFY_DIRECTORY and name:
                candidate = self._directories.get(descriptor, self.root) / os.fsdecode(
                    name
                )
                if candidate.is_dir():
                    self._watch_tree(candidate)
        return True

    def wait(self, connection: socket.socket) -> bool:
        """Return false when the browser disconnects, true on a tree change."""
        readable, _, _ = select.select([self.fd, connection], [], [])
        if connection in readable:
            try:
                if not connection.recv(1, socket.MSG_PEEK):
                    return False
            except (ConnectionError, OSError):
                return False
        if self.fd not in readable:
            return False
        self.consume()
        return True

    def drain(self, settle_s: float) -> None:
        """Consume further events until none arrive for ``settle_s`` seconds."""

        while True:
            readable, _, _ = select.select([self.fd], [], [], settle_s)
            if not readable:
                return
            try:
                os.read(self.fd, 64 * 1024)
            except OSError:
                return

    def close(self) -> None:
        os.close(self.fd)


class _FleetChangeWatch:
    """Watch every mounted docs tree for the life of the served process.

    Each tree's watch is armed on the background reader thread, one tree at a
    time, rather than before the thread starts: arming a tree walks it, and a
    walk of a shared-filesystem tree can take seconds, so arming every tree up
    front keeps the process from binding its port until the last walk is done.
    The reader thread arms a tree and only then selects across the descriptors
    armed so far, and a tree is covered by the discovery reuse window until its
    watch is armed.

    A kernel notification on one tree drops that tree's memoised walk at once,
    so the reuse window never has to hide a change the kernel reported; it only
    backstops writes the kernel does not report, such as a write from another
    login node on the shared filesystem. One thread selects across every tree's
    notification descriptor, so a change to any mounted tree is seen without
    walking anything.
    """

    def __init__(self, trees: Iterable[Path]) -> None:
        self._trees = sorted({Path(candidate).resolve() for candidate in trees})
        self._watches: dict[int, _ProjectChangeWatch] = {}
        self._lock = threading.Lock()
        self._stop_reader, self._stop_writer = os.pipe()
        self._thread: threading.Thread | None = None
        self.running = False

    @property
    def armed_roots(self) -> frozenset[Path]:
        """Return the roots currently covered by an armed watch."""

        with self._lock:
            return frozenset(watch.root for watch in self._watches.values())

    def start(self) -> _FleetChangeWatch:
        """Begin watching every mounted tree; return self for chaining."""
        if self.running:
            return self
        # `running` is set before the thread starts so the arming loop, which
        # reads it to notice a concurrent close, cannot run ahead of it.
        self.running = True
        self._thread = threading.Thread(
            target=self._run, name="reckon-tree-watch", daemon=True
        )
        self._thread.start()
        return self

    def _arm_every_tree(self) -> None:
        """Arm one tree at a time, registering each as its watch is built."""

        for tree in self._trees:
            if not self.running:
                return
            try:
                watch = _ProjectChangeWatch(tree)
            except OSError as exc:
                LOGGER.warning("Not watching %s for changes: %s", tree, exc)
                continue
            with self._lock:
                if not self.running:
                    watch.close()
                    return
                self._watches[watch.fd] = watch

    def _run(self) -> None:
        self._arm_every_tree()
        while self._watches:
            readable, _, _ = select.select([*self._watches, self._stop_reader], [], [])
            if self._stop_reader in readable:
                return
            for descriptor in readable:
                watch = self._watches.get(descriptor)
                if watch is None:
                    continue
                watch.consume()
                # The stamp is taken as the change is observed, before the
                # settle wait, so a discovery another watcher recomputed for
                # the same event is reused rather than recomputed again.
                changed_at = time.monotonic()
                # A save or a merge is a burst of events; let it settle so the
                # burst costs one invalidation rather than one per event.
                watch.drain(_CHANGE_SETTLE_S)
                _invalidate_tree_views(watch.root, changed_at=changed_at)

    def close(self) -> None:
        """Signal the watch thread to stop and release its descriptors."""
        self.running = False
        with contextlib.suppress(OSError):
            os.write(self._stop_writer, b"x")
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        with self._lock:
            for watch in self._watches.values():
                watch.close()
            self._watches.clear()
        for descriptor in (self._stop_reader, self._stop_writer):
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _resolve_paths(mounts_file: Path | None = None) -> None:
    global _MOUNTS_FILE, _STATE_ROOT, _SHARED_ROOT
    _MOUNTS_FILE = mounts_file or _mounts_path()
    _STATE_ROOT = _state_root()
    # Shared assets: prefer reckon repo's own docs/_shared, fall back to dotfiles.
    repo_shared = Path(__file__).parent.parent / "docs" / "_shared"
    dotfiles_shared = HOME / ".claude" / "skills" / "html-docs" / "assets"
    _SHARED_ROOT = repo_shared if repo_shared.is_dir() else dotfiles_shared


SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_POST_BYTES = 1_000_000
CREW_LOG_TAIL_BYTES = 64 * 1024

# The long edge a served figure thumbnail is downscaled to, in pixels. A list
# row draws a figure at 50 px, so 200 leaves it sharp on a 2x display while
# costing a fraction of the capture's bytes.
THUMB_MAX_EDGE = 200

# The window the velocity route measures when the caller names none at all, so
# the SPA's first render needs no date arithmetic of its own. A caller that
# names a window, even partially, is answered for the window it named.
VELOCITY_DEFAULT_WINDOW_DAYS = 14


def _velocity_default_window() -> tuple[str, str]:
    """The default velocity window: the last ``VELOCITY_DEFAULT_WINDOW_DAYS``."""

    def clock(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")

    end = time.time()
    return clock(end - VELOCITY_DEFAULT_WINDOW_DAYS * 86400), clock(end)


CLIENT_ASSETS = {
    "babel.js": (
        "https://unpkg.com/@babel/standalone@7.29.0/babel.min.js",
        "2623a9e22809915ce789b4461154e277ddce520d5a4320c14d44332a5d0dcea0",
    ),
    "react.js": (
        "https://unpkg.com/react@18.3.1/umd/react.production.min.js",
        "d949f1c3687aedadcedac85261865f29b17cd273997e7f6b2bfc53b2f9d4c4dd",
    ),
    "react-dom.js": (
        "https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js",
        "35f4f974f4b2bcd44da73963347f8952e341f83909e4498227d4e26b98f66f0d",
    ),
}
_GEIST = "https://unpkg.com/geist@1.7.2/dist/fonts"
_STIX_MATH = "https://unpkg.com/@fontsource/stix-two-math@5.3.0/files"
# The typefaces the reader's stylesheets name first, at the weights and styles
# they set, and a math face for MathML. The PDF renderer runs on the serving
# host, which may have none of them. Static faces, because Chromium embeds a
# variable font in a PDF as outlines without their own advances, and the
# spacing breaks.
READER_PDF_FACES = (
    # (family, weight, style, url, sha256)
    (
        "Geist",
        400,
        "normal",
        f"{_GEIST}/geist-sans/Geist-Regular.woff2",
        "d8bce822db092746889bcf3f57350b41f53708b025458fe7af30729ec4ce0df2",
    ),
    (
        "Geist",
        400,
        "italic",
        f"{_GEIST}/geist-sans/Geist-Italic.woff2",
        "15a1e65b88bdf22469784aed5fca115f17061bab4c3c85cfe7c07839c46a31b4",
    ),
    (
        "Geist",
        500,
        "normal",
        f"{_GEIST}/geist-sans/Geist-Medium.woff2",
        "b0a0867cda44efef4529a4b13ce37fd9fd6e1597708615287542a51bc7452ab4",
    ),
    (
        "Geist",
        600,
        "normal",
        f"{_GEIST}/geist-sans/Geist-SemiBold.woff2",
        "b1e6a1dd2122485d0a1f3a8d30a45443aa9453224f83018bec35f8266bc77915",
    ),
    (
        "Geist",
        700,
        "normal",
        f"{_GEIST}/geist-sans/Geist-Bold.woff2",
        "04f948593dca628e846e6b41b3ef66bc39ad59fee3571c589a3cd4e267122be2",
    ),
    (
        "Geist Mono",
        400,
        "normal",
        f"{_GEIST}/geist-mono/GeistMono-Regular.woff2",
        "e4507fb4fb5f832fbbb6c06aea4206274ba3083007f23fa8cbc0e87a10acf95b",
    ),
    (
        "Geist Mono",
        500,
        "normal",
        f"{_GEIST}/geist-mono/GeistMono-Medium.woff2",
        "85b99e603f84a47dc8118b5af058ad8f387d7c507a00faeef1b5b60eb371e844",
    ),
    (
        "Geist Mono",
        600,
        "normal",
        f"{_GEIST}/geist-mono/GeistMono-SemiBold.woff2",
        "8416445afd947018ffeb31844da808bd7f4356f5dc7d73a084659c32eae26548",
    ),
    (
        "Geist Mono",
        700,
        "normal",
        f"{_GEIST}/geist-mono/GeistMono-Bold.woff2",
        "c1287452c531c82457793da41dd5512af95d01fbc851c8b928203f9ca4967279",
    ),
    (
        "STIX Two Math",
        400,
        "normal",
        f"{_STIX_MATH}/stix-two-math-latin-400-normal.woff2",
        "8b2a6834cfe1f4e0f3724ffa59fe92000013fc5a4c41e6a3f438006c26c60f0d",
    ),
)
CLIENT_ASSETS.update(
    {url.rsplit("/", 1)[1]: (url, digest) for *_, url, digest in READER_PDF_FACES}
)
READER_PDF_TYPES = frozenset({"plan", "research", "evidence"})
_CLIENT_ASSET_LOCK = threading.Lock()


class ClientAssetError(RuntimeError):
    """A pinned client runtime or JSX transformation could not be produced."""


def _client_cache_root() -> Path:
    return _store.cache_root("client")


def _client_asset(name: str) -> Path:
    """Return one content-verified local runtime asset, downloading it once."""
    if name not in CLIENT_ASSETS:
        raise ClientAssetError(f"unknown client asset: {name}")
    url, expected = CLIENT_ASSETS[name]
    destination = _client_cache_root() / name
    with _CLIENT_ASSET_LOCK:
        if destination.is_file():
            payload = destination.read_bytes()
            if hashlib.sha256(payload).hexdigest() == expected:
                return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urlopen(url, timeout=30) as response:  # noqa: S310
                payload = response.read()
        except OSError as exc:
            raise ClientAssetError(
                f"could not cache pinned client asset {name} from {url}: {exc}"
            ) from exc
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected:
            raise ClientAssetError(
                f"client asset {name} digest mismatch: expected {expected}, got {actual}"
            )
        write_atomically(
            destination,
            lambda handle: handle.write(payload),
            fsync=False,
            mode=0o600,
            binary=True,
        )
    return destination


def client_runtime_assets() -> dict[str, bytes]:
    """Return the production browser runtimes that entry points serve locally."""
    return {
        name: _client_asset(name).read_bytes() for name in ("react.js", "react-dom.js")
    }


def compile_jsx(source: str, *, filename: str) -> bytes:
    """Compile JSX through the pinned server-side compiler and cache by content."""
    digest = hashlib.sha256(
        b"scope-isolated-script\0" + filename.encode() + b"\0" + source.encode()
    ).hexdigest()
    destination = _client_cache_root() / "compiled" / f"{digest}.js"
    if destination.is_file():
        return destination.read_bytes()
    compiler = _client_asset("babel.js")
    script = """
const Babel = require(process.argv[1]);
const filename = process.argv[2];
let source = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", chunk => source += chunk);
process.stdin.on("end", () => {
  const result = Babel.transform(source, {
    filename,
    presets: [["react", {runtime: "classic"}]],
    sourceType: "script",
  });
  process.stdout.write(result.code);
});
"""
    try:
        node = node_executable()
        result = subprocess.run(
            [str(node), "-e", script, str(compiler), filename],
            input=source,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, ServiceError, subprocess.TimeoutExpired) as exc:
        raise ClientAssetError(f"could not compile {filename}: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip() or f"node exited {result.returncode}"
        raise ClientAssetError(f"could not compile {filename}: {detail}")
    payload = (
        "(function () {\n"
        + result.stdout
        + "\n}).call(window);\n"
        + f"//# sourceURL={filename}\n"
    ).encode()
    with _CLIENT_ASSET_LOCK:
        if not destination.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            write_atomically(
                destination,
                lambda handle: handle.write(payload),
                fsync=False,
                mode=0o600,
                binary=True,
            )
    return payload


def _ui_root() -> Path:
    configured = os.environ.get("RECKON_UI_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).parent.parent / "docs" / "ui"


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
    """Return the log stamp and age, with a tail only for unresolved phases."""
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
    phase = str(pointer.get("phase") or "")
    return (
        stamp,
        age,
        _read_log_tail(path) if not phase or phase in _PRE_SPAWN_PHASES else [],
    )


def _elapsed_since(stamp: object) -> int | None:
    """Return whole elapsed seconds from an ISO timestamp, when available."""
    if not stamp or not isinstance(stamp, str):
        return None
    if stamp != stamp.strip() or stamp.endswith("z"):
        return None
    started = parse_utc(stamp)
    if started is None:
        return None
    return max(0, int((datetime.now(tz=timezone.utc) - started).total_seconds()))


def _read_crew_plan_details(plan_path: Path) -> tuple[str, float | None]:
    with plan_path.open(encoding="utf-8") as source:
        head = re.split(
            r"</head\s*>", source.read(16384), maxsplit=1, flags=re.IGNORECASE
        )[0]
    metadata = _plan_html.read_state(head)
    if metadata.get("effort_hours") is None or not metadata.get("sprint"):
        metadata = _plan_html.parse_meta(plan_path)
    return str(metadata.get("sprint") or ""), metadata.get("effort_hours")


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
        try:
            return memoized(
                "crew-plan-details",
                plan_path,
                partial(_read_crew_plan_details, plan_path),
            )
        except FileNotFoundError:
            continue
        except (OSError, ValueError, UnicodeError):
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
    referenced_projects = {str(pointer.get("project") or "") for pointer in pointers}
    roster_by_project: dict[str, dict[str, dict]] = {}
    for name in referenced_projects:
        docs = mounts[name]
        try:
            roster, _version = ledger.load(name, docs.parent, headers_only=True)
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
        # A recorded phase past launch is already the classifier's observed
        # phase. Only a pre-spawn label needs its evidence read again: the
        # worker may have started while the launcher still says starting.
        phase = str(pointer.get("phase") or "")
        if phase in _PRE_SPAWN_PHASES or not phase:
            phase = str(crew.classify_pointer(pointer).get("phase") or "")
        if not phase:
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
            if not value or value != value.strip() or value.endswith("z"):
                continue
            parsed = parse_utc(value)
            if parsed is None:
                continue
            return parsed
        return datetime.min.replace(tzinfo=timezone.utc)

    return sorted(records, key=completion_key, reverse=True)


class PlanPageRefusalError(ValueError):
    """A plan page the parser refuses, carrying the file and the refusal text.

    ``_plan_html`` raises a bare ``ValueError`` when a document violates the
    record contract, and the message names the offending record rather than the
    file. Wrapping it where the page is read carries the file too, so one
    project's refused page is reported against that project instead of taking
    down every other project's row.
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path.name}: {reason}")
        self.path = path


# A project row's own failures, isolated to that row so the rollup still serves
# the other projects.
_PROJECT_ROW_ISOLATED_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    ProjectStateError,
    PlanPageRefusalError,
)


def collect_projects(mounts: dict[str, Path]) -> dict:
    from concurrent.futures import ThreadPoolExecutor

    def project_entry(name: str, path: Path) -> dict:
        proj: dict = {"project": name, "path": str(path)}
        try:
            row = fleet_index.compute_project_row(path, name, state_root=_STATE_ROOT)
            proj["data"] = {"projects": [row]}
        except _PROJECT_ROW_ISOLATED_ERRORS as e:
            proj["error"] = str(e)
            proj["data"] = {}
        return proj

    # Each row is bound by filesystem and git latency rather than CPU, so the
    # projects are computed concurrently; the response keeps mount order.
    ordered = sorted(mounts.items())
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(ordered)))) as pool:
        out = list(pool.map(lambda item: project_entry(*item), ordered))
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
    from reckon.file_memo import memoized

    return memoized("head_meta", path, lambda: _read_head_meta_uncached(path))


def _read_head_meta_uncached(path: Path) -> tuple[str, dict[str, str]]:
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
    computed_at: float = 0.0


@dataclass(frozen=True)
class _GitCreationEntry:
    head: str
    times: dict[str, int]


_DISC_CACHE: dict[tuple[str, str], _DiscoveryCacheEntry] = {}
# One discovery per project at a time: a page load asks for the same project
# from several requests at once, and concurrent cold scans of one tree multiply
# the filesystem load instead of sharing the result.
_DISC_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_DISC_LOCKS_GUARD = threading.Lock()
# Validating a cached discovery walks the whole docs tree, which costs seconds
# on a shared filesystem and is repeated by every request of one page load. The
# served process reuses a walk for this many seconds; a write through the
# server and a kernel notification for a watched tree both drop the reuse
# immediately, so the window only backstops writes the kernel does not report —
# notably a write from another login node on the shared filesystem. Zero, the
# library default, walks on every call.
_SIGNATURE_TTL_S = 0.0
# The package source the served process started with, recorded by main() so
# /_server can report when the code on disk has moved past the code running.
_SOURCE_SNAPSHOT: served_code.SourceSnapshot | None = None
# Which of those files the served process has run since start; the report counts
# only changes to these, because a file the server never runs cannot change
# what it serves.
_EXECUTED_SOURCE: served_code.ExecutedSource | None = None
# Document verdicts are computed out of process (see _start_check_refresh). Only
# the served process opts in, in main(); a handler driven by a test or another
# caller reports pending verdicts and starts nothing.
_CHECK_REFRESH_ENABLED = False
_CHECK_REFRESHES: dict[str, subprocess.Popen] = {}
_CHECK_REFRESH_LOCK = threading.Lock()
_SIGNATURE_MEMO: dict[tuple[str, str, str], tuple[float, tuple[int, int]]] = {}
_SIGNATURE_MEMO_LOCK = threading.Lock()
# The served process opts into the longer window; the library default stays 0
# so a caller that never starts the fleet watch never reads a stale walk.
_SERVED_SIGNATURE_TTL_S = 60.0


def _served_code_report() -> dict | None:
    """Use the same executed-source report for requests and the status route."""

    if _SOURCE_SNAPSHOT is None:
        return None
    executed = None
    if _EXECUTED_SOURCE is not None:
        definitions = getattr(_EXECUTED_SOURCE, "definitions", None)
        executed = (
            definitions()
            if callable(definitions)
            else _EXECUTED_SOURCE.relative_paths()
        )
    return served_code.served_report(_SOURCE_SNAPSHOT, executed)


def _code_fingerprints(
    snapshot: served_code.SourceSnapshot, report: Mapping[str, object]
) -> tuple[str, str]:
    """Fingerprint exactly the source files the executed-code report counted."""

    names = sorted(set(report["changed"] + report["added"] + report["removed"]))
    running = hashlib.sha256()
    disk = hashlib.sha256()
    for name in names:
        known = snapshot.files.get(name)
        before = known[1] if known is not None else "missing"
        try:
            after = hashlib.sha256((snapshot.root / name).read_bytes()).hexdigest()
        except OSError:
            after = "missing"
        running.update(f"{name}:{before}\n".encode())
        disk.update(f"{name}:{after}\n".encode())
    return running.hexdigest(), disk.hexdigest()


class _CodeReload:
    """Stop the listener, drain active handlers briefly, then replace the image."""

    def __init__(self, server: ThreadingHTTPServer, exec_=os.execv) -> None:
        self.server = server
        self.exec_ = exec_
        self.requested = False
        self._lock = threading.Lock()
        self._idle = threading.Condition()
        self._active = 0
        self._process_request = server.process_request
        self._process_request_thread = server.process_request_thread
        self._verify_request = server.verify_request
        server.process_request = self._tracked_request
        server.process_request_thread = self._tracked_thread
        server.verify_request = self._verify
        server._code_reload = self

    def _verify(self, request: socket.socket, address: tuple) -> bool:
        return not self.requested and self._verify_request(request, address)

    def _tracked_request(self, request: socket.socket, address: tuple) -> None:
        with self._idle:
            self._active += 1
        try:
            self._process_request(request, address)
        except BaseException:
            with self._idle:
                self._active -= 1
                self._idle.notify_all()
            raise

    def _tracked_thread(self, request: socket.socket, address: tuple) -> None:
        try:
            self._process_request_thread(request, address)
        finally:
            with self._idle:
                self._active -= 1
                self._idle.notify_all()

    def request_reload(self) -> None:
        with self._lock:
            if self.requested:
                return
            self.requested = True
        self.server.server_close()
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def finish(self) -> None:
        if not self.requested:
            return
        with self._idle:
            self._idle.wait_for(lambda: self._active == 0, timeout=3)
        self.server.server_close()
        argv = [sys.executable, *sys.orig_argv[1:]]
        self.exec_(sys.executable, argv)


_FLEET_WATCH: _FleetChangeWatch | None = None
_GIT_CREATION_CACHE: dict[tuple[str, str], _GitCreationEntry] = {}
_GIT_CREATION_SCHEMA = "reckon.git-creation-map"
_GIT_CREATION_SCHEMA_VERSION = 1


def _git_creation_cache_path(cache_key: tuple[str, str]) -> Path:
    """Return the disposable persisted-map path for one repository docs root."""

    identity = "\0".join(cache_key).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return _config_home() / "cache" / "git-creation" / f"{digest}.json"


def _git_creation_payload(cache_key: tuple[str, str], entry: _GitCreationEntry) -> dict:
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
    try:
        write_json_atomically(
            path,
            _git_creation_payload(cache_key, entry),
            indent=2,
            sort_keys=True,
            fsync=False,
            mode=0o600,
        )
    except OSError as exc:
        LOGGER.warning("Could not persist Git creation cache %s: %s", path, exc)


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


_GIT_LAST_MODIFIED_CACHE: dict[tuple[str, str], _GitCreationEntry] = {}
_GIT_LAST_MODIFIED_SCHEMA = "reckon.git-last-modified-map"
_GIT_LAST_MODIFIED_SCHEMA_VERSION = 1


def _git_last_modified_cache_path(cache_key: tuple[str, str]) -> Path:
    identity = "\0".join(cache_key).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return _config_home() / "cache" / "git-last-modified" / f"{digest}.json"


def _git_last_modified_payload(
    cache_key: tuple[str, str], entry: _GitCreationEntry
) -> dict:
    repo, rel_docs = cache_key
    core = {
        "schema": _GIT_LAST_MODIFIED_SCHEMA,
        "version": _GIT_LAST_MODIFIED_SCHEMA_VERSION,
        "repository": repo,
        "docs": rel_docs,
        "head": entry.head,
        "times": entry.times,
    }
    checksum = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {**core, "checksum": checksum}


def _load_git_last_modified_cache(
    cache_key: tuple[str, str],
) -> _GitCreationEntry | None:
    """Load a complete, current-schema persisted map or decline it entirely."""

    if not (Path(cache_key[0]) / ".git").exists():
        return None
    path = _git_last_modified_cache_path(cache_key)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Ignoring unreadable Git last-modified cache %s: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        LOGGER.warning("Ignoring invalid Git last-modified cache %s", path)
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
        raw.get("schema") == _GIT_LAST_MODIFIED_SCHEMA
        and raw.get("version") == _GIT_LAST_MODIFIED_SCHEMA_VERSION
        and raw.get("repository") == cache_key[0]
        and raw.get("docs") == cache_key[1]
        and isinstance(raw.get("head"), str)
        and bool(raw["head"])
    )
    if not valid_times or not valid_identity:
        LOGGER.warning("Ignoring incompatible Git last-modified cache %s", path)
        return None

    core = {key: raw[key] for key in raw if key != "checksum"}
    checksum = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if raw.get("checksum") != checksum:
        LOGGER.warning("Ignoring corrupt Git last-modified cache %s", path)
        return None
    return _GitCreationEntry(head=raw["head"], times=dict(times))


def _store_git_last_modified_cache(
    cache_key: tuple[str, str], entry: _GitCreationEntry
) -> None:
    """Atomically persist one validated last-modified map without affecting service."""

    if not (Path(cache_key[0]) / ".git").exists():
        return
    path = _git_last_modified_cache_path(cache_key)
    try:
        write_json_atomically(
            path,
            _git_last_modified_payload(cache_key, entry),
            indent=2,
            sort_keys=True,
            fsync=False,
            mode=0o600,
        )
    except OSError as exc:
        LOGGER.warning("Could not persist Git last-modified cache %s: %s", path, exc)


def _parse_last_committed(output: str) -> dict[str, int]:
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
            # Git emits newest commits first, so the first time a path
            # appears is its most recently touching commit.
            times.setdefault(line, timestamp)
    return times


def _git_last_committed(repo_dir: Path, docs_dir: Path) -> dict[str, int]:
    """Return {repo-relative-path: unix_ts} for the most recent commit touching each file."""
    try:
        rel_docs = str(docs_dir.relative_to(repo_dir))
    except ValueError:
        LOGGER.warning("Docs directory %s is outside repository %s", docs_dir, repo_dir)
        return {}

    cache_key = (str(repo_dir.resolve()), rel_docs)
    cached = _GIT_LAST_MODIFIED_CACHE.get(cache_key)
    if cached is None:
        cached = _load_git_last_modified_cache(cache_key)
        if cached is not None:
            _GIT_LAST_MODIFIED_CACHE[cache_key] = cached
    head = _git_head(repo_dir)
    if head is None:
        return dict(cached.times) if cached else {}
    if cached and cached.head == head:
        return dict(cached.times)

    args = ["git", "log"]
    if cached:
        args.append(f"{cached.head}..{head}")
    args.extend(["--format=COMMIT %at", "--name-only", "--", rel_docs])
    result = _run_git(args, repo_dir, operation="last-modified lookup")
    if result is None:
        return dict(cached.times) if cached else {}

    # Commits in the new range are the freshest touch for any path they name;
    # only paths untouched since the cached head keep their cached time.
    times = _parse_last_committed(result.stdout)
    if cached:
        for path, timestamp in cached.times.items():
            times.setdefault(path, timestamp)
    entry = _GitCreationEntry(head=head, times=times)
    _GIT_LAST_MODIFIED_CACHE[cache_key] = entry
    _store_git_last_modified_cache(cache_key, entry)
    return dict(times)


def _row_times(
    path: Path,
    repo_dir: Path,
    git_times: dict[str, int],
    git_last_times: dict[str, int],
) -> tuple[int, str]:
    """Return (created_unix_ts, edited_iso) for one file in a project's docs tree.

    Delegates to the shared stamp rule so the discovery payload and the
    persisted metadata index derive one document's timestamps the same way.
    """

    return metadata_index.stamps_for(path, repo_dir, git_times, git_last_times)


def _served_index_rows(docs_dir: Path, project: str) -> list[dict]:
    """Return the metadata rows ``/_index/<project>`` serves.

    One call shape for the endpoint and for the change stream that pushes
    those rows: the repository and its commit times are always supplied, so
    the index's stamps follow the same rule the discovery payload uses and a
    reader merging the two never sees a document's timestamps move.
    """

    repo_dir = docs_dir.parent
    return metadata_index.index_rows(
        docs_dir,
        project,
        repo_dir=repo_dir,
        git_first=_git_first_committed(repo_dir, docs_dir),
        git_last=_git_last_committed(repo_dir, docs_dir),
    )


def _index_row_identity(row: Mapping[str, object]) -> tuple[str, str]:
    """Return one row's identity: its document type and slug."""

    return (str(row.get("type") or ""), str(row.get("slug") or ""))


def _index_row_diff(
    previous: Mapping[tuple[str, str], dict], rows: list[dict]
) -> dict[str, list[dict]]:
    """Return the rows that changed, were added, or were removed.

    A row's identity is its type and slug, so a file rewritten in place is a
    change carrying the same identity, a new file is an addition, and a file
    that left the tree is a removal carrying the row the stream held.
    """

    current = {_index_row_identity(row): row for row in rows}
    return {
        "changed": [
            row
            for identity, row in current.items()
            if identity in previous and previous[identity] != row
        ],
        "added": [row for identity, row in current.items() if identity not in previous],
        "removed": [
            row for identity, row in previous.items() if identity not in current
        ],
    }


def _discovery_signature(
    docs_dir: Path, project: str, state_root: Path | None
) -> tuple[int, int]:
    """Return (file count, newest mtime) over the files discovery reads."""

    key = (str(docs_dir), project, str(state_root))
    if _SIGNATURE_TTL_S > 0:
        now = time.monotonic()
        with _SIGNATURE_MEMO_LOCK:
            memo = _SIGNATURE_MEMO.get(key)
        if memo is not None and now - memo[0] < _SIGNATURE_TTL_S:
            return memo[1]
    signature = _walk_discovery_signature(docs_dir, project, state_root)
    if _SIGNATURE_TTL_S > 0:
        with _SIGNATURE_MEMO_LOCK:
            _SIGNATURE_MEMO[key] = (time.monotonic(), signature)
    return signature


def _invalidate_discovery_signatures(docs_dir: Path | None = None) -> None:
    """Forget memoised tree walks — all of them, or one docs tree's."""

    with _SIGNATURE_MEMO_LOCK:
        if docs_dir is None:
            _SIGNATURE_MEMO.clear()
            return
        root = str(docs_dir)
        for key in [key for key in _SIGNATURE_MEMO if key[0] == root]:
            del _SIGNATURE_MEMO[key]


def _walk_discovery_signature(
    docs_dir: Path, project: str, state_root: Path | None
) -> tuple[int, int]:
    # The docs-tree walk is shared with the metadata index: every HTML file
    # anywhere, figure images under the top-level figures directory, directory
    # symlinks not followed. One implementation, so the index's covered set and
    # discovery's counted set cannot drift apart.
    count = 0
    newest = 0
    for _relative, path in metadata_index._covered_files(docs_dir):
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            continue
        count += 1
        newest = max(newest, mtime)
    for path in (
        docs_dir / ".reckon" / "project-state-migration.json",
        docs_dir / "state" / project / "project.json",
        (
            state_root / project / "index.json"
            if state_root is not None
            else docs_dir / "state" / project / "index.json"
        ),
    ):
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            continue
        if path.is_file():
            count += 1
            newest = max(newest, mtime)
    return count, newest


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
        computed_at=time.monotonic(),
    )
    return result


def _invalidate_discovery_tree(docs_dir: Path, changed_at: float | None = None) -> None:
    """Forget cached discoveries for one docs tree, whatever the project.

    A tree's change notification names the tree, not the project whose page a
    reader happened to be looking at, so every project mounted on that tree is
    dropped together. A caller that names the moment it observed the change
    keeps a discovery computed after it: every watch reports the same
    filesystem event, and the recomputation the first one paid for is the
    answer the others want rather than one each repeats.
    """

    root = str(Path(docs_dir).resolve())
    for key in [key for key in _DISC_CACHE if key[1] == root]:
        entry = _DISC_CACHE[key]
        if changed_at is not None and entry.computed_at >= changed_at:
            continue
        _DISC_CACHE.pop(key, None)


def _invalidate_tree_views(root: Path, changed_at: float | None = None) -> None:
    """Drop every derived view of one docs tree after a reported change.

    One entry point for every watch: the discovery reuse window, the cached
    discoveries and the in-process metadata index all key off the same tree, so
    a change to any one of them drops all three together. The reuse window and
    the index rows go on every reported change — a reader must never be handed
    a row list that predates it — while the cached discoveries are dropped only
    when they predate the reporting watch's own observation, so N open pages
    pay for one recomputation per change.
    """

    _invalidate_discovery_signatures(root)
    _invalidate_discovery_tree(root, changed_at=changed_at)
    metadata_index.invalidate_tree(root)


def _discovery_lock(cache_key: tuple[str, str]) -> threading.Lock:
    with _DISC_LOCKS_GUARD:
        lock = _DISC_LOCKS.get(cache_key)
        if lock is None:
            lock = _DISC_LOCKS[cache_key] = threading.Lock()
        return lock


def _fresh_discovery(
    cache_key: tuple[str, str],
    docs_dir: Path,
    project: str,
    state_root: Path | None,
) -> tuple[tuple[int, int], dict | None]:
    sig = _discovery_signature(docs_dir, project, state_root)
    cached = _DISC_CACHE.get(cache_key)
    if cached and cached.local_signature == sig:
        external_signatures = _external_project_signatures(
            cached.external_projects, state_root
        )
        if external_signatures == cached.external_signatures:
            return sig, cached.result
    return sig, None


def _attach_discovery_provenance(result: dict, docs_dir: Path) -> dict:
    result["provenance"] = composed_provenance(docs_dir.parent, result)
    return result


#: Relation edges ``build_roadmap`` reads off an inventory row. Every row
#: builder takes these keys from one tuple: an edge a plan declares but a row
#: omits leaves the roadmap blind to it while nothing errors, which is how two
#: hand-maintained key lists drift apart.
EDGE_ROW_FIELDS = ("depends_on", "after", "blocks")


def edge_row(rec: Mapping[str, object]) -> dict[str, list]:
    """Return the roadmap relation edges one plan record declares, as lists."""
    return {field: list(rec.get(field) or []) for field in EDGE_ROW_FIELDS}


def discover_plans(docs_dir: Path, project: str, state_root: Path | None) -> dict:
    """Return {inventory, sprints, milestones} by scanning HTML doc pages.

    Any HTML file under docs_dir (outside infra dirs/files) is a doc; meta tags
    enrich it. Results are cached per project against a cheap (count, max-mtime)
    signature so an unchanged docs tree returns instantly.
    """
    cache_key = (project, str(docs_dir.resolve()))
    _, result = _fresh_discovery(cache_key, docs_dir, project, state_root)
    if result is not None:
        return result
    with _discovery_lock(cache_key):
        # A concurrent request may have finished the same scan while this one
        # waited; reuse it rather than scanning the tree a second time.
        sig, result = _fresh_discovery(cache_key, docs_dir, project, state_root)
        if result is not None:
            return result
        with resource_scan_scope():
            return _discover_plans_uncached(
                docs_dir, project, state_root, sig, cache_key
            )


def _discover_plans_uncached(
    docs_dir: Path,
    project: str,
    state_root: Path | None,
    sig: tuple[int, int],
    cache_key: tuple[str, str],
) -> dict:
    # Batch git first-commit lookup — gives true creation time for tracked files.
    # Falls back to inode ctime when a file is untracked or git is unavailable.
    repo_dir = docs_dir.parent
    git_times = _git_first_committed(repo_dir, docs_dir)
    git_last_times = _git_last_committed(repo_dir, docs_dir)

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

        # Scalar inventory stays on the lightweight meta path. Gates, decisions,
        # and open followups are the body state needed to explain roadmap
        # readiness. The SPA fetches the remaining full state per document.
        rec = _plan_html.parse_meta(html_file)
        gates, decisions, followups = _read_readiness_state(html_file)
        slug = resource.slug
        artifact_type = resource.type
        created, edited = _row_times(html_file, repo_dir, git_times, git_last_times)
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
            # A document saves as a print of its reader, rendered on request
            # at its canonical route with a .pdf suffix.
            "download": f"{resource.canonical_href}.pdf",
            "legacy": resource.legacy,
            "title": rec["title"],
            "type": artifact_type,
            "summary": rec.get("summary", ""),
            "owner": rec.get("owner", ""),
            "last": rec.get("modified", ""),
            "created": created,
            "edited": edited,
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
                    "graph_handle": rec.get("graph_handle") or None,
                    "impl": rec["impl"],
                    "dec_open": rec["dec_open"],
                    "decisions": decisions,
                    "followups": followups,
                    "blockers": rec["blockers"],
                    "gates": gates,
                    **edge_row(rec),
                }
            )
            if rec.get("north_star"):
                item["north_star"] = rec["north_star"]
        inventory.append(item)

    # Figure rows are appended after the typed resources; figures are
    # infrastructure to resource_map, so they never collide with a plan slug.
    plan_slugs = {item["slug"] for item in inventory if item.get("type") == "plan"}
    for figure in figure_rows(docs_dir, project, plan_slugs):
        figure_path = figure.pop("path")
        figure["created"], figure["edited"] = _row_times(
            figure_path, repo_dir, git_times, git_last_times
        )
        inventory.append(figure)

    # ── Distributed project state ──────────────────────────────────────────
    from reckon.project_state import compose_project_state, project_state_mode

    mode = project_state_mode(docs_dir)
    if mode.format == "distributed":
        composed = compose_project_state(docs_dir, project)
        inventory, sprints = _derive_lifecycle(
            project,
            inventory,
            composed.get("sprints", []),
            composed.get("blockers", []),
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
        _attach_composed_review(result, docs_dir, project)
        _attach_ready_set(result, project, docs_dir=docs_dir)
        return _cache_discovery_result(
            cache_key,
            sig,
            project,
            state_root,
            _attach_discovery_provenance(result, docs_dir),
        )

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

    inventory, sprints = _derive_lifecycle(project, inventory, sprints, blockers)
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
    _attach_ready_set(result, project, docs_dir=docs_dir)
    return _cache_discovery_result(
        cache_key,
        sig,
        project,
        state_root,
        _attach_discovery_provenance(result, docs_dir),
    )


def _attach_composed_review(result: dict, docs_dir: Path, project: str) -> None:
    """Attach the optional stored review after joining its live subjects."""

    from reckon.mcp_views import load_composed_review

    review, version = load_composed_review(
        docs_dir,
        project,
        result.get("inventory", []),
        result.get("sprints", []),
        result,
    )
    if review is None or version is None:
        return
    result["review"] = review
    result.setdefault("resource_versions", {})["review:review"] = version


def _attach_ready_set(
    result: dict, project: str, *, docs_dir: Path | None = None
) -> None:
    """Attach the HTTP projection of roadmap-owned readiness and sprint state.

    ``docs_dir`` names the checkout ``result`` was inventoried from, and is
    passed through to the roadmap so its wiring scan reads declarations from
    the same tree as the rows it judges.
    """

    from reckon.mcp_views import ready_set_view
    from reckon.roadmap import build_roadmap

    roadmap = build_roadmap(
        project,
        result.get("inventory", []),
        result.get("sprints", []),
        active_sprint_id=result.get("active_sprint_id"),
        project_manifest=result,
        review=result.get("review") or {},
        docs_dir=docs_dir,
    )
    projection = ready_set_view(roadmap)
    result["ready_set"] = projection
    result["endpoints"] = projection.get("endpoints", [])

    state_by_ref = {
        str(row.get("ref") or row.get("id")): row
        for row in projection.get("sprints", [])
        if isinstance(row, dict) and (row.get("ref") or row.get("id"))
    }
    for sprint in result.get("sprints", []):
        if not isinstance(sprint, dict):
            continue
        sprint_ref = str(sprint.get("_ref") or sprint.get("id") or "")
        state = state_by_ref.get(sprint_ref)
        if state is None:
            continue
        sprint.update(
            {key: value for key, value in state.items() if key not in {"id", "ref"}}
        )
        if "state_drift" not in state:
            sprint.pop("state_drift", None)


def _attach_schedule(
    result: dict, project: str, mounts: dict[str, Path], reference: datetime
) -> dict:
    """Return the discovery payload with a schedule anchored at ``reference``.

    The schedule derives from the same inventory and live-run rows the crew
    route serves, so the flow a browser draws and the reader an agent calls
    answer from one computation. It is attached per request rather than cached
    with the discovery result, because its bar positions are relative to
    ``reference`` and would otherwise freeze at the cache fill. ``reference``
    is caller-supplied and required; the derivation itself never reads the
    wall clock.
    """

    from reckon.roadmap import schedule_report

    payload = dict(result)
    payload["schedule"] = schedule_report(
        project,
        result.get("inventory") or [],
        _crew_rows(mounts, project),
        reference=reference,
    )
    return payload


def _read_readiness_state(
    path: Path,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Read named gate and decision state used to explain readiness."""

    from reckon.file_memo import memoized

    return memoized("readiness_state", path, lambda: _read_readiness_uncached(path))


def _read_readiness_uncached(
    path: Path,
) -> tuple[list[dict], list[dict], list[dict]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], [], []
    has_gates = 'data-reckon="gates"' in text or "data-reckon='gates'" in text
    try:
        state = _plan_html.read_state(text)
    except ValueError as exc:
        raise PlanPageRefusalError(path, str(exc)) from exc
    gates = list(state.get("gates") or []) if has_gates else []
    decisions = state.get("decisions") or {}
    decision_rows = [
        {"key": key, **(decision if isinstance(decision, dict) else {})}
        for key, decision in decisions.items()
    ]
    followups = list(state.get("followups") or [])
    return gates, decision_rows, followups


def _derive_lifecycle(
    project: str,
    inventory: list[dict],
    sprints: list[dict],
    blockers: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Attach derived blockers/effective status and hydrate sprint items."""

    from copy import deepcopy

    from reckon._schema import parse_plan_ref
    from reckon.roadmap import closure_blockers, execution_gates, unsettled_decisions

    plans = deepcopy(inventory)
    plan_by_slug = {
        str(plan.get("slug")): plan
        for plan in plans
        if plan.get("type", "plan") == "plan" and plan.get("slug")
    }
    blocker_kind = {
        str(item.get("id")): (str(item.get("kind") or "").strip() or "explicit")
        for item in blockers or []
        if isinstance(item, dict) and item.get("id")
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
            {"kind": blocker_kind.get(blocker_id, "explicit"), "id": blocker_id}
            for blocker_id in dict.fromkeys(
                explicit_by_slug.get(str(plan.get("slug")), [])
            )
        )
        blocking.extend(unpassed_gate_blockers(execution_gates(plan)))
        workflow_status = str(plan.get("status") or "draft")
        plan["workflow_status"] = workflow_status
        plan["effective_status"] = effective_status(workflow_status, blocking)
        plan["blocking"] = blocking
        plan["blockers"] = len(blocking)
        # Transition gates hold a closure or a choice, not execution, so they
        # leave this list and surface beside it instead.
        plan["closure_blockers"] = closure_blockers(plan)
        plan["decision_blockers"] = unsettled_decisions(plan)

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
        from reckon.mcp_views import sprint_metrics

        sprint["metrics"] = sprint_metrics(items)
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


def _spa_index_path() -> Path:
    package_dir = Path(__file__).resolve().parent
    candidates = (
        package_dir / "_assets" / "index.html",
        package_dir.parent / "docs" / "index.html",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(path) for path in candidates)
    raise ClientAssetError(f"canonical SPA index is missing; searched: {searched}")


def _render_spa_html(
    project: str,
    *,
    relative_assets: bool = False,
    index_path: Path | None = None,
) -> str:
    """Render the authored SPA index for one project and asset routing mode."""
    template = (index_path or _spa_index_path()).read_text(encoding="utf-8")
    escaped_project = html.escape(project, quote=True)
    rendered = re.sub(
        r'(<meta name="docs-project" content=")[^"]*(">)',
        rf"\g<1>{escaped_project}\g<2>",
        template,
        count=1,
    )
    rendered = re.sub(
        r"(<title>reckon · ).*?(</title>)",
        rf"\g<1>{escaped_project}\g<2>",
        rendered,
        count=1,
    )
    if relative_assets:
        rendered = re.sub(
            r'((?:href|src)=")/(?=(?:_shared|_ui|_runtime)/)',
            r"\1",
            rendered,
        )
    return rendered


def safe_join(root: Path, rel: str) -> Path | None:
    try:
        target = (root / rel.lstrip("/")).resolve()
    except (OSError, ValueError):
        return None
    if root not in target.parents and target != root:
        return None
    return target


def _thumbnail_cache_path(source: Path, identity: str) -> Path:
    """Return the cache file for one source revision, under the config home.

    Keyed by the source's stat identity, so a rewritten figure misses the
    cache and a restart — or a second process — reuses the thumbnails that
    are already there rather than rendering them again.
    """

    digest = hashlib.sha256(f"{source}\0{identity}".encode()).hexdigest()[:32]
    return _config_home() / "cache" / "thumbs" / f"{digest}.png"


def _render_thumbnail(source: Path) -> bytes:
    """Downscale one figure to a thumbnail and return its PNG bytes."""

    from PIL import Image

    with Image.open(source) as image:
        image.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()


def _thumbnail_bytes(source: Path, identity: str) -> bytes:
    """Return the thumbnail for one source revision, rendering it at most once.

    The first request for a revision renders the PNG and writes it to the
    cache; every later one — another request, another thread, another
    process — reads that file, so a figure costs one downscale per revision
    rather than one per view.
    """

    cached = _thumbnail_cache_path(source, identity)
    try:
        return cached.read_bytes()
    except OSError:
        pass
    body = _render_thumbnail(source)
    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        write_atomically(
            cached, lambda handle: handle.write(body), fsync=False, binary=True
        )
    except OSError as exc:
        LOGGER.warning("cannot cache thumbnail for %s: %s", source, exc)
    return body


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


def _spawn_check_refresh(argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL)


def _start_check_refresh(project: str) -> bool:
    """Check a project's pending documents in a child process; True if running.

    One check costs about a tenth of a second on a shared filesystem, so a large
    project would hold a request thread for minutes. The child runs at lower
    priority, writes the verdict store the routes read, and logs to the server's
    own output. At most one runs per project.
    """

    if not _CHECK_REFRESH_ENABLED:
        return False
    with _CHECK_REFRESH_LOCK:
        running = _CHECK_REFRESHES.get(project)
        if running is not None and running.poll() is None:
            return True
        argv = [sys.executable, "-m", "reckon.compliance", "refresh"]
        argv += ["--project", project]
        if _MOUNTS_FILE is not None:
            argv += ["--mounts", str(_MOUNTS_FILE)]
        nice = shutil.which("nice")
        if nice:
            argv = [nice, "-n", "10", *argv]
        try:
            _CHECK_REFRESHES[project] = _spawn_check_refresh(argv)
        except OSError as exc:
            print(f"compliance refresh for {project} did not start: {exc}", flush=True)
            return False
        return True


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

    def _refuse_stale_code(self) -> bool:
        """Stop a request before it can import code newer than this process."""

        report = _served_code_report()
        if _SOURCE_SNAPSHOT is None or report is None or not report["stale"]:
            return False
        running, disk = _code_fingerprints(_SOURCE_SNAPSHOT, report)
        if running == disk:
            return False
        body = json.dumps(
            {
                "error": "stale-code",
                "running_code_stamp": running,
                "disk_code_stamp": disk,
                "changed_files": report["changed"]
                + report["added"]
                + report["removed"],
                "detail": "The server is loading the changed code; retry this request.",
            }
        ).encode()
        self._send(
            HTTPStatus.SERVICE_UNAVAILABLE,
            body,
            "application/json",
            headers={"Retry-After": "5"},
        )
        reload = getattr(self.server, "_code_reload", None)
        if reload is not None:
            reload.request_reload()
        return True

    def _send(
        self,
        status: int,
        body: bytes,
        ctype: str = "text/html",
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, target: Path, ctype: str) -> None:
        """Serve a file the browser may keep, revalidated by its stat identity.

        ``no-cache`` makes every use revalidate, so a changed figure or
        stylesheet is seen on the next load, while an unchanged one costs a
        304 instead of its bytes — the difference between a refresh that
        re-downloads every image and one that downloads none.
        """
        try:
            stat = target.stat()
            etag = f'"{stat.st_ino:x}-{stat.st_size:x}-{stat.st_mtime_ns:x}"'
            if etag in (self.headers.get("If-None-Match") or ""):
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            body = target.read_bytes()
        except OSError as e:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, str(e).encode())
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def _serve_thumbnail(self, path: str) -> None:
        """Serve a figure's thumbnail: 200 px on its long edge, cached on disk.

        The response is revalidated by an ETag derived from the source's stat
        identity, which is also what keys the on-disk cache, so a browser that
        already holds a thumbnail pays a stat rather than a regeneration. An
        SVG scales without cost and is passed through unchanged; a source
        Pillow cannot open is served as it is rather than reported as a
        server failure.
        """

        rel = path[len("/_thumb/") :]
        project, _, figure = rel.partition("/")
        if not project or not SAFE_NAME.match(project):
            self._send(HTTPStatus.BAD_REQUEST, b"bad project name")
            return
        mounts = load_mounts()
        if project not in mounts:
            self._send(HTTPStatus.NOT_FOUND, b"unknown project")
            return
        target = safe_join(Path(mounts[project]), figure)
        if target is None or not target.is_file():
            self._send(HTTPStatus.NOT_FOUND, b"not found")
            return
        if target.suffix.lower() == ".svg":
            self._send_file(target, "image/svg+xml")
            return
        try:
            stat = target.stat()
        except OSError:
            self._send(HTTPStatus.NOT_FOUND, b"figure not readable")
            return
        identity = f"{stat.st_ino:x}-{stat.st_size:x}-{stat.st_mtime_ns:x}"
        etag = f'"thumb-{identity}"'
        if etag in (self.headers.get("If-None-Match") or ""):
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        try:
            body = _thumbnail_bytes(target, identity)
        except Exception as exc:  # noqa: BLE001
            # An unreadable or unsupported source is served whole rather than
            # answered with a 500: a list row is worth more drawn at full size
            # than broken.
            LOGGER.warning("cannot thumbnail %s (%s); serving the source", target, exc)
            try:
                body = target.read_bytes()
            except OSError:
                self._send(HTTPStatus.NOT_FOUND, b"figure not readable")
                return
            ctype, _ = mimetypes.guess_type(str(target))
            self._send(HTTPStatus.OK, body, ctype or "application/octet-stream")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def _send_evidence_record(self, target: Path, project: str, plan_slug: str) -> None:
        """Serve a cumulative evidence record with its fragments composed in.

        The record is its own bytes followed by each fragment in ledger
        promotion order, so a fragment merged after the record was written is
        seen on the next load. The ETag is derived from the composed bytes
        rather than the record's stat, because the record file is unchanged
        when only a fragment moves — a stat-derived tag would answer 304 and
        leave the reader on stale bytes.
        """

        try:
            body = compose_landed_record(target, plan_slug, project=project)
        except (OSError, EvidenceSynthesisError) as exc:
            # Fall back to the record's own bytes rather than failing the read:
            # an unreadable ledger suppresses composition, not the document.
            # Say so, because a record whose fragments are hidden by a read
            # failure is otherwise indistinguishable from one with none.
            LOGGER.warning("serving %s without its fragments: %s", target, exc)
            try:
                body = target.read_bytes()
            except OSError as e:
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, str(e).encode())
                return
        etag = f'"{hashlib.sha256(body).hexdigest()}"'
        if etag in (self.headers.get("If-None-Match") or ""):
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def _send_reader_pdf(self, project: str, resource) -> None:
        """Print one document's reader through this server and send the PDF.

        The browser loads the reader from this same server over loopback, so
        the PDF shows exactly what the reader shows and a tunnelled client
        receives a finished file.
        """
        host, port = self.server.server_address[:2]
        if host in ("", "0.0.0.0", "::"):  # noqa: S104 — a wildcard bind is reached on loopback
            host = "127.0.0.1"
        authority = f"[{host}]" if ":" in host else host
        hash_route = reader_hash(
            resource.type, resource.slug, archived=resource.archived
        )
        url = f"http://{authority}:{port}/{project}/#{hash_route}"
        try:
            title = _plan_html.parse_meta(resource.path).get("title") or resource.slug
        except OSError:
            title = resource.slug
        fonts: list[FontFace] = []
        for family, weight, style, source, _ in READER_PDF_FACES:
            try:
                payload = _client_asset(source.rsplit("/", 1)[1]).read_bytes()
            except (OSError, ClientAssetError) as exc:
                # A missing face degrades the PDF's look, not its content.
                LOGGER.warning("printing %s without %s: %s", resource.slug, source, exc)
                continue
            fonts.append(FontFace(family, weight, style, payload))
        try:
            body, missing = render_reader_pdf(url, title=title, fonts=fonts)
        except ReaderPdfUnavailableError as exc:
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, str(exc).encode(), "text/plain")
            return
        except ReaderPdfError as exc:
            LOGGER.warning("PDF export of %s failed: %s", resource.slug, exc)
            self._send(HTTPStatus.BAD_GATEWAY, str(exc).encode(), "text/plain")
            return
        if missing:
            LOGGER.warning(
                "printed %s with %d image(s) that did not load: %s",
                resource.slug,
                len(missing),
                ", ".join(missing),
            )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/pdf")
        self.send_header(
            "Content-Disposition", f'attachment; filename="{resource.slug}.pdf"'
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, obj) -> None:
        body = json.dumps(obj, indent=2).encode()
        accepts = self.headers.get("Accept-Encoding", "") if self.headers else ""
        if len(body) < _GZIP_MIN_BYTES or "gzip" not in accepts:
            self._send(status, body, "application/json")
            return
        # A project inventory is megabytes of repetitive JSON; compressed it is
        # a small fraction of that over a tunnelled connection.
        compressed = gzip.compress(body, compresslevel=5)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(compressed)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(compressed)

    def _send_redirect(
        self, location: str, status: HTTPStatus = HTTPStatus.PERMANENT_REDIRECT
    ) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _send_project_changes(self, project: str, docs_dir: Path) -> None:
        try:
            watch = _ProjectChangeWatch(docs_dir)
        except OSError as exc:
            self._send_json(
                HTTPStatus.NOT_IMPLEMENTED,
                {"error": "change_notifications_unavailable", "detail": str(exc)},
            )
            return

        try:
            initial = discover_plans(docs_dir, project, _STATE_ROOT)
            digest = initial.get("provenance", {}).get("content_digest", "")
            # The rows the stream diffs against: what a page painting from
            # /_index holds when its stream opens.
            known = {
                _index_row_identity(row): row
                for row in _served_index_rows(docs_dir, project)
            }
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._write_project_event("ready", digest)
            while watch.wait(self.connection):
                # The stamp is taken as the change is observed, before the
                # settle wait: every open stream observes one filesystem event,
                # and the recomputation the first of them pays for is what the
                # rest read instead of repeating.
                changed_at = time.monotonic()
                # A save or a merge is a burst of events; let it settle so the
                # burst costs one rediscovery rather than one per event.
                watch.drain(_CHANGE_SETTLE_S)
                # Every watch that reports a tree drops the same views of it,
                # so a page that paints from the index is never handed rows
                # that predate the change its own stream just reported.
                _invalidate_tree_views(docs_dir, changed_at=changed_at)
                rows = _served_index_rows(docs_dir, project)
                row_changes = _index_row_diff(known, rows)
                known = {_index_row_identity(row): row for row in rows}
                moved = any(
                    row_changes[kind] for kind in ("changed", "added", "removed")
                )
                if moved:
                    # The rows are what a page painting from the index needs,
                    # so they go out before the derived payload is recomputed:
                    # the resolver behind the next digest is the expensive
                    # half of a change, and a reader must not wait on it.
                    self._write_project_event("change", rows=row_changes)
                current = discover_plans(docs_dir, project, _STATE_ROOT)
                next_digest = current.get("provenance", {}).get("content_digest", "")
                if next_digest == digest:
                    continue
                digest = next_digest
                if not moved:
                    # A change the index did not see — derived state only —
                    # keeps the digest-only event the loader answers with a
                    # refetch.
                    self._write_project_event("change", digest)
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            watch.close()

    def _write_project_event(
        self, event: str, content_digest: str | None = None, rows: dict | None = None
    ) -> None:
        payload: dict = {}
        if content_digest is not None:
            payload["content_digest"] = content_digest
        if rows is not None:
            payload["rows"] = rows
        data = json.dumps(payload, separators=(",", ":"))
        self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode())
        self.wfile.flush()

    def _serve_velocity(self, query: dict[str, list[str]]) -> None:
        """GET /crew/velocity — the velocity view over the server's mounts.

        ``reckon.velocity.view`` composes the window and the tables — the same
        function the crew read view and the command line call — so the three
        surfaces answer one payload. This route owns only the transport: it
        resolves a named project's checkout through the server's own mount
        table, supplies the default window a caller that names none is measured
        over, and maps a refusal to the status the client reads.
        """
        from reckon import velocity as velocity_module

        def named(name: str) -> str | None:
            values = query.get(name)
            return values[0] if values else None

        project = named("project") or "*"
        if project != "*" and not SAFE_NAME.fullmatch(project):
            self._send(HTTPStatus.BAD_REQUEST, b"bad project name")
            return
        checkout_path = None
        if project != "*":
            mounts = load_mounts()
            if project not in mounts:
                self._send(HTTPStatus.NOT_FOUND, b"project not found")
                return
            checkout_path = str(mounts[project].parent)
        since, until = named("since"), named("until")
        if since is None and until is None:
            since, until = _velocity_default_window()
        limit = None
        raw_limit = named("limit")
        if raw_limit not in (None, ""):
            try:
                limit = int(raw_limit)
            except ValueError:
                self._send(HTTPStatus.BAD_REQUEST, b"bad limit")
                return
        payload = velocity_module.view(
            project,
            since=since,
            until=until,
            checkout_path=checkout_path,
            fields=named("fields"),
            limit=limit,
            cursor=named("cursor"),
        )
        if not payload.get("ok"):
            # The view refuses a missing or unparseable start by name, so the
            # client reads the reason as a 400 rather than an empty body.
            self._send_json(HTTPStatus.BAD_REQUEST, payload)
            return
        self._send_json(HTTPStatus.OK, payload)

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlsplit(self.path).path)
        if path != "/_server" and self._refuse_stale_code():
            return

        if path == "/favicon.ico":
            # Browsers auto-request this; answer cleanly instead of 404-ing
            # through the project router.
            self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            return

        if path in ("/", ""):
            first_project = next(iter(load_mounts()), None)
            if first_project is None:
                self._send(HTTPStatus.NOT_FOUND, b"no projects mounted", "text/plain")
                return
            self._send_redirect(f"/{first_project}/#home", HTTPStatus.FOUND)
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
            self._send_file(target, ctype or "application/octet-stream")
            return

        if path.startswith("/_runtime/"):
            fname = path[len("/_runtime/") :].lstrip("/")
            if fname not in ("react.js", "react-dom.js"):
                self._send(HTTPStatus.NOT_FOUND, b"runtime asset not found")
                return
            try:
                self._send(
                    HTTPStatus.OK,
                    _client_asset(fname).read_bytes(),
                    "application/javascript",
                )
            except (OSError, ClientAssetError) as exc:
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc).encode())
            return

        if path.startswith("/_ui/"):
            rel = path[len("/_ui/") :]
            fname = rel.lstrip("/")
            if not SAFE_NAME.match(fname):
                self._send(HTTPStatus.BAD_REQUEST, b"bad ui filename")
                return
            target = _ui_root() / fname
            jsx_source = target.with_suffix(".jsx") if target.suffix == ".js" else None
            if not target.is_file() and jsx_source and jsx_source.is_file():
                try:
                    self._send(
                        HTTPStatus.OK,
                        compile_jsx(
                            jsx_source.read_text(encoding="utf-8"),
                            filename=jsx_source.name,
                        ),
                        "application/javascript",
                    )
                except (OSError, ClientAssetError) as exc:
                    self._send(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc).encode())
                return
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
            self._send_file(target, ctype)
            return

        if path == "/_server":
            # Which process answers, and whether it still runs the code on
            # disk: the client renders the verdict, the server computes it.
            self._send_json(
                HTTPStatus.OK,
                {
                    "host": socket.gethostname(),
                    "pid": os.getpid(),
                    "code": _served_code_report(),
                },
            )
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

        if path == "/crew/velocity":
            self._serve_velocity(parse_qs(urlsplit(self.path).query))
            return

        if path == "/crew" or path.startswith("/crew/"):
            parts = path.strip("/").split("/")
            project = parts[1] if len(parts) >= 2 else None
            finished = len(parts) in (3, 4) and parts[2] == "finished"
            routing = len(parts) == 3 and parts[2] == "routing"
            plan = parts[3] if len(parts) == 4 and finished else None
            if len(parts) > 2 and not finished and not routing:
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
            if routing:
                try:
                    report = capabilities.derive_routing(mounts)
                except (OSError, ledger.LedgerError, ValueError) as exc:
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": "routing_error", "detail": str(exc)},
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {"project": project, "view": "routing", **report},
                )
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
            )
            from reckon.project_state import (
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
            if rec.get("type") == "plan" and rec.get("sections"):
                from reckon.mcp_views import with_section_attempts

                rec["sections"] = with_section_attempts(
                    project, slug, rec["sections"], Path(mts[project]).parent
                )
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

        if path.startswith("/_thumb/"):
            self._serve_thumbnail(path)
            return

        if path.startswith("/_index/"):
            project = path[len("/_index/") :].strip("/")
            if not project or not SAFE_NAME.match(project):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad project name"})
                return
            index_mounts = load_mounts()
            if project not in index_mounts:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown project"})
                return
            try:
                rows = _served_index_rows(index_mounts[project], project)
            except Exception as exc:  # noqa: BLE001
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "metadata_index_failed", "detail": str(exc)},
                )
                return
            self._send_json(HTTPStatus.OK, rows)
            return

        if path.startswith("/_checks/"):
            # GET /_checks/<project>                — failing documents, pending count
            # GET /_checks/<project>/<root>/<slug>  — one document's verdict
            parts = path[len("/_checks/") :].strip("/").split("/")
            project = parts[0]
            if len(parts) not in (1, 3) or not SAFE_NAME.match(project):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad path"})
                return
            check_mounts = load_mounts()
            if project not in check_mounts:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown project"})
                return
            docs_root = Path(check_mounts[project])
            if len(parts) == 1:
                try:
                    summary = compliance.project_checks(docs_root, project)
                except Exception as exc:  # noqa: BLE001
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": "compliance_failed", "detail": str(exc)},
                    )
                    return
                summary["refreshing"] = bool(summary["pending"]) and (
                    _start_check_refresh(project)
                )
                self._send_json(HTTPStatus.OK, summary)
                return
            artifact_type = ROOT_TYPES.get(parts[1])
            slug = parts[2].removesuffix(".html")
            if artifact_type not in ("plan", "research", "evidence"):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad resource type"})
                return
            try:
                document = _resolve_plan_file(
                    docs_root, slug, artifact_type, project=project
                )
            except ResourceCollision as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            if document is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown document"})
                return
            verdict = compliance.document_check(docs_root, project, document)
            relative = compliance.relative_path(docs_root, document)
            self._send_json(
                HTTPStatus.OK,
                {
                    "project": project,
                    "type": artifact_type,
                    "slug": slug,
                    "path": f"{docs_root.name}/{relative}",
                    **verdict,
                },
            )
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
            result = _attach_schedule(
                result,
                project,
                disc_mounts,
                reference=datetime.now(UTC),
            )
            self._send_json(HTTPStatus.OK, result)
            return

        if path.startswith("/_changes/"):
            project = path[len("/_changes/") :].strip("/")
            if not project or not SAFE_NAME.match(project):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad project name"})
                return
            change_mounts = load_mounts()
            if project not in change_mounts:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown project"})
                return
            self._send_project_changes(project, Path(change_mounts[project]))
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

        if rel.endswith(".pdf"):
            # A document's route with a .pdf suffix is its reader printed. Any
            # other .pdf path, such as a file under figures, is served as a file.
            try:
                printed, _ = resolve_route(root, project, rel.removesuffix(".pdf"))
            except ResourceCollision:
                printed = None
            if printed is not None and printed.type in READER_PDF_TYPES:
                self._send_reader_pdf(project, printed)
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

        record_plan = evidence_record_plan(target)
        if record_plan is not None:
            self._send_evidence_record(target, project, record_plan)
            return

        ctype, _ = mimetypes.guess_type(str(target))
        self._send_file(target, ctype or "application/octet-stream")

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
            from reckon._store import (
                OpError,
                new_section_coverage_gaps,
                validate_landing_patch,
            )

            validate_landing_patch(state, patch, text)
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

        # The patch path renders its own HTML and never reaches the store's
        # write paths, so the coverage check has to run here too: a patch that
        # drops a declaration or adds a heading would otherwise walk past it.
        undeclared, orphaned = new_section_coverage_gaps(text, new_text)
        if undeclared or orphaned:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "section_coverage",
                    "detail": (
                        "the patch would leave a heading without a declaration "
                        "or a declaration without a heading"
                    ),
                    "sections_without_declaration": undeclared,
                    "declarations_without_section": orphaned,
                },
            )
            return

        write_atomically(plan_file, lambda handle: handle.write(new_text), fsync=False)
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
        if self._refuse_stale_code():
            return
        try:
            self._handle_post()
        finally:
            # A write must be visible to the next read at once, so the reuse
            # window for tree walks never spans one.
            _invalidate_discovery_signatures()

    def _handle_post(self) -> None:
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
        # Routed through the shared atomic writer so a reader never observes a
        # half-written envelope. ``fsync`` is off to keep this route's existing
        # durability behaviour, and ``mode=None`` keeps an ordinary whole-file
        # creation mode; the serialisation matches the previous
        # ``json.dumps(envelope, indent=2)`` byte for byte.
        write_json_atomically(
            out_file,
            envelope,
            fsync=False,
            indent=2,
            sort_keys=False,
            mode=None,
        )
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "path": str(out_file), "version": new_data["_version"]},
        )


def _served_signature_ttl() -> float:
    """Return the walk-reuse window the served process reads its walks under."""

    return float(
        os.environ.get("RECKON_DISCOVERY_REUSE_S", str(_SERVED_SIGNATURE_TTL_S))
    )


def start_fleet_change_watch(mounts: dict[str, Path]) -> _FleetChangeWatch:
    """Watch every mounted docs tree, dropping that tree's walk on a change.

    The watch lives for the life of the served process. Starting it is what
    makes the longer reuse window safe: a change the kernel reports drops the
    memoised walk at once rather than waiting for the window to lapse.
    """

    global _FLEET_WATCH  # noqa: PLW0603 — one watch per served process
    if _FLEET_WATCH is None or not _FLEET_WATCH.running:
        _FLEET_WATCH = _FleetChangeWatch(mounts.values()).start()
    return _FLEET_WATCH


def main(
    port: int = 8765,
    host: str | None = None,
    mounts_file: Path | None = None,
    *,
    exec_=os.execv,
) -> None:
    global _SIGNATURE_TTL_S  # noqa: PLW0603 — the served process opts into reuse
    global _SOURCE_SNAPSHOT  # noqa: PLW0603 — recorded once, as the code loads
    global _CHECK_REFRESH_ENABLED  # noqa: PLW0603 — the served process opts in
    global _EXECUTED_SOURCE  # noqa: PLW0603 — recorded from here on
    _SOURCE_SNAPSHOT = served_code.take_snapshot()
    executed = served_code.ExecutedSource(_SOURCE_SNAPSHOT.root)
    _EXECUTED_SOURCE = executed if executed.start() else None
    _CHECK_REFRESH_ENABLED = True
    try:
        _resolve_paths(mounts_file)
        _SIGNATURE_TTL_S = _served_signature_ttl()
        _host = host or os.environ.get("DOCS_SERVER_BIND", "127.0.0.1")
        _port = port or int(os.environ.get("DOCS_SERVER_PORT", "8765"))

        # Patch Handler class attributes so do_GET can use them for fallback page.
        Handler._host = _host
        Handler._port = _port

        if _STATE_ROOT:
            _STATE_ROOT.mkdir(parents=True, exist_ok=True)
        if _MOUNTS_FILE and not _MOUNTS_FILE.exists():
            _MOUNTS_FILE.write_text("{}\n")

        # Bind before building the change watch. Building it walks every mounted
        # tree, which on a shared filesystem can take seconds per tree, and the
        # port must answer throughout that walk; the watch is armed on its own
        # thread and a tree is covered by the discovery reuse window until then.
        server = ThreadingHTTPServer((_host, _port), Handler)
        reload = _CodeReload(server, exec_)

        start_fleet_change_watch(load_mounts())

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
        server.serve_forever()
        reload.finish()
    finally:
        # Serving has stopped, or never started (a port already bound): release
        # what this function set up for the served process, so a caller that
        # ran it in a thread gets the library defaults back rather than a
        # module still acting as a server.
        if _EXECUTED_SOURCE is not None:
            _EXECUTED_SOURCE.stop()
        _EXECUTED_SOURCE = None
        _CHECK_REFRESH_ENABLED = False
        _SOURCE_SNAPSHOT = None


if __name__ == "__main__":
    main()
