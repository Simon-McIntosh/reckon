"""The tick measure's gate fires on a reading over its bound.

The section's record cites this measure's figures, so the measure has to refuse a
population that crosses the bound rather than only print the numbers a reader
hopes for. The gate is exercised from both sides: a bound no reading can meet
must exit non-zero and name the readings it refused, and the bound the section
sets must exit zero on a population that meets it.

A tiny population is used — six live runs and two ticks — because the property
under test is the gate, not the tick's cost; the cost belongs to the measure run
against 230 live runs, which no suite can afford to repeat.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests import measure_station_tick_at_230_runs as measure


@pytest.fixture()
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return tmp_path


def _run(scratch: Path, *arguments: str) -> tuple[int, str]:
    out = scratch / "arms.json"
    code = measure.main(
        [
            "--runs",
            "6",
            "--ticks",
            "2",
            "--stream-bytes",
            "16384",
            "--out",
            str(out),
            *arguments,
        ]
    )
    return code, out.read_text(encoding="utf-8")


def test_a_reading_over_the_bound_fails_the_measure(scratch: Path) -> None:
    code, text = _run(scratch, "--tick-bound-seconds", "0.000001")
    result = json.loads(text)
    assert code == 1, "a population over the tick bound must fail the measure"
    assert result["tick_bound_seconds"] == 0.000001
    assert len(result["over_bound"]) == len(result["ticks"]), (
        "every reading is over a bound no tick can meet"
    )
    assert all(row["seconds"] > 0 for row in result["ticks"])


def test_a_population_under_the_bound_passes_the_measure(scratch: Path) -> None:
    code, text = _run(scratch)
    result = json.loads(text)
    assert code == 0, result["over_bound"]
    assert result["over_bound"] == []
    assert len(result["ticks"]) == 2
    assert all(row["resident_bytes"] > 0 for row in result["ticks"])


def test_the_memory_bound_is_judged_in_bytes(scratch: Path) -> None:
    code, text = _run(scratch, "--memory-bound-bytes", "1024")
    result = json.loads(text)
    assert code == 1, "a reading over the memory bound must fail the measure"
    assert all(row["resident_bytes"] >= 1024 for row in result["over_bound"])

