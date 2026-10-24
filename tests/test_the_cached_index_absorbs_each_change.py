"""The cached index absorbs each change without a full sweep.

The served process holds a project's index build and answers every read from
it without a stat. Dropping that build on any change anywhere in the tree is
what sent the next read back into a walk of about 2,500 directories on the
shared filesystem, so each of the five sources that report a change now names
the paths it can attribute and the next read re-reads only those.

Rebuild work is counted with spies on :func:`metadata_index.build_index` and
:func:`metadata_index._dir_identity`: the first is called only by a full
rebuild, and the second once per directory whose stat identity a read has to
move. A read that calls neither served the cached build as it stood.

The negative controls are armed one per environment variable, each a defect the
case it names must fail against:

* ``RECKON_TEST_INDEX_DROP_ON_BURST`` drops the build on every kernel burst.
* ``RECKON_TEST_INDEX_IGNORE_HTML`` marks no HTML file, so an edit is not
  absorbed.
* ``RECKON_TEST_INDEX_DROP_ON_DIR_MOVE`` drops the build on every moved
  directory instead of marking it.
* ``RECKON_TEST_INDEX_SKIP_RESCAN`` returns no covered file from a re-scan, so
  one added to a directory the fast pass marked never reaches the rows.
* ``RECKON_TEST_INDEX_DROP_ON_GIT`` drops the build on every git identity
  change.
* ``RECKON_TEST_INDEX_KEEP_UNATTRIBUTED`` keeps the build when no source can
  attribute the change.
* ``RECKON_TEST_INDEX_DROP_ON_SLOW`` drops the build whenever the slow
  signature moves.
* ``RECKON_TEST_INDEX_DROP_ON_STREAM_WAKE`` drops the build on every wake of an
  open change stream.
* ``RECKON_TEST_INDEX_WHOLESALE_RELOAD`` adopts the reloaded directory stamps
  wholesale, so a change folded into an absorbing write's own shape is never
  reported.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import change_streams, file_memo, metadata_index, serve

_PROJECT = "sample"

_DROP_ON_BURST = "RECKON_TEST_INDEX_DROP_ON_BURST"
_IGNORE_HTML = "RECKON_TEST_INDEX_IGNORE_HTML"
_DROP_ON_DIR_MOVE = "RECKON_TEST_INDEX_DROP_ON_DIR_MOVE"
_SKIP_RESCAN = "RECKON_TEST_INDEX_SKIP_RESCAN"
_DROP_ON_GIT = "RECKON_TEST_INDEX_DROP_ON_GIT"
_KEEP_UNATTRIBUTED = "RECKON_TEST_INDEX_KEEP_UNATTRIBUTED"
_DROP_ON_SLOW = "RECKON_TEST_INDEX_DROP_ON_SLOW"
_DROP_ON_STREAM_WAKE = "RECKON_TEST_INDEX_DROP_ON_STREAM_WAKE"
_WHOLESALE_RELOAD = "RECKON_TEST_INDEX_WHOLESALE_RELOAD"
_CONTROLS = (
    _DROP_ON_BURST,
    _IGNORE_HTML,
    _DROP_ON_DIR_MOVE,
    _SKIP_RESCAN,
    _DROP_ON_GIT,
    _KEEP_UNATTRIBUTED,
    _DROP_ON_SLOW,
    _DROP_ON_STREAM_WAKE,
    _WHOLESALE_RELOAD,
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _plan_doc(slug: str, title: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{_PROJECT}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{title}">
<meta name="plan-status" content="active">
<title>{title}</title></head><body><main class="plan-doc"></main></body></html>
"""


def _clear_caches() -> None:
    file_memo.clear()
    metadata_index.clear()
    serve._DISC_CACHE.clear()
    serve._SIGNATURE_MEMO.clear()


@pytest.fixture(autouse=True)
def _isolated_caches():
    _clear_caches()
    yield
    _clear_caches()


