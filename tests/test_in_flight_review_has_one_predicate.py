"""The obligations reader and the review reflex decide in-flight from one rule.

A run's pointer carries the reflex's dispatch record naming the review it
launched. Two readers consult that record and must agree: the reflex consults it
before composing another review, and the obligations reader consults it to
decide whether the run still owes one. If the obligations reader treated a
pointer *path* as the whole answer while the reflex treated a pointer it could
not read as no review, one reader would withhold review-missing for a run the
other considers unreviewed — the coordinator would be told nothing is owed for
exactly the run the reflex is about to review again. Both now call one
predicate, and these tests hold every shape of the record to the same answer.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from reckon.crew import recovery, runs

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "in-flight-fixture"
SESSION = "coordinator-fixture"


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised crew home; the fixture never touches the operator's."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    live = config_home / "live"
    live.mkdir()
    return live


def _write_pointer(run_id: str, record: dict) -> None:
    runs._write_json(runs.pointer_path(run_id), record)


def _source_record(review_run_id: str) -> dict:
    """A scoring run whose pointer records the review the reflex launched."""
    return {
        "run_id": "run-source",
        "project": PROJECT,
        "session": SESSION,
        "process_alive": False,
        "node": {
            "id": "source-node",
            "plan": "fixture-plan",
            "section": "s",
            "write_paths": ["seed.txt"],
        },
        recovery.REVIEW_DISPATCH_FIELD: {
            "run_id": review_run_id,
            "status": "dispatched",
            "head": "",
        },
    }


def _answers(record: dict) -> tuple[bool, bool]:
    """The obligations reader's answer beside the reflex's, as booleans."""
    reader = obligations_module._reflex_review_in_flight(record)
    reflex = bool(recovery._review_in_flight(record))
    return reader, reflex


def test_a_readable_live_pointer_is_in_flight_to_both(crew_home: Path) -> None:
    review = "run-review"
    record = _source_record(review)
    _write_pointer(record["run_id"], record)
    _write_pointer(review, {"run_id": review, "project": PROJECT, "session": SESSION})

    reader, reflex = _answers(record)
    assert reader is True
    assert reflex is True
    # The raw reader feeding review-missing suppression agrees too.
    assert record["run_id"] in obligations_module._live_review_runs(PROJECT, SESSION)


def test_an_absent_pointer_is_not_in_flight_to_either(crew_home: Path) -> None:
    record = _source_record("run-review-absent")
    _write_pointer(record["run_id"], record)

    reader, reflex = _answers(record)
    assert reader is False
    assert reflex is False
    assert obligations_module._live_review_runs(PROJECT, SESSION) == set()


def test_an_unreadable_pointer_is_not_in_flight_to_either(crew_home: Path) -> None:
    """The case the two readers used to split on: a path that exists, unreadable."""
    review = "run-review-unreadable"
    record = _source_record(review)
    _write_pointer(record["run_id"], record)
    # The pointer path exists but holds no object a reader can resolve.
    runs.pointer_path(review).write_text("{ this is not json", encoding="utf-8")
    assert runs.pointer_path(review).exists()

    reader, reflex = _answers(record)
    assert reader is False, (
        "the obligations reader must not count an unreadable pointer"
    )
    assert reflex is False, "the reflex must not count an unreadable pointer"
    assert reader == reflex
    # The run is owed its review again, rather than withheld from review-missing.
    assert obligations_module._live_review_runs(PROJECT, SESSION) == set()


def test_a_non_object_pointer_is_not_in_flight_to_either(crew_home: Path) -> None:
    """A readable file holding a scalar names no run either."""
    review = "run-review-scalar"
    record = _source_record(review)
    _write_pointer(record["run_id"], record)
    runs.pointer_path(review).write_text(json.dumps(42), encoding="utf-8")

    reader, reflex = _answers(record)
    assert reader is False
    assert reflex is False
    assert reader == reflex
