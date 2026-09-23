"""Discovery parses each plan file at most once per unchanged state.

A page load rediscovers the whole fleet, and discovery used to re-walk the docs
tree and re-parse every file for reasons that compound: the inventory scan, the
per-plan wiring check that re-resolves a slug, and a tree signature that ran on
every call. This module pins the parse count to the structural minimum — one
parse per HTML file cold, none while nothing on disk changed, and only the
rewritten file after a single edit — and pins the fleet activity history to one
``git log`` per unchanged ``HEAD``.
"""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest

import reckon.serve as serve
from reckon import _plan_html, file_memo, fleet_index, resources, roadmap

# Armed only by the negative-control run: it makes the memo and the shared tree
# scan inert so the cold-parse assertion must see the compounding re-reads the
# memo and the scan exist to remove.
_DISABLE_MEMO_ENV = "RECKON_TEST_DISABLE_FILE_MEMO"
_PLAN_COUNT = 40


def _plan_html_text(project: str, slug: str, status: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{project}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{slug}">
<meta name="plan-status" content="{status}">
<title>{slug}</title></head><body><main class="plan-doc"></main></body></html>
"""


def _clear_caches() -> None:
    serve._DISC_CACHE.clear()
    serve._GIT_CREATION_CACHE.clear()
    serve._GIT_LAST_MODIFIED_CACHE.clear()
    serve._SIGNATURE_MEMO.clear()
    file_memo.clear()
    fleet_index._ACTIVITY_DAYS_CACHE.clear()


@pytest.fixture(autouse=True)
def _isolated_caches(monkeypatch):
    """No discovery, signature or history state survives in or out of a test."""

    _clear_caches()
    monkeypatch.setattr(serve, "_SIGNATURE_TTL_S", 0.0)
    yield
    _clear_caches()


@pytest.fixture(autouse=True)
def _negative_control_when_armed(monkeypatch):
    """Arm the declared mutation: bypass the memo and the shared tree scan."""

    if os.environ.get(_DISABLE_MEMO_ENV) != "1":
        return

    def direct(kind, path, compute, *, variant=None):
        del kind, path, variant
        return compute()

    monkeypatch.setattr(file_memo, "memoized", direct)
    for module in (_plan_html, serve, roadmap):
        if hasattr(module, "memoized"):
            monkeypatch.setattr(module, "memoized", direct)

    @contextmanager
    def _no_scope():
        yield

    monkeypatch.setattr(resources, "resource_scan_scope", _no_scope)
    monkeypatch.setattr(serve, "resource_scan_scope", _no_scope)
    monkeypatch.setattr(roadmap, "resource_scan_scope", _no_scope)


@pytest.fixture()
def docs_tree(tmp_path, monkeypatch):
    """A 40-plan temporary docs tree reached through a temporary mounts file."""

    project = "sample"
    docs_dir = tmp_path / project / "docs"
    plans_dir = docs_dir / "plans"
    plans_dir.mkdir(parents=True)
    for index in range(_PLAN_COUNT):
        slug = f"plan-{index:02d}"
        (plans_dir / f"{slug}.html").write_text(
            _plan_html_text(project, slug, "active")
        )

    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({project: str(docs_dir)}))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    monkeypatch.setattr(serve, "_STATE_ROOT", None)
    # The wiring check resolves a plan slug through the registered mount; point
    # shared mounts resolution at the temporary file so nothing reads a real
    # workspace mount.
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    return docs_dir, project


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


def _inventory_status(result: dict, slug: str) -> str:
    return next(
        item["status"]
        for item in result["inventory"]
        if item.get("slug") == slug and item.get("type") == "plan"
    )


def _rewrite_plan(path: Path, project: str, slug: str, status: str) -> None:
    previous = path.stat().st_mtime_ns
    path.write_text(_plan_html_text(project, slug, status))
    os.utime(
        path,
        ns=(path.stat().st_atime_ns, max(path.stat().st_mtime_ns, previous + 1)),
    )


def test_cold_discovery_parses_each_html_file_at_most_once(docs_tree, parse_counter):
    docs_dir, project = docs_tree

    result = serve.discover_plans(docs_dir, project, None)

    plan_rows = [item for item in result["inventory"] if item.get("type") == "plan"]
    assert len(plan_rows) == _PLAN_COUNT

    # Positive control: the counter must have seen a parse for every plan file,
    # so an unchanged-file zero is distinguishable from an instrument that
    # never fired.
    parsed_files = {path for path in parse_counter if path.suffix == ".html"}
    expected = {
        docs_dir / "plans" / f"plan-{index:02d}.html" for index in range(_PLAN_COUNT)
    }
    assert parsed_files == expected
    assert max(parse_counter.values()) <= 1


def test_second_discovery_with_no_change_parses_none(docs_tree, parse_counter):
    docs_dir, project = docs_tree
    first = serve.discover_plans(docs_dir, project, None)
    assert parse_counter  # positive control: the cold pass did parse
    parse_counter.clear()

    second = serve.discover_plans(docs_dir, project, None)

    assert second["inventory"] == first["inventory"]
    assert parse_counter == {}


def test_rewritten_file_is_the_only_one_reparsed(docs_tree, parse_counter):
    docs_dir, project = docs_tree
    serve.discover_plans(docs_dir, project, None)
    target = docs_dir / "plans" / "plan-07.html"
    parse_counter.clear()

    _rewrite_plan(target, project, "plan-07", "shipped")
    result = serve.discover_plans(docs_dir, project, None)

    assert _inventory_status(result, "plan-07") == "shipped"
    assert set(parse_counter) == {target}


def test_signature_reuse_defers_a_rewrite_until_invalidated(
    docs_tree, parse_counter, monkeypatch
):
    docs_dir, project = docs_tree
    monkeypatch.setattr(serve, "_SIGNATURE_TTL_S", 60.0)
    serve.discover_plans(docs_dir, project, None)
    target = docs_dir / "plans" / "plan-11.html"

    _rewrite_plan(target, project, "plan-11", "shipped")
    held = serve.discover_plans(docs_dir, project, None)
    assert _inventory_status(held, "plan-11") == "active"

    serve._invalidate_discovery_signatures(docs_dir)
    refreshed = serve.discover_plans(docs_dir, project, None)
    assert _inventory_status(refreshed, "plan-11") == "shipped"


def _activity_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo_dir = tmp_path / "repository"
    docs_dir = repo_dir / "docs"
    (docs_dir / "plans").mkdir(parents=True)
    (repo_dir / ".git").mkdir()
    return repo_dir, docs_dir


def test_activity30_reuses_head_and_runs_no_git_log(tmp_path, monkeypatch):
    repo_dir, docs_dir = _activity_repo(tmp_path)
    calls: list[list[str]] = []

    def run(args, **kwargs):
        del kwargs
        calls.append(list(args))
        if args[1:3] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "head-abc\n", "")
        return subprocess.CompletedProcess(args, 0, "2026-03-14\n2026-03-15\n", "")

    monkeypatch.setattr(fleet_index.subprocess, "run", run)
    now = datetime(2026, 3, 15, 12, 0, 0)  # noqa: DTZ001 — naive, matches _activity30

    first = fleet_index._activity30(repo_dir, docs_dir, now)
    assert len(first) == 30
    assert sum(first) == 2
    assert any("log" in args for args in calls)  # positive control: a log did run

    calls.clear()
    second = fleet_index._activity30(repo_dir, docs_dir, now)

    assert second == first
    assert [args for args in calls if "log" in args] == []


def test_activity30_git_timeout_yields_empty_series(tmp_path, monkeypatch):
    repo_dir, docs_dir = _activity_repo(tmp_path)
    head_return = "head-abc\n"

    def run(args, **kwargs):
        if args[1:3] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, head_return, "")
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 0))

    monkeypatch.setattr(fleet_index.subprocess, "run", run)
    now = datetime(2026, 3, 15, 12, 0, 0)  # noqa: DTZ001 — naive, matches _activity30

    assert fleet_index._activity30(repo_dir, docs_dir, now) == []