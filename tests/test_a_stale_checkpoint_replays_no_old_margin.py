"""A re-arm on a checkpoint older than the pane's window replays no old margin.

A follower reads its durable checkpoint to learn where its pane last stood, and
delivers the rows written since that place so a detach does not skip what
happened meanwhile. That margin had no cap: a checkpoint left by a session idle
for days made the re-arm draw every matching row written since, each under its
original time — a burst of days-old transitions the pane never had on screen,
which the Monitor then suppressed for exceeding its output rate. The pane's
history replay beside it is capped at the history window, so a margin older than
that window is not a margin the pane would have shown at all.

A checkpoint older than the window is therefore treated as no place: the re-arm
resumes as a first attachment does, reporting each run's current state from live
run state through the delivered-state record a fresh arm already reads, and a
run that has not moved is not announced again. A margin inside the window is
delivered as before.

These cases drive the follower's own generator against a synthetic config home
and a real producer seat, so a row stamped with the attach time cannot pass by
matching a time the test itself chose.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import cli, crew, crew_follow_commands
from reckon.crew import follow_checkpoint, runs

PROJECT = "stale-margin-proj"
SESSION = "s1"
RUN_A = "r-stale-a"
RUN_B = "r-stale-b"

# Long enough that an arming reaches its first read and records its place, and
# short enough that a file of them stays quick.
ARM_LIFETIME = 0.75

_FLEET_EVENTS = frozenset({"baseline", "transition", "manifest-rewritten"})

_DAY = 24 * 60 * 60.0


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep pointers, manifests, streams and checkpoints in temporary state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture(autouse=True)
def _a_live_owner(monkeypatch):
    """Isolate every arming from an owner stamped into the ambient environment."""
    previous = runs._RESOLVED_FOLLOWER_OWNER.resolved
    monkeypatch.delenv(runs._FOLLOWER_OWNER_ENV, raising=False)
    runs._RESOLVED_FOLLOWER_OWNER.resolved = None
    yield
    runs._RESOLVED_FOLLOWER_OWNER.resolved = previous


@pytest.fixture(autouse=True)
def _a_moved_source_cannot_reload_the_arming(monkeypatch):
    """Freeze the code stamp so an ambient source change cannot re-execute.

    The arming runs in the test's own process, and a follower that saw the code
    stamp move would re-execute the process hosting the suite; the stamp is held
    still for every case. What the cases measure is a re-arm's replay, which no
    reload takes part in.
    """
    held = runs.follower_code_stamp()
    monkeypatch.setattr(runs, "follower_code_stamp", lambda: held)


def _write_pointer(home: Path, run_id: str, node: str, *, phase: str) -> None:
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n')
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "node": {"id": node, "plan": "plan-a", "time_budget": "20m"},
            "phase": phase,
            "created_at": runs._utc_now(),
            "manifest_path": str(home / "manifests" / f"{run_id}.md"),
            "log_path": str(log),
            "process_alive": None,
        },
    )


def _deliver(home: Path, run_id: str, status: str) -> None:
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: stale-node\nstatus: {status}\ncommits: HEAD\nblockers: none\n"
    )


def _two_live_runs(home: Path) -> None:
    """Two live runs whose rows an arming draws.

    Delivered into ``complete``, so each run's baseline is an action row the
    pane keeps — a run still in a progress state is observer context the pane
    withholds and would draw nothing.
    """
    _write_pointer(home, RUN_A, "node-a", phase="working")
    _write_pointer(home, RUN_B, "node-b", phase="working")
    _deliver(home, RUN_A, "complete")
    _deliver(home, RUN_B, "complete")


def _arm(*, lifetime: float = ARM_LIFETIME, **kwargs) -> list[dict]:
    """Run one arming to its own lifetime and collect the fleet rows it drew.

    Driven directly rather than through the command, because the measure is what
    a re-arm emits for its checkpoint; the command's rendering is exercised
    elsewhere. The arming ends by its own deadline, so nothing external stops it
    and the rows are exactly what it delivered.
    """
    generator = cli._follow_watch_lines(
        PROJECT,
        session=SESSION,
        poll_interval=0.001,
        sweep=None,
        lifetime=lifetime,
        **kwargs,
    )
    return [event for event in generator if event.get("event") in _FLEET_EVENTS]


def _iso(epoch: float) -> str:
    """An epoch as the stream writes one, so a chosen stamp round-trips."""
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _event(
    run_id: str,
    node: str,
    *,
    state: str,
    observed_at: str,
    event: str = "transition",
    previous: str | None = None,
) -> dict:
    return {
        "project": PROJECT,
        "event": event,
        "run_id": run_id,
        "node": node,
        "session": SESSION,
        "from_state": previous,
        "to_state": state,
        "working": 1,
        "blocked": 0,
        "unpromoted": 0,
        "observed_at": observed_at,
        "legacy": False,
    }


def _append_stream(stream_path: Path, events: list[dict]) -> None:
    """Add transitions to the stream as a producer would, at the given stamps."""
    with stream_path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(f"{json.dumps(event)}\n")


def _ensure_stream(stream_path: Path) -> None:
    stream_path.parent.mkdir(parents=True, exist_ok=True)
    if not stream_path.exists():
        stream_path.write_text("", encoding="utf-8")


def _live_states(home: Path, stream_path: Path) -> dict[str, str]:
    """The live classification of both runs, discovered by a throwaway arming.

    The probe arming writes a fresh checkpoint that the case then overwrites
    with a stale one, so the discovery does not disturb the measured re-arm.
    """
    drawn = _arm(resume=None)
    states = {str(event["run_id"]): str(event["to_state"]) for event in drawn}
    assert set(states) == {RUN_A, RUN_B}, (
        f"both live runs must draw a baseline row; got {drawn!r}"
    )
    return states


def _stale_margin(
    stream_path: Path,
    events: list[dict],
    *,
    age_seconds: float,
    reported: dict[str, str] | None = None,
) -> None:
    """Append rows past the pane's place, then leave a checkpoint ``age`` old.

    The recorded offset is the stream's size before the rows are appended, so
    the rows are exactly the margin written after the place, and the checkpoint
    says that place was written ``age_seconds`` ago.
    """
    offset = stream_path.stat().st_size
    _append_stream(stream_path, events)
    follow_checkpoint.write(
        PROJECT,
        SESSION,
        stream_path=stream_path,
        offset=offset,
        reported=reported or {},
        written_at=time.time() - age_seconds,
    )


# ── (a) A stale checkpoint draws none of its margin rows ────────────────────


def test_a_stale_checkpoint_draws_none_of_its_margin_rows(home) -> None:
    """A five-day-old place draws none of the rows written since it.

    Rows were appended after the recorded offset while the pane was away, each
    observed five days ago. The checkpoint is older than the pane's history
    window, so those rows are not a margin the pane would have shown: the re-arm
    resumes in baseline mode and draws none of them. Both runs still sit at the
    state the delivered record names, so neither is announced again either — the
    stale margin leaves the pane with no rows at all.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        _ensure_stream(stream_path)
        crew.list_live(project=PROJECT)
        live = _live_states(home, stream_path)

        # The pane last showed both runs at their live state; nothing moved.
        runs.write_delivered(PROJECT, SESSION, live)

        old = _iso(time.time() - 5 * _DAY)
        _stale_margin(
            stream_path,
            [
                _event(
                    RUN_A,
                    "node-a",
                    state="blocked",
                    observed_at=old,
                    previous=live[RUN_A],
                ),
                _event(
                    RUN_B,
                    "node-b",
                    state="failed",
                    observed_at=old,
                    previous=live[RUN_B],
                ),
            ],
            age_seconds=5 * _DAY,
            reported=live,
        )

        drawn = _arm(resume=None)

    assert drawn == [], (
        f"a stale checkpoint draws none of its margin rows and no unmoved run; "
        f"got {drawn!r}"
    )


