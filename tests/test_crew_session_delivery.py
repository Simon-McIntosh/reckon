"""Delivery is session-local, so it is registered, scoped, and verified.

A watcher seat proves a producer exists. It never proved that the session which
dispatched would hear anything, and every measured silence lived in that gap:
one project-wide stream, several sessions reading none of it, and a guard whose
predicate was satisfied by somebody else's producer.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from reckon import cli, crew
from reckon.crew import ticker as ticker_module
from reckon.crew import recovery, runs
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep registrations, pointers, and streams in temporary state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _write_pointer(home: Path, run_id: str, node: str, *, session: str, phase: str):
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n')
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "session": session,
            "node": {"id": node, "plan": "plan-a", "time_budget": "20m"},
            "phase": phase,
            "created_at": runs._utc_now(),
            "manifest_path": str(home / "manifests" / f"{run_id}.md"),
            "log_path": str(log),
        },
    )


def _set_phase(run_id: str, phase: str) -> None:
    pointer = crew.read_pointer(run_id)
    pointer["phase"] = phase
    crew._write_json(crew.pointer_path(run_id), pointer)


def _deliver(home: Path, run_id: str, status: str) -> None:
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: n\nstatus: {status}\ncommits: HEAD\nblockers: none\n")


# ── Registration ────────────────────────────────────────────────────────────


def test_a_registration_covers_one_session_and_not_its_peers(home) -> None:
    with runs.follower_claim("proj", "session-a", delivery="stream") as (ok, record):
        assert ok is True
        assert record["session"] == "session-a"
        assert runs.follower_state("proj", "session-a")["live"] is True
        assert runs.follower_state("proj", "session-b")["live"] is False
        assert runs.follower_state("other", "session-a")["live"] is False

    assert runs.follower_state("proj", "session-a")["live"] is False


def test_a_registration_whose_lines_land_in_a_file_is_not_delivery(home) -> None:
    """A background shell writes stdout to a file and notifies only on exit.

    The follower never exits, so those lines reach nobody. Measured on this
    host: a background shell's stdout is a regular file, a per-line monitor's
    is a socket or pipe — which is what makes the distinction checkable rather
    than advisory.
    """
    with runs.follower_claim("proj", "session-a", delivery="file") as (ok, _record):
        assert ok is True
        state = runs.follower_state("proj", "session-a")
        assert state["registered"] is True
        assert state["delivery"] == "file"
        assert state["live"] is False, "a file consumer wakes nobody"


def test_delivery_mode_reads_the_descriptor_rather_than_being_declared(
    tmp_path: Path,
) -> None:
    regular = tmp_path / "out.log"
    with regular.open("w") as handle:
        assert runs.delivery_mode(handle.fileno()) == "file"

    left, right = socket.socketpair()
    try:
        assert runs.delivery_mode(left.fileno()) == "stream"
    finally:
        left.close()
        right.close()

    # A pipe nobody else holds the read end of carries lines to nowhere, so it
    # is judged by where they stop rather than by the descriptor's type.
    read_end, write_end = os.pipe()
    try:
        assert runs.delivery_mode(write_end) == "file"
    finally:
        os.close(read_end)
        os.close(write_end)


def test_followers_are_listed_per_project_with_their_liveness(home) -> None:
    with (
        runs.follower_claim("proj", "session-a", delivery="stream"),
        runs.follower_claim("proj", "session-b", delivery="file"),
    ):
        listed = {row["session"]: row for row in runs.list_followers("proj")}
    assert listed["session-a"]["live"] is True
    assert listed["session-b"]["live"] is False


# ── Scoping ─────────────────────────────────────────────────────────────────


def _follow(project: str, *, settle: float = 0.15, **kwargs) -> list[dict]:
    """Collect what a follower delivers, then stop it as a session would.

    A follower does not end on its own — that is the property under test
    elsewhere in this file — so a bounded read has to stop it, exactly as the
    harness stops a monitor.
    """
    received: list[dict] = []
    stop = threading.Event()

    def reader() -> None:
        for event in cli._follow_watch_lines(
            project, poll_interval=0.001, stop=stop, **kwargs
        ):
            if event.get("event") in {"attached", "reattached"}:
                continue
            received.append(event)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(settle)
    stop.set()
    thread.join(timeout=2)
    assert not thread.is_alive(), "a stopped follower must return"
    return received


def _follow_all(project: str, *, settle: float = 0.15, **kwargs) -> list[dict]:
    """Collect everything a follower delivers, receipts included."""
    received: list[dict] = []
    stop = threading.Event()

    def reader() -> None:
        for event in cli._follow_watch_lines(
            project, poll_interval=0.001, stop=stop, **kwargs
        ):
            received.append(event)  # noqa: PERF402 — incremental by design

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(settle)
    stop.set()
    thread.join(timeout=2)
    assert not thread.is_alive(), "a stopped follower must return"
    return received


def test_a_scoped_follower_receives_only_the_runs_its_session_dispatched(
    home,
) -> None:
    _write_pointer(home, "r-mine", "my-node", session="mine", phase="starting")
    _write_pointer(home, "r-peer", "peer-node", session="peers", phase="starting")

    with runs._project_watch_claim("proj", "1h") as (acquired, _seat):
        assert acquired
        _deliver(home, "r-mine", "complete")
        _deliver(home, "r-peer", "blocked")
        crew.list_live(project="proj")

        events = _follow("proj", session="mine")

    nodes = {event["node"] for event in events}
    assert nodes == {"my-node"}, "a peer session's run must not reach this follower"


def test_an_unscoped_follower_names_the_session_that_owns_every_line(home) -> None:
    _write_pointer(home, "r-mine", "my-node", session="mine", phase="working")

    with runs._project_watch_claim("proj", "1h") as (_acquired, _seat):
        crew.list_live(project="proj")
        events = _follow("proj")

    assert events, "an unscoped follower still receives the fleet"
    rendered = recovery.format_watch_transition(events[0], with_session=True)
    assert "mine" in rendered, "ownership must be readable in the notification"
    assert "mine" not in recovery.format_watch_transition(events[0])


def test_the_attention_filter_is_inside_the_follower(home) -> None:
    """The filter moved in so the attach line needs no pipe and no `|| true`."""
    _write_pointer(home, "r-one", "one-node", session="mine", phase="working")
    attention: list[dict] = []
    every: list[dict] = []
    stop = threading.Event()

    def reader(sink: list[dict], **kwargs) -> None:
        for event in cli._follow_watch_lines(
            "proj", session="mine", poll_interval=0.001, stop=stop, **kwargs
        ):
            if event.get("event") in {"attached", "reattached"}:
                continue
            sink.append(event)

    threads = [
        threading.Thread(target=reader, args=(attention,), kwargs={"attention": True}),
        threading.Thread(target=reader, args=(every,), daemon=True),
    ]
    for thread in threads:
        thread.daemon = True
        thread.start()
    try:
        with runs._project_watch_claim("proj", "1h"):
            crew.list_live(project="proj")
            _wait_for(lambda: any(e["to_state"] == "working" for e in every))
            _deliver(home, "r-one", "complete")
            crew.list_live(project="proj")
            _wait_for(lambda: any(e["to_state"] == "complete" for e in attention))
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=2)

    assert [event["to_state"] for event in attention] == ["complete"]
    assert [event["to_state"] for event in every] == ["working", "complete"]


def test_the_attach_line_is_one_bare_command_a_monitor_can_arm() -> None:
    line = runs._watch_attach_line("nova", session="s18-hexgrid")
    assert "|" not in line, "a pipe buffers the ticker and hides its refusals"
    assert "grep" not in line
    assert "true" not in line, "`|| true` turns a refusal into a silent success"
    assert line.startswith("reckon crew follow ")
    assert "--session s18-hexgrid" in line
    # No state filter: a filter matching nothing yet is an empty pane, which
    # reads the same as a follower that never started, and a reader watching a
    # wave wants the starts and the working transitions too.
    assert "--attention" not in line


# ── Lifetime ────────────────────────────────────────────────────────────────


def test_a_follower_outlives_a_drained_fleet_and_reports_the_next_wave(home) -> None:
    """A wave that drains releases the seat; the session has not gone away.

    The follower used to end there, so the next wave's transitions arrived at a
    monitor that had already exited — one arming covered one wave.
    """
    _write_pointer(home, "r-first", "first-node", session="mine", phase="working")
    received: list[dict] = []
    failures: list[BaseException] = []
    stop = threading.Event()

    def reader() -> None:
        try:
            for event in cli._follow_watch_lines(
                "proj", session="mine", poll_interval=0.001, stop=stop
            ):
                if event.get("event") in {"attached", "reattached"}:
                    continue
                received.append(event)
        except BaseException as exc:  # noqa: BLE001 — re-raised in the main thread
            failures.append(exc)  # pragma: no cover - surfaced below

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        with runs._project_watch_claim("proj", "1h"):
            crew.list_live(project="proj")
            _wait_for(lambda: any(e["node"] == "first-node" for e in received))

        # The wave drains: the seat is released and every pointer is promoted.
        crew.pointer_path("r-first").unlink()
        _write_pointer(home, "r-second", "second-node", session="mine", phase="working")
        with runs._project_watch_claim("proj", "1h"):
            crew.list_live(project="proj")
            _wait_for(lambda: any(e["node"] == "second-node" for e in received))
    finally:
        stop.set()
        thread.join(timeout=2)

    assert failures == []
    assert not thread.is_alive()


def test_a_follower_waits_for_a_producer_instead_of_refusing(home) -> None:
    """Arming ears before dispatching is the natural order, so allow it.

    Refusing here forced every session to dispatch blind first, and left the
    gap between two waves unwatchable.
    """
    received: list[dict] = []
    stop = threading.Event()

    def reader() -> None:
        for event in cli._follow_watch_lines(
            "proj", session="mine", poll_interval=0.001, stop=stop
        ):
            if event.get("event") in {"attached", "reattached"}:
                continue
            received.append(event)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        time.sleep(0.05)
        assert received == [], "nothing is live yet, so nothing is reported"
        _write_pointer(home, "r-late", "late-node", session="mine", phase="working")
        with runs._project_watch_claim("proj", "1h"):
            crew.list_live(project="proj")
            _wait_for(lambda: any(e["node"] == "late-node" for e in received))
    finally:
        stop.set()
        thread.join(timeout=2)

    assert not thread.is_alive()


def test_a_re_attached_follower_repeats_no_state_it_already_reported(home) -> None:
    _write_pointer(home, "r-one", "one-node", session="mine", phase="working")
    received: list[dict] = []
    stop = threading.Event()

    def reader() -> None:
        for event in cli._follow_watch_lines(
            "proj", session="mine", poll_interval=0.001, stop=stop
        ):
            if event.get("event") in {"attached", "reattached"}:
                continue
            received.append(event)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        with runs._project_watch_claim("proj", "1h"):
            crew.list_live(project="proj")
            _wait_for(lambda: len(received) == 1)
        with runs._project_watch_claim("proj", "1h"):
            crew.list_live(project="proj")
            time.sleep(0.1)
            assert len(received) == 1, "an unchanged run is not news twice"
            _deliver(home, "r-one", "complete")
            crew.list_live(project="proj")
            _wait_for(lambda: len(received) == 2)
    finally:
        stop.set()
        thread.join(timeout=2)

    assert [event["to_state"] for event in received] == ["working", "complete"]


def _wait_for(predicate, *, timeout: float = WATCHER_LOAD_BOUND_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    pytest.fail("the follower did not receive the expected transition in time")


# ── The durable stream is data, and the ticker renders it ───────────────────


def test_the_stream_stores_lossless_events_so_ownership_survives(home) -> None:
    _write_pointer(home, "r-one", "one-node", session="mine", phase="working")

    with runs._project_watch_claim("proj", "1h") as (_acquired, seat):
        crew.list_live(project="proj")
        stream = Path(seat["stream_path"])
        record = json.loads(stream.read_text().splitlines()[0])

    assert record["run_id"] == "r-one"
    assert record["session"] == "mine"
    assert record["node"] == "one-node"
    assert recovery.format_watch_transition(record).startswith(
        ticker_module.local_clock(record["observed_at"])
    )


def test_a_legacy_rendered_line_still_reaches_a_reader(home) -> None:
    """Producers hold their code until restarted, so both forms must read."""
    stream = runs.watch_stream_path("proj")
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text("10:01:00  old-node  dispatched → working  1 live\n")

    events = list(runs.read_stream_events(stream, offset=0))
    assert len(events) == 1
    assert events[0]["legacy"] is True
    assert "old-node" in events[0]["rendered"]


def test_a_second_follower_takes_over_when_the_first_registration_goes(home) -> None:
    """Streaming and registering are separable, so the streamer must catch up.

    A second follower attaches read-only while the first holds the registration.
    If the first then dies, lines keep arriving at the second while dispatch
    refuses — every visible signal says attached. Retrying the claim while
    streaming is what closes that, so the follower still delivering ends up
    holding the registration.
    """
    first = runs._FollowerRegistration("proj", "mine", delivery="stream")
    assert first.acquire() is True

    second = runs._FollowerRegistration("proj", "mine", delivery="stream")
    assert second.acquire() is False, "the first holds it"
    assert second.blocked_by.get("pid") == os.getpid()

    first.release()
    assert second.acquire() is True, "a released registration is taken over"
    assert runs.follower_state("proj", "mine")["live"] is True
    second.release()


def test_an_attaching_follower_reports_its_fleet_as_transitions(home) -> None:
    """Attaching prints the fleet, in the ticker's own vocabulary and nothing else.

    Measured: a session armed the prescribed line, both of its runs sat in a
    progress state, and the pane read `No output available` for minutes — the
    silence this surface exists to remove. The fix is that a follower carries no
    state filter, so the baseline reports every live run as a transition. It is
    not a second stream of follower status: a reader wants worker transitions
    and the fleet posture, and nothing about the follower itself.
    """
    _write_pointer(home, "r-one", "one-node", session="mine", phase="starting")
    _write_pointer(home, "r-two", "two-node", session="mine", phase="working")

    with runs._project_watch_claim("proj", "1h"):
        crew.list_live(project="proj")
        events = _follow_all("proj", session="mine")

    assert [(e["node"], e["to_state"]) for e in events] == [
        ("one-node", "dispatched"),
        ("two-node", "working"),
    ]
    assert all(e.get("event") in {"baseline", "transition"} for e in events), (
        "the follower's own lifecycle is not fleet state and does not belong here"
    )
    rendered = [recovery.format_watch_transition(event) for event in events]
    assert all(" 2 working ·  0 blocked ·  0 unpromoted" in line for line in rendered)
    for line in rendered:
        assert "[stderr]" not in line


def _delivery_under(command: str, tmp_path: Path) -> str:
    """Report what `delivery_mode` says when run inside a real shell pipeline."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(Path(runs.__file__).parents[2])!r})\n"
        "from reckon.crew.runs import delivery_mode\n"
        "print(delivery_mode(), flush=True)\n"
    )
    result = tmp_path / "out.txt"
    subprocess.run(  # noqa: S602 — the shell pipeline is what is being measured
        command.format(probe=f"{sys.executable} {probe}", out=result),
        shell=True,
        check=True,
    )
    return result.read_text().strip()


