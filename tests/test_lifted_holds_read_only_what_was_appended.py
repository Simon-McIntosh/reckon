"""A sweep decodes only the recovery-log lines appended since its last read.

The recovery log is append-only and each sweep rebuilt the whole lifted-hold
set from it, so every sweep decoded the entire record again to reach an answer
the previous sweep already had. The set still has to equal what a whole-file
read returns, a log that was replaced or shortened has to be read from the
start, and a second call over an unchanged log must decode nothing.

Decodes are counted through a recording stand-in for ``json.loads`` in
``resumption``, so "decoded nothing" is the decoder's own observation rather
than an inference from a set that happens to match.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable
from pathlib import Path

import pytest

from reckon.crew import recovery_wait, resumption
from reckon.crew.resumption import recovery_log_path

PROJECT = "proj-alpha"
LOG_LINES = 5_000


def _pair(index: int) -> tuple[str, str]:
    """The (run id, signature) pair the line at ``index`` records as lifted."""
    return f"r-{index:06d}-node", f"hold-{index % 977}"


def _report(index: int, *, dry_run: bool = False) -> str:
    """One recovery log line, in the shape a sweep appends."""
    run_id, hold = _pair(index)
    return json.dumps(
        {
            "dry_run": dry_run,
            "resumed": [{"run_id": run_id, "hold": hold}],
        },
        sort_keys=True,
    )


def _write_lines(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def _base_lifted_holds(path: Path, loads) -> set[tuple[str, str]]:
    """The base revision's read: the whole file, every line decoded."""
    lifted: set[tuple[str, str]] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            report = loads(line)
        except ValueError:
            continue
        if not isinstance(report, dict):
            continue
        if report.get("dry_run"):
            continue
        for entry in report.get("resumed") or ():
            if not isinstance(entry, dict):
                continue
            run_id = str(entry.get("run_id") or "")
            signature = str(entry.get("hold") or "")
            if run_id and signature:
                lifted.add((run_id, signature))
    return lifted


class _RecordingLoads:
    """``json.loads`` with a record of every line it was handed."""

    def __init__(self, loads) -> None:
        self._loads = loads
        self.lines: list[str] = []

    def __call__(self, payload, *args, **kwargs):
        if isinstance(payload, (str, bytes)):
            self.lines.append(payload)
        return self._loads(payload, *args, **kwargs)

    @property
    def count(self) -> int:
        return len(self.lines)

    def texts(self) -> list[str]:
        return [payload.decode("utf-8") for payload in self.lines]

    def reset(self) -> None:
        self.lines.clear()


def _record_loads(monkeypatch: pytest.MonkeyPatch) -> tuple[_RecordingLoads, object]:
    real_loads = json.loads
    recorder = _RecordingLoads(real_loads)
    monkeypatch.setattr(resumption.json, "loads", recorder)
    return recorder, real_loads