# ── (b) A checkpoint inside the window delivers its margin once ─────────────


def test_a_fresh_checkpoint_draws_each_margin_row_once(home) -> None:
    """A ten-minute-old place draws each appended row exactly once.

    The checkpoint is well inside the pane's history window, so the rows written
    since it are news the pane missed. The re-arm continues from the recorded
    offset and delivers each of them once — the behaviour a re-arm minutes after
    a detach is meant to have.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        _ensure_stream(stream_path)
        crew.list_live(project=PROJECT)
        live = _live_states(home, stream_path)

        recent = _iso(time.time() - 10 * 60)
        _stale_margin(
            stream_path,
            [
                _event(
                    RUN_A,
                    "node-a",
                    state="blocked",
                    observed_at=recent,
                    previous=live[RUN_A],
                ),
                _event(
                    RUN_B,
                    "node-b",
                    state="failed",
                    observed_at=recent,
                    previous=live[RUN_B],
                ),
            ],
            age_seconds=10 * 60,
        )

        drawn = _arm(resume=None)

    moved = [(str(e["run_id"]), str(e["observed_at"])) for e in drawn]
    assert sorted(moved) == sorted([(RUN_A, recent), (RUN_B, recent)]), (
        f"a fresh checkpoint draws each margin row exactly once; got {drawn!r}"
    )


# ── (c) A run that changed across a stale margin draws its current state ────


def test_a_run_that_changed_across_a_stale_margin_draws_one_row(home) -> None:
    """A stale margin redraws only the run whose state moved, once, at current.

    The delivered record says RUN_A was last shown at ``working`` while the live
    fleet now classifies it otherwise; RUN_B is unchanged. The stale checkpoint
    sends the re-arm through the delivered-state record, so exactly one row is
    drawn — RUN_A's, for its current state — and RUN_B, still where it was, is
    not announced again.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        _ensure_stream(stream_path)
        crew.list_live(project=PROJECT)
        live = _live_states(home, stream_path)

        # RUN_A moved from a state the pane last showed; RUN_B did not.
        runs.write_delivered(PROJECT, SESSION, {RUN_A: "working", RUN_B: live[RUN_B]})

        old = _iso(time.time() - 5 * _DAY)
        _stale_margin(
            stream_path,
            [
                _event(
                    RUN_A,
                    "node-a",
                    state="blocked",
                    observed_at=old,
                    previous="working",
                )
            ],
            age_seconds=5 * _DAY,
        )

        drawn = _arm(resume=None)

    assert [str(event["run_id"]) for event in drawn] == [RUN_A], (
        f"only the changed run is drawn across a stale margin; got {drawn!r}"
    )
    assert str(drawn[0]["to_state"]) == live[RUN_A], (
        f"the row carries the run's current live state; got {drawn[0]!r}"
    )


