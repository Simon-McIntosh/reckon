"""The station derives the fleet state on a change or at the interval.

The fleet state — every live pointer classified, the ledger read, each run's
review looked up — is built from three inputs: the live pointers, each run's
manifest and the project's plans. Those move on the scale of minutes while a
station ticks every second, so a tick that re-derives them over an unmoved
fleet pays for an answer that has not changed.

The station is driven here through its real tick over a temporary crew home,
with a stand-in counting the derivations. Thirty quiet ticks of one second each
fall inside one interval and derive once; a tick after one pointer, one
manifest and one plan have each moved derives once more; and the interval
expiring with nothing changed derives once more. Every tick's stored
obligations are compared with the ones an ungated derivation at the interval's
own instant produces, so a gate that skipped a derivation it owed is caught by
the answer as well as by the count.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import (
    obligation_snapshot,
    runs,
    station,
    station_obligations,
    station_store,
)
from reckon.crew.follower_registration import follower_registration
from tests.helpers.git import git as _git

PROJECT = "fleet-state-fixture"
SESSION = "follower-fixture"
RUN = "r-fleet-state"
PLAN = "fixture-plan"
SECTION = "fixture-section"
MOMENT = datetime(2026, 11, 19, 6, 0, tzinfo=UTC)
QUIET_TICKS = 30
INTERVAL_SECONDS = 60.0


def _plan_document(status: str) -> str:
    """One project plan, shaped as the derivation's plan read expects it."""
    return (
        "<html><head>"
        '<meta name="plan-version" content="1">'
        "</head><body>"
        f'<h2 id="{SECTION}">Fixture section</h2>'
        f'<section data-reckon="section" data-id="{SECTION}" '
        'data-effort-hours="0.25" data-capability-class="general" '
        'data-capability-reasoning="standard" '
        'data-capability-verification="strict" '
        f'data-status="{status}" data-links=""></section>'
        "</body></html>"
    )


class _Duties:
    """Run each project's derivation in the calling thread, where it is counted."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def committing(self) -> Any:
        return nullcontext()

    def request(self, project: str, **kwargs: Any) -> bool:
        store = station_store.StationStore(self.root)
        try:
            station_obligations.publish(
                store,
                project=project,
                home=kwargs.pop("home"),
                changed=kwargs.pop("changed"),
                now=kwargs.pop("moment"),
            )
        finally:
            store.close()
        return True


class _Sweeps:
    def request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _pointer_record(manifest: Path, *, phase: str = "working") -> dict[str, Any]:
    """One live pointer, shaped as the launcher and the supervisor write it."""
    return {
        "run_id": RUN,
        "project": PROJECT,
        "session": SESSION,
        "phase": phase,
        "repo": str(manifest.parent.parent / "repo"),
        "manifest_path": str(manifest),
        "node": {
            "id": RUN,
            "plan": PLAN,
            "section": SECTION,
            "write_paths": ["seed.txt"],
        },
    }


class _Derivations:
    """Count the fleet-state derivations that actually run.

    A comparison the case makes for itself derives the state to compare against,
    so the counter is suspended around it: only the station's own derivations
    are what the counts are about.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.suspended = False

    @contextmanager
    def installed(self) -> Iterator[_Derivations]:
        real = obligation_snapshot._derive_fleet_state
        counter = self

        def counting(*args: Any, **kwargs: Any) -> Any:
            if not counter.suspended:
                counter.calls += 1
            return real(*args, **kwargs)

        obligation_snapshot._derive_fleet_state = counting
        try:
            yield counter
        finally:
            obligation_snapshot._derive_fleet_state = real

    @contextmanager
    def ungated(self) -> Iterator[None]:
        self.suspended = True
        try:
            yield None
        finally:
            self.suspended = False


@pytest.fixture()
def fleet(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """A mounted project with one live run, one manifest and one plan."""
    docs = tmp_path / "repo" / "docs"
    (docs / "state" / PROJECT).mkdir(parents=True)
    (docs / "plans").mkdir(parents=True)
    plan = docs / "plans" / f"{PLAN}.html"
    plan.write_text(_plan_document("implementable"), encoding="utf-8")
    (docs.parent / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the fleet-state fixture"),
    ):
        _git(docs.parent, *arguments)
    (docs / "state" / PROJECT / "crew.json").write_text(
        json.dumps({"data": {"_version": 1, "members": [], "runs": [], "holds": []}}),
        encoding="utf-8",
    )
    mounts = home / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(docs)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    manifest = home / "manifests" / f"{RUN}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: {RUN}\nstatus: in-progress\ncommits: []\n", "utf-8")
    runs._write_json(
        runs.pointer_path(RUN),
        _pointer_record(manifest),
    )
    return {
        "home": home,
        "docs": docs,
        "plan": plan,
        "manifest": manifest,
        "root": tmp_path / "station",
    }


