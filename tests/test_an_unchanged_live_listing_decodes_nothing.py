"""An unchanged live listing decodes no pointer, and costs less than a decode.

Every monitor pass lists the live directory, and each listing used to decode
every pointer it returned even though ``_pointer_text`` already re-read only
the pointers whose stat identity had moved. The record decoded from that text
is now memoised by ``reckon.file_memo`` — the repository's one owner of
parse-per-stat-identity — and handed out as that owner's read-only view, so a
listing whose population has not moved costs one stat per pointer and neither a
decode nor a copy, while a pointer that changed, appeared or vanished is
decoded or dropped once. The last test is the cost: an unchanged pass over
pointers of a realistic size must beat decoding the same fixture, which is
the work the base revision did on every pass, and a pass that decoded nothing
while copying each record would fail it.

The counters come from a stand-in installed as the listing's own ``json``
module, so they count the module's decodes rather than the test's own reads of
the same files. Every listing's content is compared with the files decoded
directly, which is what the base revision's listing returns.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import pytest

from reckon import file_memo
from reckon._store import write_json_atomically
from reckon.crew import runs

POINTERS = 300


class _Decodes:
    """Every document the listing module decodes while this stand-in is in it."""

    def __init__(self, real: object) -> None:
        self._real = real
        self.texts: list[str] = []

    def loads(self, text: str, *args, **kwargs):
        self.texts.append(text)
        return self._real.loads(text, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def _write(directory: Path, run_id: str, **fields) -> Path:
    path = directory / f"{run_id}.json"
    write_json_atomically(
        path,
        {
            "run_id": run_id,
            "project": "p",
            "phase": "working",
            "node_id": f"node-{run_id}",
            "dispatch_options": {"overrides": ["--set", "model=default"]},
            **fields,
        },
    )
    return path


def _listed_by_the_files(directory: Path) -> list[dict]:
    """The listing decoded straight off the files, as the base revision returns it."""
    return [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]


def _median_ms(operation, readings: int = 5) -> float:
    """The median wall time of ``readings`` runs of ``operation``, in milliseconds."""
    timings = []
    for _ in range(readings):
        start = time.perf_counter()
        operation()
        timings.append((time.perf_counter() - start) * 1000.0)
    return statistics.median(timings)


@pytest.fixture()
def live(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(runs, "_POINTER_TEXT", {}, raising=False)
    directory = runs.live_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(POINTERS):
        _write(directory, f"r-{index:03d}", phase="working")
    return directory


def _pointer_body(index: int) -> dict:
    """A live pointer's own shape at the size the population carries.

    The record is the nested one a live pointer holds — its dispatch options,
    its hold, its node record and the steps it recorded — rather than one long
    string, because what a copy of a pointer costs is set by how many containers
    it holds, not by how many bytes.
    """
    steps = {}
    for step in range(56):
        steps[f"step_{step:02d}"] = {
            "index": step,
            "label": f"a launch step the run recorded, {step}",
            "at": "2026-11-18T03:04:05Z",
            "seconds": 12.5,
            "alive": True,
            "note": "a step's note carried in the record",
        }
    return {
        "dispatch_options": {
            "overrides": ["--set", "model=default"],
            "accept_directory_claim": False,
            "after_requests": [],
        },
        "hold": {
            "backend": "codex-anchored",
            "ceiling_pct": 100.0,
            "effective_pct": 88.0,
            "reason": "utilisation under the ceiling",
        },
        "node": {
            "admission_findings": [],
            "after": [],
            "anchor_paths": [f"docs/figures/x{index}/{n}.html" for n in range(12)],
            "commands": ["uv run reckon serve"],
        },
        "review_contract": {
            "document_digest": "10eb109a06fd2716a80e37109d8a9733dd4275a",
            "sections": ["58", "96"],
        },
        "detail": steps,
    }


@pytest.fixture()
def fat_live(tmp_path, monkeypatch) -> Path:
    """The live directory holding ``POINTERS`` pointers of a realistic size.

    A live pointer population here runs to tens of kilobytes apiece, and the
    cost of a pass moves with that size, so the timing comparison is made over
    pointers near the median the population already carries rather than over
    the minimal records the other fixtures use.
    """
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(runs, "_POINTER_TEXT", {}, raising=False)
    directory = runs.live_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(POINTERS):
        _write(directory, f"r-{index:03d}", phase="working", **_pointer_body(index))
    return directory


@pytest.fixture()
def decodes(monkeypatch) -> _Decodes:
    stand_in = _Decodes(json)
    monkeypatch.setattr(runs, "json", stand_in)
    return stand_in


def test_an_unchanged_listing_decodes_nothing_and_keeps_its_records_apart(
    live, decodes
):
    first = runs._list_live_records(project="p")
    assert len(first) == POINTERS
    assert len(decodes.texts) == POINTERS, "the stand-in saw no decodes to count"
    assert first == _listed_by_the_files(live)

    decodes.texts.clear()
    second = runs._list_live_records(project="p")
    assert not decodes.texts, (
        f"an unchanged listing decoded {len(decodes.texts)} pointers"
    )
    assert second == _listed_by_the_files(live)

    # The record handed out is the caller's own copy of the memo's view, so a
    # change to it is held apart from the next listing rather than refused; a
    # caller that rewrites the run's fields reads it through read_pointer.
    second[0]["phase"] = "mutated by a caller"
    assert runs._list_live_records(project="p") == _listed_by_the_files(live)

    decodes.texts.clear()
    changed = _write(live, "r-010", phase="complete")
    assert changed.exists()
    _write(live, "r-300", phase="queued")
    (live / "r-299.json").unlink()

    third = runs._list_live_records(project="p")
    assert len(decodes.texts) <= 2, (
        f"a listing of one rewrite, one addition and one removal decoded "
        f"{len(decodes.texts)} pointers"
    )
    assert third == _listed_by_the_files(live)

    kept = next(row for row in third if row["run_id"] == "r-000")
    assert kept["phase"] == "working"
    assert kept["dispatch_options"] == {"overrides": ["--set", "model=default"]}


def test_the_pointer_records_are_kept_by_the_one_shared_memo(live, decodes):
    """The record store is ``file_memo``'s, not a second cache beside it."""
    runs._list_live_records(project="p")
    decodes.texts.clear()

    file_memo.clear()

    assert len(runs._list_live_records(project="p")) == POINTERS
    assert len(decodes.texts) == POINTERS, (
        "the records survived a clear of the shared memo, so the listing keeps "
        "a parse cache of its own"
    )


