from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from reckon import _store, file_memo

PROJECT = "state-memo-sample"
SLUG = "memo-target"


def _plan(slug: str, title: str = "Memo target") -> str:
    return f"""<!doctype html>
<html><head>
<meta name="docs-project" content="{PROJECT}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{title}">
<meta name="plan-status" content="active">
<meta name="plan-summary" content="memo target">
<meta name="plan-version" content="1">
</head><body><h1>{title}</h1></body></html>
"""


@pytest.fixture(autouse=True)
def _clear_file_memo() -> None:
    file_memo.clear()
    yield
    file_memo.clear()


def _write_plan(root: Path, text: str) -> Path:
    path = root / "docs" / "plans" / f"{SLUG}.html"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def _read(root: Path) -> dict:
    state, version = _store._read_state(PROJECT, SLUG, root=root)
    assert version == 1
    return state


def test_unchanged_state_file_is_read_once(tmp_path: Path) -> None:
    path = _write_plan(tmp_path, _plan(SLUG))
    original = Path.read_text
    reads: list[Path] = []

    def counted(candidate: Path, *args, **kwargs) -> str:
        if candidate == path:
            reads.append(candidate)
        return original(candidate, *args, **kwargs)

    with patch.object(Path, "read_text", counted):
        assert _read(tmp_path)["slug"] == SLUG
        assert _read(tmp_path)["slug"] == SLUG

    assert reads == [path]


def test_size_change_is_reparsed(tmp_path: Path) -> None:
    path = _write_plan(tmp_path, _plan(SLUG, "Short"))
    assert _read(tmp_path)["title"] == "Short"

    path.write_text(_plan(SLUG, "A longer title"), encoding="utf-8")

    assert _read(tmp_path)["title"] == "A longer title"


def test_mtime_change_is_reparsed(tmp_path: Path) -> None:
    path = _write_plan(tmp_path, _plan(SLUG, "First"))
    original_stat = path.stat()
    assert _read(tmp_path)["title"] == "First"

    path.write_text(_plan(SLUG, "Other"), encoding="utf-8")
    assert path.stat().st_size == original_stat.st_size
    os.utime(
        path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1_000_000_000),
    )

    assert _read(tmp_path)["title"] == "Other"


def test_inode_change_is_reparsed(tmp_path: Path) -> None:
    path = _write_plan(tmp_path, _plan(SLUG, "First"))
    replacement = path.with_name("replacement.html")
    original_stat = path.stat()
    assert _read(tmp_path)["title"] == "First"

    replacement.write_text(_plan(SLUG, "Other"), encoding="utf-8")
    assert replacement.stat().st_size == original_stat.st_size
    os.utime(
        replacement,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    replacement.replace(path)
    assert path.stat().st_ino != original_stat.st_ino

    assert _read(tmp_path)["title"] == "Other"


def test_returned_state_is_isolated_from_the_memo(tmp_path: Path) -> None:
    _write_plan(tmp_path, _plan(SLUG))
    first = _read(tmp_path)

    first["slug"] = "poisoned"
    first["followups"].append({"id": "poisoned"})

    second = _read(tmp_path)
    assert second["slug"] == SLUG
    assert second["followups"] == []
