"""A write on another node still refreshes the index on this one.

Inotify reports only writes made on the same node, so on a shared filesystem a
document committed from a compute node leaves no kernel event on the login
node's server. The served process's ``_FleetChangeWatch`` runs a detector
thread beside its inotify event loop that stats each tree's directories and git
index on a short interval, and sweeps the covered files' mtimes on a longer one
for a tree that has a recent reader or an open change stream. A change it sees
drops the tree's derived views and wakes every open stream through that tree's
registered pipe.

These cases run the real server with every per-tree kernel watch replaced by
one whose descriptor never becomes readable — exactly how a cross-node write
looks — so the detector and the subscription pipes are the only things that can
move a served view.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest

from reckon import change_streams, metadata_index, serve

PROJECT = "sample"
FAST_S = "1"
SLOW_S = "1"
READ_WINDOW_S = "5"

#: Long enough for the slow sweep to record the tree's baseline signature
#: before a case makes the edit it wants detected.
_SETTLE_S = 1.5

#: A few fast intervals, so a scheduling stall on a loaded host cannot make the
#: assertion flap while a genuine miss still fails.
BUDGET_S = 6.0

_RealWatch = serve._ProjectChangeWatch


class _NeverFiringWatch(_RealWatch):
    """A per-tree watch whose kernel source never reports anything.

    The read end of a pipe nobody writes to stands in for the inotify
    descriptor: it is registered, it can be selected on, and it never fires. A
    wake pipe handed in by the fleet watch is the only thing that moves it, so
    the test measures the cross-node path rather than the kernel path.
    """

    def __init__(self, root: Path, *, wake_fd: int | None = None) -> None:
        self.root = Path(root).resolve()
        self._wake_fd = wake_fd
        self._carried: list[tuple[Path, bool]] = []
        self.fd, self._sink = os.pipe()

    def consume(self) -> list[tuple[Path, bool]]:
        return []

    def drain(self, settle_s: float) -> list[tuple[Path, bool]]:
        return []

    def close(self) -> None:
        for descriptor in (self.fd, self._sink):
            with suppress(OSError):
                os.close(descriptor)


def _plan_page(slug: str, title: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{PROJECT}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{title}">
<meta name="plan-status" content="active">
<title>{title}</title></head><body><main class="plan-doc"></main></body></html>
"""


def _advance_mtime(path: Path) -> None:
    """Push a file's mtime past the current second so a stat sees the write."""

    previous = path.stat().st_mtime_ns
    stat = path.stat()
    os.utime(
        path, ns=(stat.st_atime_ns, max(stat.st_mtime_ns, previous) + 1_000_000_000)
    )


def _await(condition, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def _get(port: int, path: str) -> tuple[int, object]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def _slugs(rows: object) -> set[str]:
    return {row.get("slug") for row in rows if isinstance(row, dict)}


class _ChangeStream:
    """Read ``/_changes/<project>`` frames over a raw socket.

    A raw socket rather than ``http.client`` because closing the stream has to
    deliver an end-of-file the server can select on: the point of the closing
    case below. A bare ``HTTPConnection.close()`` leaves the client's file
    object holding the descriptor, so no FIN reaches the handler.
    """

    def __init__(self, port: int, project: str) -> None:
        self._sock = socket.create_connection(("127.0.0.1", port), timeout=30)
        self._sock.sendall(
            f"GET /_changes/{project} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode()
        )
        self._buffer = b""
        while b"\r\n\r\n" not in self._buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise AssertionError("the change stream closed during its headers")
            self._buffer += chunk
        self._buffer = self._buffer.split(b"\r\n\r\n", 1)[1]

    def _readline(self) -> str | None:
        while b"\n" not in self._buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                return None
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line.decode("utf-8")

    def next_event(self) -> tuple[str, dict | None]:
        event = ""
        data: str | None = None
        while True:
            raw = self._readline()
            if raw is None:
                raise AssertionError("the change stream closed before an event")
            line = raw.rstrip("\r\n")
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
            elif not line and event:
                return event, (json.loads(data) if data is not None else None)

    def close(self) -> None:
        with suppress(OSError):
            self._sock.shutdown(socket.SHUT_RDWR)
        self._sock.close()


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        },
    )


