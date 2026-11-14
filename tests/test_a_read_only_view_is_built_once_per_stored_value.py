"""A stored document's read-only view is built once, whichever region reads it.

A reader that opens its own read-only region around a single read, as the
review store's index reader does, is handed the cached value through a fresh
region every call. The view is immutable, so every region can share the one
built for a stored value; rebuilding it per region walks the whole document on
every read, which a dispatch that classifies every live run pays hundreds of
times over.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import file_memo


@pytest.fixture(autouse=True)
def _empty_memo():
    file_memo.clear()
    yield
    file_memo.clear()


def _read(path: Path, calls: list[int]):
    def compute():
        calls.append(1)
        return json.loads(path.read_text())

    with file_memo.read_only_region():
        return file_memo.memoized("view_share_probe", path, compute)


def test_two_regions_share_one_view_of_an_unchanged_file(tmp_path, monkeypatch):
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"rows": [{"run": str(n)} for n in range(50)]}))
    calls: list[int] = []
    builds: list[int] = []
    real = file_memo._read_only

    def counting(value):
        if type(value) is dict and "rows" in value:
            builds.append(1)
        return real(value)

    monkeypatch.setattr(file_memo, "_read_only", counting)

    first = _read(path, calls)
    second = _read(path, calls)
    third = _read(path, calls)

    assert calls == [1]
    assert second is first
    assert third is first
    assert builds == [1]
    with pytest.raises(TypeError):
        first["rows"].append({})


def test_a_changed_file_gets_a_new_view(tmp_path):
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"rows": [1]}))
    calls: list[int] = []
    first = _read(path, calls)
    path.write_text(json.dumps({"rows": [1, 2, 3]}))
    second = _read(path, calls)

    assert calls == [1, 1]
    assert second is not first
    assert list(second["rows"]) == [1, 2, 3]


def test_a_mutable_hand_out_does_not_reach_the_shared_view(tmp_path):
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"rows": [1]}))
    calls: list[int] = []
    view = _read(path, calls)
    with file_memo.read_only_region():
        copy = file_memo.memoized(
            "view_share_probe", path, lambda: {"rows": []}, mutable=True
        )
    copy["rows"].append(2)

    assert list(view["rows"]) == [1]
    assert list(_read(path, calls)["rows"]) == [1]


def test_a_read_outside_any_region_still_gets_a_private_copy(tmp_path):
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"rows": [1]}))
    first = file_memo.memoized(
        "view_share_probe", path, lambda: json.loads(path.read_text())
    )
    first["rows"].append(9)
    second = file_memo.memoized(
        "view_share_probe", path, lambda: json.loads(path.read_text())
    )

    assert second["rows"] == [1]
    calls: list[int] = []
    assert list(_read(path, calls)["rows"]) == [1]
