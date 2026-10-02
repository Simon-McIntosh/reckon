"""The metadata index re-parses only the files whose bytes changed.

A rebuild walks the project's docs tree and reuses the persisted row for every
file whose content digest still matches, so a second build over an unchanged
tree parses nothing and a build after one edit parses exactly that file. The
digest, not the stat identity, is the reuse key: a file whose mtime moves but
whose bytes are unchanged — a checkout or a rebuild that only touches it — is
reused as well.

The negative control, armed by ``RECKON_TEST_DROP_INDEX_DIGEST=1``, drops the
content digest the persisted index keys reuse on, so every covered file is
re-parsed on the one-changed-file build and the assertion below fails.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon import file_memo, metadata_index

_PROJECT = "sample"
_PLANS = 12
_EVIDENCE = 4
_COVERED = _PLANS + _EVIDENCE
_DROP_DIGEST_ENV = "RECKON_TEST_DROP_INDEX_DIGEST"


def _plan_doc(slug: str, title: str | None = None) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{_PROJECT}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{title or slug}">
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


def _clear_caches() -> None:
    file_memo.clear()
    metadata_index.clear()


@pytest.fixture(autouse=True)
def _isolated_caches():
    _clear_caches()
    yield
    _clear_caches()


@pytest.fixture(autouse=True)
def _negative_control_when_armed(monkeypatch):
    """Arm the declared mutation: drop the digest reuse key from every entry."""

    if os.environ.get(_DROP_DIGEST_ENV) != "1":
        return

    original = metadata_index._load_persisted

    def without_digest(docs_dir, project):
        return {
            path: {key: value for key, value in entry.items() if key != "digest"}
            for path, entry in original(docs_dir, project).items()
        }

    monkeypatch.setattr(metadata_index, "_load_persisted", without_digest)


@pytest.fixture()
def docs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    docs = tmp_path / "repository" / "docs"
    plans = docs / "plans"
    evidence = docs / "evidence"
    plans.mkdir(parents=True)
    evidence.mkdir()
    for index in range(_PLANS):
        slug = f"plan-{index:02d}"
        (plans / f"{slug}.html").write_text(_plan_doc(slug))
    for index in range(_EVIDENCE):
        slug = f"note-{index:02d}"
        (evidence / f"{slug}.html").write_text(_evidence_doc(slug))

    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({_PROJECT: str(docs)}))
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    _clear_caches()
    yield docs
    _clear_caches()


@pytest.fixture()
def parsed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the docs-relative path of every row the build re-parses."""

    seen: list[str] = []
    original = metadata_index._row_for

    def counted(path, relative, docs_dir, project, signature, stamp):
        seen.append(relative)
        return original(path, relative, docs_dir, project, signature, stamp)

    monkeypatch.setattr(metadata_index, "_row_for", counted)
    return seen


def test_a_rebuild_parses_only_the_changed_file(docs_dir: Path, parsed: list[str]):
    docs = docs_dir

    cold = metadata_index.build_index(docs, _PROJECT)
    # Positive control: the cold build parsed every covered file exactly once,
    # so the zero on the warm build below distinguishes reuse from an
    # instrument that never fired.
    assert parsed == sorted(
        [f"plans/plan-{index:02d}.html" for index in range(_PLANS)]
        + [f"evidence/note-{index:02d}.html" for index in range(_EVIDENCE)]
    )
    assert cold.added == _COVERED

    # A restart: the in-process memo is empty again, the persisted index is not.
    parsed.clear()
    _clear_caches()
    second = metadata_index.build_index(docs, _PROJECT)

    assert parsed == []
    assert second.reused == _COVERED
    assert second.rebuilt == []
    assert second.rows == cold.rows

    # Change one plan's bytes.
    target = docs / "plans" / "plan-03.html"
    target.write_text(_plan_doc("plan-03", "a later title"))

    parsed.clear()
    _clear_caches()
    warm = metadata_index.build_index(docs, _PROJECT)

    assert parsed == ["plans/plan-03.html"]
    assert warm.rebuilt == ["plans/plan-03.html"]
    assert warm.reused == _COVERED - 1

    # The warm rows equal a cold build over the same tree.
    metadata_index._index_path(docs, _PROJECT).unlink()
    _clear_caches()
    full = metadata_index.build_index(docs, _PROJECT)
    assert warm.rows == full.rows


def test_a_touched_file_with_unchanged_bytes_is_not_parsed(
    docs_dir: Path, parsed: list[str]
):
    docs = docs_dir
    metadata_index.build_index(docs, _PROJECT)

    parsed.clear()
    _clear_caches()
    target = docs / "plans" / "plan-05.html"
    stat = target.stat()
    # Move only the mtime: the bytes are byte-for-byte what they were.
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    build = metadata_index.build_index(docs, _PROJECT)

    # The digest, not the mtime, is the reuse key: unchanged bytes are reused.
    assert parsed == []
    assert build.reused == _COVERED
    assert build.rebuilt == []
