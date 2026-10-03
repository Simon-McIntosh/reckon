"""A promoted run whose live pointer remains is not re-announced as dispatched.

Promotion writes the ledger row before it removes the live pointer, so for one
or more polls a promoted run is still observed: its pointer exists while its
work has already landed. The fleet fold decides arrivals from what it remembers
of the previous observation, so a run it drops from that memory is absent from
the next observation's recall and re-enters as a brand-new arrival — which the
fold words ``dispatched``. A live promoted run dropped this way alternates
``promoted`` and ``dispatched`` every tick and the follower never settles.

The run is held instead: while its pointer remains in the fleet it stays in the
fold's memory, so the next observation finds it in both and emits nothing. The
landing was announced once, at the transition into ``promoted``; it is not
announced again while the pointer lives.
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


def _write_recorded_run(
    tmp_path: Path, run_id: str, repo: Path, *, commits: list[str] | None = None
) -> None:
    """A completed run whose ledger row is written while its pointer lives.

    This is the promote window exactly: the row exists, so the run reads
    ``promoted``, while the live pointer has not yet been removed. Promotion
    writes the row from the run's own repository, so the pointer records the
    repository it was read from and the row is written where that repository
    resolves.
    """
    commits = ["HEAD"] if commits is None else commits
    manifest = tmp_path / "manifests" / f"{tmp_path.name}-{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\nstatus: complete\n"
        f"commits: {'HEAD' if commits else 'none'}\nblockers: none\n",
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
    record = {"run_id": run_id, "project": PROJECT, "commits": commits}
    if not commits:
        record["no_commit"] = "the report is the deliverable"
    row.write_text(json.dumps(record), encoding="utf-8")


def _snapshot(run_id: str) -> dict:
    return recovery._watch_snapshot(
        crew.read_pointer(run_id), moment=time.time(), stall_seconds=3600
    )


def test_a_promoted_live_run_is_held_and_announced_once(
    home: Path, tmp_path: Path
) -> None:
    run_id = "r-promoted-live"
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_recorded_run(tmp_path, run_id, repo)

    promoted = _snapshot(run_id)
    # Precondition: with the row written and the pointer still live, the run
    # reads promoted. A pointer that recorded no repository would read the
    # completion it left instead, and the held-run path would go unexercised.
    assert recovery._promote_record_holds(crew.read_pointer(run_id))
    assert promoted["state"] == "promoted"

    # The state the run stood in the tick before its promotion landed.
    before = {run_id: {**promoted, "state": "completed_unpromoted"}}

    # Three observations of the same live pointer, folded in sequence.
    seen: list[tuple[str | None, str]] = []
    known = before
    for _ in range(3):
        events, known = recovery.fleet_transitions(
            known,
            {run_id: _snapshot(run_id)},
            ledger_run_ids=recovery._ledger_run_id_reader(PROJECT),
        )
        seen.extend((previous, state) for _snap, previous, state, _counts in events)

    assert seen == [("completed_unpromoted", "promoted")]
    assert all(state != "dispatched" for _previous, state in seen)

    # Held, not forgotten: the run is still in the fold's memory, so a further
    # observation of the same live pointer is silent rather than a re-arrival.
    assert run_id in known
    events, _ = recovery.fleet_transitions(
        known,
        {run_id: _snapshot(run_id)},
        ledger_run_ids=recovery._ledger_run_id_reader(PROJECT),
    )
    assert events == []


def test_a_commitless_live_run_is_held_once_and_does_not_reannounce(
    home: Path, tmp_path: Path
) -> None:
    run_id = "r-recorded-live"
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_recorded_run(tmp_path, run_id, repo, commits=[])

    recorded = _snapshot(run_id)
    assert recovery._promote_record_holds(crew.read_pointer(run_id))
    assert recorded["state"] == "recorded"
    known = {run_id: {**recorded, "state": "completed_unpromoted"}}

    seen = []
    for _ in range(3):
        events, known = recovery.fleet_transitions(known, {run_id: _snapshot(run_id)})
        seen.extend((previous, state) for _snap, previous, state, _counts in events)
    assert seen == [("completed_unpromoted", "recorded")]

    departed, remaining = recovery.fleet_transitions(known, {})
    assert departed == []
    assert run_id not in remaining
