"""A warm ledger read makes the same number of stat calls at any run population.

The index's cache key was the per-document stat walk: every read of a project's
ledger asked the filesystem about every run document that project had ever
recorded, so a promotion's ledger read cost one remote stat per run. On a shared
filesystem that is paid in D-state waits — a promotion was measured spending its
minutes in ``stat`` beneath ``_run_snapshot``.

A per-project write-generation file stands in for the walk. Every ledger write
path bumps it, and a read whose generation still matches the one its index
recorded reuses the stamps the index already holds. A read whose generation
differs, or whose generation file is absent, walks the documents exactly as it
did before, so a ledger this code has never written stays correct.

These tests measure the claim at two populations, drive every write path and
check the next read sees the row it wrote, and prove both rebuild conditions
really rebuild — with the value the rebuild found, not merely a count.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest

from reckon import ledger

PROJECT = "demo"


def _run_record(index: int, **extra: object) -> dict[str, object]:
    record: dict[str, object] = {
        "run_id": f"r-20260101T0000{index:05d}-node-{index}",
        "project": PROJECT,
        "node": "a-ledger-read-does-not-walk-every-run",
        "role": "implement",
        "plan": "a-read-answers-inside-its-deadline",
        "section": "s64",
        "gate": "passed",
    }
    record.update(extra)
    return record


def _build(root: Path, count: int) -> Path:
    """Write ``count`` run documents through a real ledger write path."""
    (root / "docs" / "state" / PROJECT / "runs").mkdir(parents=True)
    for index in range(count):
        ledger.write_run_file(PROJECT, _run_record(index), root, fsync=False)
    return root


@contextlib.contextmanager
def _counting_stats():
    """Count every ``Path.stat`` a block makes, restoring the method after."""
    real = Path.stat
    seen: list[Path] = []

    def counting(self, *args, **kwargs):
        seen.append(Path(self))
        return real(self, *args, **kwargs)

    Path.stat = counting
    try:
        yield seen
    finally:
        Path.stat = real


def _warm_read(root: Path, count: int) -> tuple[int, int]:
    """Stats and rows from a read served by an index a previous read published."""
    data, _version = ledger.load(PROJECT, root)

    assert len(data["runs"]) == count, "the cold read did not see every document"

    with _counting_stats() as seen:
        data, _version = ledger.load(PROJECT, root)
    return len(seen), len(data["runs"])


def test_a_warm_read_makes_the_same_stat_calls_at_every_population(tmp_path):
    """Ten times the run documents, the same constant number of stat calls."""
    small = _warm_read(_build(tmp_path / "small", 20), 20)
    large = _warm_read(_build(tmp_path / "large", 200), 200)

    assert small[1] == 20 and large[1] == 200, "a warm read lost rows"
    assert small[0] == large[0], (
        f"the warm stat count grows with the population: {small[0]} at 20 runs, "
        f"{large[0]} at 200"
    )
    assert small[0] < 10, f"a warm read still walks: {small[0]} stat calls"


def test_a_run_file_write_bumps_the_generation_and_the_next_read_sees_it(tmp_path):
    root = _build(tmp_path / "repo", 2)
    ledger.load(PROJECT, root)
    before = ledger.ledger_generation(PROJECT, root)
    assert before is not None, "a run file write recorded no generation"

    ledger.write_run_file(PROJECT, _run_record(0, gate="failed"), root)

    after = ledger.ledger_generation(PROJECT, root)
    assert after is not None and after != before
    data, _version = ledger.load(PROJECT, root)
    gates = {row["run_id"]: row.get("gate") for row in data["runs"]}
    assert gates[_run_record(0)["run_id"]] == "failed"


def test_an_appended_run_bumps_the_generation_and_the_next_read_sees_it(tmp_path):
    root = _build(tmp_path / "repo", 2)
    ledger.load(PROJECT, root)
    before = ledger.ledger_generation(PROJECT, root)

    fresh = _run_record(99, node="another-node")
    ledger.append_run(PROJECT, fresh, root=root, allow_create=True)

    after = ledger.ledger_generation(PROJECT, root)
    assert after is not None and after != before
    data, _version = ledger.load(PROJECT, root)
    assert fresh["run_id"] in {row["run_id"] for row in data["runs"]}


def test_an_aggregate_write_bumps_the_generation_and_the_next_read_sees_it(
    tmp_path,
):
    root = _build(tmp_path / "repo", 2)
    _data, version = ledger.load(PROJECT, root)
    before = ledger.ledger_generation(PROJECT, root)

    ledger.write(
        PROJECT,
        {"members": [{"id": "new-member"}], "runs": [], "holds": []},
        version,
        root,
    )

    after = ledger.ledger_generation(PROJECT, root)
    assert after is not None and after != before
    data, _version = ledger.load(PROJECT, root)
    assert [member["id"] for member in data["members"]] == ["new-member"]
    assert len(data["runs"]) == 2, "the roster write disturbed the run documents"


def test_a_rolled_back_run_file_bumps_the_generation(tmp_path):
    """A restore puts a document back, so it must invalidate the stamps too."""
    root = _build(tmp_path / "repo", 2)
    ledger.load(PROJECT, root)
    before = ledger.ledger_generation(PROJECT, root)
    target = ledger.run_path(PROJECT, str(_run_record(0)["run_id"]), root)
    prior = target.read_text(encoding="utf-8")
    ledger.write_run_file(PROJECT, _run_record(0, gate="failed"), root)

    ledger._restore_run_files([(target, prior)], PROJECT, root)

    after = ledger.ledger_generation(PROJECT, root)
    assert after is not None and after != before
    data, _version = ledger.load(PROJECT, root)
    gates = {row["run_id"]: row.get("gate") for row in data["runs"]}
    assert gates[_run_record(0)["run_id"]] == "passed"


def test_with_no_generation_file_a_read_rebuilds_and_finds_the_change(tmp_path):
    """An absent generation is read as "walk": the edit it never saw is returned."""
    root = _build(tmp_path / "repo", 20)
    ledger.load(PROJECT, root)
    ledger.ledger_generation_path(PROJECT, root).unlink()

    target = ledger.run_path(PROJECT, str(_run_record(3)["run_id"]), root)
    changed = json.loads(target.read_text(encoding="utf-8"))
    changed["gate"] = "failed"
    target.write_text(json.dumps(changed), encoding="utf-8")

    with _counting_stats() as seen:
        data, _version = ledger.load(PROJECT, root)
    gates = {row["run_id"]: row.get("gate") for row in data["runs"]}
    assert gates[_run_record(3)["run_id"]] == "failed"
    assert len(seen) >= 20, f"the read did not walk the documents: {len(seen)} stats"


def test_a_document_added_without_a_bump_is_still_seen(tmp_path):
    """A writer that predates the generation cannot make a read serve stale rows.

    The run directory's own identity moves when a document is added or removed,
    whatever wrote it, so it catches the addition even with the generation
    unchanged.
    """
    root = _build(tmp_path / "repo", 2)
    ledger.load(PROJECT, root)

    added = _run_record(77)
    target = ledger.run_path(PROJECT, str(added["run_id"]), root)
    target.write_text(json.dumps(added), encoding="utf-8")

    data, _version = ledger.load(PROJECT, root)
    assert added["run_id"] in {row["run_id"] for row in data["runs"]}


@pytest.mark.parametrize("count", [20, 200])
def test_the_warm_read_reports_the_rows_it_reused(tmp_path, count):
    """The positive control: a constant stat count must still carry every row."""
    stats, rows = _warm_read(_build(tmp_path / f"repo-{count}", count), count)
    assert rows == count
    assert stats < 10