def test_a_sweep_decodes_only_the_lines_the_log_grew(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First read decodes everything, then only what was appended."""
    recorder, real_loads = _record_loads(monkeypatch)
    log = recovery_log_path(PROJECT)
    lines = [_report(index, dry_run=(index % 7 == 0)) for index in range(LOG_LINES)]
    _write_lines(log, lines)

    # (a) The first call decodes every line and agrees with the base read.
    recorder.reset()
    first = resumption._lifted_holds(PROJECT)
    assert recorder.count == LOG_LINES
    reference = _base_lifted_holds(log, real_loads)
    # The whole-file read is the instrument's own check that it saw a log:
    # a dry-run preview is not a lift, so the set is smaller than the lines.
    assert 0 < len(reference) < LOG_LINES
    assert set(first) == reference

    # (b) A second call over the unchanged log decodes nothing.
    recorder.reset()
    second = resumption._lifted_holds(PROJECT)
    assert recorder.count == 0
    assert set(second) == reference

    # (c) Three appended lines are the only lines the next call decodes.
    appended = [_report(LOG_LINES + offset) for offset in range(3)]
    with log.open("a", encoding="utf-8") as handle:
        for line in appended:
            handle.write(line + "\n")
    enlarged = _base_lifted_holds(log, real_loads)
    recorder.reset()
    third = resumption._lifted_holds(PROJECT)
    assert recorder.count == 3
    assert set(third) == enlarged
    assert set(third) > reference

    # (d) A log that was shortened is read from the start again.
    shorter = [_report(index) for index in range(4)]
    _write_lines(log, shorter)
    recorder.reset()
    fourth = resumption._lifted_holds(PROJECT)
    assert recorder.count == len(shorter)
    assert set(fourth) == _base_lifted_holds(log, real_loads)
    assert set(fourth) != enlarged


def test_a_replaced_log_is_read_from_the_start(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A longer log at a new inode is someone else's history, not an append."""
    recorder, real_loads = _record_loads(monkeypatch)
    log = recovery_log_path(PROJECT)
    first_lines = [_report(index) for index in range(6)]
    _write_lines(log, first_lines)
    recorder.reset()
    resumption._lifted_holds(PROJECT)
    assert recorder.count == len(first_lines)

    replacement = [_report(index, dry_run=False) for index in range(1_000, 1_012)]
    staged = tmp_path / "replacement.jsonl"
    _write_lines(staged, replacement)
    staged.replace(log)

    recorder.reset()
    replaced = resumption._lifted_holds(PROJECT)
    assert recorder.count == len(replacement)
    assert set(replaced) == _base_lifted_holds(log, real_loads)
    assert not set(replaced) & {("r-000000-node", "hold-0")}


def test_a_line_still_being_written_waits_for_its_newline(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record landing mid-append is decoded once, by the read after it closes."""
    recorder, real_loads = _record_loads(monkeypatch)
    log = recovery_log_path(PROJECT)
    complete = [_report(index) for index in range(20)]
    _write_lines(log, complete)
    pending = _report(999)
    half = pending[: len(pending) // 2]
    with log.open("a", encoding="utf-8") as handle:
        handle.write(half)

    recorder.reset()
    first = resumption._lifted_holds(PROJECT)
    assert recorder.count == len(complete)
    assert set(first) == _base_lifted_holds(log, real_loads)
    assert _pair(999) not in first
    # The offset stopped at the last newline rather than at the end of the file,
    # so the fragment waits for the bytes completing it.
    scan = recovery_wait._STREAM_SCANS[str(log)]
    assert scan.append_offset == log.stat().st_size - len(half)

    # The bytes that complete the record land, and the next read decodes it —
    # once, because the fragment itself was never consumed.
    with log.open("a", encoding="utf-8") as handle:
        handle.write(pending[len(half) :] + "\n")
    recorder.reset()
    second = resumption._lifted_holds(PROJECT)
    assert recorder.count == 1
    assert _pair(999) in second
    assert set(second) == _base_lifted_holds(log, real_loads)


def test_two_overlapping_reads_keep_every_lift(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read that starts past unclaimed lines must not publish over them.

    The offset a read advances and the set that offset stands for are one fact.
    Published apart, a second read can take its start from the offset the first
    has already advanced to while the stored set still belongs to the older one:
    it finds a start it never read from, drops everything the first had counted
    but not yet published, and records an offset past those lines — after which
    the sweep resumes a hold it already lifted.
    """
    recorder, _ = _record_loads(monkeypatch)
    log = recovery_log_path(PROJECT)
    seeded = [_report(index) for index in range(20)]
    _write_lines(log, seeded)
    seeded_set = resumption._lifted_holds(PROJECT)
    assert len(seeded_set) == len(seeded)
    assert recorder.count == len(seeded)

    appended = [_report(index) for index in range(20, 23)]
    with log.open("a", encoding="utf-8") as handle:
        for line in appended:
            handle.write(line + "\n")

    entered = threading.Event()
    release = threading.Event()
    real_appended = recovery_wait.appended_lines
    held = {"first": False}

    def blocking_appended(path):
        """Advance past the read, then hold the first caller before it publishes."""
        result = real_appended(path)
        if held["first"]:
            return result
        held["first"] = True
        entered.set()
        assert release.wait(5), "the held read was never released"
        return result

    monkeypatch.setattr(recovery_wait, "appended_lines", blocking_appended)

    failures: list[BaseException] = []

    def read() -> None:
        try:
            resumption._lifted_holds(PROJECT)
        except BaseException as error:  # noqa: BLE001 -- reported, not swallowed
            failures.append(error)

    recorder.reset()
    held_thread = threading.Thread(target=read)
    held_thread.start()
    assert entered.wait(5), "the first read never reached its append read"

    second_thread = threading.Thread(target=read)
    second_thread.start()
    # Give the second read the window to publish over the first. Where the two
    # facts advance together the second call is waiting on the same lock here,
    # and the assertion below is the same either way.
    time.sleep(0.2)
    release.set()
    held_thread.join(5)
    second_thread.join(5)
    assert not failures, failures
    assert not held_thread.is_alive() and not second_thread.is_alive()

    final = resumption._lifted_holds(PROJECT)
    assert set(final) == {_pair(index) for index in range(23)}

    decoded = recorder.texts()
    assert len(decoded) == len(set(decoded)), "a line was decoded twice"
    assert set(decoded) == set(appended)