@pytest.fixture(autouse=True)
def _armed_controls(monkeypatch, request):
    """Arm the declared mutations the run asks for, and nothing by default."""

    armed = {name for name in _CONTROLS if os.environ.get(name) == "1"}
    if not armed:
        return
    del request

    if _DROP_ON_BURST in armed:

        def drop(*_args, **_kwargs):
            return None

        monkeypatch.setattr(change_streams, "observed_change", drop)

    if _IGNORE_HTML in armed:
        real_name = metadata_index.covered_name
        monkeypatch.setattr(
            metadata_index,
            "covered_name",
            lambda relative: (
                relative.endswith((".png", ".svg", ".gif")) and real_name(relative)
            ),
        )

    if _DROP_ON_DIR_MOVE in armed:
        real_mark = metadata_index.mark_tree_paths

        def drop_moved(docs_dir, change):
            # Any moved directory is treated as unattributable, which is the
            # invalidation the detector performed before it could name paths.
            if change.dirs:
                metadata_index.invalidate_tree(docs_dir)
                return
            real_mark(docs_dir, change)

        monkeypatch.setattr(metadata_index, "mark_tree_paths", drop_moved)

    if _SKIP_RESCAN in armed:
        real_walk = metadata_index._walk_covered

        def no_scanned_files(docs_dir, roots=None, skip_subtrees=frozenset()):
            files, dirs = real_walk(docs_dir, roots, skip_subtrees)
            return ([] if roots is not None else files), dirs

        monkeypatch.setattr(metadata_index, "_walk_covered", no_scanned_files)

    if _DROP_ON_GIT in armed:
        real_fast = change_streams._FleetChangeWatch._fast_tree

        def drop_on_git(self, tree):
            before = self._signal_for(tree).git_identity
            real_fast(self, tree)
            signal = self._signal_for(tree)
            if signal.git_identity != before:
                metadata_index.invalidate_tree(tree)

        monkeypatch.setattr(change_streams._FleetChangeWatch, "_fast_tree", drop_on_git)

    if _KEEP_UNATTRIBUTED in armed:
        monkeypatch.setattr(metadata_index, "invalidate_tree", lambda docs_dir: None)

    if _DROP_ON_SLOW in armed:
        real_slow = change_streams._FleetChangeWatch._slow_tree

        def drop_on_slow(self, tree):
            signal = self._signal_for(tree)
            seen = signal.slow_signature
            real_slow(self, tree)
            if seen is not None and signal.slow_signature != seen:
                metadata_index.invalidate_tree(tree)

        monkeypatch.setattr(
            change_streams._FleetChangeWatch, "_slow_tree", drop_on_slow
        )

    if _DROP_ON_STREAM_WAKE in armed:
        real_note = change_streams.observed_change

        def wake_drops(root, events):
            change = real_note(root, events)
            if not change.files and not change.dirs:
                # A wake that names nothing is read as a whole-tree change.
                return None
            return change

        monkeypatch.setattr(serve, "observed_change", wake_drops)

    if _WHOLESALE_RELOAD in armed:
        monkeypatch.setattr(
            change_streams,
            "merge_dir_stamps",
            lambda tree, held, fresh: dict(fresh),
        )


