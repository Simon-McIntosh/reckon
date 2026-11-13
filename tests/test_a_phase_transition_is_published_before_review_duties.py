"""A run's phase transition is published before review duties are derived.

A project's review-duty derivation reads the review store once per run and can
take far longer than a tick. A transition is what wakes the session that owns a
run, so committing each project's transitions and then deriving that project's
duties before the next project's transitions holds a run's wake behind work
that has nothing to do with it, and a derivation in flight must not delay
another tick's phase events for any project.

Both projects are driven through one real ``Station.tick`` on a temporary crew
home. The first project's review-duty derivation is held open by the case; the
second project's run has already transitioned by then, and that transition is
the row that wakes its session.
"""

from __future__ import annotations

import json
import socket
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import obligations, station, station_obligations, station_store
from tests.helpers.git import git as _git

# Two projects, in the order the tick walks them: the first owns the derivation
# the case blocks, the second owns the run whose transition must not wait for it.
FIRST = "alpha-fixture"
SECOND = "zeta-fixture"
SESSION = "session-one"
RUN_FIRST = "r-alpha"
RUN_SECOND = "r-zeta"
MOMENT = datetime(2026, 11, 13, 1, 0, tzinfo=UTC)


def _write_pointer(home: Path, project: str, run_id: str, *, phase: str) -> Path:
    """One live pointer, shaped as the launcher and the supervisor write it."""
    stream = home / "runs" / run_id / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    if not stream.exists():
        stream.write_text(
            json.dumps({"type": "system", "subtype": "init"}) + "\n", encoding="utf-8"
        )
    record = {
        "run_id": run_id,
        "project": project,
        "session": SESSION,
        "phase": phase,
        "created_at": "2026-11-13T00:10:00Z",
        "launcher_host": socket.gethostname(),
        "node": {"id": run_id},
        "reason": f"now {phase}",
        "log_path": str(stream),
        "worktree": str(home / "trees" / run_id),
    }
    path = home / "live" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _append_stream_line(path: Path) -> None:
    """Move the stream reader, as a delivering worker's next line would."""
    stream = Path(json.loads(path.read_text(encoding="utf-8"))["log_path"])
    with stream.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"type": "assistant", "message": {"content": []}}) + "\n"
        )


class _Sweeps:
    """The station's sweep arm, run where the case can see it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, project: str, *, session=None, **kwargs) -> dict:
        self.calls.append((project, str(session)))
        return {}


def _station(root: Path, home: Path) -> station.Station:
    store = station_store.StationStore(root)
    beat = station.SweepBeat(sweep=_Sweeps(), start=lambda action: action())
    return station.Station(store, home=home, projects=dict, sweeps=beat)


@pytest.fixture()
def mounted(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Both projects mounted on one repository, as the station's own reads need."""
    docs = tmp_path / "repo" / "docs"
    for project in (FIRST, SECOND):
        (docs / "state" / project).mkdir(parents=True, exist_ok=True)
    (docs.parent / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the phase transition fixture"),
    ):
        _git(docs.parent, *arguments)
    mounts = home / "mounts.json"
    mounts.write_text(json.dumps({FIRST: str(docs), SECOND: str(docs)}), "utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    return docs


@pytest.mark.timeout(60)
def test_a_later_projects_transition_does_not_wait_for_an_earlier_derivation(
    home: Path, tmp_path: Path, mounted: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "station"
    first_pointer = _write_pointer(
        home=home, project=FIRST, run_id=RUN_FIRST, phase="working"
    )
    second_pointer = _write_pointer(
        home=home, project=SECOND, run_id=RUN_SECOND, phase="queued"
    )

    entered = threading.Event()
    release = threading.Event()
    armed = False

    def derive_review_duty(project, row, pointer, **kwargs):
        if project == FIRST and armed:
            entered.set()
            assert release.wait(20), "the review-duty derivation was never released"
        return {
            "kind": "review-ready",
            "run_id": str(row.get("run_id") or ""),
            "age_seconds": 0,
            "next_command": f"reckon crew promote --run {RUN_FIRST}",
        }

    def classified_rows(project):
        if project != FIRST:
            return []
        return [
            {
                "run_id": RUN_FIRST,
                "session": SESSION,
                "project": FIRST,
                "classification": "promotable",
                "age_seconds": 0,
            }
        ]

    def followers(project):
        return [{"session": SESSION, "live": True}]

    monkeypatch.setattr(obligations, "_review_duty_item", derive_review_duty)
    monkeypatch.setattr(obligations, "_classified_rows", classified_rows)
    monkeypatch.setattr(station_obligations, "follower_rows", followers)

    node = _station(root, home)
    # The first tick is inventory: a run this station has never seen is recorded
    # as its baseline rather than published as a move.
    node.tick(now=MOMENT)
    assert node.store.events() == []

    # The second project's run moves. From here the derivation the first project
    # owns is held open, so that transition can only be published by an ordering
    # which commits every project's transitions before any derivation.
    _write_pointer(home=home, project=FIRST, run_id=RUN_FIRST, phase="blocked-upstream")
    _write_pointer(
        home=home, project=SECOND, run_id=RUN_SECOND, phase="blocked-upstream"
    )
    _append_stream_line(second_pointer)
    _append_stream_line(first_pointer)
    armed = True

    failure: list[BaseException] = []

    def tick() -> None:
        try:
            node.tick(now=MOMENT + timedelta(seconds=5))
        except BaseException as error:  # noqa: BLE001 - carried to the case
            failure.append(error)

    worker = threading.Thread(target=tick, name="station-tick")
    worker.start()
    try:
        assert entered.wait(20), (
            "the review-duty derivation was never reached; "
            f"alive={worker.is_alive()} failure={failure!r}"
        )
        # Read the station's committed rows while the derivation is still open.
        reader = station_store.StationStore(root)
        try:
            committed = reader.events()
        finally:
            reader.close()
        moved = {
            str(row.get("run")): (row.get("from_state"), row.get("to_state"))
            for row in committed
            if row.get("kind") == "transition"
        }
        assert moved.get(RUN_SECOND) == ("queued", "blocked-upstream"), (
            "the later project's transition waited for the earlier project's "
            f"derivation: {sorted(moved)}"
        )
    finally:
        release.set()
        worker.join(30)

    print("EVENTS", node.store.events())
    print("POST", [(r["run_id"], r["phase"]) for r in node.store.pointer_rows()])
    print(
        "KNOWN2",
        {k: v.get("state") for k, v in node.store.recorded_fleet(SECOND).items()},
    )
    assert not failure, failure
    assert not worker.is_alive()
    assert any(
        row.get("run") == RUN_SECOND and row.get("kind") == "transition"
        for row in node.store.events()
    ), node.store.events()
