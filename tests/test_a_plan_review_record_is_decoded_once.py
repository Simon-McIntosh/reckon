"""Stored review records and persisted memos decode once per file change."""

import json
import os
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from reckon import file_memo, metadata_index
from reckon.crew import plan_review


def test_an_unchanged_record_is_decoded_once(tmp_path):
    path = tmp_path / "review.json"
    expected = {"findings": [{"answer": "pending"}]}
    path.write_text(json.dumps(expected), encoding="utf-8")

    with patch.object(plan_review.json, "loads", wraps=plan_review.json.loads) as loads:
        assert plan_review._read_record(path) == expected
        assert loads.call_count == 1
        assert plan_review._read_record(path) == expected
        assert loads.call_count == 1


def test_a_record_rewritten_in_place_is_decoded_again(tmp_path):
    path = tmp_path / "review.json"
    path.write_text('{"answer": "pending"}', encoding="utf-8")
    inode = path.stat().st_ino

    with patch.object(plan_review.json, "loads", wraps=plan_review.json.loads) as loads:
        assert plan_review._read_record(path) == {"answer": "pending"}
        assert loads.call_count == 1
        path.write_text('{"answer": "acted with evidence"}', encoding="utf-8")
        assert path.stat().st_ino == inode
        assert plan_review._read_record(path) == {"answer": "acted with evidence"}
        assert loads.call_count == 2


@pytest.mark.parametrize("in_region", [False, True])
def test_a_returned_record_is_a_mutable_copy(tmp_path, in_region):
    path = tmp_path / "review.json"
    expected = {"findings": [{"answer": "pending"}]}
    path.write_text(json.dumps(expected), encoding="utf-8")

    with file_memo.read_only_region() if in_region else nullcontext():
        record = plan_review._read_record(path)
        record["findings"][0]["answer"] = "acted"
        record["extra"] = True
        assert plan_review._read_record(path) == expected


def test_an_unchanged_persisted_memo_is_decoded_once(tmp_path):
    path = tmp_path / "memo.json"
    expected = {"answer": "pending"}
    metadata_index._write_persisted_memo(path, {}, {}, expected)

    with patch.object(
        metadata_index.json, "loads", wraps=metadata_index.json.loads
    ) as loads:
        assert metadata_index._read_persisted_memo(path, {}) == expected
        assert loads.call_count == 1
        assert metadata_index._read_persisted_memo(path, {}) == expected
        assert loads.call_count == 1


def test_a_rewritten_persisted_memo_is_decoded_again(tmp_path):
    path = tmp_path / "memo.json"
    metadata_index._write_persisted_memo(path, {}, {}, {"answer": "pending"})
    inode = path.stat().st_ino

    with patch.object(
        metadata_index.json, "loads", wraps=metadata_index.json.loads
    ) as loads:
        assert metadata_index._read_persisted_memo(path, {}) == {"answer": "pending"}
        assert loads.call_count == 1
        path.write_text(
            json.dumps(
                {
                    "schema": metadata_index._PERSISTED_SCHEMA,
                    "sources": {},
                    "value": {"answer": "acted with evidence"},
                }
            ),
            encoding="utf-8",
        )
        assert path.stat().st_ino == inode
        assert metadata_index._read_persisted_memo(path, {}) == {
            "answer": "acted with evidence"
        }
        assert loads.call_count == 2


def test_a_cached_persisted_payload_still_validates_source_digests(tmp_path):
    path = tmp_path / "memo.json"
    expected = {"answer": "pending"}
    producers = {"parser": SimpleNamespace(__name__="parser")}
    metadata_index._write_persisted_memo(path, {"parser": "original"}, {}, expected)

    with (
        patch.object(
            metadata_index.json, "loads", wraps=metadata_index.json.loads
        ) as loads,
        patch.object(
            metadata_index, "_module_source_digest", return_value="original"
        ) as digest,
    ):
        assert metadata_index._read_persisted_memo(path, producers) == expected
        digest.return_value = "changed"
        assert (
            metadata_index._read_persisted_memo(path, producers)
            is metadata_index._PERSISTED_MISSING
        )
        assert digest.call_count == 2
        assert loads.call_count == 1


