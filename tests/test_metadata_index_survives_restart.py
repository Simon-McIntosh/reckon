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