def test_the_one_caller_that_changes_a_pointer_moves_only_that_pointer(live, decodes):
    """``_mutate_pointer`` copies the shared record; the rest of the pass is untouched."""
    runs._list_live_records(project="p")

    def mutation(record: dict) -> dict:
        record["phase"] = "complete"
        record["dispatch_options"]["overrides"].append("--set model=changed")
        return record

    written = runs._mutate_pointer("r-007", mutation)
    assert written["phase"] == "complete"

    decodes.texts.clear()
    listed = runs._list_live_records(project="p")
    assert len(decodes.texts) == 1, (
        f"a listing after one pointer was rewritten decoded {len(decodes.texts)} "
        "pointers"
    )
    assert listed == _listed_by_the_files(live)
    changed = next(row for row in listed if row["run_id"] == "r-007")
    assert changed["phase"] == "complete"
    assert changed["dispatch_options"]["overrides"] == [
        "--set",
        "model=default",
        "--set model=changed",
    ]


def test_the_unchanged_listing_costs_less_than_the_base_revisions_pass(fat_live):
    """The second pass is cheaper than the one it replaced, not only quieter.

    The base revision kept each pointer's text and decoded it again on every
    pass, so one of its unchanged passes cost a stat and a decode per pointer.
    That pass is reproduced here over the same fixture — the texts are read
    once, as the base kept them, and only the stat and the decode are timed —
    and the unchanged listing must beat it. Pointers are of a realistic size
    because the cost that made a copy per pointer expensive moves with it.
    """
    paths = sorted(fat_live.glob("*.json"))
    texts = [path.read_text() for path in paths]

    runs._list_live_records()

    def base_pass():
        records = []
        for path, text in zip(paths, texts, strict=True):
            path.stat()
            records.append(json.loads(text))
        return records

    unchanged = _median_ms(runs._list_live_records)
    base = _median_ms(base_pass)

    assert unchanged < base, (
        f"an unchanged listing took {unchanged:.1f}ms against {base:.1f}ms for the "
        "base revision's stat-and-decode pass over the same pointers, so a pass "
        "that decodes nothing costs more than the pass it replaced"
    )


