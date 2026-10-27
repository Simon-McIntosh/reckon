"""A live crew follower adopts code changes without losing its stream."""

from __future__ import annotations

import io
import json
import os
import queue
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import cli as cli_module
from reckon.crew import runs


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _event(number: int) -> dict:
    return {
        "project": "proj",
        "event": "transition",
        "observed_at": "2026-09-04T04:00:00+00:00",
        # A lift grant keyed by its group is the one row the pane prints the
        # moment it arrives: run transitions are held for an arrival or settle
        # window, and a plain move into working is never printed at all. The
        # reload is measured on the reader-to-pane path every row shares, so a
        # row that reaches the pane at once is what can show a gap.
        "run_id": "",
        "node": f"node-{number}",
        "session": "session-a",
        "role": "implement",
        "backend": "local",
        "model": "model",
        "alias": "model",
        "effort": "medium",
        "from_state": "",
        "to_state": "lift-granted",
        "working": 0,
        "blocked": 0,
        "unpromoted": 0,
        "detail": "",
        "needs_help_complete": False,
    }


def _wait_for_registration(project: str, session: str) -> dict:
    deadline = time.monotonic() + 8
    state = runs.follower_state(project, session)
    while time.monotonic() < deadline and not state["registered"]:
        time.sleep(0.02)
        state = runs.follower_state(project, session)
    assert state["registered"], "the running follower never registered"
    return state


PROBE_NODE_PREFIX = "attach-probe-"


def _probe_event(number: int) -> dict:
    """A throwaway transition, named so no measured run can be mistaken for it."""
    event = _event(number)
    event["run_id"] = f"{PROBE_NODE_PREFIX}run-{number}"
    event["node"] = f"{PROBE_NODE_PREFIX}{number}"
    return event


def _is_probe_row(line: str) -> bool:
    """Whether a rendered row belongs to a throwaway attach probe."""
    return PROBE_NODE_PREFIX in line


# A follower that reloads onto new code mid-stream prints one line of its own to
# say why the rows below it changed format. That line belongs to the pane rather
# than to the fleet, so it is not one of the transitions under measurement here:
# it is skipped exactly as a probe row is, and the measured count below is the
# count of *run* rows either way.
FORMAT_MARKER = "ticker format updated"

# A follower attaching to a seat whose producer runs older code prints one line
# of its own naming the gap, so every row the pane shows is composed by code
# this follower's is not. That line is about the producer rather than about a
# run, so it is skipped on the same terms as the format marker: counting it
# would drop a genuine transition from the measure.
STALE_PRODUCER_MARKER = "runs older code"


def _is_pane_line(line: str) -> bool:
    """Whether a printed line belongs to the pane rather than to the fleet."""
    return _is_probe_row(line) or FORMAT_MARKER in line or STALE_PRODUCER_MARKER in line


def _drain_probe_rows(lines: queue.Queue) -> None:
    """Discard every probe row already queued, so the measure starts clean.

    A follower polls the stream faster than the read below times out, but a
    stalled attach can still render more than one probe before the first is
    read. Those rows prove the attach and are not transitions under
    measurement, so none of them is left for the measured reads to count.
    """
    while True:
        try:
            row = lines.get_nowait()
        except queue.Empty:
            return
        assert _is_pane_line(row), f"a measured row arrived before the measure: {row!r}"


def _await_attached(
    stream_path: Path, lines: queue.Queue, *, attempts: int = 20
) -> str:
    """Return the probe row that proves the follower is reading the stream.

    A follower fixes its read offset to the stream's length at the instant it
    attaches, so a record already written when it arrives sits behind that
    offset and is never rendered. The first record a caller appends can
    therefore be lost to the attach, which makes a plain append-then-read racy:
    the offset is taken while the caller is appending. Appending a throwaway
    probe until one is rendered proves the follower is reading before the
    transitions under measurement are seeded, so none of them is behind the
    offset. Only a probe row ends the wait, and the rows it may have queued
    alongside it are drained before the caller seeds anything.
    """
    for number in range(attempts):
        runs._append_watch_lines(stream_path, [_probe_event(number)])
        try:
            row = lines.get(timeout=1.0)
        except queue.Empty:
            continue
        if _is_probe_row(row):
            _drain_probe_rows(lines)
            return row
    raise AssertionError("the follower never rendered a transition")


