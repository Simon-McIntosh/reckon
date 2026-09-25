from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from reckon import file_memo
from reckon._plan_html import parse_plan
from reckon.doccheck import _read_lifecycle_state, audit_links


def _plan(slug: str, title: str = "Memo target") -> str:
    return f"""<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{title}">
<meta name="plan-status" content="active">
<meta name="plan-summary" content="memo target">
<meta name="plan-impl" content="0.5">
<meta name="plan-version" content="1">
</head><body><h1 id="top">{title}</h1><a href="#top">top</a></body></html>
"""


@pytest.fixture(autouse=True)
def _clear_file_memo() -> None:
    file_memo.clear()
    yield
    file_memo.clear()


def test_unchanged_plan_is_read_once(tmp_path: Path) -> None:
    path = tmp_path / "target.html"
    path.write_text(_plan("target"), encoding="utf-8")
    original = Path.read_text
    reads: list[Path] = []

    def counted(candidate: Path, *args, **kwargs) -> str:
        reads.append(candidate)
        return original(candidate, *args, **kwargs)

    with patch.object(Path, "read_text", counted):
        first = parse_plan(path)
        second = parse_plan(path)
        lifecycle = _read_lifecycle_state(path)
        assert audit_links([path], tmp_path, project="proj") == {}

    assert first["slug"] == second["slug"] == lifecycle["slug"] == "target"
    assert reads.count(path) == 1


def test_size_change_is_reparsed(tmp_path: Path) -> None:
    path = tmp_path / "target.html"
    path.write_text(_plan("short"), encoding="utf-8")
    assert parse_plan(path)["slug"] == "short"

    path.write_text(_plan("longer-slug"), encoding="utf-8")

    assert parse_plan(path)["slug"] == "longer-slug"


def test_mtime_change_is_reparsed(tmp_path: Path) -> None:
    path = tmp_path / "target.html"
    path.write_text(_plan("first"), encoding="utf-8")
    original_stat = path.stat()
    assert parse_plan(path)["slug"] == "first"

    path.write_text(_plan("other"), encoding="utf-8")
    assert path.stat().st_size == original_stat.st_size
    os.utime(
        path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1_000_000_000),
    )

    assert parse_plan(path)["slug"] == "other"


def test_inode_change_is_reparsed(tmp_path: Path) -> None:
    path = tmp_path / "target.html"
    replacement = tmp_path / "replacement.html"
    path.write_text(_plan("first"), encoding="utf-8")
    original_stat = path.stat()
    assert parse_plan(path)["slug"] == "first"

    replacement.write_text(_plan("other"), encoding="utf-8")
    assert replacement.stat().st_size == original_stat.st_size
    os.utime(
        replacement,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    replacement.replace(path)
    assert path.stat().st_ino != original_stat.st_ino

    assert parse_plan(path)["slug"] == "other"


def test_returned_plan_is_isolated_from_the_memo(tmp_path: Path) -> None:
    path = tmp_path / "target.html"
    path.write_text(_plan("target"), encoding="utf-8")
    first = parse_plan(path)

    first["slug"] = "poisoned"
    first["followups"].append({"id": "poisoned"})

    second = parse_plan(path)
    assert second["slug"] == "target"
    assert second["followups"] == []


def test_relative_corpus_resolves_absolute_anchor_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs_dir = tmp_path / "docs"
    source = docs_dir / "plans" / "source.html"
    target = docs_dir / "evidence" / "archive" / "target.html"
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    source.write_text(
        _plan("source").replace(
            "</body>",
            '<a href="/proj/evidence/archive/target#result">result</a></body>',
        ),
        encoding="utf-8",
    )
    target.write_text(
        _plan("target").replace('<h1 id="top">', '<h1 id="result">'),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert (
        audit_links([Path("docs/plans/source.html")], Path("docs"), project="proj")
        == {}
    )