def _station(fleet: dict[str, Any], moment: list[datetime]) -> station.Station:
    """One station whose clock the case advances a tick at a time."""
    store = station_store.StationStore(fleet["root"])
    return station.Station(
        store,
        home=fleet["home"] / "crew",
        projects=lambda: {PROJECT: fleet["docs"]},
        duties=_Duties(fleet["root"]),
        sweeps=_Sweeps(),
        clock=lambda: moment[0],
    )


def _published(node: station.Station, counter: _Derivations, moment: datetime) -> str:
    """The stored obligations, compared with an ungated derivation's.

    A gate that skipped a derivation it owed publishes the previous answer,
    which is what this comparison catches: the immediate derivation is the
    answer the base revision publishes on every tick.
    """
    row = node.store.read_obligation(SESSION)
    assert row is not None, "the tick published no obligations at all"
    with counter.ungated():
        state = obligation_snapshot.fleet_state(PROJECT, now=moment)
    payload = obligation_snapshot.payload_for(state, SESSION)
    stored = station_obligations.stored_identity(row)
    derived = station_obligations.payload_identity(payload)
    assert stored == derived, (
        f"the tick at {moment.isoformat()} published {stored} where an ungated "
        f"derivation gives {derived}"
    )
    return stored


def test_thirty_quiet_ticks_derive_the_fleet_state_once(
    fleet: dict[str, Any],
) -> None:
    """Thirty ticks inside one interval derive once and publish one answer."""
    moment = [MOMENT]
    node = _station(fleet, moment)
    counter = _Derivations()
    with (
        follower_registration(PROJECT, SESSION, delivery="terminal"),
        counter.installed(),
    ):
        for index in range(QUIET_TICKS):
            moment[0] = MOMENT + timedelta(seconds=index)
            node.tick()
            _published(node, counter, moment[0])
    assert counter.calls == 1, (
        f"{QUIET_TICKS} quiet ticks derived the fleet state {counter.calls} times"
    )


def _advance(node, counter, moment, seconds):
    """Move the clock, tick once, and report the derivations that tick cost."""
    before = counter.calls
    moment[0] = moment[0] + timedelta(seconds=seconds)
    node.tick()
    _published(node, counter, moment[0])
    return counter.calls - before


def test_each_moved_input_derives_on_its_own_tick(fleet: dict[str, Any]) -> None:
    """A moved pointer, manifest or plan derives once; a quiet tick derives none."""
    manifest = fleet["manifest"]
    moment = [MOMENT]
    node = _station(fleet, moment)
    counter = _Derivations()
    with (
        follower_registration(PROJECT, SESSION, delivery="terminal"),
        counter.installed(),
    ):
        assert _advance(node, counter, moment, 0) == 1
        assert _advance(node, counter, moment, 1) == 0
        runs._write_json(
            runs.pointer_path(RUN), _pointer_record(manifest, phase="finalizing")
        )
        assert _advance(node, counter, moment, 1) == 1
        manifest.write_text(
            f"node: {RUN}\nstatus: complete\ncommits: []\ngate: passed\n",
            encoding="utf-8",
        )
        assert _advance(node, counter, moment, 1) == 1
        fleet["plan"].write_text(_plan_document("done"), encoding="utf-8")
        assert _advance(node, counter, moment, 1) == 1


def test_the_interval_expiring_derives_once_with_nothing_changed(
    fleet: dict[str, Any],
) -> None:
    """Past the interval an unmoved fleet derives once, and then holds again."""
    moment = [MOMENT]
    node = _station(fleet, moment)
    counter = _Derivations()
    with (
        follower_registration(PROJECT, SESSION, delivery="terminal"),
        counter.installed(),
    ):
        assert _advance(node, counter, moment, 0) == 1
        assert _advance(node, counter, moment, INTERVAL_SECONDS) == 1
        assert _advance(node, counter, moment, 1) == 0
