"""A session's pane stays while a delivered run still owes its review.

A follower settles when the work it carries has drained, and the review a
delivered run owes is composed by the reflex inside that same follower. A rule
that read every terminal run as drained therefore ended the pane over exactly
the work whose composer lived in it: the run delivered, the follower left, and
no review of the delivered head was ever composed.

These cases drive the follower's own generator, so the settle judgement is
measured where a pane makes it rather than on the predicate alone. The clock is
injected, so the grace is an exact number of fake seconds and a settle is
measured against the moment the population drained.

Both settle paths are covered. A pane reading a station takes the reading the
station wrote for each population; a pane with no reading asks the same
has-live-work rule of the pointers directly. The reading in the first arm is
written the way the station writes it -- through ``record_live_work``, from
``live_work_populations`` -- so the two paths are judged from one rule.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import crew
from reckon import crew_follow_commands as follow_commands
from reckon.crew import runs, station_store, station_subscriber

PROJECT = "review-pane-proj"
SESSION = "s-review-pane"
RUN = "r-delivered-one"

# The grace these cases drive is the module's own, so shortening the constant
# is the mutation a control applies rather than a case's own choice.
GRACE = follow_commands.FOLLOWER_SETTLE_GRACE_SECONDS
# The wait pass's step in real time only: the case's own clock never advances
# with it, so a window lasts exactly as long as the case says.
POLL = 0.005

# A path that is neither runtime source nor test source, so a manifest naming
# it resolves to the review tier ``none``.
EVIDENCE_PATH = "docs/evidence/fragments/demo/a-delivered-note.html"

# The full object id a delivery's manifest names, so the head resolves from the
# record without a worktree to read and without git resolving a citation.
DELIVERED_HEAD = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class _Clock:
    """A monotonic stand-in the case advances by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every crew directory at a temporary config home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.delenv(follow_commands.FOLLOWER_SETTLE_GRACE_ENV, raising=False)
    # The once-per-run warning memo is process state, so a case measures its own
    # reports rather than whichever run an earlier case had already named.
    monkeypatch.setattr(station_subscriber, "_WARNED_DUTY_READ_FAILURES", set())
    return config_home


def _manifest_path(home: Path, run_id: str) -> Path:
    return home / "manifests" / f"{run_id}.md"


def _write_manifest(
    home: Path, run_id: str, *, changed_paths=None, commits=DELIVERED_HEAD
) -> None:
    path = _manifest_path(home, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"node: {run_id}\nstatus: complete\n"
    if commits is not None:
        body += f"commits: [{commits}]\n"
    if changed_paths is not None:
        body += f"changed_paths: [{changed_paths}]\n"
    path.write_text(body, encoding="utf-8")


def _pointer_record(home: Path, run_id: str, *, phase: str) -> dict:
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    if not log.exists():
        log.write_text('{"type":"turn.started"}\n', encoding="utf-8")
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "role": "implement",
        "node": {"id": f"node-{run_id}", "plan": "plan-a", "time_budget": "20m"},
        "phase": phase,
        "created_at": runs._utc_now(),
        "manifest_path": str(_manifest_path(home, run_id)),
        "log_path": str(log),
        # A worktree the collector has already reclaimed: the run's own record
        # then carries its head, which is the reading a settled run leaves.
        "worktree": str(home / "reclaimed" / run_id),
        # The worker has ended, so a finished manifest is the delivery rather
        # than an outcome its still-running process could supersede.
        "process_alive": False,
    }


def _write_pointer(home: Path, run_id: str, *, phase: str) -> None:
    crew._write_json(
        crew.pointer_path(run_id), _pointer_record(home, run_id, phase=phase)
    )


def _read_pointer(run_id: str) -> dict:
    return json.loads(crew.pointer_path(run_id).read_text(encoding="utf-8"))


def _deliver(
    home: Path, run_id: str, *, changed_paths=None, commits=DELIVERED_HEAD
) -> None:
    """Deliver a run in place: its manifest reports completion, it is ended."""
    _write_manifest(home, run_id, changed_paths=changed_paths, commits=commits)
    _write_pointer(home, run_id, phase="complete")


def _store_review(home: Path) -> None:
    """Store a complete independent review of the delivered run."""
    from reckon.crew import review

    review.store_review(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN,
            "status": "parsed",
            "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 18),
            "absent": [],
        }
    )


def _promote(home: Path) -> None:
    """Promote the delivered run: its live pointer goes as the ledger row lands."""
    crew.pointer_path(RUN).unlink(missing_ok=True)


def _station(tmp_path: Path) -> station_store.StationStore:
    store = station_store.StationStore(tmp_path / "runtime-store")
    store.heartbeat()
    return store


