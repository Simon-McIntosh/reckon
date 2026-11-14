"""A warm ledger read makes the same number of stat calls at any run population.

The index's cache key was the per-document stat walk: every read of a project's
ledger asked the filesystem about every run document that project had ever
recorded, so a promotion's ledger read cost one remote stat per run. On a shared
filesystem that is paid in D-state waits — a promotion was measured spending its
minutes in ``stat`` beneath ``_run_snapshot``.

A per-project write generation stands in for the walk. It is the store's own
file under the crew home, keyed by the project alone, so every process agrees
on it whichever index directory it resolves. Every ledger write path bumps it,
and a read whose generation still matches the one its index recorded reuses the
stamps the index already holds. A read whose generation differs, or whose index
recorded none, walks the documents exactly as it did before, so a ledger this
code has never written stays correct.

These tests measure the claim at two populations, drive every write path and
check the next read sees the row it wrote, and prove both rebuild conditions
really rebuild — with the value the rebuild found, not merely a count.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import _store, ledger

PROJECT = "demo"


@pytest.fixture(autouse=True)
def _config_home_inside_the_case(tmp_path, monkeypatch):
    """Keep the generation file — which lives under the config home — in the case."""
    home = tmp_path / "config-home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))


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
    before = ledger.ledger_generation(PROJECT)

    ledger.write_run_file(PROJECT, _run_record(0, gate="failed"), root)

    after = ledger.ledger_generation(PROJECT)
    assert after != before
    data, _version = ledger.load(PROJECT, root)
    gates = {row["run_id"]: row.get("gate") for row in data["runs"]}
    assert gates[_run_record(0)["run_id"]] == "failed"


def test_an_appended_run_bumps_the_generation_and_the_next_read_sees_it(tmp_path):
    root = _build(tmp_path / "repo", 2)
    ledger.load(PROJECT, root)
    before = ledger.ledger_generation(PROJECT)

    fresh = _run_record(99, node="another-node")
    ledger.append_run(PROJECT, fresh, root=root, allow_create=True)

    after = ledger.ledger_generation(PROJECT)
    assert after != before
    data, _version = ledger.load(PROJECT, root)
    assert fresh["run_id"] in {row["run_id"] for row in data["runs"]}


def test_an_aggregate_write_bumps_the_generation_and_the_next_read_sees_it(
    tmp_path,
):
    root = _build(tmp_path / "repo", 2)
    _data, version = ledger.load(PROJECT, root)
    before = ledger.ledger_generation(PROJECT)

    ledger.write(
        PROJECT,
        {"members": [{"id": "new-member"}], "runs": [], "holds": []},
        version,
        root,
    )

    after = ledger.ledger_generation(PROJECT)
    assert after != before
    data, _version = ledger.load(PROJECT, root)
    assert [member["id"] for member in data["members"]] == ["new-member"]
    assert len(data["runs"]) == 2, "the roster write disturbed the run documents"


def test_a_rolled_back_run_file_bumps_the_generation(tmp_path):
    """A restore puts a document back, so it must invalidate the stamps too."""
    root = _build(tmp_path / "repo", 2)
    ledger.load(PROJECT, root)
    target = ledger.run_path(PROJECT, str(_run_record(0)["run_id"]), root)
    prior = target.read_text(encoding="utf-8")
    ledger.write_run_file(PROJECT, _run_record(0, gate="failed"), root)

    # Read the generation immediately before the restore: the write above moved
    # it too, and a value captured before that write could not attribute the
    # change to the restore.
    before = ledger.ledger_generation(PROJECT)
    ledger._restore_run_files([(target, prior)], PROJECT, root)
    after = ledger.ledger_generation(PROJECT)

    assert after != before
    data, _version = ledger.load(PROJECT, root)
    gates = {row["run_id"]: row.get("gate") for row in data["runs"]}
    assert gates[_run_record(0)["run_id"]] == "passed"


def test_with_no_generation_file_a_read_rebuilds_and_finds_the_change(tmp_path):
    """An absent generation is read as "walk": the edit it never saw is returned."""
    root = _build(tmp_path / "repo", 20)
    ledger.load(PROJECT, root)
    ledger.ledger_generation_path(PROJECT).unlink()

    target = ledger.run_path(PROJECT, str(_run_record(3)["run_id"]), root)
    changed = json.loads(target.read_text(encoding="utf-8"))
    changed["gate"] = "failed"
    target.write_text(json.dumps(changed), encoding="utf-8")

    with _counting_stats() as seen:
        data, _version = ledger.load(PROJECT, root)
    gates = {row["run_id"]: row.get("gate") for row in data["runs"]}
    assert gates[_run_record(3)["run_id"]] == "failed"
    assert len(seen) >= 20, f"the read did not walk the documents: {len(seen)} stats"


def test_a_failed_advance_leaves_the_next_read_walking(tmp_path, monkeypatch):
    """A bump that cannot be recorded must not hide the edit it followed.

    The store suppresses a failed advance, which for a ledger leaves the counter
    equal to the one the index recorded and an edit invisible to a warm read.
    The ledger's bump verifies the advance and removes the counter when it did
    not move, so the next read goes back to the documents.
    """
    root = _build(tmp_path / "repo", 2)
    ledger.load(PROJECT, root)
    assert ledger.ledger_generation(PROJECT) is not None

    monkeypatch.setattr(_store, "advance_ledger_write_generation", lambda project: None)

    target = ledger.run_path(PROJECT, str(_run_record(0)["run_id"]), root)
    before = target.stat()
    target.write_text(json.dumps(_run_record(0, gate="failed")), encoding="utf-8")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    ledger.write_run_file(PROJECT, _run_record(1), root)

    assert not ledger.ledger_generation_path(PROJECT).exists(), (
        "the counter survived a failed advance, so a reader would compare it "
        "with the index and reuse stamps for the edited document"
    )
    with _counting_stats() as seen:
        data, _version = ledger.load(PROJECT, root)
    gates = {row["run_id"]: row.get("gate") for row in data["runs"]}
    assert gates[_run_record(0)["run_id"]] == "failed"
    assert len(seen) >= 2, f"the read did not walk the documents: {len(seen)}"


def test_a_write_survives_a_crew_home_that_refuses_the_advance(tmp_path, monkeypatch):
    """A read-only crew home must not fail the ledger write, nor hide changes.

    A fenced worker runs with its crew home mounted read-only, so the counter
    cannot exist there and the advance cannot succeed. The write still lands and
    the read walks, which is the correct answer; failing the write would take out
    every ledger write in such a process to guard a counter it does not have.
    """
    root = _build(tmp_path / "repo", 2)
    path = ledger.ledger_generation_path(PROJECT)
    path.unlink()
    directory = path.parent
    directory.chmod(0o500)
    try:
        ledger.write_run_file(PROJECT, _run_record(0, gate="failed"), root)
    finally:
        directory.chmod(0o700)

    assert ledger.ledger_generation(PROJECT) is None
    data, _version = ledger.load(PROJECT, root)
    gates = {row["run_id"]: row.get("gate") for row in data["runs"]}
    assert gates[_run_record(0)["run_id"]] == "failed"


def test_a_ledger_no_write_has_touched_never_reuses_a_stale_index(tmp_path):
    """No counter means no key, so even a warm read goes back to the documents.

    A project whose documents were all written by something that never bumped —
    a writer outside every ledger write path, or a ledger this code has not
    written since the key existed — has no generation to compare. An index
    published against that absence must not be trusted: an in-place edit that
    keeps the document's mtime is invisible except by walking, so the read walks
    and returns the edit.
    """
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT / "runs").mkdir(parents=True)
    target = ledger.run_path(PROJECT, str(_run_record(0)["run_id"]), root)
    target.write_text(json.dumps(_run_record(0)), encoding="utf-8")
    assert not ledger.ledger_generation_path(PROJECT).exists()

    ledger.load(PROJECT, root)
    before = target.stat()
    target.write_text(json.dumps(_run_record(0, gate="failed")), encoding="utf-8")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

    data, _version = ledger.load(PROJECT, root)
    gates = {row["run_id"]: row.get("gate") for row in data["runs"]}
    assert gates[_run_record(0)["run_id"]] == "failed"


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


def test_a_write_is_seen_through_another_index_directory(tmp_path, monkeypatch):
    """One generation per project, not one per index directory.

    The index directory falls back to the user cache when the configured one
    cannot be written, so two processes — or two configurations — can publish
    indexes in different directories. They must agree on the generation: a
    writer publishing through one leaves the other's index unchanged, and only
    a shared key tells the reader to walk its documents again.
    """
    root = _build(tmp_path / "repo", 3)
    run_id = str(_run_record(0)["run_id"])

    monkeypatch.setattr(ledger, "_index_directory", lambda: tmp_path / "configured")
    configured_index = ledger._run_index_path(PROJECT, root)
    configured_generation_path = ledger.ledger_generation_path(PROJECT)
    ledger.load(PROJECT, root)

    monkeypatch.setattr(ledger, "_index_directory", lambda: tmp_path / "fallback")
    fallback_index = ledger._run_index_path(PROJECT, root)
    ledger.load(PROJECT, root)
    assert fallback_index != configured_index, "the two configurations share an index"
    assert ledger.ledger_generation_path(PROJECT) == configured_generation_path, (
        "the generation file moves with the index directory, so the two "
        "configurations cannot see each other's writes"
    )

    ledger.write_run_file(PROJECT, _run_record(0, gate="failed"), root)

    monkeypatch.setattr(ledger, "_index_directory", lambda: tmp_path / "configured")
    data, _version = ledger.load(PROJECT, root)
    gates = {row["run_id"]: row.get("gate") for row in data["runs"]}
    assert gates[run_id] == "failed", "the write from the other directory was missed"


def _seed_aggregate(root: Path, rows: list[dict[str, object]]) -> None:
    path = root / "docs" / "state" / PROJECT / "crew.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _store._write_json_envelope(
        path,
        PROJECT,
        ledger.LEDGER_SLUG,
        {"members": [], "runs": rows, "holds": []},
        0,
    )


def test_a_run_split_is_seen_by_the_next_read(tmp_path):
    """The split writes documents and bumps once, so the next read sees them."""
    root = tmp_path / "repo"
    rows = [_run_record(index) for index in range(3)]
    _seed_aggregate(root, rows)
    for command in (
        ["init", "-q"],
        ["add", "docs"],
        [
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "seed",
        ],
    ):
        subprocess.run(["git", "-C", str(root), *command], check=True)

    data, _version = ledger.load(PROJECT, root)
    assert len(data["runs"]) == 3, "the seeded aggregate was not read"

    before = ledger.ledger_generation(PROJECT)
    entry = ledger._split_project_runs(PROJECT, root / "docs", dry_run=False)
    assert entry["written"] == 3, entry
    assert ledger.ledger_generation(PROJECT) != before, (
        "the split wrote documents without advancing the generation, so the "
        "next read would reuse stamps for a ledger it just changed"
    )

    data, _version = ledger.load(PROJECT, root)
    gates = {row["run_id"]: row.get("gate") for row in data["runs"]}
    assert gates == {str(row["run_id"]): "passed" for row in rows}


@pytest.mark.parametrize("count", [20, 200])
def test_the_warm_read_reports_the_rows_it_reused(tmp_path, count):
    """The positive control: a constant stat count must still carry every row."""
    stats, rows = _warm_read(_build(tmp_path / f"repo-{count}", count), count)
    assert rows == count
    assert stats < 10
