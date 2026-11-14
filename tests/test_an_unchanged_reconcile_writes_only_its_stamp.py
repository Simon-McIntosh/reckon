"""An unchanged reconcile writes only its stamp, so a cold process lists nothing.

The store directory's stat identity moves whenever any process creates and
removes a file in it -- a writer's temporary, a record being renamed into place
-- while every record the index describes is untouched. A reconcile that finds
nothing changed must record the identity it settled so that no other process
repeats the check, and a full store listing under the index lock is what every
other reader pays when that result lives only in the process that reconciled.
The station, each project's watch producer, every MCP server and CLI call, and
every process started later all see the store as moved.

The settled identity is persisted in a small stamp beside the document. A
reconcile that changes no entry advances the stamp and leaves the document
alone, so a freshly started process answers from the document without listing
the store. A record that really was added still moves the document.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon.crew import review, review_records

PROJECT = "fixture"
RUNS = 8
RECORDS_PER_RUN = 4
HEAD = "0123456789abcdef0123456789abcdef01234567"

# A process that counts every listing of one store directory before it calls
# ``store_index_document``. It runs as a fresh interpreter so nothing the test
# process remembers about the store -- a settled identity, a cached document and
# its maps -- can answer for it.
_LISTING_PROBE = r"""
import json
import os
import sys
from pathlib import Path

target = os.path.realpath(sys.argv[1])
listings = []
real_scandir = os.scandir
real_glob = Path.glob


def counted_scandir(value=".", *args, **kwargs):
    if not isinstance(value, int):
        try:
            here = os.path.realpath(value)
        except (TypeError, ValueError):
            here = None
        if here == target:
            listings.append("scandir")
    return real_scandir(value, *args, **kwargs)


def counted_glob(self, pattern, **kwargs):
    try:
        here = os.path.realpath(str(self))
    except (TypeError, ValueError):
        here = None
    if here == target:
        listings.append("glob:" + str(pattern))
    return real_glob(self, pattern, **kwargs)


os.scandir = counted_scandir
Path.glob = counted_glob

from reckon.crew import review_records

document = review_records.store_index_document(Path(sys.argv[1]))
print(
    json.dumps(
        {
            "listings": listings,
            "entries": len((document or {}).get("entries") or {}),
            "version": int((document or {}).get("version") or 0),
        }
    )
)
"""


def _payload(run_id: str, index: int) -> dict:
    return {
        "project": PROJECT,
        "reviewed_run_id": run_id,
        "review_run_id": f"review-of-{run_id}-{index:08x}",
        "reviewed_at": "2026-11-14T12:00:00Z",
        "reviewed_head_sha": HEAD,
        "status": "parsed",
        "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 18),
        "absent": [],
        "total": 18 * len(review.REVIEW_DIMENSIONS),
    }


def _store_directory() -> Path:
    return review.review_store_root() / PROJECT


def _populate(directory: Path) -> list[str]:
    """Write the store's records at the paths its own naming would choose."""
    directory.mkdir(parents=True, exist_ok=True)
    run_ids: list[str] = []
    for run in range(RUNS):
        run_id = f"r-20261114T1200{run:02d}-node-{run:02d}"
        run_ids.append(run_id)
        for index in range(RECORDS_PER_RUN):
            name = f"{run_id}.json" if index == 0 else f"{run_id}.at-{index:040x}.json"
            (directory / name).write_text(
                json.dumps(_payload(run_id, index)), encoding="utf-8"
            )
    return run_ids


def _move_identity_without_touching_a_record(directory: Path) -> None:
    """Create and remove a file in the store, which moves its stat identity."""
    before = review_records._store_directory_identity(directory)
    assert before is not None, "the store could not be stat'ed"
    transient = directory / ".a-writer-temporary"
    for _attempt in range(50):
        transient.write_text("x", encoding="utf-8")
        transient.unlink()
        if review_records._store_directory_identity(directory) != before:
            return
        time.sleep(0.01)
    pytest.fail(
        "creating and removing a file in the store did not move its identity, "
        "so the reconcile this test drives was never reached"
    )


def _second_process(directory: Path) -> dict:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    completed = subprocess.run(
        [sys.executable, "-c", _LISTING_PROBE, str(directory)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.fixture()
def stored(tmp_path):
    directory = _store_directory()
    run_ids = _populate(directory)
    for name in ("_DOCUMENT_MAPS", "_STORE_INDEXES", "_RESOLVED_DIRECTORIES"):
        cache = getattr(review_records, name, None)
        clear = getattr(cache, "clear", None)
        if clear is not None:
            clear()
    return directory, run_ids, tmp_path


def test_an_unchanged_reconcile_writes_only_its_stamp(stored) -> None:
    directory, run_ids, _tmp = stored
    review_records.store_index_document(directory)
    index = directory / ".review-index" / "index.json"
    before = os.stat(index)
    before_bytes = index.read_bytes()

    _move_identity_without_touching_a_record(directory)

    # One process calls it: the reconcile finds nothing changed and must not
    # rewrite the document to record the identity it settled.
    review_records.store_index_document(directory)
    after = os.stat(index)
    assert (after.st_ino, after.st_mtime_ns, after.st_size) == (
        before.st_ino,
        before.st_mtime_ns,
        before.st_size,
    ), "an unchanged reconcile rewrote the index document"
    assert index.read_bytes() == before_bytes, (
        "an unchanged reconcile changed the index document's bytes"
    )

    # A process started after that reconcile must answer from what the stamp
    # records, not repeat the store listing the reconcile just did.
    second = _second_process(directory)
    assert second["listings"] == [], (
        f"a freshly started process listed an unchanged store: {second['listings']}"
    )
    assert second["entries"] == RUNS * RECORDS_PER_RUN, (
        f"the cold process answered a document holding {second['entries']} entries"
    )

    # A record that really was added still moves the document.
    foreign = directory / f"{run_ids[1]}.at-{'f' * 40}.json"
    foreign.write_text(json.dumps(_payload(run_ids[1], 777)), encoding="utf-8")
    settled = review_records.store_index_document(directory)
    assert foreign.name in settled["entries"], (
        "an added record did not reach the index document"
    )
    assert index.read_bytes() != before_bytes, (
        "an added record left the index document unchanged"
    )