@contextmanager
def _served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, git: bool):
    """A real server over a temporary tree, with every kernel watch muted."""

    repo = tmp_path / "checkout"
    docs = repo / "docs"
    (docs / "plans").mkdir(parents=True)
    (docs / "plans" / "alpha.html").write_text(
        _plan_page("alpha", "Alpha"), encoding="utf-8"
    )
    if git:
        _git(["init", "-q"], repo)
        _git(["add", "docs/plans/alpha.html"], repo)
        _git(["commit", "-q", "-m", "seed"], repo)

    state_root = tmp_path / "state"
    state_root.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RECKON_CROSS_NODE_FAST_S", FAST_S)
    monkeypatch.setenv("RECKON_CROSS_NODE_SLOW_S", SLOW_S)
    monkeypatch.setenv("RECKON_CROSS_NODE_READ_WINDOW_S", READ_WINDOW_S)
    monkeypatch.setattr(serve, "load_mounts", lambda: {PROJECT: docs})
    monkeypatch.setattr(serve, "_STATE_ROOT", state_root)
    monkeypatch.setattr(serve, "_ProjectChangeWatch", _NeverFiringWatch)
    monkeypatch.setattr(change_streams, "_ProjectChangeWatch", _NeverFiringWatch)
    metadata_index.clear()
    serve._DISC_CACHE.clear()
    serve._SIGNATURE_MEMO.clear()

    serve._FLEET_WATCH = None
    watch = serve.start_fleet_change_watch({PROJECT: docs})

    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield docs, server, watch
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        watch.close()
        serve._FLEET_WATCH = None
        metadata_index.clear()


@pytest.fixture()
def served(tmp_path, monkeypatch):
    with _served(tmp_path, monkeypatch, git=False) as ctx:
        yield ctx


@pytest.fixture()
def git_served(tmp_path, monkeypatch):
    with _served(tmp_path, monkeypatch, git=True) as ctx:
        yield ctx


# ── The subscription pipe and its registry ────────────────────────────────


def test_wait_treats_a_readable_wake_pipe_as_a_change(tmp_path) -> None:
    """The real watch's select drains the pipe and reports a change."""

    docs = tmp_path / "docs"
    docs.mkdir()
    reader, writer = os.pipe()
    watch = _RealWatch(docs, wake_fd=reader)
    client, server_side = socket.socketpair()
    try:
        os.write(writer, b"x")
        assert watch.wait(client) is True, "a readable wake pipe must report a change"

        server_side.close()
        assert watch.wait(client) is False, "a disconnect is the only false"
    finally:
        watch.close()
        client.close()
        for descriptor in (reader, writer):
            with suppress(OSError):
                os.close(descriptor)


def test_a_subscription_is_a_no_op_without_a_fleet_watch(tmp_path) -> None:
    """A server that never starts the watch has no pipe to register."""

    docs = tmp_path / "docs"
    docs.mkdir()
    original = serve._FLEET_WATCH
    serve._FLEET_WATCH = None
    try:
        assert serve._subscribe_tree(docs) is None
    finally:
        serve._FLEET_WATCH = original


# ── The detector refreshes a served view ──────────────────────────────────


def test_a_new_document_appears_within_a_fast_interval(served) -> None:
    """A document added on another node reaches /_index through the detector."""

    docs, server, _watch = served
    port = server.server_port
    assert "beta" not in _slugs(_get(port, f"/_index/{PROJECT}")[1])

    (docs / "plans" / "beta.html").write_text(_plan_page("beta", "Beta"), "utf-8")
    _advance_mtime(docs / "plans" / "beta.html")

    def seen() -> bool:
        return "beta" in _slugs(_get(port, f"/_index/{PROJECT}")[1])

    assert _await(seen, BUDGET_S), "the new document never reached /_index"


def test_a_committed_byte_change_refreshes_the_row(git_served) -> None:
    """A commit from another node moves the git signal, not the kernel one."""

    docs, server, _watch = git_served
    port = server.server_port
    target = docs / "plans" / "alpha.html"
    assert _row(port, "alpha")["title"] == "Alpha"

    target.write_text(_plan_page("alpha", "Alpha committed elsewhere"), "utf-8")
    _git(["add", "docs/plans/alpha.html"], docs.parent)
    _git(["commit", "-q", "-m", "retitle"], docs.parent)

    assert _await(
        lambda: _row(port, "alpha")["title"] == "Alpha committed elsewhere", BUDGET_S
    ), "the committed retitle never refreshed the row"


def _row(port: int, slug: str) -> dict:
    for row in _get(port, f"/_index/{PROJECT}")[1]:
        if isinstance(row, dict) and row.get("slug") == slug:
            return row
    raise AssertionError(f"no row for {slug}")


