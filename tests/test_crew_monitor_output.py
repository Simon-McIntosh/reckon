"""Read-only fleet follower and harness monitor contracts."""

from __future__ import annotations

import inspect
import json
import os
import re
import select
import threading
from pathlib import Path

import pytest

from reckon import cli
from reckon.crew import recovery, runs


def _append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{line}\n")
        stream.flush()
        os.fsync(stream.fileno())


def test_three_followers_receive_each_subsequent_transition_once(
    tmp_path, monkeypatch
) -> None:
    stream_path = tmp_path / "project.events"
    stream_path.touch()
    active = True
    cursor_count = 0
    cursor_condition = threading.Condition()

    def producer_live(_project: str) -> bool:
        return active

    def cursor(_project: str) -> dict:
        nonlocal cursor_count
        with cursor_condition:
            cursor_count += 1
            number = cursor_count
            offset = stream_path.stat().st_size
            cursor_condition.notify_all()
        return {
            "stream_path": str(stream_path),
            "offset": offset,
            "baseline": [
                {
                    "run_id": f"r-baseline-{number}",
                    "node": f"baseline-{number}",
                    "to_state": "working",
                    "observed_at": "2026-08-26T10:00:00Z",
                }
            ],
        }

    def seat_claim(*_args, **_kwargs):
        pytest.fail("a read-only follower must not acquire the watcher seat")

    monkeypatch.setattr(runs, "producer_live", producer_live)
    monkeypatch.setattr(runs, "watch_stream_cursor", cursor, raising=False)
    monkeypatch.setattr(runs, "_project_watch_claim", seat_claim)

    outputs: list[list[str] | None] = [None, None, None]
    failures: list[BaseException] = []

    stop = threading.Event()

    def follow(index: int) -> None:
        try:
            outputs[index] = [
                recovery.format_watch_transition(event)
                for event in cli._follow_watch_lines(
                    "proj", poll_interval=0.001, stop=stop
                )
                if event.get("event") not in {"attached", "reattached"}
            ]
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    readers: list[threading.Thread] = []
    transitions = [
        {
            "run_id": "r-first",
            "node": "first-node",
            "observed_at": f"2026-08-26T10:0{index}:00Z",
            "from_state": previous,
            "to_state": current,
            "live": 1,
            "blocked": 1 if current == "blocked" else 0,
            "unpromoted": 1 if current == "complete" else 0,
        }
        for index, (previous, current) in enumerate(
            (("dispatched", "working"), ("working", "blocked"), ("blocked", "complete")),
            start=1,
        )
    ]
    for index, transition in enumerate(transitions):
        reader = threading.Thread(target=follow, args=(index,))
        reader.start()
        readers.append(reader)
        with cursor_condition:
            assert cursor_condition.wait_for(
                lambda: cursor_count == index + 1, timeout=2
            )
        _append_line(stream_path, json.dumps(transition))

    active = False
    stop.set()
    for reader in readers:
        reader.join(timeout=2)
        assert not reader.is_alive()

    rendered = [recovery.format_watch_transition(event) for event in transitions]
    baselines = [
        recovery.format_watch_transition(
            {
                "node": f"baseline-{number}",
                "to_state": "working",
                "observed_at": "2026-08-26T10:00:00Z",
            }
        )
        for number in (1, 2, 3)
    ]
    assert failures == []
    assert outputs == [
        [baselines[0], *rendered],
        [baselines[1], *rendered[1:]],
        [baselines[2], rendered[2]],
    ]


def test_follower_flushes_each_line_to_a_pipe() -> None:
    read_descriptor, write_descriptor = os.pipe()
    try:
        with os.fdopen(write_descriptor, "w", buffering=8192) as writer:
            cli._echo_follow_line("one transition", stream=writer)
            readable, _, _ = select.select([read_descriptor], [], [], 0.2)
            assert readable == [read_descriptor]
            assert os.read(read_descriptor, 1024) == b"one transition\n"
    finally:
        os.close(read_descriptor)


