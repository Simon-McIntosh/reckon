"""Read-only fleet follower and harness monitor contracts."""

from __future__ import annotations

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

    def visibility(_project: str) -> dict:
        return {
            "watcher_live": active,
            "arming_line": "reckon crew watch --project proj",
        }

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
            "baseline": [f"baseline-{number}"],
        }

    def seat_claim(*_args, **_kwargs):
        pytest.fail("a read-only follower must not acquire the watcher seat")

    monkeypatch.setattr(runs, "project_watch_visibility", visibility)
    monkeypatch.setattr(runs, "watch_stream_cursor", cursor, raising=False)
    monkeypatch.setattr(runs, "_project_watch_claim", seat_claim)

    outputs: list[list[str] | None] = [None, None, None]
    failures: list[BaseException] = []

    def follow(index: int) -> None:
        try:
            outputs[index] = list(cli._follow_watch_lines("proj", poll_interval=0.001))
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    readers: list[threading.Thread] = []
    transitions = [
        "10:01:00  first-node  dispatched → working  1 live · 0 blocked · 0 unpromoted",
        "10:02:00  first-node  working → blocked  1 live · 1 blocked · 0 unpromoted",
        "10:03:00  first-node  blocked → complete  1 live · 0 blocked · 1 unpromoted",
    ]
    for index, transition in enumerate(transitions):
        reader = threading.Thread(target=follow, args=(index,))
        reader.start()
        readers.append(reader)
        with cursor_condition:
            assert cursor_condition.wait_for(
                lambda: cursor_count == index + 1, timeout=2
            )
        _append_line(stream_path, transition)

    active = False
    for reader in readers:
        reader.join(timeout=2)
        assert not reader.is_alive()

    assert failures == []
    assert outputs == [
        ["baseline-1", *transitions],
        ["baseline-2", *transitions[1:]],
        ["baseline-3", transitions[2]],
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
    reference = Path(
        "skills/reckon-ship/references/orchestrator-harness/claude-code.md"
    ).read_text()
    section = reference.split("## Arming the fleet watch after dispatch", 1)[1].split(
        "## Resuming a held wave without a human", 1
    )[0]
    words = " ".join(section.split())

    assert "harness `Monitor`" in words
    assert "reckon crew follow --project <project>" in words
    assert "four runs" in words
    assert "eight hours earlier" in words
    assert "three terminal events" in words
    assert "more than two hours" in words
    assert "run_in_background" not in words


def _attach_filter(project: str = "demo") -> re.Pattern[str]:
    """Extract the regex the paste-ready attach line hands to grep."""
    line = runs._watch_attach_line(project)
    return re.compile(re.search(r"-E '([^']+)'", line).group(1))


def _transition(
    to_state: str, *, from_state: str = "dispatched", blocked: int = 3
) -> str:
    """Render a ticker line through the formatter a follower actually emits."""
    return recovery.format_watch_transition(
        {
            "observed_at": "2026-08-26T09:15:00Z",
            "node": "some-node",
            "from_state": from_state,
            "to_state": to_state,
            "live": 4,
            "blocked": blocked,
            "unpromoted": 2,
        }
    )


@pytest.mark.parametrize("state", runs.WATCH_ATTENTION_STATES)
def test_attach_filter_wakes_the_session_on_every_attention_state(state: str) -> None:
    assert _attach_filter().search(_transition(state))


@pytest.mark.parametrize("state", runs.WATCH_PROGRESS_STATES)
def test_attach_filter_stays_quiet_through_progress_carrying_a_blocked_count(
    state: str,
) -> None:
    """The summary field names states too, so the filter anchors on the arrow.

    Without that anchor every line matches, because `N blocked / N unpromoted`
    trails each one -- a firehose that reads as a working channel until the
    monitor is stopped for volume.
    """
    line = _transition(state, blocked=3)
    assert "blocked" in line
    assert not _attach_filter().search(line)


def test_attention_and_progress_partition_every_recovery_class() -> None:
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
