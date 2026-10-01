"""A metadata index survives a restart: a rebuild costs stats, not reads.

The served process used to rebuild its whole row list from cold after every
restart, one read and parse per plan, evidence and figure file on the shared
filesystem. The index persists one row per file together with the stat
identity it was built from, so a rebuild re-parses only the files whose
identity moved.

The negative control, armed by ``RECKON_TEST_IGNORE_METADATA_INDEX=1``, makes
the rebuild ignore the persisted index: the post-restart build parses all 200
files and the zero-parse assertion below fails.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon import _plan_html, file_memo, metadata_index, serve

_PROJECT = "sample"
_PLAN_COUNT = 140
_EVIDENCE_COUNT = 60
_DOC_COUNT = _PLAN_COUNT + _EVIDENCE_COUNT
_FIGURE_COUNT = 1
_IGNORE_INDEX_ENV = "RECKON_TEST_IGNORE_METADATA_INDEX"
#: A fixed first-commit time (2020-01-01T00:00:00Z) that no live file's ctime
#: or mtime can equal, so a stamp read from git is distinguishable from one
#: read from the inode.
_GIT_COMMIT_TS = 1_577_836_800


def _plan_doc(slug: str, title: str | None = None) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{_PROJECT}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{title or slug}">
<meta name="plan-status" content="active">
<meta name="plan-sprint" content="S1">
<title>{title or slug}</title></head><body><main class="plan-doc"></main></body></html>
"""


