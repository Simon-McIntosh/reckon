"""Agent readers answer from the same index the served page paints from.

The MCP server and the CLI run their own processes, with no change watch and a
zero-second reuse window, so a read that derived its answers from
``discover_plans`` walked every tree it touched and parsed every plan file —
cold on each restart — while the served process answered from the persisted
index. A read of one sprint could cost a whole-project parse.

A project-level summary and a sprint summary are list-level reads: they take
which documents exist and their slug, href, type, title, status, sprint and
stamps from ``reckon.metadata_index``, and the project's own sprint, milestone,
blocker and timeline state from the project state document. The heavier
per-document state belongs to a read of that document.

The counters below are installed on every plan-file parse entry point and on
the docs-tree walk, and fire on the cold index build first — so the zero they
read afterwards distinguishes a reused index from an instrument that never
fired. The negative control routes the MCP views back through
``discover_plans``: both counts exceed zero and the zero assertions fail.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon import _plan_html, file_memo, mcp, metadata_index, serve

_PROJECT = "sample"
_PLAN_COUNT = 140
_EVIDENCE_COUNT = 60
_DOC_COUNT = _PLAN_COUNT + _EVIDENCE_COUNT
_SPRINT = "S1"
#: The state document is the project's own derived state: its shape is the
#: sprint read's only input beside the list-level rows.
_STATE = {
    "sprints": [
        {
            "id": _SPRINT,
            "theme": "The index serves the readers",
            "status": "active",
            "items": [{"slug": "plan-000"}, {"slug": "plan-001"}],
        }
    ],
    "milestones": [],
    "blockers": [],
    "timeline": [],
    "active_sprint_id": _SPRINT,
    "north_stars": [],
}


def _plan_doc(slug: str, title: str | None = None, status: str = "active") -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{_PROJECT}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{title or slug}">
<meta name="plan-status" content="{status}">
<meta name="plan-sprint" content="{_SPRINT}">
<title>{title or slug}</title></head><body><main class="plan-doc"></main></body></html>
"""


def _rewrite(path: Path, text: str) -> None:
    """Rewrite one file with a moved stat identity, so a re-stat sees it."""

    previous = path.stat().st_mtime_ns
    path.write_text(text)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, max(stat.st_mtime_ns, previous + 1)))