def test_a_filter_between_the_follower_and_a_file_is_still_a_file(
    tmp_path: Path,
) -> None:
    """The measured shape: a session armed `follow | grep` as a background shell.

    The first hop is a pipe, which looks like a live consumer, so a check that
    stopped there registered the follower as delivering while every line ended
    in a file the harness reads only after the command exits — and a follower
    does not exit. The verdict has to belong to the end of the chain.
    """
    assert _delivery_under("{probe} > {out}", tmp_path) == "file"
    assert (
        _delivery_under(
            "{{ {probe} 2>&1 | grep --line-buffered -E 'file|stream|terminal' "
            "|| true; }} > {out}",
            tmp_path,
        )
        == "file"
    )
    assert (
        _delivery_under("{{ {probe} | grep -E . | cat; }} > {out}", tmp_path) == "file"
    )


def test_a_registration_is_judged_by_where_its_lines_land_now(home, tmp_path) -> None:
    """A recorded verdict is a snapshot; the guard reads the descriptor instead.

    Two ways a snapshot goes stale, and the second is why this is measured from
    outside: the consumer at the end of the chain can go away after
    registration, and a registration written before the check understood pipe
    chains claims a delivery it never had. So the follower here declares
    `stream` while its lines go to a file, and the reader must disagree with it.
    """
    holder = tmp_path / "holder.py"
    holder.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(Path(runs.__file__).parents[2])!r})\n"
        "from reckon.crew.runs import follower_claim\n"
        'with follower_claim("proj", "declared", delivery="stream"):\n'
        "    print('registered', flush=True)\n"
        "    time.sleep(30)\n"
    )
    log = tmp_path / "holder.out"
    with log.open("w") as sink:
        process = subprocess.Popen(
            [sys.executable, str(holder)],
            stdout=sink,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "RECKON_HOME": str(home)},
        )
    try:
        # Wait for the settled registration, not merely for the lock: a claim
        # takes the lock and then writes its record, and reading the instant
        # between the two is what this fixture used to race.
        deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
        while time.monotonic() < deadline:
            state = runs.follower_state("proj", "declared")
            if state["registered"] and state["follower"].get("pid"):
                break
            time.sleep(0.02)
        assert state["registered"] is True, "the follower never registered"
        assert state["delivery_recorded"] == "stream", "it declared a stream"
        assert state["delivery_observed"] == "file", "its lines end in a file"
        assert state["delivery"] == "file", "measurement outranks the declaration"
        assert state["live"] is False, "so it is not delivery"
    finally:
        process.terminate()
        process.wait(timeout=WATCHER_LOAD_BOUND_SECONDS)

    assert runs.follower_state("proj", "declared")["live"] is False


