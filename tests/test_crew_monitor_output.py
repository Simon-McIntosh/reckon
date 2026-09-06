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
            (
                ("dispatched", "working"),
                ("working", "blocked"),
                ("blocked", "complete"),
            ),
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
    assert "--session <session>'," in words, "the armed command must be the bare one"
    # A default state filter is what produced an empty pane on this host, so the
    # reference has to say the follower reports everything and carries no state
    # filter at all.
    assert "do not add a state filter by default" in words
    assert "No output available" in words
    assert "session_attached" in words
    # The measured failure the contrast exists to prevent.
    assert "four runs" in words
    assert "eight hours earlier" in words
    assert "three terminal events" in words
    assert "more than two hours" in words


def test_every_state_the_snapshot_can_emit_is_in_the_watch_vocabulary() -> None:
    """A state the snapshot can emit must be one the watch surface knows.

    The follower once routed through the attention set, and `stalled` — the
    state a coordinator most needs, a worker gone quiet inside its budget —
    was in neither set, so the check stayed green while nothing could wake
    anyone for it. The follower carries no state filter any more; what
    survives is the vocabulary check: read the states out of the function
    that emits them and require every one to be a state the surface names.
    """
    source = inspect.getsource(recovery._watch_snapshot)
    emitted = set(re.findall(r'state = "([a-z_]+)"', source))
    emitted |= {"complete", "blocked", "failed"}  # assigned from manifest_status
    routed = set(runs.WATCH_ATTENTION_STATES) | set(runs.WATCH_PROGRESS_STATES)
    assert emitted <= routed, f"unrouted states: {sorted(emitted - routed)}"
    assert "stalled" in runs.WATCH_ATTENTION_STATES


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


def test_every_emitted_state_lands_in_exactly_one_fleet_bucket() -> None:
    """A figure a reader adds up has to add up.

    The four summary buckets partition the fleet, so a state that belongs to
    none of them silently vanishes from every total while the line still prints
    — the same shape as the routing gap that left `stalled` unmatched by any
    filter. Read the states out of the function that emits them rather than
    maintaining a second list beside it.
    """
    source = inspect.getsource(recovery._watch_snapshot)
    emitted = set(re.findall(r'state = "([a-z_]+)"', source))
    emitted |= {"complete", "blocked", "failed"}  # assigned from manifest_status
    emitted |= set(recovery.RECOVERY_CLASSES)  # reached through the fallback

    buckets = {
        "working": set(recovery.FLEET_WORKING_STATES),
        "blocked": set(recovery.FLEET_BLOCKED_STATES),
        "unpromoted": set(recovery.FLEET_UNPROMOTED_STATES),
        "waiting": set(recovery.FLEET_WAITING_STATES),
    }
    for left, right in (
        ("working", "blocked"),
        ("working", "unpromoted"),
        ("working", "waiting"),
        ("blocked", "unpromoted"),
        ("blocked", "waiting"),
        ("unpromoted", "waiting"),
    ):
        assert not buckets[left] & buckets[right], f"{left} and {right} overlap"

    counted = set().union(*buckets.values())
    assert emitted <= counted, f"uncounted states: {sorted(emitted - counted)}"


def test_a_blocked_or_delivered_run_is_not_counted_as_working() -> None:
    """The reading that prompted this: `live` was a pointer count, and a reader
    takes the first figure for work in progress."""
    fleet = {
        "r-1": {"run_id": "r-1", "state": "working"},
        "r-2": {"run_id": "r-2", "state": "blocked"},
        "r-3": {"run_id": "r-3", "state": "complete"},
        "r-4": {"run_id": "r-4", "state": "stalled"},
        "r-5": {"run_id": "r-5", "state": "waiting"},
    }
    counts = recovery._fleet_counts(fleet)
    assert counts == {"working": 1, "blocked": 2, "unpromoted": 1, "waiting": 1}
    assert sum(counts.values()) == len(fleet), "the buckets must account for the fleet"