@pytest.fixture()
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A committed checkout holding a docs tree, over a temporary config home."""

    config_home = tmp_path / "config"
    config_home.mkdir()
    repo = tmp_path / "checkout"
    docs = repo / "docs"
    (docs / "notes" / "inner").mkdir(parents=True)
    (docs / "state" / _PROJECT / "deep" / "deeper").mkdir(parents=True)
    (docs / "figures").mkdir()
    (docs / "notes" / "alpha.html").write_text(
        _plan_doc("alpha", "Alpha"), encoding="utf-8"
    )
    (docs / "notes" / "beta.html").write_text(
        _plan_doc("beta", "Beta"), encoding="utf-8"
    )
    (docs / "notes" / "inner" / "gamma.html").write_text(
        _plan_doc("gamma", "Gamma"), encoding="utf-8"
    )
    (docs / "figures" / "plot.svg").write_text(
        '<svg viewBox="0 0 4 3"><rect/></svg>', encoding="utf-8"
    )
    (docs / "state" / _PROJECT / "index.json").write_text(
        json.dumps({"version": 1}), encoding="utf-8"
    )
    _git(["init", "-q"], repo)
    _git(["add", "docs"], repo)
    _git(["commit", "-q", "-m", "seed"], repo, when="2026-01-01T00:00:00+0000")

    mounts = config_home / "mounts.json"
    mounts.write_text(json.dumps({_PROJECT: str(docs)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setattr(serve, "_STATE_ROOT", None)
    _clear_caches()
    return docs, repo


def _git(args: list[str], cwd: Path, *, when: str | None = None) -> None:
    env = {**os.environ, **_GIT_ENV}
    if when is not None:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def _read(docs: Path, repo: Path) -> list[dict]:
    """Read the served path, with the git maps the served process supplies."""

    return metadata_index.index_rows(
        docs,
        _PROJECT,
        repo_dir=repo,
        git_first=serve._git_first_committed(repo, docs),
        git_last=serve._git_last_committed(repo, docs),
    )


def _full_walk_rows(docs: Path, repo: Path) -> list[dict]:
    """The rows a build with no cached shape produces, the comparison arm."""

    metadata_index.clear()
    return _read(docs, repo)


def _persist(docs: Path, repo: Path) -> list[dict]:
    """Build the index and hold it, as a served process's first read does."""

    return _read(docs, repo)


@contextmanager
def _spies(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    """Count full rebuilds and record every directory identity a read moves."""

    calls = {"build": 0, "dirs": []}
    real_build = metadata_index.build_index
    real_identity = metadata_index._dir_identity

    def build(*args, **kwargs):
        calls["build"] += 1
        return real_build(*args, **kwargs)

    def identity(path):
        calls["dirs"].append(os.fspath(path))
        return real_identity(path)

    monkeypatch.setattr(metadata_index, "build_index", build)
    monkeypatch.setattr(metadata_index, "_dir_identity", identity)
    yield calls


def _advance_mtime(path: Path, seconds: int = 30) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + seconds * 1_000_000_000))


def _watch(docs: Path) -> change_streams._ProjectChangeWatch:
    return change_streams._ProjectChangeWatch(docs)


def _burst(watch: change_streams._ProjectChangeWatch) -> None:
    """Handle one settled kernel burst, as the fleet watch's reader does."""

    change_streams.note_watch_burst(watch)


def _detector(
    docs: Path, monkeypatch: pytest.MonkeyPatch
) -> change_streams._FleetChangeWatch:
    watch = change_streams._FleetChangeWatch({_PROJECT: docs})
    monkeypatch.setattr(watch, "_spawn_changes", lambda tree: None)
    return watch


def _slugs(rows: list[dict]) -> set[str]:
    return {str(row.get("slug")) for row in rows}


# ── (1) the kernel watch over a change the index does not cover ───────────