def test_the_live_read_can_answer_whether_this_session_is_attached(home) -> None:
    """The enforcement is in the CLI and the read is in MCP, so both need the
    session.

    A coordinator is told to read fleet state through the `crew` tool. Without a
    session argument that read can only report that a producer exists — the
    project-wide half — while the field that decides whether this caller hears
    anything is `session_attached`. The refusal then arrives from a surface the
    coordinator was told not to use for reads.
    """
    from reckon import mcp

    _write_pointer(home, "r-mine", "my-node", session="mine", phase="working")
    _write_pointer(home, "r-peer", "peer-node", session="peers", phase="working")

    unscoped = mcp._crew("proj", view="live")
    assert unscoped["watcher"]["session_attached"] is None
    assert "--session" not in unscoped["watcher"]["attach_line"]
    assert all("mine" not in row for row in unscoped["runs"])

    with runs.follower_claim("proj", "mine", delivery="stream"):
        scoped = mcp._crew("proj", view="live", session="mine")

    assert scoped["watcher"]["session_attached"] is True
    assert "--session mine" in scoped["watcher"]["attach_line"]
    assert {row["node"]: row["mine"] for row in scoped["runs"]} == {
        "my-node": True,
        "peer-node": False,
    }

    detached = mcp._crew("proj", view="live", session="mine")
    assert detached["watcher"]["session_attached"] is False


