"""A miss in the review store is answered from an index, not a whole-store pass.

The misfiled fallback exists to find a review a writer filed under a run id
other than the one it reviews. Answering each lookup by reading every record in
the store makes a per-turn reader whose live pointers carry no record pay one
whole-store pass per pointer: the coordinator obligations hook walks the store
for every live run, and its UserPromptSubmit budget is thirty seconds. The
fallback therefore answers from an index of the records the store holds, built
once per process and rebuilt when the directory's stat identity moves, and it
does not run at all for a run whose own file — bare or revision-keyed — is
present.

The store is large enough to measure the difference: a handful of records would
let a whole-store pass finish inside the noise of a single lookup and hide the
cost the index removes.

Every crew directory is environment-resolved under ``tmp_path``; nothing
touches the operator's own store.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import review as review_module

PROJECT = "proj"
STORE_SIZE = 3000
# The one run whose record sits at the revision-keyed path, the shape a record
# written through the store takes, while the rest sit at the bare legacy path.
KEYED_INDEX = 7
OTHER_HEAD = "f" * 40
MISFILED_HEAD = "beef" * 10


def _record(run_id: str, head: str | None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "project": PROJECT,
        "reviewed_run_id": run_id,
        "status": "parsed",
        "scores": {},
        "absent": [],
        "total": 80,
        "timestamp": "2026-10-01T00:00:00+00:00",
    }
    if head is not None:
        record["reviewed_head_sha"] = head
    return record


def _stored_head(index: int) -> str:
    return f"{index:040x}"


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a synthesised review store of ``STORE_SIZE`` records."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    directory = review_module.review_store_root() / PROJECT
    directory.mkdir(parents=True)
    for index in range(STORE_SIZE):
        run_id = f"r-stored-{index:05d}"
        head = _stored_head(index)
        path = (
            review_module.review_path(PROJECT, run_id, reviewed_head_sha=head)
            if index == KEYED_INDEX
            else review_module.review_path(PROJECT, run_id)
        )
        path.write_text(json.dumps(_record(run_id, head)), encoding="utf-8")
    return directory


@pytest.fixture()
def record_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every file the code under test reads through ``Path.read_text``."""
    reads: list[str] = []
    original = Path.read_text

    def counting(self: Path, *args: Any, **kwargs: Any) -> str:
        reads.append(str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting)
    return reads


def _store_reads(reads: list[str], store: Path) -> list[str]:
    return [path for path in reads if path.startswith(str(store))]


def test_many_misses_read_each_stored_record_at_most_once(
    store: Path, record_reads: list[str]
) -> None:
    """Twenty-five misses must not mean twenty-five passes over the store."""
    for index in range(25):
        found = review_module.stored_record(PROJECT, f"r-absent-{index:02d}")
        assert found == (None, None)

    reads = _store_reads(record_reads, store)
    assert len(reads) == STORE_SIZE, (
        "the index is built once and answers every later miss"
    )
    assert len(set(reads)) == STORE_SIZE, (
        "no record file is read a second time across the passes"
    )


def test_a_record_at_its_expected_path_never_reaches_the_scan(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A head mismatch on the run's own file is not a reason to read the store."""
    run_id = f"r-stored-{KEYED_INDEX:05d}"
    keyed_head = _stored_head(KEYED_INDEX)
    scanned: list[str] = []
    original = review_module._record_filed_elsewhere

    def spy(
        directory: Path, reviewed_run_id: str, reviewed_head_sha: str | None
    ) -> tuple[Path | None, dict[str, Any] | None]:
        scanned.append(reviewed_run_id)
        return original(directory, reviewed_run_id, reviewed_head_sha)

    monkeypatch.setattr(review_module, "_record_filed_elsewhere", spy)

    path, record = review_module.stored_record(
        PROJECT, run_id, reviewed_head_sha=keyed_head
    )
    assert path is not None and record is not None, (
        "the record at the run's revision-keyed path is the one returned"
    )
    assert record["reviewed_head_sha"] == keyed_head

    assert review_module.stored_record(
        PROJECT, run_id, reviewed_head_sha=OTHER_HEAD
    ) == (None, None)
    assert scanned == [], (
        "the run's own file exists, so the store-wide search for a record "
        "filed under another run id does not run"
    )


def test_a_misfiled_record_is_still_found_and_flagged(store: Path) -> None:
    """A run with no file of its own still finds one filed under another id."""
    run_id = "r-misfiled-target"
    path = review_module.review_path(PROJECT, "r-reviewer-run")
    path.write_text(json.dumps(_record(run_id, MISFILED_HEAD)), encoding="utf-8")

    found_path, found = review_module.stored_record(
        PROJECT, run_id, reviewed_head_sha=MISFILED_HEAD
    )

    assert found_path == path, "the path returned is the file it was read from"
    assert found is not None, "the record's own reviewed_run_id names this run"
    assert found["misfiled"] is True


def test_a_record_added_to_the_store_is_seen_on_the_next_call(store: Path) -> None:
    """The index is rebuilt when the store's directory has gained an entry."""
    run_id = "r-late-arrival"
    assert review_module.stored_record(PROJECT, run_id) == (None, None)

    path = review_module.review_path(PROJECT, "r-reviewer-run")
    path.write_text(json.dumps(_record(run_id, MISFILED_HEAD)), encoding="utf-8")

    found_path, found = review_module.stored_record(
        PROJECT, run_id, reviewed_head_sha=MISFILED_HEAD
    )

    assert found_path == path
    assert found is not None
    assert found["misfiled"] is True