def _evidence_doc(slug: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{_PROJECT}">
<meta name="reckon-type" content="evidence">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{slug}">
<title>{slug}</title></head><body><main class="plan-doc"></main></body></html>
"""


def _svg_text(width: int, height: int) -> str:
    return f'<svg viewBox="0 0 {width} {height}"><rect/></svg>'


def _clear_caches() -> None:
    serve._DISC_CACHE.clear()
    serve._GIT_CREATION_CACHE.clear()
    serve._GIT_LAST_MODIFIED_CACHE.clear()
    serve._SIGNATURE_MEMO.clear()
    file_memo.clear()
    metadata_index.clear()


@pytest.fixture(autouse=True)
def _isolated_caches():
    """No parse memo, discovery cache or index survives in or out of a test."""

    _clear_caches()
    yield
    _clear_caches()


@pytest.fixture(autouse=True)
def _negative_control_when_armed(monkeypatch):
    """Arm the declared mutation: rebuild ignoring the persisted index."""

    if os.environ.get(_IGNORE_INDEX_ENV) != "1":
        return

    monkeypatch.setattr(metadata_index, "_load_persisted", lambda docs_dir, project: {})


@pytest.fixture()
def project_tree(tmp_path, monkeypatch):
    """A 200-document temporary project plus one figure, over a temporary home."""

    config_home = tmp_path / "config"
    config_home.mkdir()
    repository = tmp_path / "repository"
    docs_dir = repository / "docs"
    plans_dir = docs_dir / "plans"
    evidence_dir = docs_dir / "evidence"
    figures_dir = docs_dir / "figures" / "plan-000"
    for directory in (plans_dir, evidence_dir, figures_dir):
        directory.mkdir(parents=True)

    for index in range(_PLAN_COUNT):
        slug = f"plan-{index:03d}"
        (plans_dir / f"{slug}.html").write_text(_plan_doc(slug))
    for index in range(_EVIDENCE_COUNT):
        slug = f"note-{index:03d}"
        (evidence_dir / f"{slug}.html").write_text(_evidence_doc(slug))
    (figures_dir / "plot.svg").write_text(_svg_text(640, 480))

    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({_PROJECT: str(docs_dir)}))
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    monkeypatch.setattr(serve, "_STATE_ROOT", None)
    return docs_dir


@pytest.fixture()
def parse_counter(monkeypatch):
    """Count calls to the uncached meta parse, keyed by file."""

    original = _plan_html._parse_meta_uncached
    counts: dict[Path, int] = {}

    def counted(path, slug):
        key = Path(path)
        counts[key] = counts.get(key, 0) + 1
        return original(path, slug)

    monkeypatch.setattr(_plan_html, "_parse_meta_uncached", counted)
    return counts


def _rewrite(path: Path, slug: str, title: str) -> None:
    previous = path.stat().st_mtime_ns
    path.write_text(_plan_doc(slug, title))
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, max(stat.st_mtime_ns, previous + 1)))


def _title(rows: list[dict], slug: str) -> str:
    return next(row["title"] for row in rows if row["slug"] == slug)


@contextmanager
def _served_index():
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(port: int, path: str) -> tuple[int, object]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_rebuild_after_a_restart_parses_nothing(project_tree, parse_counter):
    docs_dir = project_tree

    first = metadata_index.build_index(docs_dir, _PROJECT)

    assert len(first.rows) == _DOC_COUNT + _FIGURE_COUNT
    # Positive control: the cold build parsed every document exactly once, so
    # the post-restart zero below distinguishes a reused index from an
    # instrument that never fired.
    assert len(parse_counter) == _DOC_COUNT
    assert max(parse_counter.values()) == 1

    # A restart: every in-process memo is empty again, the index file is not.
    file_memo.clear()
    metadata_index.clear()
    parse_counter.clear()

    second = metadata_index.build_index(docs_dir, _PROJECT)

    assert parse_counter == {}
    assert second.reused == _DOC_COUNT + _FIGURE_COUNT
    assert second.rebuilt == []
    assert second.rows == first.rows


def test_one_rewritten_file_is_the_only_one_rebuilt(project_tree, parse_counter):
    docs_dir = project_tree
    metadata_index.build_index(docs_dir, _PROJECT)
    file_memo.clear()
    metadata_index.clear()
    parse_counter.clear()
    target = docs_dir / "plans" / "plan-007.html"

    _rewrite(target, "plan-007", "A rewritten title")
    rebuilt = metadata_index.build_index(docs_dir, _PROJECT)

    assert set(parse_counter) == {target}
    assert rebuilt.rebuilt == ["plans/plan-007.html"]
    assert rebuilt.reused == _DOC_COUNT + _FIGURE_COUNT - 1
    assert _title(rebuilt.rows, "plan-007") == "A rewritten title"


def test_a_reported_change_updates_the_rows(project_tree, parse_counter):
    docs_dir = project_tree
    rows = metadata_index.index_rows(docs_dir, _PROJECT)
    assert _title(rows, "plan-011") == "plan-011"
    target = docs_dir / "plans" / "plan-011.html"

    _rewrite(target, "plan-011", "A later title")

    # The in-process rows stand until the watch reports the tree that changed.
    assert _title(metadata_index.index_rows(docs_dir, _PROJECT), "plan-011") == (
        "plan-011"
    )
    metadata_index.invalidate_tree(docs_dir)
    refreshed = metadata_index.index_rows(docs_dir, _PROJECT)

    assert _title(refreshed, "plan-011") == "A later title"


def test_the_index_endpoint_serves_only_row_fields(project_tree, parse_counter):
    docs_dir = project_tree
    metadata_index.build_index(docs_dir, _PROJECT)
    file_memo.clear()
    metadata_index.clear()
    parse_counter.clear()

    with _served_index() as port:
        status, payload = _get(port, f"/_index/{_PROJECT}")

    # The endpoint answered from the persisted index, not from a fresh parse.
    assert status == 200
    assert parse_counter == {}

    declared = (
        "slug",
        "href",
        "type",
        "title",
        "status",
        "sprint",
        "archived",
        "created",
        "edited",
        "width",
        "height",
    )
    assert tuple(metadata_index.ROW_FIELDS) == declared
    assert isinstance(payload, list)
    assert len(payload) == _DOC_COUNT + _FIGURE_COUNT
    assert all(set(row) <= set(declared) for row in payload)
    # Every declared field is carried by at least one row, so a row that
    # silently dropped one is visible rather than merely permitted.
    seen = set().union(*(set(row) for row in payload))
    assert seen == set(declared)


def test_the_index_endpoint_declines_an_unmounted_project(project_tree):
    with _served_index() as port:
        status, payload = _get(port, "/_index/not-a-project")

    assert status == 404
    assert payload["error"] == "unknown project"


def test_the_index_and_discovery_agree_on_stamps(project_tree, monkeypatch):
    """The endpoint's row stamps come from the same source discovery reads.

    The served discovery payload derives created/edited from git first/last
    commit time; the loader merges those rows over the index's, so an index
    that stamped from the inode would move a document's timestamps at readiness.
    """

    docs_dir = project_tree
    repo_dir = docs_dir.parent
    git_times = {
        str(path.relative_to(repo_dir)): _GIT_COMMIT_TS
        for _relative, path in metadata_index._covered_files(docs_dir)
    }
    monkeypatch.setattr(serve, "_git_first_committed", lambda repo, docs: git_times)
    monkeypatch.setattr(serve, "_git_last_committed", lambda repo, docs: git_times)

    with _served_index() as port:
        status, payload = _get(port, f"/_index/{_PROJECT}")

    assert status == 200
    row = next(item for item in payload if item["slug"] == "plan-000")
    expected = serve._row_times(
        docs_dir / "plans" / "plan-000.html", repo_dir, git_times, git_times
    )
    # The index's created stamp is the git first-commit time, not the inode's
    # ctime, and matches what the discovery rule returns for the same file.
    assert row["created"] == _GIT_COMMIT_TS
    assert (row["created"], row["edited"]) == expected


def test_the_change_watch_invalidates_the_tree(project_tree, monkeypatch):
    """A reported change drops the changed tree's in-process index rows.

    The reader thread routes its invalidation through serve._invalidate_tree_views;
    a watch that consumed the event without dropping the index would leave the
    page serving the pre-change rows.
    """

    docs_dir = project_tree
    # Warm the in-process rows, so a dropped index is observable.
    assert metadata_index.index_rows(docs_dir, _PROJECT)

    invalidated: list[Path] = []
    real = serve._invalidate_tree_views

    def spy(root):
        invalidated.append(Path(root))
        real(root)

    monkeypatch.setattr(serve, "_invalidate_tree_views", spy)
    watch = serve._FleetChangeWatch([docs_dir])
    watch.start()
    try:
        _rewrite(docs_dir / "plans" / "plan-001.html", "plan-001", "a watched change")
        deadline = time.monotonic() + 10
        while not invalidated and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        watch.close()

    assert invalidated, "the change watch never invalidated the tree's views"
    assert invalidated[0] == docs_dir.resolve()
    # The invalidation dropped the rows, so the next read rebuilds them.
    assert _title(metadata_index.index_rows(docs_dir, _PROJECT), "plan-001") == (
        "a watched change"
    )


def test_the_index_and_discovery_walk_one_tree(project_tree, monkeypatch):
    """Discovery's change signature sweeps the index's single docs-tree walk.

    The index's covered set and discovery's counted set are the same walk, so
    they cannot drift into two implementations that disagree on symlink policy
    or figure suffixes.
    """

    docs_dir = project_tree
    walked: list[Path] = []
    original = metadata_index._covered_files

    def spy(root):
        walked.append(Path(root))
        return original(root)

    monkeypatch.setattr(metadata_index, "_covered_files", spy)

    count, _newest = serve._walk_discovery_signature(docs_dir, _PROJECT, None)

    assert walked == [docs_dir]
    assert count == len(original(docs_dir))