def test_the_refusal_names_where_the_arming_primitive_is_documented() -> None:
    """A correct line armed the wrong way is the case the refusal has to teach.

    The refusal is the surface a blocked coordinator is provably reading, so the
    harness-local instruction has to be reachable from there rather than only
    from a skill section the coordinator has already passed.
    """
    refusal = crew.WatcherRequired(
        "proj",
        {
            "arming_line": "reckon crew watch --project proj",
            "attach_line": runs._watch_attach_line("proj", session="mine"),
            "follower": {"delivery": "file"},
        },
        session="mine",
    )
    message = str(refusal)
    assert "references/orchestrator-harness/" in message
    assert "reports each line as it is written" in message
    assert "writes to a file" in message
    assert "--no-watch" in message


def test_a_dead_registration_is_taken_over_rather_than_hand_deleted(home) -> None:
    """A killed follower leaves its lock file; the next one must not be blocked.

    The advisory lock is released by the kernel when its holder dies, so the
    file that remains is a record and not a claim. Asserted rather than reasoned
    about, because a peer session read the leftover file as a block and worked
    around it with a dispatch waiver.
    """
    path = runs.follower_lock_path("proj", "mine")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project": "proj",
                "session": "mine",
                "pid": 999_999_999,
                "pid_start_time": "1",
                "delivery": "unknown",
                "started_at": runs._utc_now(),
            }
        )
    )
    assert runs.follower_state("proj", "mine")["live"] is False

    with runs.follower_claim("proj", "mine", delivery="stream") as (held, _record):
        assert held is True, "a dead holder's lock is free to take"
        assert runs.follower_state("proj", "mine")["live"] is True


