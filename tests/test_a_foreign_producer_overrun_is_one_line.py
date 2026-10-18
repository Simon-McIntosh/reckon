"""A reload overrun keeps the foreign seat's host on one pane line."""

from __future__ import annotations

import json
import os
import socket
import threading

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew_follow_commands as follow
from reckon.crew import runs

PROJECT = "pane-producer"
SESSION = "pane-session"
FOREIGN_HOST = "producer-elsewhere.invalid"
OLD_STAMP = "0" * 64


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _overrun(monkeypatch, *, host: str) -> list[dict]:
    seat = runs.watch_lock_path(PROJECT)
    seat.parent.mkdir(parents=True, exist_ok=True)
    seat.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "pid": os.getpid(),
                "pid_start_time": runs._process_start_time(os.getpid()),
                "host": host,
                "code_stamp": OLD_STAMP,
            }
        )
    )
    now = [1000.0]
    waits = [0]
    stop = threading.Event()

    def sleeper(_seconds):
        waits[0] += 1
        now[0] += 1.0
        if waits[0] >= 3:
            stop.set()

    monkeypatch.setattr(runs, "producer_live", lambda _project: True)
    monkeypatch.setattr(
        runs,
        "follower_owner",
        lambda: (os.getpid(), runs._process_start_time(os.getpid())),
    )
    return list(
        cli_module._follow_watch_lines(
            PROJECT,
            session=SESSION,
            resume={"offset": 0, "reported": {}},
            reloaded_in_place=True,
            producer_reload_window=1.0,
            poll_interval=1.0,
            sleeper=sleeper,
            clock=lambda: now[0],
            stop=stop,
            on_poll=None,
            sweep=None,
        )
    )


def _render(monkeypatch, events: list[dict], *, json_output: bool) -> list[str]:
    monkeypatch.setattr(
        follow, "_follow_watch_lines", lambda *_a, **_kw: (dict(e) for e in events)
    )
    monkeypatch.setattr(follow, "_hand_off_from_follower", lambda *_a: False)
    arguments = ["crew", "follow", "--project", PROJECT, "--session", SESSION]
    if json_output:
        arguments.append("--json")
    result = CliRunner().invoke(cli_module.main, arguments)
    assert result.exit_code == 0, result.output
    return result.output.splitlines()


def _producer_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if f"producer {PROJECT} runs older code" in line]


def test_foreign_overrun_draws_one_host_line_and_keeps_both_events(
    isolated_home, monkeypatch
) -> None:
    events = _overrun(monkeypatch, host=FOREIGN_HOST)
    kinds = [event.get("event") for event in events]
    assert kinds.count(cli_module.FOLLOWER_PRODUCER_RELOADING_EVENT) == 2
    assert kinds.count(cli_module.FOLLOWER_STALE_PRODUCER_EVENT) == 1

    pane = _producer_lines(_render(monkeypatch, events, json_output=False))
    assert len(pane) == 1, pane
    assert FOREIGN_HOST in pane[0]
    assert OLD_STAMP[:8] in pane[0]
    assert str(events[-1]["current_stamp"])[:8] in pane[0]
    assert "reloads itself" in pane[0]

    stream = [
        json.loads(line) for line in _render(monkeypatch, events, json_output=True)
    ]
    stream_kinds = [event.get("event") for event in stream]
    assert cli_module.FOLLOWER_PRODUCER_RELOADING_EVENT in stream_kinds
    assert cli_module.FOLLOWER_STALE_PRODUCER_EVENT in stream_kinds


def test_local_overrun_keeps_reloading_and_cycle_lines(
    isolated_home, monkeypatch
) -> None:
    events = _overrun(monkeypatch, host=socket.gethostname())
    pane = _producer_lines(_render(monkeypatch, events, json_output=False))
    assert len(pane) == 2, pane
    assert "it is reloading itself" in pane[0]
    assert "cycle it with:" in pane[1]
    assert runs.watch_cycle_line(PROJECT) in pane[1]