@pytest.fixture
def persisted_index(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(metadata_index, "_config_home", lambda: tmp_path / "home")
    monkeypatch.setattr(metadata_index, "_discovery_producers", lambda: ())
    metadata_index.clear()
    metadata_index._store_persisted(
        docs,
        "sample",
        [{"path": "plan.html", "signature": [1, 2, 3, 4]}],
        {"plans": [1, 2, 3, 4]},
    )
    yield docs
    metadata_index.clear()


def test_a_warm_persisted_payload_does_not_deepcopy(persisted_index):
    docs = persisted_index
    with patch.object(
        metadata_index.json, "loads", wraps=metadata_index.json.loads
    ) as loads:
        assert metadata_index._persisted_payload(docs, "sample")["entries"]
        assert loads.call_count == 1
        with patch.object(
            file_memo.copy, "deepcopy", side_effect=AssertionError("payload copied")
        ):
            assert metadata_index._persisted_payload(docs, "sample")["entries"]
        assert loads.call_count == 1
        metadata_index._store_persisted(docs, "sample", [{"path": "changed.html"}])
        assert metadata_index._persisted_payload(docs, "sample")["entries"] == [
            {"path": "changed.html"}
        ]
        assert loads.call_count == 2


def test_persisted_payloads_are_read_only_and_outer_maps_are_independent(
    persisted_index,
):
    docs = persisted_index
    payload = metadata_index._persisted_payload(docs, "sample")
    assert isinstance(payload, dict)
    assert isinstance(payload["entries"], list)
    with pytest.raises(TypeError, match="read-only"):
        payload["entries"][0]["path"] = "changed.html"
    entries = metadata_index._load_persisted(docs, "sample")
    entries.clear()
    stamps = metadata_index._load_dir_stamps(docs, "sample")
    stamps.clear()
    assert "plan.html" in metadata_index._load_persisted(docs, "sample")
    assert metadata_index._load_dir_stamps(docs, "sample") == {"plans": [1, 2, 3, 4]}


def test_only_the_latest_index_file_is_retained_per_tree(persisted_index, monkeypatch):
    docs = persisted_index
    for offset in range(3):
        path = docs.parent / f"memo-{offset}.json"
        metadata_index._write_persisted_memo(path, {}, {}, {"entries": [offset]})
        monkeypatch.setattr(metadata_index, "_index_path", lambda *args, p=path: p)
        assert metadata_index._persisted_payload(docs, "sample")["entries"] == [offset]
        assert len(metadata_index._PERSISTED_INDEX_CACHE) == 1


def test_index_retention_evicts_the_least_recent_tree(persisted_index, monkeypatch):
    monkeypatch.setattr(metadata_index, "_MAX_PERSISTED_INDEXES", 2)
    docs = [persisted_index / str(index) for index in range(3)]
    for tree in docs:
        tree.mkdir()
        metadata_index._store_persisted(tree, "sample", [{"path": "plan.html"}])
        assert metadata_index._persisted_payload(tree, "sample")["entries"]
    assert len(metadata_index._PERSISTED_INDEX_CACHE) == 2
    with patch.object(
        metadata_index.json, "loads", wraps=metadata_index.json.loads
    ) as loads:
        assert metadata_index._persisted_payload(docs[-1], "sample")["entries"]
        assert loads.call_count == 0
        assert metadata_index._persisted_payload(docs[0], "sample")["entries"]
        assert loads.call_count == 1


def test_an_index_above_the_retention_budget_is_not_kept(persisted_index, monkeypatch):
    monkeypatch.setattr(metadata_index, "_MAX_PERSISTED_INDEX_BYTES", 1)
    with patch.object(
        metadata_index.json, "loads", wraps=metadata_index.json.loads
    ) as loads:
        for _ in range(2):
            assert metadata_index._persisted_payload(persisted_index, "sample")[
                "entries"
            ]
        assert loads.call_count == 2
    assert not metadata_index._PERSISTED_INDEX_CACHE


def test_discovery_refreshes_a_signature_without_mutating_a_shared_entry(
    persisted_index,
):
    docs = persisted_index
    path = docs / "plan.html"
    path.write_text("same bytes", encoding="utf-8")
    signature = metadata_index._stat_signature(path.stat())
    metadata_index._store_persisted(
        docs,
        "sample",
        [
            {
                "path": "plan.html",
                "signature": signature,
                "digest": file_memo.content_digest(path),
                "discovery": {"state": {"title": "plan"}},
            }
        ],
    )
    held = metadata_index._load_persisted(docs, "sample")["plan.html"]
    observed = path.stat()
    os.utime(path, ns=(observed.st_atime_ns, observed.st_mtime_ns + 1_000_000))
    assert metadata_index.discovery_state(docs, "sample", build=False) == {
        "plan.html": {"state": {"title": "plan"}}
    }
    assert held["signature"] == signature
    assert metadata_index._load_persisted(docs, "sample")["plan.html"]["signature"] == (
        metadata_index._stat_signature(path.stat())
    )
