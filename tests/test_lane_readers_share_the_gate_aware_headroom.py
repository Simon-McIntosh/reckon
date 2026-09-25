"""Every production reader of a lane document reports the same headroom.

A lane's published document carries two headroom figures: the engine's own
(``headroom``/``engine_headroom``) and the admission-limited one derived from
the router's gate. Three surfaces read that document -- the flight probe, the
dispatch carry and the shared reader ``crew(view="lanes")`` uses -- and before
this node the flight probe and the dispatch carry each parsed the top-level
engine field themselves, so a document whose gate was full read as though slots
were free. So the flight probe reported 12 while the gate-aware reader
reported 7 from the same file, minutes apart.

Each case here passes the reader's own figure only when it comes from the
shared gate-aware resolver. The shared-headroom case has an executed negative
control: the flight probe is dropped back to reading the top-level engine field
itself, and its assertion must then fail with the engine figure. The declared
mutation those controls restore is logged verbatim by the gate that runs them.
"""

from __future__ import annotations

import importlib
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew.lane_document import (
    UNKNOWN,
    read_lane_document,
    read_lane_document_file,
)

flight_module = importlib.import_module("reckon.flight")
dispatch_module = importlib.import_module("reckon.crew.dispatch")

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = (
    "flight._probe_lane_document keeps reading the top-level engine headroom "
    "field itself; the shared-headroom case must fail with flight reporting "
    "the engine figure"
)
NEGATIVE_CONTROL = os.environ.get("RECKON_LANE_HEADROOM_NEGATIVE_CONTROL", "").strip()


@contextmanager
def _control(monkeypatch: pytest.MonkeyPatch, guard: str):
    """Drop the shared resolver back to the engine field, for the red run only."""
    if NEGATIVE_CONTROL in {guard, "all"}:
        monkeypatch.setattr(
            flight_module,
            "_lane_document_headroom",
            lambda payload: payload.get("headroom"),
        )
    yield


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "lane.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _document(observed_at: str) -> dict:
    """A document whose gate is full and whose engine still claims six slots."""
    return {
        "headroom": 6,
        "engine_headroom": 6,
        "running": 14,
        "waiting": 0,
        "state": "measured",
        "observed_at": observed_at,
        "suggested_shelf_life_seconds": 3600,
        "router_generation_gate": {"width": 14, "in_flight": 14, "waiting": 3},
        "admission": {
            "headroom": -3,
            "verdict": "congested",
            "waiting": 3,
            "oldest_wait_seconds": 12,
        },
    }


def test_every_reader_takes_the_admission_headroom_not_the_engine_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared-headroom case: all three readers agree, none returns the engine."""
    stamp = datetime.now(UTC).isoformat()
    document = _document(stamp)
    path = _write(tmp_path, document)

    with _control(monkeypatch, "flight-engine-headroom"):
        file_report = read_lane_document_file(path)
        carry = dispatch_module._lane_reading_carry(document)
        probe = flight_module._probe_lane_document({"lane_document": str(path)})

    # The shared reader resolves the gate.
    assert file_report["headroom"] == -3
    assert file_report["engine_headroom"] == 6
    # The dispatch carry takes the same figure through the shared reader.
    assert carry["headroom"] == -3
    # The flight probe takes it too; under the mutation this is the engine 6 and
    # the case fails exactly here.
    assert probe["lane_headroom"] == -3

    assert file_report["headroom"] == carry["headroom"] == probe["lane_headroom"]


def test_readers_fall_back_to_the_router_gate_arithmetic(
    tmp_path: Path,
) -> None:
    """No ``admission`` block: the gate arithmetic supplies the figure."""
    stamp = datetime.now(UTC).isoformat()
    document = {
        "headroom": 6,
        "state": "measured",
        "observed_at": stamp,
        "router_generation_gate": {"width": 8, "in_flight": 6, "waiting": 1},
    }
    path = _write(tmp_path, document)

    file_report = read_lane_document_file(path)
    carry = dispatch_module._lane_reading_carry(document)
    probe = flight_module._probe_lane_document({"lane_document": str(path)})

    assert file_report["engine_headroom"] == 6
    assert file_report["headroom"] == 1
    assert carry["headroom"] == 1
    assert probe["lane_headroom"] == 1
    assert file_report["headroom"] == carry["headroom"] == probe["lane_headroom"]


def test_a_document_without_a_gate_keeps_the_engine_reading(tmp_path: Path) -> None:
    """No gate block: the readers report the engine figure as they always did."""
    stamp = datetime.now(UTC).isoformat()
    document = {
        "headroom": 40,
        "state": "measured",
        "observed_at": stamp,
        "suggested_shelf_life_seconds": 3600,
    }
    path = _write(tmp_path, document)

    file_report = read_lane_document_file(path)
    carry = dispatch_module._lane_reading_carry(document)
    probe = flight_module._probe_lane_document({"lane_document": str(path)})

    assert file_report["headroom"] == 40
    assert carry["headroom"] == 40
    assert probe["lane_headroom"] == 40
    assert file_report["admission_headroom"] == UNKNOWN


def test_an_open_and_a_congested_gate_give_distinct_reasons() -> None:
    """A reason that reads identically for both verdicts cannot be acted on."""
    base = {
        "headroom": 6,
        "state": "measured",
        "observed_at": datetime.now(UTC).isoformat(),
    }
    open_document = {
        **base,
        "router_generation_gate": {"width": 8, "in_flight": 1, "waiting": 0},
    }
    congested_document = {
        **base,
        "router_generation_gate": {"width": 8, "in_flight": 8, "waiting": 2},
    }

    opened = read_lane_document(open_document)
    congested = read_lane_document(congested_document)

    assert opened["admission_verdict"] == "open"
    assert congested["admission_verdict"] == "congested"
    assert opened["admission_reason"] != congested["admission_reason"]
    assert "open" in opened["admission_reason"]
    assert "congested" in congested["admission_reason"]
