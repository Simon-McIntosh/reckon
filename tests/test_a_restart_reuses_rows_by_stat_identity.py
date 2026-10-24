"""An unchanged file's persisted row is reused by its stat identity.

``build_index`` computed every covered file's content digest before it could
decide whether the persisted row still applied, and the digest memo lives only
in the serving process — so the first read after any restart read and hashed
every covered file, although each persisted entry already records the file's
stat identity. The build compares that identity first and serves the row when it
matches, leaving the digest to decide only for a file that moved: a rewrite that
restored the same bytes keeps its row, and any byte change is parsed again.

The negative controls are armed one per environment variable, each a defect the
test it names must fail against:

* ``RECKON_TEST_STAT_NO_SIGNATURE`` restores the digest-first order by refusing
  every signature match, so a restart hashes every file again.
* ``RECKON_TEST_STAT_NO_MTIME`` drops the modification time from the comparison,
  so a same-size in-place edit matches and is served stale.
* ``RECKON_TEST_STAT_NO_DIGEST`` skips the digest fallback, so a file rewritten
  with the same bytes is parsed again instead of reusing its row.
* ``RECKON_TEST_STAT_WITH_DEVICE`` compares the full stat identity including the
  device, which a shared filesystem reports differently on each host, so every
  file is digested.
"""

from __future__ import annotations

import itertools
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon import _plan_html, file_memo, metadata_index

_PROJECT = "sample"
_NO_SIGNATURE = "RECKON_TEST_STAT_NO_SIGNATURE"
_NO_MTIME = "RECKON_TEST_STAT_NO_MTIME"
_NO_DIGEST = "RECKON_TEST_STAT_NO_DIGEST"
_WITH_DEVICE = "RECKON_TEST_STAT_WITH_DEVICE"
_CONTROLS = (_NO_SIGNATURE, _NO_MTIME, _NO_DIGEST, _WITH_DEVICE)


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

    if _NO_SIGNATURE in armed:
        monkeypatch.setattr(
            metadata_index, "_same_stat_identity", lambda recorded, observed: False
        )

    if _NO_MTIME in armed:

        def without_mtime(recorded, observed):
            if not isinstance(recorded, list):
                return False
            return list(recorded)[:2] == metadata_index._stat_signature(observed)[:2]

        monkeypatch.setattr(metadata_index, "_same_stat_identity", without_mtime)

    if _NO_DIGEST in armed:
        real_digest = file_memo.content_digest
        seen = itertools.count()

        def never_settles(path):
            # A digest that differs on every call, so no persisted digest ever
            # matches and the fallback reuses nothing.
            return f"{real_digest(path)}-{next(seen)}"

        monkeypatch.setattr(file_memo, "content_digest", never_settles)

    if _WITH_DEVICE in armed:

        def including_device(recorded, observed):
            return recorded == list(metadata_index.signature_from_stat(observed))

        monkeypatch.setattr(metadata_index, "_same_stat_identity", including_device)


@pytest.fixture()
def docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary docs tree over a temporary configuration home."""

    config_home = tmp_path / "config"
    config_home.mkdir()
    docs_dir = tmp_path / "repository" / "docs"
    (docs_dir / "notes" / "inner").mkdir(parents=True)
    (docs_dir / "evidence").mkdir()
    (docs_dir / "alpha.html").write_text(_plan_doc("alpha"), encoding="utf-8")
    (docs_dir / "notes" / "one.html").write_text(_plan_doc("one"), encoding="utf-8")
    (docs_dir / "notes" / "inner" / "two.html").write_text(
        _plan_doc("two"), encoding="utf-8"
    )
    (docs_dir / "notes" / "inner" / "three.html").write_text(
        _plan_doc("three"), encoding="utf-8"
    )
    (docs_dir / "evidence" / "note.html").write_text(
        _evidence_doc("note"), encoding="utf-8"
    )
    (docs_dir / "evidence" / "second.html").write_text(
        _evidence_doc("second"), encoding="utf-8"
    )

    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({_PROJECT: str(docs_dir)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    _clear_caches()
    return docs_dir


@contextmanager
def _digest_spy() -> Iterator[list[str]]:
    """Record every file ``content_digest`` is asked to read and hash."""

    asked: list[str] = []
    real = file_memo.content_digest

    def counting(path):
        asked.append(os.fspath(path))
        return real(path)

    file_memo.content_digest = counting
    try:
        yield asked
    finally:
        file_memo.content_digest = real


@contextmanager
def _parse_spy() -> Iterator[list[str]]:
    """Record every file whose metadata is parsed from its bytes."""

    parsed: list[str] = []
    real = _plan_html._parse_meta_uncached

    def counting(path, slug=None):
        parsed.append(os.fspath(path))
        return real(path, slug)

    _plan_html._parse_meta_uncached = counting
    try:
        yield parsed
    finally:
        _plan_html._parse_meta_uncached = real


def _persist(docs_dir: Path) -> list[dict]:
    """Build and persist the index, as a first read of a fresh tree does."""

    _clear_caches()
    return metadata_index.index_rows(docs_dir, _PROJECT, revalidate=False)


def _cold_build(docs_dir: Path):
    """Build with no process cache, as a restart does."""

    _clear_caches()
    return metadata_index.build_index(docs_dir, _PROJECT)


def _covered_count(docs_dir: Path) -> int:
    return len(metadata_index._walk_covered(docs_dir)[0])


def test_a_second_build_reads_no_file_and_reuses_every_row(docs: Path) -> None:
    before = _persist(docs)
    assert before

    with _digest_spy() as digests, _parse_spy() as parses:
        build = _cold_build(docs)

    assert build.reused == _covered_count(docs)
    assert build.rebuilt == []
    assert build.added == 0
    assert len(build.rows) == len(before)
    assert digests == []
    assert parses == []


def test_a_file_whose_bytes_change_is_parsed_again(docs: Path) -> None:
    _persist(docs)
    target = docs / "notes" / "one.html"
    text = target.read_text(encoding="utf-8")
    # Same length, different bytes: the size alone cannot tell them apart.
    target.write_text(text.replace('content="one"', 'content="ONE"'), encoding="utf-8")

    with _digest_spy() as digests, _parse_spy() as parses:
        build = _cold_build(docs)

    assert os.fspath(target) in digests
    assert os.fspath(target) in parses
    assert build.rebuilt == ["notes/one.html"]


def test_a_file_rewritten_with_the_same_bytes_is_reused_through_the_digest(
    docs: Path,
) -> None:
    _persist(docs)
    target = docs / "notes" / "inner" / "two.html"
    target.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")

    with _digest_spy() as digests, _parse_spy() as parses:
        build = _cold_build(docs)

    assert digests.count(os.fspath(target)) == 1
    assert parses == []
    assert build.rebuilt == []
    assert build.added == 0


def test_a_persisted_row_is_reused_when_only_the_device_differs(
    docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _persist(docs)
    recorded = metadata_index.signature_from_stat

    def other_host(observed):
        device, *rest = recorded(observed)
        return (device + 24, *rest)

    monkeypatch.setattr(metadata_index, "signature_from_stat", other_host)

    with _digest_spy() as digests, _parse_spy() as parses:
        build = _cold_build(docs)

    assert build.reused == _covered_count(docs)
    assert digests == []
    assert parses == []