# ── (d) The history window decides whether a margin is replayed ─────────────


def _pin_history_window(monkeypatch, window: str) -> None:
    """Resolve this project's pane history window to ``window``.

    ``_follow_history_caps`` reads ``ticker.history_window`` from the project's
    flight config, so pinning the resolver pins the window the staleness check
    and the history replay both read.
    """

    class Resolved:
        def __init__(self) -> None:
            self.config = {"ticker": {"history_window": window}}

    monkeypatch.setattr("reckon.flight.resolve", lambda *a, **k: Resolved())


@pytest.mark.parametrize(
    ("window", "age_seconds", "expected"),
    [
        ("1h", 2 * 60 * 60.0, "baseline"),
        ("1d", 2 * 60 * 60.0, "margin"),
    ],
)
def test_the_history_window_decides_whether_a_margin_is_replayed(
    home, monkeypatch, window, age_seconds, expected
) -> None:
    """A two-hour-old place is stale under an hour window and fresh under a day.

    The same checkpoint and the same margin are measured against two windows.
    Under an hour the two-hour-old place is stale, so the margin is not replayed
    and no row is drawn; under a day the place is inside the window, so the
    margin is delivered once. The window comes from the project's
    ``ticker.history_window``, the same reader the history replay uses.
    """
    _pin_history_window(monkeypatch, window)
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        _ensure_stream(stream_path)
        crew.list_live(project=PROJECT)
        live = _live_states(home, stream_path)
        runs.write_delivered(PROJECT, SESSION, live)

        recent = _iso(time.time() - age_seconds)
        _stale_margin(
            stream_path,
            [
                _event(
                    RUN_A,
                    "node-a",
                    state="blocked",
                    observed_at=recent,
                    previous=live[RUN_A],
                )
            ],
            age_seconds=age_seconds,
            reported=live,
        )

        drawn = _arm(resume=None)

    if expected == "baseline":
        assert drawn == [], (
            f"a checkpoint older than the window replays no margin; got {drawn!r}"
        )
    else:
        a_rows = [event for event in drawn if str(event["run_id"]) == RUN_A]
        assert len(a_rows) == 1, (
            f"a checkpoint inside the window delivers its margin row once; got "
            f"{drawn!r}"
        )
        assert str(a_rows[0]["observed_at"]) == recent, a_rows[0]


