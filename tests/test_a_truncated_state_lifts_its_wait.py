"""Scheduler state truncation has the same meaning in both wait readers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from reckon.crew import recovery_wait, resumption


@pytest.mark.parametrize(
    ("printed", "terminal", "lifts"),
    [
        ("OUT_OF_ME+", "OUT_OF_MEMORY", True),
        ("OUT_OF_MEMORY", "OUT_OF_MEMORY", True),
        ("RUNNIN+", "COMPLETED", False),
        ("COMPLETE", "COMPLETED", False),
    ],
)
@pytest.mark.parametrize("reader", ["sweep", "classifier"])
def test_wait_reader_interprets_scheduler_states(
    tmp_path: Path, printed: str, terminal: str, lifts: bool, reader: str
) -> None:
    record = {"worktree": str(tmp_path)}
    wait = {
        "probe": [sys.executable, "-c", f"print({printed!r})"],
        "terminal": [terminal],
    }

    if reader == "sweep":
        result = resumption._run_condition_probe(record, wait)
        assert result["observed"] == printed
        assert result["terminal"] is lifts
    else:
        result = recovery_wait._run_wait_condition_probe(record, wait)
        assert result["observed"] == printed
        assert result["state"] == ("met" if lifts else "unknown")