def test_a_liveness_read_over_a_listing_leaves_the_next_listing_unchanged(
    live, decodes, monkeypatch
):
    """The liveness reader stamps a copy, never the record a listing handed out.

    ``launcher_host_is_gone`` records the observation it made on the record it
    was given. Handed a listing's record, that write either reaches the memo
    every later listing reads or, over the read-only view, raises. The stamp
    belongs to a copy, so the listing's record keeps what its file holds.
    """
    from reckon.crew import process_liveness, recovery_liveness
    from tests.test_a_foreign_run_is_freed_by_its_allocation_ending import (
        FOREIGN_HOST,
        Scheduler,
    )

    _write(
        live,
        "r-foreign",
        launcher_host=FOREIGN_HOST,
        launcher_job_id="1319675",
        launched_at="2026-11-18T03:04:05Z",
        attempt=1,
    )
    monkeypatch.setattr(
        process_liveness,
        "_run_scheduler_query",
        Scheduler(
            "1319675|CANCELLED|2026-11-18T03:00:00\n1320449|RUNNING|2026-11-18T04:00:00\n"
        ),
    )

    listed = runs._list_live_records(project="p")
    foreign = next(record for record in listed if record.get("run_id") == "r-foreign")
    decodes.texts.clear()

    assert recovery_liveness.launcher_host_is_gone(record=foreign) is True

    stamp = foreign.get("launcher_host_observation")
    assert isinstance(stamp, dict), (
        "the observation did not reach the caller's own record, so this read "
        "cannot tell a copy from a rejection"
    )
    assert stamp["process_gone"] is True

    decodes.texts.clear()
    again = runs._list_live_records(project="p")
    assert decodes.texts == [], "the next listing decoded a pointer it had listed"
    assert again == _listed_by_the_files(live), (
        "the liveness read reached the memo through the record the listing handed out"
    )
    assert "launcher_host_observation" not in next(
        record for record in again if record.get("run_id") == "r-foreign"
    ), "the stamp the caller made reached the next listing"


def test_a_waiting_request_over_a_listing_leaves_the_next_listing_unchanged(
    live, decodes
):
    """The admission pass stamps a copy, never the record a listing handed out.

    ``resolve_waits`` records the obligations and the live runs exactly as the
    pass that persists them derives them. Handed a listing's record, that write
    either reaches the memo every later listing reads or, over the read-only
    view, raises inside the admission pass.
    """
    from reckon.crew.admission import release

    _write(live, "r-waiter", phase="awaiting-upstream", waits=["p#1"])
    listed = runs._list_live_records(project="p")
    waiter = next(record for record in listed if record.get("run_id") == "r-waiter")
    decodes.texts.clear()

    release.resolve_waits(
        waiter,
        live=listed,
        docs_dir=Path(__file__).resolve().parents[1] / "docs",
        cache={},
    )

    assert "upstream_obligations" in waiter and "upstream_live" in waiter, (
        "the pass wrote neither field to the record it derived, so this read "
        "cannot tell a copy from a rejection"
    )

    decodes.texts.clear()
    again = runs._list_live_records(project="p")
    assert decodes.texts == [], "the next listing decoded a pointer it had listed"
    assert again == _listed_by_the_files(live), (
        "the admission pass reached the memo through the record the listing handed out"
    )
    landed = next(record for record in again if record.get("run_id") == "r-waiter")
    assert "upstream_obligations" not in landed, (
        "the obligations the pass derived reached the next listing"
    )
