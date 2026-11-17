"""A tick over the target fleet reads no stream that did not move.

The station asks two questions of every live run on every tick — has its newest
stream carried an assistant record, and which tool call did it close on — and
both walk the stream to answer. Unmemoised, a tick's cost is the fleet's whole
stream volume rather than the bytes that arrived since the last one: at 230 live
runs a tick with nothing new still opened roughly four stream files per run,
which is the wall-clock the tick spent growing past its bound. This case drives
60 live runs rather than 230: the property is that a quiet tick opens no stream
at all, so its count is the same at any population, and a fleet-scale fixture
inside a shared suite competes for the machine with the rest of the suite.

The counter wraps ``Path.open`` and keeps the stream paths opened during a
measured window, so the assertion is against the files actually read rather than
a call count that a different read could satisfy. The cold tick is the counter's
own control: it must see the reads that are known to happen.
"""

from __future__ import annotations

import contextlib
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from pathlib import Path

import pytest

from reckon.crew import recovery_wait, station, station_store
from tests.measure_station_tick_at_230_runs import populate, seed_projects

RUNS = 60
STREAMS = 16384


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A station over a crew home carrying the fleet's target live runs."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    (tmp_path / "runtime").mkdir()
    mounts = seed_projects(tmp_path / "projects", home)
    streams = populate(home, RUNS, STREAMS)
    store = station_store.StationStore(tmp_path / "runtime")
    node = station.Station(
        store,
        home=home / "crew",
        projects=lambda: dict(mounts),
        duties=SimpleNamespace(
            committing=contextlib.nullcontext, request=lambda *a, **k: None
        ),
        sweeps=SimpleNamespace(request=lambda *a, **k: None),
    )
    try:
        yield node, streams
    finally:
        node.reflex.stop()
        store.close()


def _count_stream_opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count every stream file opened through ``Path.open``."""
    opened: list[str] = []
    original = Path.open

    def counting(self: Path, *arguments, **keywords):
        if self.name == "stream.jsonl":
            opened.append(str(self))
        return original(self, *arguments, **keywords)

    monkeypatch.setattr(Path, "open", counting)
    return opened


def test_a_quiet_tick_opens_no_stream(fleet, monkeypatch: pytest.MonkeyPatch) -> None:
    node, _streams = fleet
    moment = datetime.now(tz=UTC)
    opened = _count_stream_opens(monkeypatch)
    node.tick(now=moment)
    cold = len(opened)
    assert cold > 0, "the counter must see the reads a cold tick is known to make"
    opened.clear()
    node.tick(now=moment + timedelta(seconds=5))
    assert opened == [], "a tick over a fleet that did not move read these streams"


def test_a_tick_reads_only_the_stream_that_grew(fleet, monkeypatch) -> None:
    node, streams = fleet
    moment = datetime.now(tz=UTC)
    node.tick(now=moment)
    opened = _count_stream_opens(monkeypatch)
    stamps = time.time()
    with streams[0].open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"type": "assistant", "text": "x" * 2048, "ts": stamps}) + "\n"
        )
    node.tick(now=moment + timedelta(seconds=5))
    assert opened, "the stream that grew was not read"
    assert set(opened) == {str(streams[0])}, (
        f"read streams that did not move: {sorted(set(opened))}"
    )


def test_a_stream_that_grew_answers_with_its_new_tail(tmp_path: Path) -> None:
    path = tmp_path / "stream.jsonl"
    path.write_text(json.dumps({"type": "user", "text": "start"}) + "\n")
    assert recovery_wait._last_tool_call(path) is None
    path.write_text(json.dumps({"type": "user", "text": "later"}) + "\n")
    assert recovery_wait._last_tool_call(path) is None
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "sleep 5"},
                            }
                        ]
                    },
                }
            )
            + "\n"
        )
    assert recovery_wait._last_tool_call(path) == ("Bash", "sleep 5")