def _measured_rows(
    lines: queue.Queue, count: int, *, timeout: float = 5.0
) -> list[str]:
    """Read ``count`` rows carrying a measured node, ignoring the pane's own lines.

    A probe row is evidence of the attach and not a transition under
    measurement, so it is skipped rather than counted: a row left over from
    proving the attach can never stand in for a measured one. The pane's own
    lines — the reload's format marker — are skipped for the same reason: they
    are about the follower rather than about a run, so counting one would drop a
    genuine transition from the measure.
    """
    rows: list[str] = []
    deadline = time.monotonic() + timeout
    while len(rows) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"only {len(rows)} of {count} measured rows arrived")
        row = lines.get(timeout=remaining)
        if _is_pane_line(row):
            continue
        rows.append(row)
    return rows


def _await_rows(rows: list[dict], count: int, *, timeout: float = 20.0) -> None:
    """Wait until ``count`` rows have reached the pane's stream."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(rows) < count:
        time.sleep(0.01)
    assert len(rows) >= count, f"only {len(rows)} of {count} rows arrived"


def test_running_follower_reloads_without_stream_or_registration_gap(
    isolated_home, tmp_path
) -> None:
    """A station replacement leaves the subscriber's stream whole.

    The follower no longer reloads onto new code: the station is the one
    process on a node that does, and the follower is a subscriber to the rows it
    commits. What a reader has to be shown, then, is that a station replaced
    mid-flight leaves the subscriber with every row written before the
    replacement and every row written after it, once each and in order, and its
    registration still set on its own row.
    """
    from reckon.crew import station as station_module
    from reckon.crew import station_store, station_subscriber
    from reckon.crew_follow_commands import _StationRegistration

    root = tmp_path / "runtime"
    store = station_store.StationStore(root)
    # Two connections, as the station process and the follower hold them.
    writer = station_store.StationStore(root)
    station_module.Station(writer, home=isolated_home, projects=dict)
    writer.heartbeat()

    subscriber = station_subscriber.StationSubscriber(
        store, "proj", session="session-a"
    )
    subscriber.register(pid=os.getpid(), host="node-a", delivery="stream")
    rows: list[dict] = []
    stop = threading.Event()

    def reader() -> None:
        rows.extend(
            cli_module._follow_watch_lines(
                "proj",
                session="session-a",
                poll_interval=0.001,
                stop=stop,
                sweep=None,
                subscriber=subscriber,
                registration=_StationRegistration(subscriber),
            )
        )

    def publish(station_store_connection, number: int) -> None:
        event = _event(number)
        station_store_connection.write_event(
            audience="session-a",
            at=datetime.now(tz=UTC),
            project="proj",
            run="",
            kind="transition",
            from_state=event.get("from_state"),
            to_state=event.get("to_state"),
            payload=event,
        )

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for number in range(1, 19):
            publish(writer, number)
        _await_rows(rows, 18)

        # The station is replaced: a fresh process over the same node-local
        # store. Its predecessor wrote its last row above and is closed, so
        # nothing writes on the old connection while the new one runs.
        writer.close()
        reloaded = station_store.StationStore(root)
        station_module.Station(reloaded, home=isolated_home, projects=dict)
        reloaded.heartbeat()
        for number in range(19, 24):
            publish(reloaded, number)
        _await_rows(rows, 23)
    finally:
        stop.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert [row.get("node") for row in rows] == [
        f"node-{number}" for number in range(1, 24)
    ], "the replacement left no gap and repeated no row"
    registration = store.read_cursor("proj", "session-a")["registration"]
    assert registration is not None, "the pane's registration survived the replacement"
    assert registration["pid"] == os.getpid()
    assert subscriber.cursor_day is not None


def test_two_probe_rows_queued_at_the_attach_leave_the_measure_unaffected(
    monkeypatch, tmp_path
) -> None:
    """A stalled attach leaves nothing behind for the first measured read.

    The follower polls faster than the read below times out, but a stall can
    still render two probe rows before the first is read, and the second was
    once handed to the measure as a measured row — which then failed the node
    set with a message about the transitions rather than about the probe. Both
    rows are drained at the attach, and any that survives is skipped by the
    measured reads.
    """
    lines: queue.Queue[str] = queue.Queue()
    for number in (7, 8):
        lines.put(f"{PROBE_NODE_PREFIX}{number} rendered")
    monkeypatch.setattr(runs, "_append_watch_lines", lambda *_args, **_kwargs: None)

    _await_attached(tmp_path / "stream.jsonl", lines)

    assert lines.empty(), "the attach drained every probe row it queued"
    lines.put("node-1 rendered")
    assert _measured_rows(lines, 1) == ["node-1 rendered"]


def test_failed_reexec_reports_once_and_keeps_the_registration(
    monkeypatch,
) -> None:
    stamps = iter(["imported", "changed"])
    monkeypatch.setattr(runs, "follower_code_stamp", lambda: next(stamps, "changed"))
    monkeypatch.setattr(
        cli_module.os,
        "execve",
        lambda *_args: (_ for _ in ()).throw(OSError("execution refused")),
    )
    output = io.StringIO()

    class Registration:
        prepared = False
        cancelled = False

        def prepare_reexec(self) -> None:
            self.prepared = True

        def cancel_reexec(self) -> None:
            self.cancelled = True

    registration = Registration()
    reloader = cli_module._FollowerReloader("proj", registration, stream=output)
    reloader.poll({"reported": {"run": "working"}, "offset": 12})
    reloader.poll({"reported": {"run": "working"}, "offset": 12})

    assert registration.prepared is True
    assert registration.cancelled is True
    assert output.getvalue().count("could not reload itself") == 1
    assert "cycle it with:" in output.getvalue()
    assert cli_module._FOLLOWER_CHECKPOINT_ENV not in os.environ


def _released_registration(project: str, session: str) -> Path:
    """Write a registration whose lock is free and whose process is gone.

    The file is what a released registration leaves behind: nothing unlinks it
    when the lock is released, so the directory grows one entry per session
    name that has ever followed the project.
    """
    path = runs.follower_lock_path(project, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project": project,
                "session": session,
                "pid": 999_999_999,
                "pid_start_time": "1",
                "delivery": "stream",
                "started_at": "2026-09-01T09:00:00Z",
            }
        )
    )
    return path


def test_payload_lists_delivering_followers_and_counts_released(isolated_home) -> None:
    """One delivering registration among twenty-seven is one row and a 26.

    The registry is append-only: releasing the lock never unlinks the file, so
    a reader was handed one row per session name that had ever followed the
    project. The row count answered "how many names" instead of that question.
    """
    for number in range(26):
        _released_registration("proj", f"released-{number}")

    with runs.follower_claim("proj", "delivering", delivery="stream"):
        payload = runs.project_watch_visibility("proj")

        sessions = [row["session"] for row in payload["followers"]]
        assert sessions == ["delivering"], "only delivering registrations are rows"
        assert payload["followers_released"] == 26
        assert payload["followers_live"] == 1
        assert payload["delivering_sessions"] == ["delivering"]
        assert "not_live_because" not in payload["followers"][0]

    # Reading the payload repairs nothing: a released registration stays on
    # disk, because unlinking it here would race a fresh inode against a stable
    # one and hand two processes the same session's lock.
    remaining = sorted(runs.follower_dir("proj").glob("*.lock"))
    assert len(remaining) == 27, "no registration file is unlinked on a read path"


def test_released_count_is_zero_rather_than_omitted(isolated_home) -> None:
    """A directory with nothing released reports a zero, not a missing key."""
    with runs.follower_claim("proj", "only", delivery="stream"):
        payload = runs.project_watch_visibility("proj")

    assert "followers_released" in payload, "the figure is stated, never omitted"
    assert payload["followers_released"] == 0
    assert payload["followers_live"] == 1


def test_delivering_registration_is_reported_whatever_its_age(isolated_home) -> None:
    """Delivery is decided by the held lock, not by the file's age.

    Every released row here is newer on disk than the delivering one, so a
    payload that ranked rows by mtime would drop the only registration that
    delivers. A follower armed in the morning and still delivering at night
    must survive rows half its age.
    """
    long_lived = 1_600_000_000  # 2020-09-13, older than any released row
    for number in range(3):
        _released_registration("proj", f"released-{number}")

    with runs.follower_claim("proj", "long-lived", delivery="stream"):
        os.utime(
            runs.follower_lock_path("proj", "long-lived"), (long_lived, long_lived)
        )
        payload = runs.project_watch_visibility("proj")

    assert [row["session"] for row in payload["followers"]] == ["long-lived"]
    assert payload["followers_released"] == 3


def test_sweep_removes_released_registrations_older_than_the_threshold(
    isolated_home,
) -> None:
    """A released registration past the threshold is removed and counted.

    Releasing a registration drops its lock and leaves its file, so the
    directory holds one entry per session name that has ever followed the
    project. The sweep is the only thing that removes one, and it reports what it
    removed rather than leaving the caller to re-list the directory.
    """
    released_at = 1_600_000_000.0
    for number in range(3):
        path = _released_registration("proj", f"departed-{number}")
        os.utime(path, (released_at, released_at))

    removed = runs.sweep_released_followers(
        "proj",
        stale_after_seconds=7 * 24 * 3600,
        now=released_at + 30 * 24 * 3600,
    )

    assert len(removed) == 3, "the removed entries come back from the sweep"
    assert {entry["session"] for entry in removed} == {
        "departed-0",
        "departed-1",
        "departed-2",
    }
    assert list(runs.follower_dir("proj").glob("*.lock")) == [], "the files are gone"


def test_sweep_keeps_a_delivering_registration_whatever_its_age(
    isolated_home,
) -> None:
    """The held lock decides, not the timestamp, so a live follower survives.

    Every released entry here would be swept at this reference time; the
    delivering one is older still, and the sweep must leave it, because a
    follower armed in the morning is still delivering at night.
    """
    armed_at = 1_600_000_000.0
    with runs.follower_claim("proj", "long-lived", delivery="stream"):
        os.utime(runs.follower_lock_path("proj", "long-lived"), (armed_at, armed_at))
        removed = runs.sweep_released_followers(
            "proj",
            stale_after_seconds=7 * 24 * 3600,
            now=armed_at + 30 * 24 * 3600,
        )
        state = runs.follower_state("proj", "long-lived")

    assert removed == [], "a delivering registration is not the sweep's to remove"
    assert state["live"] is True, "the swept directory still reports it delivering"
    assert runs.follower_lock_path("proj", "long-lived").exists()


def test_sweep_keeps_a_registration_released_moments_ago(isolated_home) -> None:
    """A session restarting is not swept out from under itself."""
    released_at = 2_000_000_000.0
    path = _released_registration("proj", "restarting")
    os.utime(path, (released_at, released_at))

    removed = runs.sweep_released_followers(
        "proj",
        stale_after_seconds=7 * 24 * 3600,
        now=released_at + 60,
    )

    assert removed == [], "a recent release is inside the restart window"
    assert path.exists(), "the file is still there for the session to reclaim"


def test_no_read_path_sweeps_a_registration(isolated_home, monkeypatch) -> None:
    """Reading the registry reports; it never removes."""
    for number in range(2):
        _released_registration("proj", f"departed-{number}")
    calls: list[tuple] = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise AssertionError("a read path removed a registration")

    monkeypatch.setattr(runs, "sweep_released_followers", refuse)

    with runs.follower_claim("proj", "delivering", delivery="stream"):
        payload = runs.project_watch_visibility("proj")

    assert calls == [], "no read path calls the sweep"
    assert payload["followers_released"] == 2
    assert len(list(runs.follower_dir("proj").glob("*.lock"))) == 3
