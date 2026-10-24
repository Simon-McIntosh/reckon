"""A cold read sweeps each recorded directory once, however many readers arrive.

A process restart drops the in-process index, so the first read after it pays
the cache-miss path: :func:`_reuse_covered` stats every recorded directory to
decide what moved, and the build then recorded the same identities again. A
second reader arriving at the same moment swept the same tree a third time.
The path now runs in one reader at a time per tree and stamp source, the
identities it read are handed to the build and to the shape record, and a tree
with a path in flight takes marks so a change landing mid-path still reaches
the rows it returns.

The negative controls are armed one per environment variable, each a defect
the case it names must fail against:

* ``RECKON_TEST_COLD_NO_WAIT`` lets every reader sweep, removing the wait.
* ``RECKON_TEST_COLD_BUILD_REREADS`` has the build read the identities again.
* ``RECKON_TEST_COLD_WAITERS_INHERIT`` makes waiters return the failed path's
  result instead of reading for themselves.
* ``RECKON_TEST_COLD_SHAPE_REREADS`` has the shape record read the identities
  again.
* ``RECKON_TEST_COLD_MARK_DROPPED`` keeps ``mark_tree_paths`` dropping marks
  for a tree with no cached build.
* ``RECKON_TEST_COLD_WAITERS_SKIP_CACHE`` returns the leader's rows to waiters
  without reading again.
* ``RECKON_TEST_COLD_STORE_ANYWAY`` stores the build regardless of the
  generation it started against.
* ``RECKON_TEST_COLD_EARLY_STORE`` publishes the build before the path takes
  its marks, so a reader entering in that gap consumes them.
"""

from __future__ import annotations

import json
import os
import threading
from collections import Counter
from pathlib import Path

import pytest

from reckon import file_memo, metadata_index

_PROJECT = "sample"
_READERS = 6
_EXPECTED_SLUGS = ["alpha", "beta", "gamma", "plot.svg"]

_NO_WAIT = "RECKON_TEST_COLD_NO_WAIT"
_BUILD_REREADS = "RECKON_TEST_COLD_BUILD_REREADS"
_WAITERS_INHERIT = "RECKON_TEST_COLD_WAITERS_INHERIT"
_SHAPE_REREADS = "RECKON_TEST_COLD_SHAPE_REREADS"
_MARK_DROPPED = "RECKON_TEST_COLD_MARK_DROPPED"
_WAITERS_SKIP_CACHE = "RECKON_TEST_COLD_WAITERS_SKIP_CACHE"
_STORE_ANYWAY = "RECKON_TEST_COLD_STORE_ANYWAY"
_EARLY_STORE = "RECKON_TEST_COLD_EARLY_STORE"


def _armed(name: str) -> bool:
    return os.environ.get(name) == "1"


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


@pytest.fixture(autouse=True)
def _isolated_caches():
    _clear_caches()
    yield
    _clear_caches()


@pytest.fixture
def docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A docs tree with several recorded directories, over a temporary home."""

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = tmp_path / "checkout" / "docs"
    (root / "notes" / "inner").mkdir(parents=True)
    (root / "figures").mkdir()
    (root / "state" / _PROJECT).mkdir(parents=True)
    (root / "notes" / "alpha.html").write_text(
        _plan_doc("alpha", "Alpha"), encoding="utf-8"
    )
    (root / "notes" / "beta.html").write_text(
        _plan_doc("beta", "Beta"), encoding="utf-8"
    )
    (root / "notes" / "inner" / "gamma.html").write_text(
        _plan_doc("gamma", "Gamma"), encoding="utf-8"
    )
    (root / "figures" / "plot.svg").write_text(
        '<svg viewBox="0 0 4 3"><rect/></svg>', encoding="utf-8"
    )
    (root / "state" / _PROJECT / "index.json").write_text(
        json.dumps({"version": 1}), encoding="utf-8"
    )
    return root


def _rows(docs: Path) -> list[dict]:
    return metadata_index.index_rows(docs, _PROJECT)


def _persist(docs: Path) -> list[dict]:
    """Build the index and hold it, as a served process's first read does."""

    metadata_index.clear()
    return _rows(docs)


