"""A duty clears when the run it names has left the fleet.

A station keeps a session's failed sweeps as durable state and republishes them
as duties until that session's next completed sweep replaces the list. A run
promoted, landed, discarded or reaped between two sweeps therefore leaves a row
naming a command that can no longer act: the row carries ``crew observe --run``,
and the run it names has no pointer left to observe.

Three facts are measured here. A failure whose run the fleet still holds is
raised, so the drop is the run's ending rather than the row's kind; a failure
whose run has ended is pruned from the durable record as well as withheld from
the answer, because a record left behind returns on the next pass that finds
nothing else changed; and a row for a run with no pointer and no record is
still clearable by hand, which it is not while a deferral cannot name a
project.

The negative control is this module run against the base revision, where the
row stands after its run has ended and the deferral for the runless case is
refused.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import obligation_snapshot, runs, station_obligations, station_store

obligations = importlib.import_module("reckon.crew.obligations")

PROJECT = "duty-ends-fixture"
SESSION = "duty-ends-session"
OTHER_SESSION = "duty-ends-other-session"
RUN_ID = "r-duty-ends"
OTHER_RUN_ID = "r-duty-ends-other"
ERROR = "ValueError: unreadable ledger row"
NOW = datetime.now(tz=UTC)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root the mount registry resolves, before any record is written."""
    config = tmp_path / "config"
    config.mkdir()
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    mounts = config / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(config))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    return root


@pytest.fixture()
def store(tmp_path: Path):
    station = station_store.StationStore(tmp_path / "runtime")
    station.connect()
    try:
        yield station
    finally:
        station.close()


def _pointer(run_id: str = RUN_ID) -> Path:
    """Write a live pointer, which is what the fleet holding a run looks like."""
    path = obligation_snapshot.live_pointer_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"run_id": run_id, "project": PROJECT}), encoding="utf-8"
    )
    return path


def _landed(repository: Path, run_id: str = RUN_ID) -> Path:
    """Write the run record a promotion leaves beside the project's ledger."""
    path = ledger.run_path(PROJECT, run_id, repository)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"run_id": run_id, "project": PROJECT})
    path.write_text(payload, encoding="utf-8")
    return path


def _failure(run_id: str) -> dict[str, str]:
    return {"run_id": run_id, "error": ERROR}


def _record(store, session: str, run_id: str) -> None:
    """Store one failed run the way a completed sweep reports it."""
    station_obligations.record_sweep_report(
        store,
        PROJECT,
        session,
        {"reviews": {"failed": [_failure(run_id)]}},
    )


def _stored(store) -> dict:
    """The durable per-session failure record, as the station reads it."""
    key = station_obligations._sweep_failure_key(PROJECT)
    return json.loads(store.station_value(key) or "{}")


def _payload(session: str = SESSION) -> dict:
    return {
        "project": PROJECT,
        "session": session,
        "obligations": [],
        "acknowledged": [],
        "summary": {"count": 0, "oldest_age_seconds": 0},
    }


def _raised(failures) -> list[tuple[str, str]]:
    """The sweep rows one pass publishes, as (kind, run_id) pairs."""
    published = station_obligations._with_sweep_failures(_payload(), failures)
    rows = published["obligations"]
    return [(str(row["kind"]), str(row["run_id"])) for row in rows]


def test_a_failure_is_raised_while_the_fleet_still_holds_its_run(
    repository: Path, store
) -> None:
    """The positive control: a live run's failure is a duty, not a leftover."""
    _pointer()
    _record(store, SESSION, RUN_ID)

    assert _raised(_stored(store)[SESSION]) == [("sweep-failed", RUN_ID)]


@pytest.mark.parametrize("recorded_as", ["discarded", "landed"])
def test_a_failure_clears_once_its_run_has_left_the_fleet(
    repository: Path, store, recorded_as: str
) -> None:
    """A run whose pointer is gone owes nothing from a sweep.

    Both endings look the same to the station -- no pointer to observe -- and
    either one settles the row, which is why the drop does not depend on telling
    a discard from a landing. ``landed`` adds the ledger record a promotion
    writes, so the second case is a run the fleet has already settled rather
    than one it has merely stopped holding.
    """
    pointer = _pointer()
    _record(store, SESSION, RUN_ID)
    assert _raised(_stored(store)[SESSION]) == [("sweep-failed", RUN_ID)]

    pointer.unlink()
    if recorded_as == "landed":
        _landed(repository)
    assert not obligation_snapshot.live_pointer_path(RUN_ID).exists()

    assert _raised(_stored(store)[SESSION]) == []

    station_obligations.prune_sweep_failures(store, PROJECT)
    assert SESSION not in _stored(store)