def _evidence_doc(slug: str, title: str | None = None) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{_PROJECT}">
<meta name="reckon-type" content="evidence">
<meta name="plan-status" content="">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{title or slug}">
<title>{title or slug}</title></head><body><main class="plan-doc"></main></body></html>
"""


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


@pytest.fixture()
def project_tree(tmp_path, monkeypatch):
    """A 200-document temporary project and a temporary configuration home."""

    config_home = tmp_path / "config"
    state_root = config_home / "state"
    repository = tmp_path / "repository"
    docs_dir = repository / "docs"
    plans_dir = docs_dir / "plans"
    evidence_dir = docs_dir / "evidence"
    for directory in (plans_dir, evidence_dir):
        directory.mkdir(parents=True)

    for index in range(_PLAN_COUNT):
        slug = f"plan-{index:03d}"
        (plans_dir / f"{slug}.html").write_text(_plan_doc(slug))
    for index in range(_EVIDENCE_COUNT):
        slug = f"note-{index:03d}"
        (evidence_dir / f"{slug}.html").write_text(_evidence_doc(slug))

    (state_root / _PROJECT).mkdir(parents=True, exist_ok=True)
    (state_root / _PROJECT / "index.json").write_text(
        json.dumps({"version": 1, "data": _STATE}), encoding="utf-8"
    )
    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({_PROJECT: str(docs_dir)}))
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    monkeypatch.setattr(serve, "_STATE_ROOT", state_root)
    return docs_dir


@pytest.fixture()
def counters(monkeypatch):
    """Count plan-file parses and docs-tree walks through the real functions."""

    meta_parses: list[Path] = []
    full_parses: list[Path] = []
    walks: list[tuple] = []
    original_meta = _plan_html._parse_meta_uncached
    original_full = _plan_html._parse_plan_uncached
    original_walk = serve._walk_discovery_signature

    def counted_meta(path, slug):
        meta_parses.append(Path(path))
        return original_meta(path, slug)

    def counted_full(path, slug):
        full_parses.append(Path(path))
        return original_full(path, slug)

    def counted_walk(docs_dir, project, state_root):
        walks.append((docs_dir, project, state_root))
        return original_walk(docs_dir, project, state_root)

    monkeypatch.setattr(_plan_html, "_parse_meta_uncached", counted_meta)
    monkeypatch.setattr(_plan_html, "_parse_plan_uncached", counted_full)
    monkeypatch.setattr(serve, "_walk_discovery_signature", counted_walk)
    return {
        "meta": meta_parses,
        "full": full_parses,
        "walks": walks,
        "clear": lambda: (meta_parses.clear(), full_parses.clear(), walks.clear()),
    }


def test_the_cold_build_parses_every_document_once(project_tree, counters):
    """Positive control: the counters see a parse when one happens."""

    rows = metadata_index.index_rows(project_tree, _PROJECT)

    assert len(counters["meta"]) == _DOC_COUNT
    assert counters["full"] == []
    assert {row["slug"] for row in rows} >= {"plan-000", "plan-001", "note-000"}


def test_a_project_summary_answers_from_the_index(project_tree, counters):
    metadata_index.index_rows(project_tree, _PROJECT)
    # A restart: every in-process memo is empty again, the index file is not.
    file_memo.clear()
    metadata_index.clear()
    counters["clear"]()

    summary = mcp._read_plan(_PROJECT, view="summary")

    # Walks, meta parses, full parses: all three counts stay zero, and the
    # tuple reports each one that moved if the mutation is armed.
    assert (len(counters["walks"]), len(counters["meta"]), len(counters["full"])) == (
        0,
        0,
        0,
    )
    assert summary["state"]["plans"] == _PLAN_COUNT
    # The sprint card and every indexed document are listed, the latter beyond
    # the first page's cursor.
    assert _SPRINT in {row.get("id") for row in summary["resources"]}
    assert summary["pagination"]["total"] == _PLAN_COUNT + _EVIDENCE_COUNT + 1
    # The count the index cannot derive is absent, not reported as zero.
    assert "open_followups" not in summary["state"]


def test_a_sprint_summary_answers_from_the_index(project_tree, counters):
    metadata_index.index_rows(project_tree, _PROJECT)
    file_memo.clear()
    metadata_index.clear()
    counters["clear"]()

    summary = mcp._read_plan(
        project=_PROJECT,
        resource={"project": _PROJECT, "type": "sprint", "id": _SPRINT},
        view="summary",
    )

    # Walks, meta parses, full parses: all three counts stay zero, and the
    # tuple reports each one that moved if the mutation is armed.
    assert (len(counters["walks"]), len(counters["meta"]), len(counters["full"])) == (
        0,
        0,
        0,
    )
    assert summary["resource"]["id"] == _SPRINT
    assert summary["resource"]["type"] == "sprint"
    assert summary["title"] == "The index serves the readers"
    assert summary["state"]["items"] == 2
    assert summary["state"]["status"] == "active"


def test_a_rewritten_document_reaches_the_next_project_summary(project_tree, counters):
    """One process, two calls: the second read sees a file rewritten between."""

    first = mcp._read_plan(_PROJECT, view="summary")
    assert first["state"]["plans"] == _PLAN_COUNT
    target = project_tree / "evidence" / "note-000.html"
    _rewrite(target, _evidence_doc("note-000", "A rewritten title"))

    counters["clear"]()
    second = mcp._read_plan(_PROJECT, view="summary")

    titles = {row.get("slug"): row.get("title") for row in second["resources"]}
    assert titles.get("note-000") == "A rewritten title"
    assert counters["meta"] == [target]
    assert counters["walks"] == []


def test_a_rewritten_document_reaches_the_next_sprint_summary(project_tree, counters):
    """A sprint read re-stats its rows too, so an item's new status lands."""

    def read_sprint():
        return mcp._read_plan(
            project=_PROJECT,
            resource={"project": _PROJECT, "type": "sprint", "id": _SPRINT},
            view="summary",
        )

    first = read_sprint()
    assert first["state"]["completed"] == 0
    target = project_tree / "plans" / "plan-000.html"
    _rewrite(target, _plan_doc("plan-000", status="shipped"))

    counters["clear"]()
    second = read_sprint()

    assert second["state"]["completed"] == 1
    assert counters["meta"] == [target]
    assert counters["walks"] == []
