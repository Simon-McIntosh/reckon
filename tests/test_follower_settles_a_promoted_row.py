"""A terminal ledger row settles a run's follower row once.

Promotion writes the run's ledger row and only then removes the live pointer.
When the promotion's later steps fail — a peer's ``git index.lock`` taken — the
row is written and the pointer is left behind. The pointer's own reading and the
ledger row then disagree, and a fold that lets the pointer's reading win lets
the run's row flap between ``promoted`` and ``dispatched`` every poll, and keeps
re-emitting after the pointer is reaped.

The fold settles the run on the ledger row instead: once the row words the run
``promoted``, the landing is announced once and the pointer's later reading
announces nothing. The run leaves the fleet on its own pointer's disappearance
without a second row, and a genuine re-dispatch of the same id is still read as
an arrival.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import recovery

PROJECT = "proj"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Resolve every crew directory under the pytest temporary home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _write_promoted_run(tmp_path: Path, run_id: str, repo: Path) -> None:
    """A completed run whose ledger row is written while its pointer lives."""
    manifest = tmp_path / "manifests" / f"{tmp_path.name}-{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\nstatus: complete\ncommits: HEAD\nblockers: none\n",
        encoding="utf-8",
    )
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repo),
            "node": {"id": "node-a", "plan": "plan-a", "time_budget": "20m"},
            "phase": "working",
            "created_at": datetime.now(UTC).isoformat(),
            "manifest_path": str(manifest),
            "process_alive": None,
        },
    )
    row = ledger.run_path(PROJECT, run_id, str(repo))
    row.parent.mkdir(parents=True, exist_ok=True)
    row.write_text(
        json.dumps({"run_id": run_id, "project": PROJECT, "commits": ["HEAD"]}),
        encoding="utf-8",
    )


def _snapshot(run_id: str) -> dict:
    return recovery._watch_snapshot(
        crew.read_pointer(run_id), moment=time.time(), stall_seconds=3600
    )


def _fold(known: dict, current: dict):
    return recovery.fleet_transitions(
        known,
        current,
        ledger_run_ids=recovery._ledger_run_id_reader(PROJECT),
    )


def test_a_promoted_row_settles_once_and_does_not_alternate(
    home: Path, tmp_path: Path
) -> None:
    """A promoted ledger row wins over the pointer's own reading, once.

    The pointer is left behind reading ``dispatched`` while the ledger row says
    the run landed, which is the incident's disagreement. The two readings
    disagree every poll, so the fold is fed both across six observations. The
    landing is announced once as ``promoted`` and never answered by a
    ``dispatched`` row for the run.
    """
    run_id = "r-promoted-settles"
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_promoted_run(tmp_path, run_id, repo)

    promoted = _snapshot(run_id)
    assert recovery._promote_record_holds(crew.read_pointer(run_id))
    assert promoted["state"] == "promoted"

    dispatched = {**promoted, "state": "dispatched"}
    known = {run_id: {**promoted, "state": "completed_unpromoted"}}

    # The survivor's own word alternates with the ledger row's: the ledger row
    # sees promotion, the left-behind pointer keeps reading its stale phase.
    readings = [promoted, dispatched, promoted, dispatched, promoted, dispatched]

    seen: list[tuple[str | None, str]] = []
    for reading in readings:
        events, known = _fold(known, {run_id: reading})
        seen.extend((previous, state) for _s, previous, state, _c in events)

    assert [state for _previous, state in seen] == ["promoted"], seen
    assert all(state != "dispatched" for _previous, state in seen), seen
    # Settled, not forgotten: the memory still carries the promoted word, so a
    # later reading cannot re-open the run.
    assert str(known[run_id]["state"]) == "promoted"


def test_removing_the_pointer_emits_nothing_further(home: Path, tmp_path: Path) -> None:
    """A gc reap of the surviving pointer is not a second row for the run."""
    run_id = "r-promoted-reaped"
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_promoted_run(tmp_path, run_id, repo)

    promoted = _snapshot(run_id)
    # The run settled on a prior tick: its landing was already announced.
    known = {run_id: promoted}

    events, after = _fold(known, {})

    assert events == [], f"a settled run's disappearance is not news: {events!r}"
    assert run_id not in after, "the run gives up its slot when its pointer goes"


def test_a_settled_run_redispatched_reads_as_an_arrival(
    home: Path, tmp_path: Path
) -> None:
    """After the reap, a genuine re-dispatch of the same id is an arrival."""
    run_id = "r-promoted-redispatched"
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_promoted_run(tmp_path, run_id, repo)

    promoted = _snapshot(run_id)
    known = {run_id: promoted}
    _events, after = _fold(known, {})
    assert run_id not in after

    events, _after = _fold(after, {run_id: {**promoted, "state": "working"}})

    assert [(previous, state) for _s, previous, state, _c in events] == [
        (None, "dispatched")
    ], events