def test_a_json_write_in_a_deep_directory_keeps_the_build(
    tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A covered-set change to a non-covered file costs neither spy."""

    docs, repo = tree
    before = _persist(docs, repo)
    watch = _watch(docs)
    try:
        target = docs / "state" / _PROJECT / "deep" / "deeper" / "crew.json"
        target.write_text(json.dumps({"run": 1}), encoding="utf-8")
        _burst(watch)
        with _spies(monkeypatch) as calls:
            rows = _read(docs, repo)
    finally:
        watch.close()

    assert rows == before
    assert calls["build"] == 0, "a JSON write rebuilt the index"
    assert calls["dirs"] == [], "a JSON write stat-ed recorded directories"

    assert rows == _full_walk_rows(docs, repo)


# ── (2) the kernel watch over covered content ─────────────────────────────


def test_a_kernel_burst_over_html_rebuilds_only_its_own_rows(
    tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An edited, added and removed document each reach only their own row."""

    docs, repo = tree
    _persist(docs, repo)
    watch = _watch(docs)
    try:
        (docs / "notes" / "alpha.html").write_text(
            _plan_doc("alpha", "Alpha edited"), encoding="utf-8"
        )
        _advance_mtime(docs / "notes" / "alpha.html")
        (docs / "notes" / "inner" / "delta.html").write_text(
            _plan_doc("delta", "Delta"), encoding="utf-8"
        )
        (docs / "notes" / "beta.html").unlink()
        _burst(watch)
        with _spies(monkeypatch) as calls:
            rows = _read(docs, repo)
    finally:
        watch.close()

    titles = {row["slug"]: row["title"] for row in rows}
    assert titles["alpha"] == "Alpha edited"
    assert "delta" in titles
    assert "beta" not in titles
    assert calls["build"] == 0
    assert set(calls["dirs"]) == {
        os.fspath(docs / "notes"),
        os.fspath(docs / "notes" / "inner"),
    }, "a kernel burst stat-ed a directory no marked file lives in"

    assert rows == _full_walk_rows(docs, repo)


# ── (3) the detector's fast pass ──────────────────────────────────────────


def test_the_fast_pass_marks_the_directory_that_moved(
    tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A moved directory is re-scanned; nothing else is."""

    docs, repo = tree
    before = _persist(docs, repo)
    watch = _fleet_change_watch(docs, monkeypatch)
    resolved = docs.resolve()
    watch._fast_tree(resolved)

    (docs / "notes" / "inner" / "note.json").write_text("{}", encoding="utf-8")
    watch._fast_tree(resolved)
    with _spies(monkeypatch) as calls:
        unchanged = _read(docs, repo)
    assert unchanged == before
    assert calls["build"] == 0
    assert set(calls["dirs"]) == {os.fspath(docs / "notes" / "inner")}

    (docs / "notes" / "inner" / "added.html").write_text(
        _plan_doc("added", "Added"), encoding="utf-8"
    )
    watch._fast_tree(resolved)
    with _spies(monkeypatch) as calls:
        rows = _read(docs, repo)
    assert "added" in _slugs(rows)
    assert calls["build"] == 0
    assert set(calls["dirs"]) == {os.fspath(docs / "notes" / "inner")}

    assert rows == _full_walk_rows(docs, repo)


def _fleet_change_watch(
    docs: Path, monkeypatch: pytest.MonkeyPatch
) -> change_streams._FleetChangeWatch:
    return _detector(docs, monkeypatch)


# ── (4) git identity ──────────────────────────────────────────────────────


def _commit(repo: Path, path: Path, when: str) -> None:
    _git(["add", os.fspath(path.relative_to(repo))], repo)
    _git(
        [
            "commit",
            "-q",
            "-m",
            f"touch {path.name}",
        ],
        repo,
        when=when,
    )


def test_a_commit_marks_only_the_covered_paths_it_touches(
    tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staging marks nothing, a JSON commit marks nothing, an HTML commit marks its file."""

    docs, repo = tree
    before = _persist(docs, repo)
    watch = _detector(docs, monkeypatch)
    resolved = docs.resolve()
    watch._fast_tree(resolved)

    # Staged but uncommitted: the git index moved, HEAD did not.
    (docs / "notes" / "alpha.html").write_text(
        _plan_doc("alpha", "Alpha"), encoding="utf-8"
    )
    _advance_mtime(docs / "notes" / "alpha.html")
    _git(["add", "docs/notes/alpha.html"], repo)
    watch._fast_tree(resolved)
    with _spies(monkeypatch) as calls:
        staged_rows = _read(docs, repo)
    assert staged_rows == before
    assert calls["build"] == 0
    assert calls["dirs"] == [], "a staged change stat-ed recorded directories"

    # A commit touching only a state file: no covered path, no commit date.
    state = docs / "state" / _PROJECT / "index.json"
    state.write_text(json.dumps({"version": 2}), encoding="utf-8")
    _commit(repo, state, "2026-01-02T00:00:00+0000")
    watch._fast_tree(resolved)
    with _spies(monkeypatch) as calls:
        json_rows = _read(docs, repo)
    assert json_rows == before
    assert calls["build"] == 0, "a JSON-only commit swept the index"
    assert calls["dirs"] == []

    # A commit touching a covered document moves that row's edited stamp.
    alpha = docs / "notes" / "alpha.html"
    alpha.write_text(_plan_doc("alpha", "Alpha committed"), encoding="utf-8")
    # Older than the commit that will carry it, so the row's edited stamp is the
    # commit date rather than the working tree's mtime.
    os.utime(alpha, (1_700_000_000, 1_700_000_000))
    _commit(repo, alpha, "2026-01-03T00:00:00+0000")
    watch._fast_tree(resolved)
    with _spies(monkeypatch) as calls:
        committed = _read(docs, repo)
    newest = {row["slug"]: row for row in committed if row["slug"] == "alpha"}["alpha"]
    commit_time = int(
        subprocess.run(
            ["git", "-C", os.fspath(repo), "log", "-1", "--format=%ct", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, **_GIT_ENV},
        ).stdout.strip()
    )
    # The row renders a commit date the way the served process does: local
    # time, with no offset spelled out.
    expected = (
        datetime.fromtimestamp(commit_time, tz=UTC)
        .astimezone()
        .replace(tzinfo=None)
        .isoformat(timespec="seconds")
    )
    assert newest["edited"] == expected, "the commit date never reached the row"
    assert newer_titles(committed)["alpha"] == "Alpha committed"
    assert calls["build"] == 0, "an HTML commit swept the index"
    assert set(calls["dirs"]) == {os.fspath(docs / "notes")}


def newer_titles(rows: list[dict]) -> dict[str, str]:
    return {str(row["slug"]): str(row["title"]) for row in rows}


# ── (5) a change no source can attribute ──────────────────────────────────


def test_an_unattributable_burst_drops_the_build(
    tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An overflowed event queue costs the next read the full sweep."""

    docs, repo = tree
    before = _persist(docs, repo)
    watch = _watch(docs)
    try:
        # The kernel reports an overflow as an event with no name; the burst
        # that follows is a fragment of what happened.
        watch._overflowed = True
        (docs / "notes" / "alpha.html").write_text(
            _plan_doc("alpha", "Alpha after overflow"), encoding="utf-8"
        )
        _burst(watch)
        with _spies(monkeypatch) as calls:
            rows = _read(docs, repo)
    finally:
        watch.close()

    assert rows != before
    assert calls["build"] == 1, "an unattributable change kept the build"
    assert calls["dirs"], "an unattributable change stat-ed no directory"

    assert rows == _full_walk_rows(docs, repo)


# ── (6) the detector's slow pass ──────────────────────────────────────────


def test_the_slow_pass_marks_the_file_that_moved(
    tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-place edit is absorbed; a state rewrite marks nothing."""

    docs, repo = tree
    _persist(docs, repo)
    watch = _detector(docs, monkeypatch)
    resolved = docs.resolve()
    watch._slow_tree(resolved)

    alpha = docs / "notes" / "alpha.html"
    alpha.write_text(_plan_doc("alpha", "Alpha swept"), encoding="utf-8")
    _advance_mtime(alpha)
    watch._slow_tree(resolved)
    with _spies(monkeypatch) as calls:
        rows = _read(docs, repo)
    assert newer_titles(rows)["alpha"] == "Alpha swept"
    assert calls["build"] == 0, "the slow pass swept the index"
    assert set(calls["dirs"]) == {os.fspath(docs / "notes")}

    state = docs / "state" / _PROJECT / "index.json"
    state.write_text(json.dumps({"version": 3}), encoding="utf-8")
    _advance_mtime(state, seconds=60)
    after_edit = _read(docs, repo)
    watch._slow_tree(resolved)
    with _spies(monkeypatch) as calls:
        unchanged = _read(docs, repo)
    assert unchanged == after_edit
    assert calls["build"] == 0, "a state rewrite dropped the build"
    assert calls["dirs"] == []

    assert rows == _full_walk_rows(docs, repo)


# ── (7) an open change stream ─────────────────────────────────────────────


class _Stream:
    """Read one ``/_changes`` frame stream over a raw socket."""

    def __init__(self, port: int, project: str) -> None:
        self._sock = socket.create_connection(("127.0.0.1", port))
        self._sock.sendall(
            (
                f"GET /_changes/{project} HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\nAccept: text/event-stream\r\n\r\n"
            ).encode()
        )
        self._file = self._sock.makefile("r", encoding="utf-8", newline="")

    def next_event(self) -> tuple[str, dict | None]:
        event = ""
        data = None
        while True:
            line = self._file.readline()
            if not line:
                raise AssertionError("the stream closed")
            line = line.rstrip("\r\n")
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


def test_an_open_stream_keeps_the_build_across_a_json_write(
    tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows the stream reads after a JSON burst come from the kept build."""

    docs, repo = tree
    monkeypatch.setattr(serve, "load_mounts", lambda: {_PROJECT: docs})
    _persist(docs, repo)
    read_calls = {"count": 0}
    real_rows = metadata_index.index_rows

    def counting_rows(*args, **kwargs):
        read_calls["count"] += 1
        return real_rows(*args, **kwargs)

    monkeypatch.setattr(metadata_index, "index_rows", counting_rows)
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    key = (_PROJECT, str(docs.resolve()), True)
    stream = None
    try:
        stream = _Stream(server.server_port, _PROJECT)
        assert stream.next_event()[0] == "ready"
        opened = read_calls["count"]
        state = docs / "state" / _PROJECT / "crew-record.json"
        state.write_text(json.dumps({"run": 2}), encoding="utf-8")
        deadline = time.monotonic() + 10.0
        while read_calls["count"] <= opened and time.monotonic() < deadline:
            time.sleep(0.05)
        assert read_calls["count"] > opened, "the stream never read after the burst"
        kept = metadata_index._CACHE[key]
        with _spies(monkeypatch) as calls:
            (docs / "state" / _PROJECT / "second-record.json").write_text(
                "{}", encoding="utf-8"
            )
            deadline = time.monotonic() + 10.0
            before_read = read_calls["count"]
            while read_calls["count"] <= before_read and time.monotonic() < deadline:
                time.sleep(0.05)
            assert read_calls["count"] > before_read
    finally:
        if stream is not None:
            stream.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert calls["build"] == 0, "the stream's read swept the index"
    assert metadata_index._CACHE[key] is kept


# ── (8) a change an absorbing read's write folded in ──────────────────────


def test_a_change_folded_into_an_absorbing_write_still_fires(
    tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory the read's shape already includes is still reported.

    The absorbing read re-scans a marked directory and then writes the shape it
    measured. A second document landing between that scan and that write is
    inside the shape the detector reloads, so a reload that adopts it wholesale
    compares the directory against a stamp that already contains the second
    write and reports nothing — the pass is silent and no stream is woken.
    """

    docs, repo = tree
    _persist(docs, repo)
    detector = _fleet_change_watch(docs, monkeypatch)
    resolved = docs.resolve()
    detector._fast_tree(resolved)

    (docs / "notes" / "alpha.html").write_text(
        _plan_doc("alpha", "Alpha edited"), encoding="utf-8"
    )
    metadata_index.mark_tree_paths(
        docs, metadata_index.TreeChange(dirs=frozenset({"notes"}))
    )
    # Written after the mark and before the read that absorbs it, so the read's
    # own re-scan measures the directory with both writes in it.
    (docs / "notes" / "gamma.html").write_text(
        _plan_doc("gamma", "Gamma"), encoding="utf-8"
    )
    rows = _read(docs, repo)
    assert "gamma" in _slugs(rows), "the read did not absorb the marked directory"

    woken: list[Path] = []
    monkeypatch.setattr(detector, "notify", woken.append)

    detector._fast_tree(resolved)

    assert woken == [resolved], "the pass reported nothing for the second write"
    marked = metadata_index._MARKS.get(str(resolved))
    assert marked is not None and "notes" in marked.dirs, (
        "the pass reported a change without marking the directory it saw"
    )
    assert "gamma" in _slugs(_read(docs, repo))
