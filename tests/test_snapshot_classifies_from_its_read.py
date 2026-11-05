"""A sweep classifies each row from the records it already read.

Reducing a live pointer to a snapshot can ask whether a review is in flight
for a finished run. That question is answered from the live-pointer
population, and a sweep holds that population already: one read of the
directory serves every row. When the hand-over is missing, each scoring row
lists the directory again, which is both a per-row cost and -- inside a
producer's own transition write -- a re-entry into the publisher that builds
the snapshot.

The check counts ``list_live`` calls while a one-row sweep reduces a scoring
pointer with no live review: none may be taken when the sweep's records are
handed over, and at least one is taken without them. The row is asserted to be
``scoring`` first, so a zero is read as "the sweep supplied the answer" rather
than as "the branch was never reached".
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from reckon.crew import recovery_review_dispatch, recovery_watch, runs


def _scoring_pointer(config_home: Path, run_id: str) -> dict:
    """A complete run with no live review: the branch that reads the live set."""
    manifest = config_home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: {run_id}\n", encoding="utf-8"
    )
    record = {
        "run_id": run_id,
        "project": "sample",
        "node": {"id": run_id, "plan": "fixture", "section": "s1"},
        "backend": "local",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
        "created_at": "2026-11-05T10:00:00Z",
    }
    runs._write_json(runs.pointer_path(run_id), record)
    return record


def _handover(records: list[dict]) -> dict:
    """The sweep's records, handed over only where the snapshot takes them.

    The parameter is absent on a tree where the hand-over does not exist yet,
    and the same case then measures the read that tree takes; that is what
    makes the count comparable across the two.
    """
    if "pointers" in inspect.signature(recovery_watch._watch_snapshot).parameters:
        return {"pointers": records}
    return {}


@pytest.fixture
def swept_run(tmp_path: Path, monkeypatch) -> tuple[Path, list[dict]]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    record = _scoring_pointer(config_home, "r-finished")
    return config_home, [record]


def test_a_sweep_snapshots_a_scoring_row_without_listing_the_live_set(
    swept_run: tuple[Path, list[dict]], monkeypatch
) -> None:
    _config_home, records = swept_run
    listed: list[int] = []
    real = recovery_review_dispatch.list_live

    def counting(*args, **kwargs):
        listed.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(recovery_review_dispatch, "list_live", counting)

    snapshots = [
        recovery_watch._watch_snapshot(
            record,
            moment=1_900_000_000.0,
            stall_seconds=600,
            cache={},
            **_handover(records),
        )
        for record in records
    ]

    # Positive control: the row really is the scoring row whose review search
    # is the read under test. A classification that never reached it would
    # make the count below say nothing.
    assert [snapshot["classification"] for snapshot in snapshots] == ["scoring"]
    handed = len(listed)

    # Second control, on the same row with the same counter: classified with
    # no population handed over, the row does read the live set. The zero
    # above therefore reports the hand-over and not a counter that sees
    # nothing.
    listed.clear()
    unhanded = recovery_watch._watch_snapshot(
        records[0], moment=1_900_000_000.0, stall_seconds=600, cache={}
    )
    assert unhanded["classification"] == "scoring"
    assert len(listed) >= 1

    assert handed == 0, f"the sweep's row listed the live set {handed} time(s)"
