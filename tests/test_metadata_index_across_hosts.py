"""A persisted metadata index stays valid on every host of a shared filesystem.

The served process on a login node and the agent and command-line readers on a
compute node build the same persisted index. A shared filesystem reports a
different device number for the same file on each host while the inode, size
and timestamps agree, so an index whose rows were keyed on the device number
matched no row on the other host: each reader re-parsed the whole project and
rewrote the index, and the next reader on the first host did the same.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html, file_memo, metadata_index

_PROJECT = "sample"


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


@pytest.fixture()
def docs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    docs = tmp_path / "repository" / "docs" / "plans"
    docs.mkdir(parents=True)
    for index in range(12):
        (docs / f"plan-{index:02d}.html").write_text(_plan_doc(f"plan-{index:02d}"))
    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({_PROJECT: str(docs.parent)}))
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    file_memo.clear()
    metadata_index.clear()
    yield docs.parent
    file_memo.clear()
    metadata_index.clear()


def test_a_rebuild_on_another_host_parses_nothing(docs_dir: Path, monkeypatch):
    first = metadata_index.build_index(docs_dir, _PROJECT)
    assert first.added == 12

    original = metadata_index.file_signature

    def other_host(path):
        device, *rest = original(path)
        return (device + 24, *rest)

    parses: list[Path] = []
    parse = _plan_html._parse_meta_uncached

    def counted(path, slug):
        parses.append(Path(path))
        return parse(path, slug)

    monkeypatch.setattr(metadata_index, "file_signature", other_host)
    monkeypatch.setattr(_plan_html, "_parse_meta_uncached", counted)
    file_memo.clear()
    metadata_index.clear()

    second = metadata_index.build_index(docs_dir, _PROJECT)

    assert second.rebuilt == []
    assert second.reused == 12
    assert parses == []
    assert [row["slug"] for row in second.rows] == [row["slug"] for row in first.rows]


def test_a_real_edit_is_still_rebuilt_on_another_host(docs_dir: Path, monkeypatch):
    metadata_index.build_index(docs_dir, _PROJECT)
    original = metadata_index.file_signature

    def other_host(path):
        device, *rest = original(path)
        return (device + 24, *rest)

    monkeypatch.setattr(metadata_index, "file_signature", other_host)
    edited = docs_dir / "plans" / "plan-03.html"
    edited.write_text(
        _plan_doc("plan-03").replace(">plan-03</title>", ">Renamed</title>")
    )
    file_memo.clear()
    metadata_index.clear()

    second = metadata_index.build_index(docs_dir, _PROJECT)

    assert second.rebuilt == ["plans/plan-03.html"]