# ── The mechanism: the staleness check and the delivered record ─────────────


def test_the_stale_check_reads_the_caps_and_the_delivered_record(
    home, monkeypatch
) -> None:
    """The staleness decision is the caps reader's, and suppression the record's.

    The window the re-arm compares against is resolved through
    ``_follow_history_caps``, so the margin and the history replay cannot
    disagree about where the window falls, and a stale place is suppressed
    through ``runs.read_delivered`` — the record a fresh arm already reads —
    rather than the checkpoint's own ``reported`` map, which only the in-place
    reload path consumes. Both are load-bearing, so both are called on a stale
    re-arm; the stubbed record naming both runs at their live state leaves the
    pane with no row, proving the suppression came through it.
    """
    _two_live_runs(home)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        _ensure_stream(stream_path)
        crew.list_live(project=PROJECT)
        live = _live_states(home, stream_path)

        calls = {"caps": 0, "delivered": 0}
        real_caps = crew_follow_commands._follow_history_caps
        real_delivered = runs.read_delivered

        def spy_caps(project):
            calls["caps"] += 1
            return real_caps(project)

        def spy_delivered(project, session):
            calls["delivered"] += 1
            return real_delivered(project, session)

        monkeypatch.setattr(crew_follow_commands, "_follow_history_caps", spy_caps)
        monkeypatch.setattr(runs, "read_delivered", spy_delivered)

        runs.write_delivered(PROJECT, SESSION, live)
        _stale_margin(
            stream_path,
            [
                _event(
                    RUN_A,
                    "node-a",
                    state="blocked",
                    observed_at=_iso(time.time() - 5 * _DAY),
                    previous=live[RUN_A],
                )
            ],
            age_seconds=5 * _DAY,
            reported=live,
        )

        drawn = _arm(resume=None)

    assert calls["caps"] >= 1, "the staleness check must read the pane history caps"
    assert calls["delivered"] >= 1, (
        "a stale re-arm suppresses through the delivered-state record"
    )
    assert drawn == [], f"the suppressed margin draws no row; got {drawn!r}"


# ── A record written before the stamp is aged by its file's mtime ───────────


def test_a_record_without_a_stamp_is_aged_by_its_file_mtime(home) -> None:
    """A checkpoint predating the stamp is aged by the file's modification time.

    The stamp was added to the record, so a checkpoint written before it carries
    none; its age must still be established rather than read as fresh, and its
    own file's modification time is what stands in.
    """
    from reckon.crew import follow_checkpoint as fc

    path = fc.checkpoint_path(PROJECT, SESSION)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamped = {
        "version": fc.CHECKPOINT_VERSION,
        "project": PROJECT,
        "session": SESSION,
        "reported": {},
    }
    path.write_text(json.dumps(stamped), encoding="utf-8")
    old = time.time() - 5 * _DAY
    os.utime(path, (old, old))

    moment = fc.written_moment(PROJECT, SESSION, stamped)
    assert moment is not None, "an unstamped record must still be aged"
    assert abs(moment - old) < 5.0, (
        f"the record is aged by its file's mtime; got {moment} against {old}"
    )