def _directory_count(docs: Path) -> int:
    return len(metadata_index._load_dir_stamps(docs, _PROJECT))


def _slugs(rows: list[dict]) -> list[str]:
    return sorted(row["slug"] for row in rows)


class _IdentitySpy:
    """Count every directory identity a read takes, and hold the reader."""

    def __init__(self, real) -> None:
        self._real = real
        self.counts: Counter[str] = Counter()
        self._lock = threading.Lock()
        self._seen_any = False
        self._threads: set[int] = set()
        self.hook = None

    def __call__(self, path: Path) -> list[int] | None:
        with self._lock:
            self.counts[os.fspath(path)] += 1
            overall_first = not self._seen_any
            self._seen_any = True
            ident = threading.get_ident()
            thread_first = ident not in self._threads
            self._threads.add(ident)
            hook = self.hook
        if hook is not None:
            hook(overall_first, thread_first)
        return self._real(path)


def _install_spy(monkeypatch: pytest.MonkeyPatch, real=None):
    """Count ``_dir_identity`` calls, optionally standing in for the real one."""

    actual = metadata_index._dir_identity if real is None else real
    spy = _IdentitySpy(actual)
    monkeypatch.setattr(metadata_index, "_dir_identity", spy)
    return spy


def _run_threads(targets: list) -> None:
    """Run one reader per target and wait for every one of them."""

    threads = [threading.Thread(target=target, daemon=True) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not any(thread.is_alive() for thread in threads), "a reader never finished"


def _hold_until_all_arrived() -> tuple[threading.Lock, threading.Event]:
    state = {"arrived": 0}
    lock = threading.Lock()
    arrived = threading.Event()

    def note() -> None:
        with lock:
            state["arrived"] += 1
            if state["arrived"] >= _READERS:
                arrived.set()

    return note, arrived


# ── (1) several readers arrive cold together ──────────────────────────────


def test_concurrent_cold_readers_sweep_each_directory_once(
    docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readers arriving together cause one identity read per recorded directory."""

    _persist(docs)
    directory_count = _directory_count(docs)
    metadata_index.clear()

    spy = _install_spy(monkeypatch)

    note, all_arrived = _hold_until_all_arrived()
    inside_lock = threading.Lock()
    inside: set[int] = set()
    all_inside = threading.Event()

    def hold_leader(_overall_first: bool, _thread_first: bool) -> None:
        assert all_arrived.wait(10), "the other readers never arrived"

    def hold_each(_overall_first: bool, thread_first: bool) -> None:
        if not thread_first:
            return
        with inside_lock:
            inside.add(threading.get_ident())
            if len(inside) >= _READERS:
                all_inside.set()
        assert all_inside.wait(10), "a reader never reached the build"

    if _armed(_NO_WAIT):
        # Control: every reader sweeps, so the count is a multiple of the
        # directory count instead of one read per directory.
        monkeypatch.setattr(metadata_index, "_join_cold_path", lambda key: None)
        spy.hook = hold_each
    else:
        spy.hook = hold_leader

    results: list[list[dict] | None] = [None] * _READERS
    errors: list[BaseException] = []

    def reader(index: int):
        def run() -> None:
            note()
            try:
                results[index] = _rows(docs)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        return run

    _run_threads([reader(index) for index in range(_READERS)])

    assert not errors, errors
    assert all(rows is not None for rows in results)
    first = _slugs(results[0] or [])
    assert all(_slugs(rows or []) == first for rows in results)
    total = sum(spy.counts.values())
    assert total == directory_count, (
        f"{total} identity reads for {directory_count} recorded directories"
    )
    assert max(spy.counts.values()) == 1


# ── (2) one cold read ─────────────────────────────────────────────────────


def test_one_cold_read_stats_each_recorded_directory_once(
    docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build reuses the identities its shape check already read."""

    _persist(docs)
    directory_count = _directory_count(docs)
    metadata_index.clear()

    if _armed(_BUILD_REREADS):
        # Control: the build stats every directory again, so the count doubles.
        real_build = metadata_index.build_index

        def build_without_identities(*args, **kwargs):
            kwargs["identities"] = None
            return real_build(*args, **kwargs)

        monkeypatch.setattr(metadata_index, "build_index", build_without_identities)

    spy = _install_spy(monkeypatch)
    rows = _rows(docs)

    assert _slugs(rows) == _EXPECTED_SLUGS
    total = sum(spy.counts.values())
    assert total == directory_count, (
        f"{total} identity reads for {directory_count} recorded directories"
    )
    assert max(spy.counts.values()) == 1


# ── (3) a cold path that fails ────────────────────────────────────────────


def test_a_failed_cold_path_lets_its_waiters_read_for_themselves(
    docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing path releases its waiters; each reads and returns rows."""

    _persist(docs)
    metadata_index.clear()

    raised = {"once": False}
    real_identity = metadata_index._dir_identity

    def failing_identity(path: Path) -> list[int] | None:
        if not raised["once"]:
            raised["once"] = True
            raise RuntimeError("directory stat failed")
        return real_identity(path)

    if _armed(_WAITERS_INHERIT):
        # Control: waiters re-raise the failed path's error instead of reading.
        def inherit(flight) -> bool:
            flight.done.wait()
            if flight.failed:
                raise flight.error
            return True

        monkeypatch.setattr(metadata_index, "_await_flight", inherit)

    spy = _install_spy(monkeypatch, failing_identity)
    note, all_arrived = _hold_until_all_arrived()

    def hold_leader(_overall_first: bool, _thread_first: bool) -> None:
        assert all_arrived.wait(10), "the other readers never arrived"

    spy.hook = hold_leader

    results: list[list[dict] | None] = [None] * _READERS
    errors: list[BaseException] = []

    def reader(index: int):
        def run() -> None:
            note()
            try:
                results[index] = _rows(docs)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        return run

    _run_threads([reader(index) for index in range(_READERS)])

    assert len(errors) == 1, f"{len(errors)} readers failed"
    assert isinstance(errors[0], RuntimeError)
    answered = [rows for rows in results if rows is not None]
    assert len(answered) == _READERS - 1
    assert all(_slugs(rows) == _EXPECTED_SLUGS for rows in answered)


# ── (4) a recorded directory gained a covered file ────────────────────────


def test_a_directory_that_gained_a_file_stats_it_once_more(
    docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a directory the re-scan reached for the first time is statted twice."""

    _persist(docs)
    directory_count = _directory_count(docs)
    metadata_index.clear()
    (docs / "notes" / "fresh").mkdir()
    (docs / "notes" / "fresh" / "delta.html").write_text(
        _plan_doc("delta", "Delta"), encoding="utf-8"
    )

    if _armed(_SHAPE_REREADS):
        # Control: the shape record stats every directory again.
        real_shape = metadata_index._record_covered_shape

        def shape_without_identities(*args, **kwargs):
            kwargs["identities"] = None
            return real_shape(*args, **kwargs)

        monkeypatch.setattr(
            metadata_index, "_record_covered_shape", shape_without_identities
        )

    spy = _install_spy(monkeypatch)
    rows = _rows(docs)

    assert "delta" in _slugs(rows)
    total = sum(spy.counts.values())
    assert total == directory_count + 1, (
        f"{total} identity reads for {directory_count} recorded directories "
        "plus the one the re-scan reached for the first time"
    )
    assert spy.counts[os.fspath(docs / "notes" / "fresh")] == 1


# ── (5) a mark recorded while the path runs ───────────────────────────────


def test_a_mark_recorded_while_the_path_runs_reaches_the_leader(
    docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file added after the walk passed its directory still reaches the rows."""

    metadata_index.clear()

    if _armed(_MARK_DROPPED):
        # Control: a tree with no cached build drops the mark, as it used to.
        real_mark = metadata_index.mark_tree_paths

        def drop_without_build(docs_dir: Path, change) -> None:
            root = str(Path(docs_dir).resolve())
            with metadata_index._LOCK:
                cached = any(key[1] == root for key in metadata_index._CACHE)
            if not cached:
                return
            real_mark(docs_dir, change)

        monkeypatch.setattr(metadata_index, "mark_tree_paths", drop_without_build)

    held = threading.Event()
    release = threading.Event()
    real_identity = metadata_index._dir_identity

    def hold_first(path: Path) -> list[int] | None:
        if not held.is_set():
            held.set()
            assert release.wait(10), "the test never released the reader"
        return real_identity(path)

    monkeypatch.setattr(metadata_index, "_dir_identity", hold_first)

    result: dict[str, list[dict]] = {}

    def reader() -> None:
        result["rows"] = _rows(docs)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    assert held.wait(10), "the reader never entered the path"

    (docs / "notes" / "delta.html").write_text(
        _plan_doc("delta", "Delta"), encoding="utf-8"
    )
    metadata_index.mark_tree_paths(
        docs, metadata_index.TreeChange(files=frozenset({"notes/delta.html"}))
    )

    release.set()
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert "delta" in _slugs(result["rows"])


# ── (6) a mark recorded after the path finished ───────────────────────────


def test_a_mark_after_the_path_reaches_a_waiting_reader(
    docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A waiter reads through the cache, so a mark after the path reaches it."""

    _persist(docs)
    metadata_index.clear()
    key = metadata_index._cache_key(docs, _PROJECT, False)

    leader_held = threading.Event()
    release_leader = threading.Event()
    real_identity = metadata_index._dir_identity

    def hold_first(path: Path) -> list[int] | None:
        if not leader_held.is_set():
            leader_held.set()
            assert release_leader.wait(10), "the test never released the leader"
        return real_identity(path)

    monkeypatch.setattr(metadata_index, "_dir_identity", hold_first)

    waiter_waiting = threading.Event()
    real_await = metadata_index._await_flight

    def await_flight(flight) -> bool:
        waiter_waiting.set()
        return real_await(flight)

    monkeypatch.setattr(metadata_index, "_await_flight", await_flight)

    absorb_entered = threading.Event()
    release_waiter = threading.Event()

    if _armed(_WAITERS_SKIP_CACHE):
        # Control: the waiter is handed the leader's rows without reading.
        def skip_cache(key_, docs_dir, project, repo_dir, git_first, git_last):
            absorb_entered.set()
            assert release_waiter.wait(10), "the test never released the waiter"
            with metadata_index._LOCK:
                build = metadata_index._CACHE[key_]
            return [dict(row) for row in build.rows]

        monkeypatch.setattr(metadata_index, "_read_after_flight", skip_cache)
    else:
        real_absorb = metadata_index._absorb_marked

        def held_absorb(*args, **kwargs):
            absorb_entered.set()
            assert release_waiter.wait(10), "the test never released the waiter"
            return real_absorb(*args, **kwargs)

        monkeypatch.setattr(metadata_index, "_absorb_marked", held_absorb)

    results: dict[str, list[dict]] = {}

    def leader() -> None:
        results["leader"] = _rows(docs)

    def waiter() -> None:
        results["waiter"] = _rows(docs)

    first = threading.Thread(target=leader, daemon=True)
    first.start()
    assert leader_held.wait(10), "the leader never entered the path"

    second = threading.Thread(target=waiter, daemon=True)
    second.start()
    assert waiter_waiting.wait(10), "no reader waited on the path"

    release_leader.set()
    assert absorb_entered.wait(10), "the waiter never read the cache"

    (docs / "notes" / "delta.html").write_text(
        _plan_doc("delta", "Delta"), encoding="utf-8"
    )
    metadata_index.mark_tree_paths(
        docs, metadata_index.TreeChange(files=frozenset({"notes/delta.html"}))
    )

    release_waiter.set()
    first.join(timeout=20)
    second.join(timeout=20)
    assert not first.is_alive() and not second.is_alive()

    assert "delta" not in _slugs(results["leader"])
    assert "delta" in _slugs(results["waiter"])
    assert os.fspath(docs.resolve()) == key[1]


# ── (7) a drop recorded while the path runs ───────────────────────────────


def test_a_drop_during_the_path_keeps_its_build_uncached(
    docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build that started before a drop is not stored as the tree's index."""

    _persist(docs)
    metadata_index.clear()

    if _armed(_STORE_ANYWAY):
        # Control: the build is stored regardless of the generation.

        def store_anyway(key_, build, _generation) -> None:
            with metadata_index._LOCK:
                metadata_index._CACHE[key_] = build

        monkeypatch.setattr(metadata_index, "_store_cold_build", store_anyway)

    held = threading.Event()
    release = threading.Event()
    real_identity = metadata_index._dir_identity

    def hold_first(path: Path) -> list[int] | None:
        if not held.is_set():
            held.set()
            assert release.wait(10), "the test never released the reader"
        return real_identity(path)

    monkeypatch.setattr(metadata_index, "_dir_identity", hold_first)

    result: dict[str, list[dict]] = {}

    def reader() -> None:
        result["rows"] = _rows(docs)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    assert held.wait(10), "the reader never entered the path"

    metadata_index.invalidate_tree(docs)
    release.set()
    thread.join(timeout=20)
    assert not thread.is_alive()

    root = str(docs.resolve())
    with metadata_index._LOCK:
        cached = [key for key in metadata_index._CACHE if key[1] == root]
    assert cached == [], "a build that started before the drop was cached"
    assert _slugs(result["rows"]) == _EXPECTED_SLUGS


# ── (8) a reader that enters as the path publishes its build ──────────────


def test_a_reader_entering_the_gap_sees_the_mark_and_so_does_the_path(
    docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mark a reader entering at publication takes must not be lost."""

    metadata_index.clear()
    key = metadata_index._cache_key(docs, _PROJECT, False)
    real_build = metadata_index.build_index
    real_store = metadata_index._store_cold_build

    results: dict[str, list[dict]] = {}

    def inner() -> None:
        results["inner"] = _rows(docs)

    inner_thread = threading.Thread(target=inner, daemon=True)

    def mark_delta() -> None:
        (docs / "notes" / "delta.html").write_text(
            _plan_doc("delta", "Delta"), encoding="utf-8"
        )
        metadata_index.mark_tree_paths(
            docs, metadata_index.TreeChange(files=frozenset({"notes/delta.html"}))
        )

    def enter_the_gap() -> None:
        inner_thread.start()
        inner_thread.join(timeout=10)

    def sweep_then_maybe_publish_early(*args, **kwargs):
        build = real_build(*args, **kwargs)
        mark_delta()
        if _armed(_EARLY_STORE):
            # Control: the build is published before the path takes its
            # marks, so the reader entering here consumes the mark and the
            # path keeps rows from before the change.
            with metadata_index._LOCK:
                metadata_index._CACHE[key] = build
            enter_the_gap()
        return build

    def store_then_let_a_reader_enter(key_, build, generation) -> None:
        real_store(key_, build, generation)
        if not inner_thread.is_alive() and "inner" not in results:
            enter_the_gap()

    monkeypatch.setattr(metadata_index, "build_index", sweep_then_maybe_publish_early)
    monkeypatch.setattr(
        metadata_index, "_store_cold_build", store_then_let_a_reader_enter
    )

    results["path"] = _rows(docs)
    inner_thread.join(timeout=20)
    assert not inner_thread.is_alive(), "the reader never finished"

    marked = sorted([*_EXPECTED_SLUGS, "delta"])
    assert _slugs(results["inner"]) == marked, "the entering reader missed the mark"
    assert _slugs(results["path"]) == marked, "the path's own reader missed the mark"
