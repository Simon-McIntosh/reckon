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
from collections.abc import Iterable
from pathlib import Path

import pytest

from reckon.crew import resumption
from reckon.crew.resumption import recovery_log_path

PROJECT = "proj-alpha"
LOG_LINES = 5_000


def _report(index: int, *, dry_run: bool = False) -> str:
    """One recovery-log line, in the shape a sweep appends."""
    return json.dumps(
        {
            "dry_run": dry_run,
            "resumed": [
                {
                    "run_id": f"r-{index:06d}-node",
                    "hold": f"hold-{index % 977}",
                }
            ],
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
        if isinstance(payload, str):
            self.lines.append(payload)
        return self._loads(payload, *args, **kwargs)

    @property
    def count(self) -> int:
        return len(self.lines)

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
