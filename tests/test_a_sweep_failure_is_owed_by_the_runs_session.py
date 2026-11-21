"""A stored sweep failure is owed by the session the run's live pointer names.

A station keeps failed runs per project as durable state, keyed by the session
whose sweep recorded them, and republishes each as a ``sweep-failed`` row. That
key outlives the run's owner, so a run handed from one session to another raised
its row in the session that could not act on it: the row's own command observes
the run, and the wrong session could clear it only until the next sweep raised
it again.

Three facts are measured here, each starting from a failure written through
``record_sweep_report``: the row goes to the session the live pointer names and
not to the session that recorded it; a run still owned by the session that
recorded it keeps its row; and a run whose pointer names no session raises no
row anywhere while its failure stays in the stored record.

The negative control is this module against the base revision, where the first
case raises the row in the recording session.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import obligation_snapshot, station_obligations, station_store

PROJECT = "sweep-owner-fixture"
OWNER = "sweep-owner-session"
RECORDER = "sweep-recorder-session"
RUN_ID = "r-sweep-owner"
ERROR = "ValueError: unreadable ledger row"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def _git(tree: Path, *args: str) -> None:
    """Run one git command in the fixture tree, refusing a silent failure."""
    result = subprocess.run(
        [
            "git",
            "-C",
            str(tree),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (args, result.stderr)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root the mount registry resolves, before any record is written."""
    config = tmp_path / "config"
    config.mkdir()
    mounts = config / "mounts.json"
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _git(root, "init", "-q")
    _git(
        root,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "landed",
    )
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


@pytest.fixture(autouse=True)
def live_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both sessions deliver for the project, so a missing row is a fact of its own.

    Neither registration names an owner: the row a session is given here comes
    from the stored failure record and the run's pointer, which is the question
    the case asks. With both sessions live, a session carrying no sweep row has
    had a row derived and been given none.
    """
    rows = [{"session": OWNER, "live": True}, {"session": RECORDER, "live": True}]
    monkeypatch.setattr(station_obligations, "follower_rows", lambda project: rows)


def _pointer(session: str | None) -> Path:
    """Write this run's live pointer, naming ``session`` when one is given."""
    path = obligation_snapshot.live_pointer_path(RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"run_id": RUN_ID, "project": PROJECT}
    if session is not None:
        record["session"] = session
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _record(store, session: str) -> None:
    """Store this run's failure the way that session's completed sweep reports it."""
    station_obligations.record_sweep_report(
        store,
        PROJECT,
        session,
        {"reviews": {"failed": [{"run_id": RUN_ID, "error": ERROR}]}},
    )


def _refresh(store) -> None:
    """One station refresh of this project, over its stored failure record."""
    station_obligations.refresh(
        store,
        project=PROJECT,
        changed=True,
        now=NOW,
        sweep_failures=station_obligations.prune_sweep_failures(store, PROJECT),
    )


def _sweep_rows(store, session: str) -> list[str]:
    """The runs one session's stored obligations raise as sweep-failed rows."""
    stored = store.read_obligation(session)
    if stored is None:
        return []
    document = station_obligations.stored_document(stored) or {}
    rows = document.get("obligations")
    return [
        str(row.get("run_id") or "")
        for row in rows or ()
        if str(row.get("kind") or "") == "sweep-failed"
    ]


def _stored(store) -> dict:
    """The durable per-session failure record, as the station reads it."""
    stored = store.station_value(station_obligations._sweep_failure_key(PROJECT))
    return json.loads(stored or "{}")


def test_the_row_goes_to_the_session_the_pointer_names(repository: Path, store) -> None:
    """A run handed to another session owes its row there, not to its recorder."""
    _pointer(OWNER)
    _record(store, RECORDER)

    _refresh(store)

    assert _sweep_rows(store, OWNER) == [RUN_ID]
    assert _sweep_rows(store, RECORDER) == []


def test_a_run_its_recorder_still_keeps_its_own_row(repository: Path, store) -> None:
    """The owner reading is not a new gate on a run nobody handed over."""
    _pointer(RECORDER)
    _record(store, RECORDER)

    _refresh(store)

    assert _sweep_rows(store, RECORDER) == [RUN_ID]
    assert _sweep_rows(store, OWNER) == []


def test_a_pointer_naming_no_session_raises_no_row_anywhere(
    repository: Path, store
) -> None:
    """A failure no session can act on is kept, and owed by no one."""
    _pointer(None)
    _record(store, RECORDER)

    _refresh(store)

    assert _sweep_rows(store, OWNER) == []
    assert _sweep_rows(store, RECORDER) == []
    stored = _stored(store)
    assert [row["run_id"] for row in stored[RECORDER]] == [RUN_ID]