def test_a_followers_owner_is_readable_without_reaching_for_ps(home) -> None:
    """Whose follower is this, answered from the documented read surface.

    A peer reported a live follower as an orphan to be reaped, having correctly
    established only that it was not its own; the process consuming its stdout
    belonged to another session that was mid-flight. A follower's owner is that
    consumer, so the read that lists followers names it.
    """
    _write_pointer(home, "r-one", "one-node", session="mine", phase="working")

    with runs.follower_claim("proj", "mine", delivery="stream"):
        rows = runs.project_watch_visibility("proj", session="mine")["followers"]

    assert len(rows) == 1
    row = rows[0]
    assert row["session"] == "mine"
    assert row["pid"] == os.getpid()
    assert row["consumer_pid"] == os.getppid(), "the process reading its stdout"
    assert row["since"], "and when it attached"


def test_a_registration_mid_write_is_not_read_as_unknown_delivery(home) -> None:
    """The instant between taking the lock and writing the record is not a verdict.

    A claim flocks and then writes, so a reader arriving between the two sees a
    held lock and an empty file. Reporting that as `delivery: unknown` would
    refuse a dispatch against a follower that is perfectly fine — found when
    suite load widened the window enough to lose a race that passed alone.
    """
    path = runs.follower_lock_path("proj", "mine")
    path.parent.mkdir(parents=True, exist_ok=True)
    settled: list[dict] = []

    def hold() -> None:
        with runs.follower_claim("proj", "mine", delivery="stream"):
            settled.append(runs.follower_state("proj", "mine"))
            time.sleep(0.4)

    # An empty file whose lock is held by nobody is not a registration at all.
    path.write_text("")
    state = runs.follower_state("proj", "mine")
    assert state["registered"] is False
    assert state["live"] is False

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    try:
        _wait_for(lambda: bool(settled))
        observed = runs.follower_state("proj", "mine")
        assert observed["registered"] is True
        assert observed["delivery_recorded"] == "stream"
        assert observed["follower"].get("pid") == os.getpid()
    finally:
        thread.join(timeout=2)


