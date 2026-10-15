"""A bounded stop names a producer reload and a recorded clean-tree wait."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from importlib import import_module

import pytest

from reckon.crew import obligation_snapshot, runs
from reckon.hooks import coordinator_obligations as hook

PROJECT = "reload-hook-fixture"
SESSION = "reload-hook-session"
PATH = "reckon/crew_dispatch_commands.py"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    monkeypatch.setenv("RECKON_HOME", str(root))
    monkeypatch.setattr(hook, "locate_session", lambda payload: (PROJECT, SESSION))
    monkeypatch.setenv(hook.STOP_DERIVATION_BUDGET_ENV, "0.000001")
    return root


def _snapshot(*, age_seconds=0, code_stamp="older", producer_live=True):
    instant = datetime.now(tz=UTC)
    producer = {
        "pid": os.getpid() if producer_live else 2_147_483_647,
        "pid_start_time": (
            obligation_snapshot.process_start_time(os.getpid())
            if producer_live
            else "1"
        ),
        "started_at": instant.isoformat(),
        "code_stamp": code_stamp,
    }
    document = obligation_snapshot.document_for(
        {"project": PROJECT, "session": SESSION, "obligations": []},
        computed_at=instant - timedelta(seconds=age_seconds),
        stream_offset=0,
        producer=producer,
    )
    obligation_snapshot.write_snapshot(PROJECT, SESSION, document)


def _seat(**fields):
    path = runs.watch_lock_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def _stop(capsys):
    assert hook._stop({}) == 0
    return json.loads(capsys.readouterr().out)["systemMessage"]


def _prompt(capsys):
    assert hook._prompt({}) == 0
    return json.loads(capsys.readouterr().out)["hookSpecificOutput"][
        "additionalContext"
    ]


def test_bounded_stop_names_reload_without_ensure(home, capsys):
    _snapshot()
    line = _stop(capsys)
    assert "producer reloading" in line, line
    assert "watch --ensure" not in line, line


def test_recorded_deferral_names_its_file_on_both_paths(home, capsys):
    _snapshot()
    _seat(
        reload_deferral={
            "reason": "tracked files modified against HEAD",
            "paths": [PATH],
        }
    )
    prompt = _prompt(capsys)
    stop = _stop(capsys)
    for line in (prompt, stop):
        assert PATH in line, line
        assert "committed" in line, line
        assert "watch --ensure" not in line, line


def test_absence_past_reload_window_keeps_remedy(home, capsys):
    _snapshot(age_seconds=600, producer_live=False)
    _seat()
    line = _stop(capsys)
    assert "no producer" in line, line
    assert "watch --ensure" in line, line


def test_foreign_host_is_carried_from_freshness(home, capsys, monkeypatch):
    calls = iter(("other-node", ""))
    monkeypatch.setattr(
        obligation_snapshot, "live_foreign_seat_host", lambda project: next(calls)
    )
    line = _stop(capsys)
    assert "other-node" in line, line


def test_producer_records_deferral_and_clears_it_as_reload_starts(home, monkeypatch):
    from reckon import crew_follow_commands as follow

    dispatch = import_module("reckon.crew.dispatch")

    seat = runs.watch_lock_path(PROJECT)
    seat.parent.mkdir(parents=True, exist_ok=True)
    with seat.open("a+b") as handle:
        runs._WATCH_SEAT_HANDLES[PROJECT] = handle
        try:
            runs._write_watch_record(handle, {"project": PROJECT})
            monkeypatch.setattr(runs, "follower_code_stamp", lambda: "current")
            reloader = follow._FollowerReloader(PROJECT, None, seat=True)
            reloader.code_stamp = "older"
            monkeypatch.setattr(
                follow.checkout_trust,
                "tracked_reckon_tree_is_dirty",
                lambda root: (True, f" M {PATH}"),
            )
            reloader.poll({})
            deferred = runs._read_watch_record(handle)["reload_deferral"]
            assert PATH in deferred["paths"]
            assert "tracked" in deferred["reason"]

            monkeypatch.setattr(
                follow.checkout_trust,
                "tracked_reckon_tree_is_dirty",
                lambda root: (False, ""),
            )
            monkeypatch.setattr(reloader, "_replacement_imports", lambda: (True, ""))
            monkeypatch.setattr(
                dispatch, "_export_launched_workers_for_reexec", lambda: None
            )
            monkeypatch.setattr(runs, "prepare_watch_seat_reexec", lambda project: None)
            monkeypatch.setattr(
                os, "execve", lambda *args: (_ for _ in ()).throw(OSError("probe"))
            )
            reloader.checked_at = None
            reloader.poll({})
            assert "reload_deferral" not in runs._read_watch_record(handle)
        finally:
            runs._WATCH_SEAT_HANDLES.pop(PROJECT, None)