def test_an_ended_failure_does_not_clear_another_sessions(
    repository: Path, store
) -> None:
    """Pruning removes the ended run's entry, not the record it lives in."""
    _pointer()
    other = _pointer(OTHER_RUN_ID)
    _record(store, SESSION, RUN_ID)
    _record(store, OTHER_SESSION, OTHER_RUN_ID)

    other.unlink()
    assert _raised(_stored(store)[SESSION]) == [("sweep-failed", RUN_ID)]
    assert _raised(_stored(store).get(OTHER_SESSION, ())) == []
    station_obligations.prune_sweep_failures(store, PROJECT)
    assert OTHER_SESSION not in _stored(store)


def _row(run_id: str = RUN_ID) -> dict:
    return {
        "kind": "blocked",
        "run_id": run_id,
        "node": "fixture-node",
        "plan": "fixture-plan",
        "age_seconds": 600,
        "next_command": "reckon crew observe --run " + run_id,
    }


def _store_duties(session: str = SESSION, run_id: str = RUN_ID) -> Path:
    """Store the duty document a session's view is served from."""
    directory = obligation_snapshot.obligations_home() / PROJECT
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session}.json"
    document = {
        "project": PROJECT,
        "session": session,
        "obligations": [_row(run_id)],
        "acknowledged": [],
        "summary": {"count": 1, "oldest_age_seconds": 600},
    }
    path.write_text(json.dumps({"payload": {"document": document}}), encoding="utf-8")
    return path


def _held_row(run_id: str = RUN_ID) -> dict:
    return {
        "kind": "worktree-held",
        "run_id": run_id,
        "node": "fixture-node",
        "plan": "fixture-plan",
        "age_seconds": 600,
        "next_command": "reckon crew gc --run " + run_id + " --apply",
    }


def _served(rows: list[dict]) -> list[tuple[str, str]]:
    """The rows an answer still serves once its settled duties are withheld."""
    settled = obligation_snapshot.without_departed_run_duties(
        {"obligations": list(rows), "summary": {"count": len(rows), "oldest_age_seconds": 0}}
    )
    return [(str(row["kind"]), str(row["run_id"])) for row in settled["obligations"]]


@pytest.mark.parametrize("release_owned", [True, False])
def test_a_held_tree_settles_once_its_release_has_an_owner(
    release_owned: bool,
) -> None:
    """A retained tree is a duty until the cleanup queue owns its release.

    The tree outlives the run, so a missing pointer settles nothing on its own:
    with no owner the row is the only surface naming the bytes, which is why
    the same row is kept in one arm and withheld in the other.
    """
    # Written from the config home rather than through the module's helper, so
    # the base revision fails on the settlement it lacks rather than on a name
    # it does not carry.
    intent = obligation_snapshot.crew_home() / "cleanup" / f"{RUN_ID}.json"
    intent.parent.mkdir(parents=True, exist_ok=True)
    if release_owned:
        intent.write_text(json.dumps({"run_id": RUN_ID}), encoding="utf-8")

    assert _served([_held_row()]) == (
        [] if release_owned else [("worktree-held", RUN_ID)]
    )


def test_a_deferral_reaches_a_run_with_no_pointer_and_no_record(
    repository: Path,
) -> None:
    """A row that outlives its run's records is still clearable by hand.

    The run here holds no live pointer and no ledger record, and its duty
    document is the only place its project is written down. A deferral that
    cannot name that project is refused, and one naming the wrong project is
    read by nothing, so the duty stands however the coordinator tries.
    """
    _store_duties()
    assert not obligation_snapshot.live_pointer_path(RUN_ID).exists()
    assert not ledger.run_path(PROJECT, RUN_ID, repository).exists()
    assert runs.recorded_promoted_acknowledgements(PROJECT) == []

    until = (NOW + timedelta(hours=2)).isoformat()
    runs.record_run_acknowledgement(RUN_ID, "awaiting a peer release", until)

    recorded = json.loads(runs.acknowledgement_path(RUN_ID).read_text(encoding="utf-8"))
    assert recorded["project"] == PROJECT, recorded
    record = runs.recorded_promoted_acknowledgements(PROJECT)
    assert [str(entry["run_id"]) for entry in record] == [RUN_ID]

    owed, deferred = obligations._partition_acknowledged([_row()], {RUN_ID: record[0]})
    assert owed == []
    assert [entry["run_id"] for entry in deferred] == [RUN_ID]


def test_a_deferral_for_a_run_no_record_names_is_still_refused(
    repository: Path,
) -> None:
    """The fallback resolves a project; it does not accept any run at all."""
    until = (NOW + timedelta(hours=2)).isoformat()

    with pytest.raises(runs.CrewError):
        runs.record_run_acknowledgement(RUN_ID, "awaiting a peer release", until)