def test_a_row_that_is_not_live_says_which_condition_ended_it(home) -> None:
    """A stale registration must not read as coverage.

    Its `since` records when it attached, so a dead row's only timestamp makes
    it look older and better established rather than stale — and a consumer
    counting rows to ask "is this project covered" is then answered by a
    registration that ended. Reported by a session that read `followers` as 2
    while one of them was dead.
    """
    path = runs.follower_lock_path("proj", "departed")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project": "proj",
                "session": "departed",
                "pid": 999_999_999,
                "pid_start_time": "1",
                "delivery": "stream",
                "started_at": "2026-09-01T09:00:00Z",
            }
        )
    )

    with runs.follower_claim("proj", "attached", delivery="stream"):
        payload = runs.project_watch_visibility("proj")

    rows = {row["session"]: row for row in payload["followers"]}
    assert set(rows) == {"departed", "attached"}, "the array stays lossless"
    assert payload["followers_live"] == 1, "the count answers coverage, not the length"
    assert payload["delivering_sessions"] == ["attached"]

    assert rows["attached"]["live"] is True
    assert "not_live_because" not in rows["attached"]

    departed = rows["departed"]
    assert departed["live"] is False
    assert "is gone" in departed["not_live_because"]
    assert departed["since"] == "2026-09-01T09:00:00Z", (
        "its only timestamp is its attach time, which is why the reason matters"
    )


