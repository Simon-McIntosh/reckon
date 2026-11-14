"""An admission pass reads the persisted plan store once and checks one plan at a time.

Each queued launch validates its section waits, and each wait reads its plan's
persisted parse. That read went through the whole validated discovery state,
which stats every covered file in the docs tree, so a pass over many launches
re-stat the tree per launch and one pass ran for seventeen minutes. Inside a
pass the store is now read once and each entry is checked against its own file
when its plan is asked for, so a plan written during the pass is parsed afresh.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import file_memo, metadata_index
from reckon.crew.admission import readiness

PROJECT = "entry-proj"


def _docs(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    (docs / "plans").mkdir(parents=True)
    for slug in ("a", "b"):
        (docs / "plans" / f"{slug}.html").write_text(f"<html>{slug}</html>")
    return docs


def _entry(docs: Path, slug: str) -> dict:
    path = docs / "plans" / f"{slug}.html"
    return {
        "path": f"plans/{slug}.html",
        "signature": metadata_index._stat_signature(path.stat()),
        "digest": file_memo.content_digest(path),
        "discovery": {"state": {"slug": slug}},
    }


@pytest.fixture
def store(tmp_path, monkeypatch: pytest.MonkeyPatch):
    docs = _docs(tmp_path)
    entries = {f"plans/{s}.html": _entry(docs, s) for s in ("a", "b")}
    reads: list[str] = []

    def persisted(docs_dir, project):
        reads.append(project)
        return dict(entries)

    def whole_tree(*_args, **_kwargs):
        raise AssertionError("a single plan read must not validate the whole tree")

    monkeypatch.setattr(metadata_index, "persisted_discovery_entries", persisted)
    monkeypatch.setattr(metadata_index, "discovery_state", whole_tree)
    return docs, reads


def test_a_pass_reads_the_store_once_for_many_plans(store):
    docs, reads = store
    with readiness.readiness_pass():
        for _ in range(3):
            for slug in ("a", "b"):
                state = readiness._persisted_plan_state(
                    PROJECT, docs, f"plans/{slug}.html"
                )
                assert state == {"state": {"slug": slug}}
    assert reads == [PROJECT]


def test_a_plan_written_during_the_pass_is_not_seeded(store):
    docs, _reads = store
    with readiness.readiness_pass():
        assert readiness._persisted_plan_state(PROJECT, docs, "plans/a.html")
        (docs / "plans" / "a.html").write_text(
            "<html>a, rewritten at more length</html>"
        )
        assert readiness._persisted_plan_state(PROJECT, docs, "plans/a.html") is None
        assert readiness._persisted_plan_state(PROJECT, docs, "plans/b.html")


def test_outside_a_pass_each_read_reads_the_store(store):
    docs, reads = store
    readiness._persisted_plan_state(PROJECT, docs, "plans/a.html")
    readiness._persisted_plan_state(PROJECT, docs, "plans/b.html")
    assert reads == [PROJECT, PROJECT]
