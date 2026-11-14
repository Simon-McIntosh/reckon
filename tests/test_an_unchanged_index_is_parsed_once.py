"""An unchanged index document is parsed once per process.

Every ``store_index_document`` call outside a producer tick used to read and
parse the store's whole document, so a 4.8 MB index was decoded twice on the
station's refresh path and once more for each reader that asked it a question.
The document is only replaced when a writer moves it, so a read that finds the
same file may keep the mapping it already parsed and spend one stat instead.
The test counts decodes through a recording stand-in for ``json.loads`` and
checks that a write still makes the next read see the new document. It also
checks that a reader cannot change the document another reader is handed: the
reuse hands every reader of an unchanged file one held parse, so the shape it
hands out decides whether that sharing is safe.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon import file_memo
from reckon.crew import review, review_records

PROJECT = "fixture"
HEAD = "0123456789abcdef0123456789abcdef01234567"
CALLS = 100


def _payload(run_id: str, index: int) -> dict:
    return {
        "project": PROJECT,
        "reviewed_run_id": run_id,
        "review_run_id": f"review-of-{run_id}-{index:08x}",
        "reviewed_head_sha": HEAD,
        "status": "parsed",
        "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 18),
        "absent": [],
        "total": 18 * len(review.REVIEW_DIMENSIONS),
    }


def _forget_parsed_document() -> None:
    # The memo is process-wide and outlives one store, so the measurement has to
    # start from a process that holds nothing.
    file_memo.clear()


@pytest.fixture()
def indexed_store():
    directory = review.review_store_root() / PROJECT
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(3):
        run_id = f"r-20261113T1200{index:02d}-node-{index:02d}"
        review_records.record_written(directory / f"{run_id}.json", _payload(run_id, 0))
    # Settle the store, then drop this process's copy so the measurement starts
    # from a file that has to be read.
    review_records.store_index_document(directory)
    _forget_parsed_document()
    yield directory
    _forget_parsed_document()


@contextmanager
def _count_index_parses(index: Path):
    """Record every decode of the index document's current bytes."""
    parsed: list[bytes] = []
    real_loads = json.loads
    document_bytes = index.read_bytes()

    def counting_loads(raw, *args, **kwargs):
        if isinstance(raw, (bytes, bytearray)) and bytes(raw) == document_bytes:
            parsed.append(bytes(raw))
        return real_loads(raw, *args, **kwargs)

    json.loads = counting_loads
    try:
        yield parsed
    finally:
        json.loads = real_loads


def test_an_unchanged_index_is_parsed_once(indexed_store) -> None:
    directory = indexed_store
    index = review_records._index_document_path(directory)
    assert index.is_file()

    with _count_index_parses(index) as parses:
        for _ in range(CALLS):
            document = review_records.store_index_document(directory)

    assert document is not None
    assert len(parses) == 1, (
        f"{CALLS} reads of an unchanged index document parsed it {len(parses)} times"
    )

    new_run = "r-20261113T130000-node-99"
    review_records.record_written(directory / f"{new_run}.json", _payload(new_run, 5))

    with _count_index_parses(index) as parses:
        refreshed = review_records.store_index_document(directory)

    assert len(parses) == 1, (
        f"the first read after a write parsed the index document {len(parses)} times"
    )
    assert refreshed is not None
    assert f"{new_run}.json" in refreshed["entries"], sorted(refreshed["entries"])
    assert refreshed["entries"][f"{new_run}.json"]["reviewed_run_id"] == new_run


def test_the_index_read_routes_through_the_shared_memo(
    indexed_store, monkeypatch
) -> None:
    directory = indexed_store
    real = file_memo.memoized
    kinds: list[str] = []

    def recording(kind, path, compute, **kwargs):
        kinds.append(kind)
        return real(kind, path, compute, **kwargs)

    monkeypatch.setattr(file_memo, "memoized", recording)
    review_records.store_index_document(directory)
    assert kinds == ["index_document"], kinds


def test_a_held_index_document_is_read_only(indexed_store) -> None:
    directory = indexed_store
    held = review_records.store_index_document(directory)
    assert held is not None
    with pytest.raises(TypeError):
        held["entries"]["injected.json"] = {}
    with pytest.raises(TypeError):
        held["entries"].clear()
    again = review_records.store_index_document(directory)
    assert again is not None
    assert "injected.json" not in again["entries"]
