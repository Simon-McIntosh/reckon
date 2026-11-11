"""Stored review records and persisted memos decode once per file change."""

import json
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
