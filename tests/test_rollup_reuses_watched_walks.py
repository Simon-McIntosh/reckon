"""The fleet rollup reuses walks, and a watched tree's change drops its walk.

A page load asks the rollup for every mounted project, and validating one
project's cached discovery walks that project's whole docs tree. The served
process keeps a kernel watch on every mounted tree and drops that tree's
memoised walk the moment the tree changes, so twenty rollup requests with
nothing changed walk nothing, and a rewrite is seen on the next request rather
than when a reuse window lapses.

Everything here runs over temporary mounted projects reached through a
temporary mounts file and a temporary state root, so no real workspace is read
or written.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
from pathlib import Path

import pytest

from reckon import crew, file_memo, serve

# Armed only by the negative-control run: it leaves the served watch unstarted,
# so a rewrite is not seen until the reuse window lapses and the within-1-s
# assertion must fail.
_SKIP_WATCH_ENV = "RECKON_TEST_SKIP_WATCH"

# The rollup must see a watched tree's change within this many seconds. The
# served reuse window is far longer, so only the watch can satisfy it.
_VISIBLE_WITHIN_S = 1.0
_POLL_S = 0.02

_PROJECTS = ("alpha", "beta", "gamma")
_SLUG = "work"


def _plan_html_text(project: str, slug: str, title: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{project}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{title}">
<meta name="plan-status" content="active">
<title>{title}</title></head><body><main class="plan-doc"></main></body></html>
"""


def _clear_caches() -> None:
    serve._DISC_CACHE.clear()
    serve._SIGNATURE_MEMO.clear()
    file_memo.clear()


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch):
    """No discovery, signature or memo state survives in or out of a test."""

    _clear_caches()
    monkeypatch.setattr(serve, "_SIGNATURE_TTL_S", serve._SERVED_SIGNATURE_TTL_S)
    yield
    _clear_caches()


@pytest.fixture()
def mounted(tmp_path, monkeypatch):
    """Three temporary mounted projects behind a temporary mounts file."""

    trees = {}
    for name in _PROJECTS:
        docs = tmp_path / name / "docs"
        (docs / "plans").mkdir(parents=True)
        (docs / "plans" / f"{_SLUG}.html").write_text(
            _plan_html_text(name, _SLUG, f"{name} work"), encoding="utf-8"
        )
        trees[name] = docs

    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(
        json.dumps({name: str(docs) for name, docs in trees.items()}), encoding="utf-8"
    )
    state_root = tmp_path / "state"
    state_root.mkdir()
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    monkeypatch.setattr(serve, "_STATE_ROOT", state_root)
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    # The rollup reads live crew pointers from the real config home otherwise.
    monkeypatch.setattr(crew, "list_live", lambda **_kwargs: [])
    return serve.load_mounts(), trees


@pytest.fixture()
def fleet_watch(mounted):
    """Start the served process's watch over the mounted trees."""

    if os.environ.get(_SKIP_WATCH_ENV) == "1":
        yield None
        return
    mounts, _trees = mounted
    serve._FLEET_WATCH = None
    watch = serve.start_fleet_change_watch(mounts)
    try:
        yield watch
    finally:
        watch.close()
        serve._FLEET_WATCH = None


@pytest.fixture()
def walk_counter(monkeypatch):
    """Record every docs tree whose signature walk actually ran."""

    original = serve._walk_discovery_signature
    walked: list[Path] = []

    def counted(docs_dir, project, state_root):
        walked.append(Path(docs_dir).resolve())
        return original(docs_dir, project, state_root)

    monkeypatch.setattr(serve, "_walk_discovery_signature", counted)
    return walked


def _mount_trees(mounts: dict[str, Path]) -> set[Path]:
    return {Path(path).resolve() for path in mounts.values()}


def _title(docs_dir: Path, project: str, slug: str) -> str:
    """Read one plan's title as the rollup would, sharing its state root."""

    result = serve.discover_plans(docs_dir, project, serve._STATE_ROOT)
    return next(
        item["title"] for item in result["inventory"] if item.get("slug") == slug
    )


def _rewrite(path: Path, project: str, title: str) -> None:
    """Rewrite one plan file, advancing its mtime past the current second."""

    previous = path.stat().st_mtime_ns
    path.write_text(_plan_html_text(project, _SLUG, title), encoding="utf-8")
    os.utime(
        path,
        ns=(
            path.stat().st_atime_ns,
            max(path.stat().st_mtime_ns, previous) + 1_000_000_000,
        ),
    )


def _await(condition, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(_POLL_S)
    return condition()


def test_no_change_walks_nothing_after_the_cold_pass(
    mounted, walk_counter, fleet_watch
):
    mounts, _trees = mounted

    serve.collect_projects(mounts)

    # Positive control: the counter must have seen one walk per tree, so the
    # unchanged-tree zero below is distinguishable from an instrument that
    # never fired.
    assert set(walk_counter) == _mount_trees(mounts)

    walk_counter.clear()
    for _ in range(20):
        serve.collect_projects(mounts)

    assert walk_counter == []


def test_a_watched_tree_change_is_seen_within_a_second(
    mounted, walk_counter, fleet_watch
):
    mounts, trees = mounted
    serve.collect_projects(mounts)
    walk_counter.clear()

    alpha = trees["alpha"].resolve()
    _rewrite(alpha / "plans" / f"{_SLUG}.html", "alpha", "alpha work rewritten")

    def _poll_rollup() -> bool:
        serve.collect_projects(mounts)
        return bool(walk_counter)

    seen = _await(_poll_rollup, _VISIBLE_WITHIN_S)
    assert seen, "the rewrite was not seen within 1 s"
    assert walk_counter == [alpha], "only the changed tree may be walked"

    # The rollup's own call left the fresh walk memoised, so reading the title
    # costs no walk and must not disturb the count above.
    assert _title(alpha, "alpha", _SLUG) == "alpha work rewritten"
    assert walk_counter == [alpha]


def test_the_served_reuse_window_is_sixty_seconds(monkeypatch):
    monkeypatch.delenv("RECKON_DISCOVERY_REUSE_S", raising=False)

    assert serve._served_signature_ttl() == 60.0

    monkeypatch.setenv("RECKON_DISCOVERY_REUSE_S", "7")
    assert serve._served_signature_ttl() == 7.0


def test_a_post_drops_every_memoised_walk(mounted, walk_counter):
    mounts, _trees = mounted
    serve.collect_projects(mounts)
    walk_counter.clear()

    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _body = _post(server.server_port, "/state/alpha/probe", {"hello": 1})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert status == http.client.OK

    # The POST wrote outside every docs tree, so only the write path's own
    # invalidation can empty the memo; the next rollup therefore re-walks all.
    serve.collect_projects(mounts)
    assert set(walk_counter) == _mount_trees(mounts)


def _post(port: int, path: str, payload: dict) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        body = json.dumps(payload).encode()
        connection.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/json", "If-Match": '"0"'},
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        connection.close()