def test_a_follower_whose_consumer_exited_names_that(home) -> None:
    """Streaming to a session that has gone is not delivery either."""
    state = runs._follower_liveness(runs.follower_lock_path("proj", "absent"))
    assert state["live"] is False
    assert state["not_live_because"] == "no registration remains"


def test_the_descriptor_trace_is_paid_once_per_process(home, monkeypatch) -> None:
    """A hot path must not pay an unbounded scan repeatedly.

    Resolving where a *foreign* process's lines end walks every process's
    descriptors — 211 ms on a host with 1663 of them. Dispatch's producer-arming
    loop polls liveness twenty times a second, so putting that trace on it spent
    the entire arming budget on measurement: the watcher tests went from 5 s to
    42 s and began failing `watcher-required` against a producer that was fine.
    Measured by counting the scans rather than the milliseconds, so the guard
    cannot pass on a fast host and fail on a slow one.
    """
    scans = 0
    real = runs._pipe_reader_pids

    def counted(inode, *, exclude):
        nonlocal scans
        scans += 1
        return real(inode, exclude=exclude)

    monkeypatch.setattr(runs, "_pipe_reader_pids", counted)

    read_end, write_end = os.pipe()
    try:
        # A pipe is the only descriptor kind whose end needs finding; the cheap
        # kinds must never reach the scan at all.
        runs._trace_delivery(os.fstat(write_end), pid=os.getpid(), hops=4)
        assert scans == 1
    finally:
        os.close(read_end)
        os.close(write_end)

    scans = 0
    for _ in range(5):
        runs.delivery_mode_of(os.getpid())
    assert scans <= 1, "a live process's descriptor is traced once, then cached"

    # And the cheap kinds answer without a scan on every call.
    scans = 0
    with (home / "sink.log").open("w") as sink:
        assert runs.delivery_mode(sink.fileno()) == "file"
    assert scans == 0


def test_an_orphaned_producer_is_unreadable_to_nobody(home) -> None:
    """Admission and readability are different questions about one process.

    A producer whose supervisor dies is reparented to init. It stops satisfying
    the dispatch guard, correctly, because nothing is listening to the seat it
    holds — but it keeps appending real transitions, and a follower that refuses
    to read them waits forever on data that is arriving. Measured: an orphaned
    producer with 51 KB of stream and a live run left its session's pane empty
    for four minutes with no way to tell why.
    """
    path = runs.watch_lock_path("proj")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        runs._write_watch_record(
            handle,
            {
                "project": "proj",
                "pid": os.getpid(),
                "pid_start_time": runs._process_start_time(os.getpid()),
                # Reparented to init: the supervisor that made it observable
                # is gone.
                "parent_pid": 1,
                "parent_start_time": "23",
                "started_at": runs._utc_now(),
            },
        )

    assert runs.producer_live("proj") is True, "its stream is still being written"
    assert runs.project_watch_visibility("proj")["watcher_live"] is False, (
        "and it must not admit a dispatch, because nothing is listening to it"
    )
    assert runs.project_watch_visibility("proj")["observer_alive"] is False