# ── The slow sweep: activation, and the read stamp it rests on ────────────


def test_an_in_place_edit_is_seen_while_a_stream_is_open(served) -> None:
    """An uncommitted edit refreshes within a slow interval, stream open."""

    docs, server, _watch = served
    port = server.server_port
    stream = _ChangeStream(port, PROJECT)
    try:
        assert stream.next_event()[0] == "ready"
        # Let the slow sweep record the tree's baseline before the edit, so a
        # pass that has not yet armed cannot read the post-edit state as it.
        time.sleep(_SETTLE_S)
        (docs / "plans" / "alpha.html").write_text(
            _plan_page("alpha", "Edited in place"), "utf-8"
        )
        _advance_mtime(docs / "plans" / "alpha.html")
        assert _await(
            lambda: _row(port, "alpha")["title"] == "Edited in place", BUDGET_S
        ), "the in-place edit was never swept"
    finally:
        stream.close()


def test_an_unread_tree_pays_no_slow_sweep(served) -> None:
    """No stream and no recent read: the slow sweep leaves the tree alone."""

    docs, server, watch = served
    port = server.server_port
    # Build the index without stamping a read, so the tree is neither streamed
    # nor recently read and the slow sweep must leave it alone.
    serve._served_index_rows(docs, PROJECT)
    assert docs.resolve() not in watch._read_at

    target = docs / "plans" / "alpha.html"
    target.write_text(_plan_page("alpha", "Quiet edit"), "utf-8")
    _advance_mtime(target)

    time.sleep(3.0)
    assert _row(port, "alpha")["title"] == "Alpha", "an unread tree was swept"


def test_a_read_stamps_the_tree_and_a_detection_keeps_the_stamp(served) -> None:
    """A /_index read arms the slow sweep, and a detection does not disarm it."""

    docs, server, watch = served
    port = server.server_port
    docs = docs.resolve()
    assert "alpha" in _slugs(_get(port, f"/_index/{PROJECT}")[1])
    assert docs in watch._read_at, "a read must stamp the tree on the watch"

    stamp = watch._read_at[docs]
    serve._invalidate_tree_views(docs)
    assert watch._read_at.get(docs) == stamp, "an invalidation must not clear the stamp"

    time.sleep(_SETTLE_S)
    target = docs / "plans" / "alpha.html"
    target.write_text(_plan_page("alpha", "After a read"), "utf-8")
    _advance_mtime(target)
    assert _await(lambda: _row(port, "alpha")["title"] == "After a read", BUDGET_S), (
        "a read tree was not swept within a slow interval"
    )


# ── The open stream is told ───────────────────────────────────────────────


def test_an_open_stream_receives_the_added_row_and_stays_open(served) -> None:
    """The detector wakes the stream through its pipe with the added row."""

    docs, server, _watch = served
    port = server.server_port
    stream = _ChangeStream(port, PROJECT)
    try:
        assert stream.next_event()[0] == "ready"
        (docs / "plans" / "gamma.html").write_text(
            _plan_page("gamma", "Gamma"), "utf-8"
        )
        _advance_mtime(docs / "plans" / "gamma.html")

        event, data = stream.next_event()
        assert event == "change"
        added = (data or {}).get("rows", {}).get("added", [])
        assert any(row.get("slug") == "gamma" for row in added), (
            f"the change event carried no gamma row: {data!r}"
        )
        # Still open: a second document is reported on the same stream.
        (docs / "plans" / "delta.html").write_text(
            _plan_page("delta", "Delta"), "utf-8"
        )
        _advance_mtime(docs / "plans" / "delta.html")

        def next_added() -> bool:
            name, payload = stream.next_event()
            rows = (payload or {}).get("rows", {}).get("added", [])
            return name == "change" and any(r.get("slug") == "delta" for r in rows)

        assert next_added(), "the stream did not stay open for a second change"
    finally:
        stream.close()


def test_closing_a_stream_removes_its_pipe(served) -> None:
    """The stream owns its subscription's lifetime."""

    docs, server, watch = served
    root = docs.resolve()
    stream = _ChangeStream(server.server_port, PROJECT)
    assert _await(lambda: bool(watch._subscribers.get(root)), BUDGET_S), (
        "the stream never registered a pipe"
    )
    stream.close()
    assert _await(lambda: not watch._subscribers.get(root), BUDGET_S), (
        "closing the stream left its pipe registered"
    )


# ── One change fires once, and a rebuild reloads quietly ──────────────────


