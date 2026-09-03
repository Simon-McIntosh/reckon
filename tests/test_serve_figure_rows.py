"""Figure discovery rows: dimensions, plan linkage, and edited stamps."""

from __future__ import annotations

import os
import struct
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from reckon import serve

_CREATED_TS = 1_000_000
_LAST_COMMIT_TS = 2_000_000


@pytest.fixture(autouse=True)
def empty_discovery_caches():
    serve._DISC_CACHE.clear()
    serve._GIT_CREATION_CACHE.clear()
    serve._GIT_LAST_MODIFIED_CACHE.clear()
    yield
    serve._DISC_CACHE.clear()
    serve._GIT_CREATION_CACHE.clear()
    serve._GIT_LAST_MODIFIED_CACHE.clear()


def _png_bytes(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
    )


def _svg_text(width: int, height: int) -> str:
    return f'<svg viewBox="0 0 {width} {height}"><rect/></svg>'


def _plan_html(slug: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{slug}">
<meta name="plan-status" content="active">
<title>{slug}</title></head><body><main class="plan-doc"></main></body></html>
"""


@pytest.fixture
def repo(tmp_path):
    repo_dir = tmp_path / "repository"
    docs_dir = repo_dir / "docs"
    (docs_dir / "plans").mkdir(parents=True)
    (docs_dir / "figures" / "host-plan").mkdir(parents=True)
    (docs_dir / "figures" / "no-such-plan").mkdir(parents=True)
    (repo_dir / ".git").mkdir()

    (docs_dir / "plans" / "host-plan.html").write_text(_plan_html("host-plan"))
    (docs_dir / "figures" / "host-plan" / "linked.png").write_bytes(
        _png_bytes(640, 480)
    )
    (docs_dir / "figures" / "no-such-plan" / "orphan.svg").write_text(
        _svg_text(300, 150)
    )
    return repo_dir, docs_dir


def _mock_git(monkeypatch, *, head: str, created: str, last_modified: str):
    def run(args, **kwargs):
        if args[1:3] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, head + "\n", "")
        if "--diff-filter=A" in args:
            return subprocess.CompletedProcess(args, 0, created, "")
        if args[:2] == ["git", "log"]:
            return subprocess.CompletedProcess(args, 0, last_modified, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(serve.subprocess, "run", run)


def _iso(ts: int) -> str:
    # Mirrors serve._row_times: a naive local stamp, not a UTC one.
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")  # noqa: DTZ006


def test_figure_rows_carry_slug_dims_and_plan_link(repo, monkeypatch):
    _repo_dir, docs_dir = repo
    created = (
        f"COMMIT {_CREATED_TS}\n"
        "docs/plans/host-plan.html\n"
        "docs/figures/host-plan/linked.png\n"
        "docs/figures/no-such-plan/orphan.svg\n"
    )
    last_modified = (
        f"COMMIT {_LAST_COMMIT_TS}\n"
        "docs/plans/host-plan.html\n"
        "docs/figures/host-plan/linked.png\n"
        "docs/figures/no-such-plan/orphan.svg\n"
    )
    _mock_git(monkeypatch, head="head-1", created=created, last_modified=last_modified)

    result = serve.discover_plans(docs_dir, "sample", None)
    figures = {
        item["slug"]: item for item in result["inventory"] if item["type"] == "figure"
    }

    assert set(figures) == {"host-plan/linked.png", "no-such-plan/orphan.svg"}

    linked = figures["host-plan/linked.png"]
    assert linked["dims"] == "640 \u00d7 480"
    assert linked["for_plan"] == "host-plan"

    orphan = figures["no-such-plan/orphan.svg"]
    assert orphan["dims"] == "300 \u00d7 150"
    assert orphan["for_plan"] == ""


def test_every_row_carries_an_edited_stamp_not_earlier_than_created(repo, monkeypatch):
    _repo_dir, docs_dir = repo
    created = (
        f"COMMIT {_CREATED_TS}\n"
        "docs/plans/host-plan.html\n"
        "docs/figures/host-plan/linked.png\n"
        "docs/figures/no-such-plan/orphan.svg\n"
    )
    last_modified = (
        f"COMMIT {_LAST_COMMIT_TS}\n"
        "docs/plans/host-plan.html\n"
        "docs/figures/host-plan/linked.png\n"
        "docs/figures/no-such-plan/orphan.svg\n"
    )
    _mock_git(monkeypatch, head="head-1", created=created, last_modified=last_modified)

    # Every fixture file predates the recorded last commit, so a clean
    # working tree reports the commit time rather than the write-time mtime.
    clean_ts = _LAST_COMMIT_TS - 100
    for relative in (
        "plans/host-plan.html",
        "figures/host-plan/linked.png",
        "figures/no-such-plan/orphan.svg",
    ):
        path = docs_dir / relative
        os.utime(path, ns=(clean_ts * 1_000_000_000, clean_ts * 1_000_000_000))

    result = serve.discover_plans(docs_dir, "sample", None)

    assert result["inventory"], "expected at least one inventory row"
    for item in result["inventory"]:
        assert item["created"] == _CREATED_TS
        assert item["edited"] == _iso(_LAST_COMMIT_TS)
        edited_dt = datetime.fromisoformat(item["edited"])
        created_dt = datetime.fromtimestamp(item["created"])  # noqa: DTZ006
        assert edited_dt >= created_dt


def test_file_modified_after_its_last_commit_reports_its_mtime(repo, monkeypatch):
    _repo_dir, docs_dir = repo
    figure_path = docs_dir / "figures" / "host-plan" / "linked.png"

    created = f"COMMIT {_CREATED_TS}\ndocs/figures/host-plan/linked.png\n"
    last_modified = f"COMMIT {_LAST_COMMIT_TS}\ndocs/figures/host-plan/linked.png\n"
    _mock_git(monkeypatch, head="head-1", created=created, last_modified=last_modified)

    # The last recorded commit is far in the past; the working tree copy was
    # touched more recently than that, so its edited stamp must be the mtime.
    future = datetime.now(tz=UTC) + timedelta(days=1)
    future_ts = int(future.timestamp())
    stat = figure_path.stat()
    os.utime(figure_path, ns=(stat.st_atime_ns, future_ts * 1_000_000_000))

    result = serve.discover_plans(docs_dir, "sample", None)
    linked = next(
        item
        for item in result["inventory"]
        if item.get("slug") == "host-plan/linked.png"
    )
    assert linked["edited"] == _iso(future_ts)


def test_discovery_cache_signature_changes_when_a_figure_is_added(repo, monkeypatch):
    _repo_dir, docs_dir = repo
    created = f"COMMIT {_CREATED_TS}\ndocs/plans/host-plan.html\n"
    last_modified = f"COMMIT {_LAST_COMMIT_TS}\ndocs/plans/host-plan.html\n"
    _mock_git(monkeypatch, head="head-1", created=created, last_modified=last_modified)

    first = serve.discover_plans(docs_dir, "sample", None)
    first_figure_slugs = {
        item["slug"] for item in first["inventory"] if item["type"] == "figure"
    }
    assert "host-plan/added-later.png" not in first_figure_slugs

    (docs_dir / "figures" / "host-plan" / "added-later.png").write_bytes(
        _png_bytes(100, 50)
    )

    second = serve.discover_plans(docs_dir, "sample", None)
    assert second is not first
    second_figure_slugs = {
        item["slug"] for item in second["inventory"] if item["type"] == "figure"
    }
    assert "host-plan/added-later.png" in second_figure_slugs
