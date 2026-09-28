"""Shared atomic JSON writer and numeric decoder behaviour.

These four checks fix the contract the shared primitives owe their future
callers: a failed write leaves no sibling temporary behind, two writers never
produce a file a reader can see mid-write, the durability flush is under the
caller's control, and the decoder refuses the bool that would otherwise read as
a measurement.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from reckon import _store
from reckon._observations import optional_number
from reckon._store import write_json_atomically


def test_writer_leaves_no_temporary_behind_after_failure(tmp_path, monkeypatch):
    """A refused write keeps the destination and leaves no stray sibling."""
    target = tmp_path / "record.json"
    target.write_text('{"state": "old"}\n', encoding="utf-8")

    def refuse(*_args, **_kwargs):
        raise RuntimeError("injected serialisation failure")

    monkeypatch.setattr(_store.json, "dump", refuse)

    with pytest.raises(RuntimeError):
        write_json_atomically(target, {"state": "new"})

    assert target.read_text(encoding="utf-8") == '{"state": "old"}\n'
    assert list(tmp_path.iterdir()) == [target]


def test_two_concurrent_writers_never_interleave(tmp_path):
    """A reader observes one whole payload or the other, never a blend."""
    target = tmp_path / "shared.json"
    payloads = (
        {"writer": "left", "rows": ["L"] * 4000},
        {"writer": "right", "rows": ["R"] * 4000},
    )
    faults: list[str] = []

    def write_and_check(payload: dict) -> None:
        for _ in range(40):
            write_json_atomically(target, payload)
            try:
                observed = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                faults.append(f"unreadable: {exc!r}")
                return
            if observed != payload:
                faults.append("interleaved payload")
                return

    threads = [
        threading.Thread(target=write_and_check, args=(payload,))
        for payload in payloads
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert faults == []
    assert json.loads(target.read_text(encoding="utf-8")) in payloads


def test_fsync_is_called_when_on_and_not_when_off(tmp_path, monkeypatch):
    """The durability flush is present by default and skippable by the caller."""
    target = tmp_path / "durable.json"
    calls: list[int] = []
    real_fsync = os.fsync

    def record(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(_store.os, "fsync", record)

    write_json_atomically(target, {"n": 1})
    assert len(calls) == 1

    calls.clear()
    write_json_atomically(target, {"n": 2}, fsync=False)
    assert calls == []


@pytest.mark.parametrize("value", [True, False, "1.5", None, [1], {"n": 1}])
def test_optional_number_refuses_bool_and_non_numbers(value):
    assert optional_number(value) is None


def test_optional_number_returns_float_for_real_numbers():
    assert optional_number(3) == 3.0
    assert isinstance(optional_number(3), float)
    assert optional_number(2.5) == 2.5