def test_one_change_invalidates_once_and_a_rebuild_reloads(
    git_served, monkeypatch
) -> None:
    """One change fires once; a rebuild reloads the stamps without firing.

    A move of the git index file is used because only the fast pass can see
    it: the covered files are untouched, so the slow sweep stays quiet and the
    count below measures one signal rather than two.
    """

    docs, server, watch = git_served
    port = server.server_port
    docs = docs.resolve()
    _get(port, f"/_index/{PROJECT}")

    def armed() -> bool:
        signal = watch._signals.get(docs)
        return signal is not None and signal.git_identity is not None

    assert _await(armed, BUDGET_S), "the detector never armed the git signal"

    calls: list[Path] = []
    original = serve._invalidate_tree_views

    def counted(root, *, changed_at=None):
        calls.append(Path(root).resolve())
        return original(root, changed_at=changed_at)

    monkeypatch.setattr(serve, "_invalidate_tree_views", counted)

    _touch_git_index(docs.parent)
    assert _await(lambda: calls, BUDGET_S), "the git index move never fired"
    assert len(calls) == 1, f"one change fired {len(calls)} invalidations"

    # A reader rebuilds the persisted index; the detector reloads its stamps
    # from that file and stays quiet over the passes that follow.
    for _ in range(3):
        _get(port, f"/_index/{PROJECT}")
        time.sleep(1.2)
    assert len(calls) == 1, "a rebuild fired the detector again"


def _touch_git_index(repo: Path) -> None:
    """Move the git index file's identity, as an add or commit would."""

    path = serve._git_directory(repo) / "index"
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


# ── The git signal leaves the HEAD memo alone ─────────────────────────────


def test_a_git_index_change_fires_without_moving_head_identity(
    git_served, monkeypatch
) -> None:
    """Touching .git/index is a change the fast pass sees, with HEAD still."""

    docs, _server, _watch = git_served
    repo = docs.parent
    calls: list[Path] = []
    original = serve._invalidate_tree_views

    def counted(root, *, changed_at=None):
        calls.append(Path(root).resolve())
        return original(root, changed_at=changed_at)

    monkeypatch.setattr(serve, "_invalidate_tree_views", counted)

    head_before = serve._head_identity(repo)
    assert head_before is not None, "the fixture must be a git checkout"

    # Warm the memo, then arrange to notice any git process started after it.
    assert serve._git_head(repo) is not None
    ran: list[list[str]] = []
    monkeypatch.setattr(
        serve, "_run_git", lambda argv, cwd, **kw: ran.append(argv) or None
    )

    _touch_git_index(repo)
    assert _await(lambda: calls, BUDGET_S), "the git index change never fired"

    assert serve._head_identity(repo) == head_before, (
        "the index write must not move HEAD"
    )
    serve._git_head(repo)
    assert ran == [], "an unchanged HEAD must start no git process"


# ── Threading and failure behaviour ───────────────────────────────────────


def test_the_detector_runs_beside_the_event_loop_thread(served) -> None:
    _docs, _server, watch = served
    assert watch._thread is not None and watch._thread.is_alive()
    assert watch._detector_thread is not None and watch._detector_thread.is_alive()
    assert watch._detector_thread is not watch._thread


def test_an_oserror_in_a_pass_leaves_the_thread_running(served, monkeypatch) -> None:
    docs, server, watch = served
    port = server.server_port
    docs = docs.resolve()
    _get(port, f"/_index/{PROJECT}")

    # The detector stats the persisted directories; wait until it has picked
    # up the stamps the failing pass will need to reach the raising call.
    def has_stamps() -> bool:
        signal = watch._signals.get(docs)
        return signal is not None and bool(signal.dir_stamps)

    assert _await(has_stamps, BUDGET_S), "the detector never loaded the stamps"

    def boom(path):
        raise OSError("storage stall")

    monkeypatch.setattr(metadata_index, "_dir_identity", boom)
    time.sleep(2.5)
    assert watch._detector_thread is not None and watch._detector_thread.is_alive(), (
        "an OSError in a pass must not take the detector down"
    )
    monkeypatch.undo()
    _get(port, f"/_index/{PROJECT}")


def test_close_stops_and_joins_the_detector_thread(served) -> None:
    _docs, _server, watch = served
    detector = watch._detector_thread
    reader = watch._thread
    watch.close()
    assert not detector.is_alive(), "the detector thread outlived close()"
    assert not reader.is_alive(), "the reader thread outlived close()"
    assert watch._detector_thread is None