def _publish(store: station_store.StationStore, pointers) -> None:
    """Write the station's reading as its tick writes it."""
    project_live, unowned, sessions = station_subscriber.live_work_populations(pointers)
    station_subscriber.record_live_work(
        store,
        PROJECT,
        at=datetime.now(UTC),
        project_live=project_live,
        unowned=unowned,
        sessions=sessions,
    )


def _yield_briefly(_seconds: float) -> None:
    time.sleep(POLL)


def _wait_until(predicate, *, timeout: float, description: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail(f"timed out after {timeout:g}s waiting for {description}")


def _wait_for_settle(arming: _Arming, *, description: str) -> None:
    """Wait for a settle while keeping the pane's clock moving.

    The grace is measured on the pane's clock, so a wait that only sleeps in
    real time can never see it elapse: the pane takes its reading, finds the
    population empty, and needs its clock to move past the grace before it
    ends. Advancing one grace per real-time poll lets the sample that starts
    the emptiness and the sample that ends it both land.
    """
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if arming.settles():
            return
        arming.clock.advance(GRACE)
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {description}")


class _Arming:
    """One follower generator consumed on a thread, with its own clock."""

    def __init__(self, *, clock: _Clock, subscriber=None):
        self.clock = clock
        self.subscriber = subscriber
        self.events: list[dict] = []
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> _Arming:
        generator = follow_commands._follow_watch_lines(
            PROJECT,
            session=SESSION,
            poll_interval=POLL,
            sleeper=_yield_briefly,
            stop=self.stop,
            sweep=None,
            clock=self.clock,
            settle=True,
            subscriber=self.subscriber,
        )

        def run() -> None:
            for event in generator:
                self.events.append(event)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return self

    def end(self) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(5.0)

    def settles(self) -> list[dict]:
        return [
            e
            for e in self.events
            if e.get("event") == follow_commands.FOLLOWER_SETTLE_EVENT
        ]


def _let_a_sample_land(arming: _Arming) -> None:
    """Let the arming take its first, live reading before anything drains.

    The judgement is sampled on the first wait pass and then on a cadence, so a
    live sighting needs the population still live when the arming starts. The
    sleep is real time only; the case's own clock moves only where it says.
    """
    time.sleep(0.3)
    arming.clock.advance(5.0)
    time.sleep(0.2)


def _advance_past_grace(arming: _Arming, rounds: int = 3) -> None:
    for _ in range(rounds):
        arming.clock.advance(GRACE)
        time.sleep(0.1)


def test_a_station_reading_of_an_owed_review_holds_the_pane(
    home: Path, tmp_path: Path
) -> None:
    """A delivered run owing a review keeps the pane through its station reading.

    The run is drawn live first, so the settle is measured from a real drain.
    Its worker then delivers with no review stored, and the station publishes
    the reading it derives from the same rule the pane settles by. The pane
    must stay: the review it owes is composed by the reflex inside it.
    """
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        _write_pointer(home, RUN, phase="working")
        store = _station(tmp_path)
        clock = _Clock()
        subscriber = station_subscriber.StationSubscriber(
            store, PROJECT, session=SESSION, clock=clock
        )
        _publish(store, [_read_pointer(RUN)])
        arming = _Arming(clock=clock, subscriber=subscriber).start()
        try:
            _let_a_sample_land(arming)
            _deliver(home, RUN)
            _publish(store, [_read_pointer(RUN)])
            _advance_past_grace(arming)
            assert not arming.settles(), (
                "the pane settled over a delivered run whose review it owes"
            )
        finally:
            arming.end()


def test_the_fallback_listing_of_an_owed_review_holds_the_pane(
    home: Path, tmp_path: Path
) -> None:
    """A pane with no station reading asks the same rule of the pointers.

    The station has never written a reading for this project, so the pane falls
    back to listing the live pointers. The delivered run owes its review there
    too, so the fallback cannot disagree with the station about which run holds
    the pane.
    """
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        _write_pointer(home, RUN, phase="working")
        store = _station(tmp_path)
        clock = _Clock()
        subscriber = station_subscriber.StationSubscriber(
            store, PROJECT, session=SESSION, clock=clock
        )
        arming = _Arming(clock=clock, subscriber=subscriber).start()
        try:
            _let_a_sample_land(arming)
            _deliver(home, RUN)
            _advance_past_grace(arming)
            assert not arming.settles(), (
                "the fallback listing settled over a delivered run owing a review"
            )
        finally:
            arming.end()


def test_the_station_arm_settles_once_the_review_is_promoted(
    home: Path, tmp_path: Path
) -> None:
    """The hold ends when the reviewed run is promoted: nothing is owed then."""
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        _write_pointer(home, RUN, phase="working")
        store = _station(tmp_path)
        clock = _Clock()
        subscriber = station_subscriber.StationSubscriber(
            store, PROJECT, session=SESSION, clock=clock
        )
        _publish(store, [_read_pointer(RUN)])
        arming = _Arming(clock=clock, subscriber=subscriber).start()
        try:
            _let_a_sample_land(arming)
            _deliver(home, RUN)
            _publish(store, [_read_pointer(RUN)])
            arming.clock.advance(GRACE)
            time.sleep(0.2)
            assert not arming.settles(), "the pane settled over the review it owed"

            _store_review(home)
            _publish(store, [_read_pointer(RUN)])
            _advance_past_grace(arming)
            assert not arming.settles(), (
                "the pane settled over the promotion the stored review owes"
            )

            _promote(home)
            _publish(store, [])
            _advance_past_grace(arming)
            _wait_for_settle(
                arming, description="the pane to settle once the review was promoted"
            )
        finally:
            arming.end()


def test_the_fallback_arm_settles_once_the_review_is_promoted(
    home: Path, tmp_path: Path
) -> None:
    """Both settle paths end the hold once the reviewed run is promoted."""
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        _write_pointer(home, RUN, phase="working")
        store = _station(tmp_path)
        clock = _Clock()
        subscriber = station_subscriber.StationSubscriber(
            store, PROJECT, session=SESSION, clock=clock
        )
        arming = _Arming(clock=clock, subscriber=subscriber).start()
        try:
            _let_a_sample_land(arming)
            _deliver(home, RUN)
            _advance_past_grace(arming)
            assert not arming.settles(), "the pane settled over the review it owed"

            _store_review(home)
            _advance_past_grace(arming)
            assert not arming.settles(), (
                "the pane settled over the promotion the stored review owes"
            )

            _promote(home)
            _advance_past_grace(arming)
            _wait_for_settle(
                arming, description="the pane to settle once the review was promoted"
            )
        finally:
            arming.end()


def test_a_stored_review_that_is_not_promoted_keeps_the_pane(
    home: Path, tmp_path: Path
) -> None:
    """A promotable run keeps the pane: the coordinator still owes a promotion.

    The review being stored is what ends the review duty, not what ends the
    session's work. Until the run is promoted the coordinator has an action
    outstanding on it, and the follower is the only reader that shows it.
    """
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        _write_pointer(home, RUN, phase="working")
        store = _station(tmp_path)
        clock = _Clock()
        subscriber = station_subscriber.StationSubscriber(
            store, PROJECT, session=SESSION, clock=clock
        )
        _publish(store, [_read_pointer(RUN)])
        arming = _Arming(clock=clock, subscriber=subscriber).start()
        try:
            _let_a_sample_land(arming)
            _deliver(home, RUN)
            _store_review(home)
            _publish(store, [_read_pointer(RUN)])
            _advance_past_grace(arming)
            time.sleep(0.2)
            assert not arming.settles(), (
                "the pane settled over a promotion the coordinator still owes"
            )
        finally:
            arming.end()


def test_a_delivery_that_owes_no_review_settles(home: Path, tmp_path: Path) -> None:
    """An evidence-only delivery owes no review, so the pane drains.

    The tier rule exempts a run that changes no runtime or test source; the
    merged-head gate checks it instead. Counting every delivered run would hold
    the pane open for a review nothing will ever dispatch.
    """
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        _write_pointer(home, RUN, phase="working")
        store = _station(tmp_path)
        clock = _Clock()
        subscriber = station_subscriber.StationSubscriber(
            store, PROJECT, session=SESSION, clock=clock
        )
        _publish(store, [_read_pointer(RUN)])
        arming = _Arming(clock=clock, subscriber=subscriber).start()
        try:
            _let_a_sample_land(arming)
            _deliver(home, RUN, changed_paths=EVIDENCE_PATH)
            _publish(store, [_read_pointer(RUN)])
            _advance_past_grace(arming)
            _wait_for_settle(
                arming,
                description="the pane to settle over an evidence-only delivery",
            )
        finally:
            arming.end()


def test_a_delivery_with_no_resolvable_head_settles(home: Path, tmp_path: Path) -> None:
    """A delivery naming no resolvable head settles, however owed it reads.

    The review is keyed on the head the run delivered, and the head reader
    answers none for a run whose record names no revision, so a review of this
    run can never be composed and a hold for it would never end. The
    obligations still list the run as review-missing to the coordinator, which
    is where a run with nothing reviewable belongs.
    """
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        _write_pointer(home, RUN, phase="working")
        store = _station(tmp_path)
        clock = _Clock()
        subscriber = station_subscriber.StationSubscriber(
            store, PROJECT, session=SESSION, clock=clock
        )
        _publish(store, [_read_pointer(RUN)])
        arming = _Arming(clock=clock, subscriber=subscriber).start()
        try:
            _let_a_sample_land(arming)
            _deliver(home, RUN, commits=None)
            _publish(store, [_read_pointer(RUN)])
            _advance_past_grace(arming)
            _wait_for_settle(
                arming,
                description="the pane to settle over a delivery with no head",
            )
        finally:
            arming.end()


def test_a_duty_that_cannot_be_read_warns_and_settles(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """A duty read that raises is named in a warning and the pane settles.

    Settling silently would make a corrupt record look like a drained session,
    and holding would keep a pane alive for ever over a record nothing can
    read. The warning names the run, so the defect is visible from the log.
    """
    from reckon.crew import obligations

    def _explode(*_args, **_kwargs):
        raise ValueError("manifest unreadable")

    monkeypatch.setattr(obligations, "_review_duty_item", _explode)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        _write_pointer(home, RUN, phase="working")
        store = _station(tmp_path)
        clock = _Clock()
        subscriber = station_subscriber.StationSubscriber(
            store, PROJECT, session=SESSION, clock=clock
        )
        _publish(store, [_read_pointer(RUN)])
        arming = _Arming(clock=clock, subscriber=subscriber).start()
        try:
            _let_a_sample_land(arming)
            _deliver(home, RUN)
            _publish(store, [_read_pointer(RUN)])
            _advance_past_grace(arming)
            _wait_for_settle(
                arming,
                description="the pane to settle over a duty that could not be read",
            )
        finally:
            arming.end()
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(RUN in line and "manifest unreadable" in line for line in warnings), (
        warnings
    )


def test_a_duty_that_cannot_be_read_warns_once_per_run(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """An unreadable record is reported once per run, not once per tick.

    The station asks this rule of every live pointer on every tick, so a
    warning that is not memoised prints about once a second for as long as the
    record stays unreadable, and the repetition buries the first report. Each
    run is named once, and a second run is still named for itself.
    """
    from reckon.crew import obligations

    def _explode(*_args, **_kwargs):
        raise ValueError("manifest unreadable")

    monkeypatch.setattr(obligations, "_review_duty_item", _explode)
    other_run = "r-delivered-two"
    _deliver(home, RUN)
    _deliver(home, other_run)
    with caplog.at_level("WARNING"):
        for _ in range(3):
            assert not station_subscriber.live_work_populations([_read_pointer(RUN)])[0]
            assert not station_subscriber.live_work_populations(
                [_read_pointer(other_run)]
            )[0]
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len([line for line in warnings if RUN in line]) == 1, warnings
    assert len([line for line in warnings if other_run in line]) == 1, warnings


def test_a_delivered_pointer_costs_one_head_resolution(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One pass resolves a delivered run's head once, not once per reader.

    The duty resolves the head for the review it looks for and carries it on
    the item, so the hold reads that same head rather than resolving it again.
    Two resolutions per pointer would double the manifest and git reads the
    station makes for every delivered run it shows.
    """
    from reckon.crew import recovery

    resolved: list[dict] = []
    resolve = recovery._review_head_and_tree

    def _counting(pointer):
        resolved.append(dict(pointer))
        return resolve(pointer)

    monkeypatch.setattr(recovery, "_review_head_and_tree", _counting)
    _deliver(home, RUN)
    project_live, _unowned, sessions = station_subscriber.live_work_populations(
        [_read_pointer(RUN)]
    )
    assert project_live
    assert sessions == [SESSION]
    assert len(resolved) == 1, resolved


def test_a_drained_session_settles(home: Path, tmp_path: Path) -> None:
    """A session with no live or owed work settles, as it always did."""
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        _write_pointer(home, RUN, phase="working")
        store = _station(tmp_path)
        clock = _Clock()
        subscriber = station_subscriber.StationSubscriber(
            store, PROJECT, session=SESSION, clock=clock
        )
        _publish(store, [_read_pointer(RUN)])
        arming = _Arming(clock=clock, subscriber=subscriber).start()
        try:
            _let_a_sample_land(arming)
            crew.pointer_path(RUN).unlink()
            _publish(store, [])
            _advance_past_grace(arming)
            _wait_for_settle(
                arming, description="the pane to settle once the session drained"
            )
        finally:
            arming.end()