def test_harness_arms_a_monitor_and_attaches_followers() -> None:
    """The arming section must state the contrast, not just the right answer.

    Naming only the correct primitive left the wrong one available by default:
    a shell string is most naturally pasted into the shell tool, whose one
    notification arrives when the command exits — and the follower does not
    exit. So the section is checked for the discriminator itself, and the
    wrong primitive has to be named in order to be excluded.
    """
    reference = Path(
        "skills/reckon-ship/references/orchestrator-harness/claude-code.md"
    ).read_text()
    section = reference.split("## Arming the fleet watch after dispatch", 1)[1].split(
        "## Resuming a held wave without a human", 1
    )[0]
    words = " ".join(section.split())

    assert "`Monitor`" in words
    assert "run_in_background" in words, "the wrong primitive is not excluded"
    assert "per **stdout line**" in words
    assert "when the command **exits**" in words
    assert "belongs in a `Monitor`, and only there" in words
    assert "persistent: true" in words
    assert "--session <session> --attention" in words
    assert "session_attached" in words
    # The measured failure the contrast exists to prevent.
    assert "four runs" in words
    assert "eight hours earlier" in words
    assert "three terminal events" in words
    assert "more than two hours" in words


def _transition(
    to_state: str, *, from_state: str = "dispatched", blocked: int = 3
) -> dict:
    """Build one transition the way the producer records it."""
    return {
        "observed_at": "2026-08-26T09:15:00Z",
        "run_id": f"r-{to_state}",
        "node": "some-node",
        "session": "mine",
        "from_state": from_state,
        "to_state": to_state,
        "live": 4,
        "blocked": blocked,
        "unpromoted": 2,
    }


def _selected(event: dict) -> bool:
    """Ask the follower's own filter, which is where the filter now lives."""
    return cli._follow_selects(event, session=None, run_ids=(), attention=True)


@pytest.mark.parametrize("state", runs.WATCH_ATTENTION_STATES)
def test_the_filter_wakes_the_session_on_every_attention_state(state: str) -> None:
    assert _selected(_transition(state))


@pytest.mark.parametrize("state", runs.WATCH_PROGRESS_STATES)
def test_the_filter_stays_quiet_through_progress_carrying_a_blocked_count(
    state: str,
) -> None:
    """A shell filter matched the summary field, so progress woke the session.

    Every rendered line trails `N blocked · N unpromoted`, so an unanchored
    pattern matched the whole stream — a firehose that reads as a working
    channel until the monitor is stopped for volume. Selecting on the
    transition's own state cannot express that mistake.
    """
    event = _transition(state, blocked=3)
    assert "blocked" in recovery.format_watch_transition(event)
    assert not _selected(event)


def test_a_state_the_snapshot_can_emit_is_never_unrouted() -> None:
    """`stalled` was in neither set, so no filter ever woke anyone for it.

    The partition was asserted against the recovery classes, which do not
    include the states the ticker itself derives — so the one state a
    coordinator most needs (a worker gone quiet inside its budget) was
    unroutable and the check stayed green. Read the states out of the function
    that emits them instead.
    """
    source = inspect.getsource(recovery._watch_snapshot)
    emitted = set(re.findall(r'state = "([a-z_]+)"', source))
    emitted |= {"complete", "blocked", "failed"}  # assigned from manifest_status
    routed = set(runs.WATCH_ATTENTION_STATES) | set(runs.WATCH_PROGRESS_STATES)
    assert emitted <= routed, f"unrouted states: {sorted(emitted - routed)}"
    assert "stalled" in runs.WATCH_ATTENTION_STATES


def test_attention_and_progress_stay_disjoint_across_every_recovery_class() -> None:
    """A new run classification has to be routed before it can go unnoticed."""
    attention = set(runs.WATCH_ATTENTION_STATES)
    progress = set(runs.WATCH_PROGRESS_STATES)
    assert not attention & progress
    assert set(recovery.RECOVERY_CLASSES) <= attention | progress


def test_watch_payloads_carry_the_line_that_attaches_this_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seat is project-global; only the attach line delivers to this session."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    real = Path.home() / ".config" / "reckon" / "crew" / "watch"
    before = sorted(q.name for q in real.glob("demo*")) if real.is_dir() else []

    for payload in (runs.watch_state("demo"), runs.project_watch_visibility("demo")):
        assert payload["attach_line"] == runs._watch_attach_line("demo")
        assert "crew follow" in payload["attach_line"]
        assert payload["arming_line"] != payload["attach_line"]

    after = sorted(q.name for q in real.glob("demo*")) if real.is_dir() else []
    assert after == before, "the redirected home leaked a lock into the real one"
