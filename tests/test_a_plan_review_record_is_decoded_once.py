"""Stored review records share a decode until their file changes."""

import json
from contextlib import nullcontext
from unittest.mock import patch

import pytest

from reckon import file_memo
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
