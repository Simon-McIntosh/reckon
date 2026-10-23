"""A directory whose entry set moved is re-scanned; the rest are reused.

A covered-set reader has the persisted index's file list and each recorded
directory's stat identity, and a directory's identity moves exactly when an
entry is added to or removed from it. So a read that finds most directories
unchanged reads only the ones that moved, and a project with no persisted index
still walks.

The negative controls are armed one per environment variable, each a defect the
test it names must fail against:

* ``RECKON_TEST_RESCAN_NO_COVERED`` hands a cache-miss build no covered set, as
  the served read did before the reuse was incremental.
* ``RECKON_TEST_RESCAN_SHALLOW`` stops a rescan at the directories it was
  handed instead of descending into the subdirectories they gained.
* ``RECKON_TEST_RESCAN_KEEP_REMOVED`` keeps a vanished directory's files.
* ``RECKON_TEST_RESCAN_STALE_IDENTITY`` records nothing after a scan, so a
  re-scanned directory keeps the identity it had before the change and the
  next read re-scans it.
* ``RECKON_TEST_RESCAN_ALL_OR_NOTHING`` leaves the discovery reader on the
  fallback that walks whenever any directory moved.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import pytest

from reckon import file_memo, metadata_index

_PROJECT = "sample"
_NO_COVERED = "RECKON_TEST_RESCAN_NO_COVERED"
_SHALLOW = "RECKON_TEST_RESCAN_SHALLOW"
_KEEP_REMOVED = "RECKON_TEST_RESCAN_KEEP_REMOVED"
_STALE_IDENTITY = "RECKON_TEST_RESCAN_STALE_IDENTITY"
_ALL_OR_NOTHING = "RECKON_TEST_RESCAN_ALL_OR_NOTHING"
_CONTROLS = (_NO_COVERED, _SHALLOW, _KEEP_REMOVED, _STALE_IDENTITY, _ALL_OR_NOTHING)


def _plan_doc(slug: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{_PROJECT}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{slug}">
<meta name="plan-status" content="active">
<title>{slug}</title></head><body><main class="plan-doc"></main></body></html>
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


def _clear_caches() -> None:
    file_memo.clear()
    metadata_index.clear()


@pytest.fixture(autouse=True)
def _isolated_caches():
    _clear_caches()
    yield
    _clear_caches()


@pytest.fixture(autouse=True)
def _armed_controls(monkeypatch):
    """Arm the declared mutations the run asks for, and nothing by default."""

    armed = {name for name in _CONTROLS if os.environ.get(name) == "1"}
    if not armed:
        return

    if _NO_COVERED in armed:
        monkeypatch.setattr(metadata_index, "_reuse_covered", lambda *a, **k: None)

    if _SHALLOW in armed:
        real_walk = metadata_index._walk_covered

        def shallow(docs_dir, roots=None, skip_subtrees=frozenset()):
            files, dirs = real_walk(docs_dir, roots, skip_subtrees)
            if roots is None:
                return files, dirs
            reached = {
                Path(os.fspath(root)).relative_to(docs_dir).as_posix() for root in roots
            }
            return (
                [
                    (relative, path)
                    for relative, path in files
                    if PurePosixPath(relative).parent.as_posix() in reached
                ],
                [(relative, path) for relative, path in dirs if relative in reached],
            )

        monkeypatch.setattr(metadata_index, "_walk_covered", shallow)

    if _KEEP_REMOVED in armed:
        base = metadata_index._reuse_covered

        def keep_removed(docs_dir, project):
            result = base(docs_dir, project)
            if result is None:
                return None
            covered, dirs, scanned = result
            by_dir: dict[str, list[str]] = {}
            for relative in metadata_index._load_persisted(docs_dir, project):
                by_dir.setdefault(PurePosixPath(relative).parent.as_posix(), []).append(
                    relative
                )
            kept = dict(covered)
            for relative in metadata_index._load_dir_stamps(docs_dir, project):
                if not (docs_dir / relative).is_dir():
                    for file_relative in by_dir.get(relative, ()):
                        kept[file_relative] = docs_dir / file_relative
            return sorted(kept.items()), dirs, scanned

        monkeypatch.setattr(metadata_index, "_reuse_covered", keep_removed)

    if _STALE_IDENTITY in armed:

        def record_nothing(docs_dir, project, files, dirs):
            return None

        monkeypatch.setattr(metadata_index, "_record_covered_shape", record_nothing)

    if _ALL_OR_NOTHING in armed:
        base = metadata_index._reuse_covered

        def all_or_nothing(docs_dir, project):
            result = base(docs_dir, project)
            if result is None:
                return None
            return None if result[2] else result

        monkeypatch.setattr(metadata_index, "_reuse_covered", all_or_nothing)


@pytest.fixture()
def docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A nested temporary docs tree over a temporary configuration home.

    The documents that must produce rows sit under ``notes`` rather than under
    a typed root, because a plan is only identified at ``<root>/<slug>.html``
    or under its ``archive`` and a deeper typed path is a collision.
    """

    config_home = tmp_path / "config"
    config_home.mkdir()
    docs_dir = tmp_path / "repository" / "docs"
    (docs_dir / "notes" / "inner").mkdir(parents=True)
    (docs_dir / "evidence").mkdir()
    (docs_dir / "figures" / "plots").mkdir(parents=True)
    (docs_dir / "charts").mkdir()
    (docs_dir / "alpha.html").write_text(_plan_doc("alpha"), encoding="utf-8")
    (docs_dir / "notes" / "one.html").write_text(_plan_doc("one"), encoding="utf-8")
    (docs_dir / "notes" / "inner" / "two.html").write_text(
        _plan_doc("two"), encoding="utf-8"
    )
    (docs_dir / "evidence" / "note.html").write_text(
        _evidence_doc("note"), encoding="utf-8"
    )
    (docs_dir / "figures" / "plots" / "plot.svg").write_text(
        '<svg viewBox="0 0 4 3"><rect/></svg>', encoding="utf-8"
    )
    (docs_dir / "charts" / "reads.txt").write_text("not covered\n", encoding="utf-8")

    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({_PROJECT: str(docs_dir)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    _clear_caches()
    return docs_dir


@contextmanager
def _scandir_spy(docs_dir: Path) -> Iterator[list[str]]:
    """Record the docs-tree directories each ``os.scandir`` reads, in order."""

    seen: list[str] = []
    root = os.fspath(docs_dir)
    real = os.scandir

    def counting(path):
        candidate = os.fspath(path)
        if candidate == root or candidate.startswith(root + os.sep):
            seen.append(candidate)
        return real(path)

    os.scandir = counting
    try:
        metadata_index.os.scandir = counting
        yield seen
    finally:
        os.scandir = real
        metadata_index.os.scandir = real


def _persist(docs_dir: Path) -> None:
    """Build and persist the index, as a first read of a fresh tree does."""

    metadata_index.clear()
    metadata_index.index_rows(docs_dir, _PROJECT, revalidate=False)


def _cold_rows(docs_dir: Path) -> list[dict]:
    """Read the served path with no process cache, as a restart does."""

    metadata_index.clear()
    return metadata_index.index_rows(docs_dir, _PROJECT, revalidate=False)


def _cold_covered(docs_dir: Path) -> set[str]:
    """Read the discovery signature's covered set with no process cache."""

    metadata_index.clear()
    return {
        relative
        for relative, _path in metadata_index._covered_files_for_project(
            docs_dir, _PROJECT
        )
    }


def _full_walk(docs_dir: Path) -> set[str]:
    """The covered set a whole-tree walk produces, the comparison arm."""

    files, _dirs = metadata_index._walk_covered(docs_dir)
    return {relative for relative, _path in files}


def test_a_non_covered_file_rescans_only_its_directory(docs: Path) -> None:
    _persist(docs)
    before = _cold_rows(docs)

    (docs / "notes" / "inner" / "reads.txt").write_text(
        "not covered\n", encoding="utf-8"
    )
    with _scandir_spy(docs) as scans:
        after = _cold_rows(docs)

    assert after == before
    assert set(scans) == {os.fspath(docs / "notes" / "inner")}


def test_added_removed_and_new_subdirectory_files_track_a_full_walk(docs: Path) -> None:
    _persist(docs)
    (docs / "evidence" / "extra.html").write_text(
        _evidence_doc("extra"), encoding="utf-8"
    )
    (docs / "notes" / "one.html").unlink()
    (docs / "notes" / "inner" / "sub").mkdir()
    (docs / "notes" / "inner" / "sub" / "three.html").write_text(
        _plan_doc("three"), encoding="utf-8"
    )

    covered = _cold_covered(docs)
    slugs = {row["slug"] for row in _cold_rows(docs)}
    full_walk = _full_walk(docs)

    assert "extra" in slugs
    assert "one" not in slugs
    assert "three" in slugs
    assert covered == full_walk


def test_a_removed_directory_drops_its_files(docs: Path) -> None:
    _persist(docs)

    shutil.rmtree(docs / "notes" / "inner")

    covered = _cold_covered(docs)
    assert covered == _full_walk(docs)
    assert "notes/inner/two.html" not in covered


def test_a_second_cold_read_after_a_change_scans_nothing(docs: Path) -> None:
    _persist(docs)

    (docs / "notes" / "inner" / "reads.txt").write_text(
        "not covered\n", encoding="utf-8"
    )
    with _scandir_spy(docs) as first:
        _cold_rows(docs)
    assert set(first) == {os.fspath(docs / "notes" / "inner")}

    with _scandir_spy(docs) as second:
        _cold_rows(docs)
    assert second == []


def test_the_discovery_reader_scans_only_the_moved_directory(docs: Path) -> None:
    _persist(docs)
    full_walk = _full_walk(docs)

    (docs / "notes" / "inner" / "reads.txt").write_text(
        "not covered\n", encoding="utf-8"
    )
    with _scandir_spy(docs) as scans:
        covered = _cold_covered(docs)

    assert set(scans) == {os.fspath(docs / "notes" / "inner")}
    assert covered == full_walk
