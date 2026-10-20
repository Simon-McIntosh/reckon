"""A discovery pass shares its covered files and their observed modification times."""

from __future__ import annotations

import os
import struct
import time
from collections import Counter
from pathlib import Path

import pytest

from reckon import metadata_index, serve

_PROJECT = "sample"
_COVERED_COUNT = 2_000
_EXTRA_SIGNATURE_STATS = "RECKON_TEST_REPEAT_SIGNATURE_STATS"


def _plan(slug: str) -> str:
    return (
        '<!doctype html><html><head><meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-title" content="{slug}">'
        '<meta name="plan-status" content="active"></head><body></body></html>'
    )


def _png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", 8, 6)
    )


@pytest.fixture
def docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    root = tmp_path / "project" / "docs"
    plans = root / "plans"
    figures = root / "figures"
    shared = root / "_shared"
    for directory in (plans, figures, shared):
        directory.mkdir(parents=True)
    for slug in ("alpha", "beta", "gamma"):
        (plans / f"{slug}.html").write_text(_plan(slug), encoding="utf-8")
    for slug in ("alpha/chart.png", "beta/chart.png", "loose.png"):
        path = figures / slug
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_png())
    for number in range(_COVERED_COUNT - 6):
        (shared / f"f{number:04d}.html").write_text("<html></html>", encoding="utf-8")
    serve._DISC_CACHE.clear()
    serve._SIGNATURE_MEMO.clear()
    metadata_index.index_rows(root, _PROJECT)
    assert (
        len(metadata_index._covered_files_for_project(root, _PROJECT)) == _COVERED_COUNT
    )
    serve._DISC_CACHE.clear()
    serve._SIGNATURE_MEMO.clear()
    yield root
    serve._DISC_CACHE.clear()
    serve._SIGNATURE_MEMO.clear()


def test_discovery_reuses_one_covered_file_observation(
    docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.environ.get(_EXTRA_SIGNATURE_STATS) == "1":
        walk_signature = serve._walk_discovery_signature
        repeated = False

        def repeated_signature(docs_dir, project, state_root):
            nonlocal repeated
            signature = walk_signature(docs_dir, project, state_root)
            if not repeated:
                repeated = True
                for _, path in metadata_index._covered_files_for_project(
                    docs_dir, project
                ):
                    path.stat()
            return signature

        monkeypatch.setattr(serve, "_walk_discovery_signature", repeated_signature)

    real_stat = os.stat
    real_lstat = os.lstat
    real_scandir = os.scandir
    calls: Counter[str] = Counter()
    prefix = os.fspath(docs) + os.sep

    def covered(path: object) -> bool:
        if isinstance(path, int):
            return False
        name = os.fspath(path)
        if isinstance(name, bytes):
            name = os.fsdecode(name)
        return name == os.fspath(docs) or name.startswith(prefix)

    def stat(path, *args, **kwargs):
        if covered(path):
            calls["stat"] += 1
        return real_stat(path, *args, **kwargs)

    def lstat(path, *args, **kwargs):
        if covered(path):
            calls["lstat"] += 1
        return real_lstat(path, *args, **kwargs)

    def scandir(path):
        if covered(path):
            calls["scandir"] += 1
        return real_scandir(path)

    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(os, "scandir", scandir)

    # The covered set is the only path source figure_rows may use. Walking
    # figures/ again — the revert this guards against — is a few extra calls in
    # a fixture with three figures, too few to cross the ceiling above, so the
    # walk itself is made to fail: the row set must still come back.
    figures_root = os.fspath(docs / "figures")
    real_rglob = Path.rglob
    real_glob = Path.glob

    def refuse_walk(self: Path, pattern: str, *args: object, **kwargs: object):
        name = os.fspath(self)
        if name == figures_root or name.startswith(figures_root + os.sep):
            raise AssertionError(
                "figure_rows walked figures/ instead of using the covered set"
            )
        return real_rglob(self, pattern, *args, **kwargs)

    def refuse_glob(self: Path, pattern: str, *args: object, **kwargs: object):
        name = os.fspath(self)
        if name == figures_root or name.startswith(figures_root + os.sep):
            raise AssertionError(
                "figure_rows walked figures/ instead of using the covered set"
            )
        return real_glob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", refuse_walk)
    monkeypatch.setattr(Path, "glob", refuse_glob)

    result = serve.discover_plans(docs, _PROJECT, None)
    inventory = result["inventory"]
    assert sorted(row["slug"] for row in inventory if row["type"] == "plan") == [
        "alpha",
        "beta",
        "gamma",
    ]
    figure_rows = [row for row in inventory if row["type"] == "figure"]
    assert sorted(row["slug"] for row in figure_rows) == [
        "alpha/chart.png",
        "beta/chart.png",
        "loose.png",
    ]
    assert {row["slug"]: (row["for_plan"], row["dims"]) for row in figure_rows} == {
        "alpha/chart.png": ("alpha", "8 \u00d7 6"),
        "beta/chart.png": ("beta", "8 \u00d7 6"),
        "loose.png": ("", "8 \u00d7 6"),
    }
    cache_key = (_PROJECT, str(docs.resolve()))
    assert serve._DISC_CACHE[cache_key].local_signature[0] == _COVERED_COUNT
    total = sum(calls.values())
    print(
        f"covered={_COVERED_COUNT} stat={calls['stat']} lstat={calls['lstat']} scandir={calls['scandir']} total={total}"
    )
    assert total <= _COVERED_COUNT * 1.5

    old_signature = serve._walk_discovery_signature(docs, _PROJECT, None)
    changed = docs / "_shared" / "f0000.html"
    stamp = max(time.time_ns(), old_signature[1] + 1_000_000_000)
    os.utime(changed, ns=(stamp, stamp))
    assert serve._walk_discovery_signature(docs, _PROJECT, None) != old_signature
