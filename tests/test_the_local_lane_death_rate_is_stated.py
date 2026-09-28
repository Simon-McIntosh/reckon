"""The local lane's death rate is stated as a number the fence can be gated on.

The rate decides whether a dispatch fence refuses or merely warns, so the file
has to be readable by a gate rather than only by a human: the block names its
window, its population and its count, and it carries a run whose death the
parser saw.

A count alone would pass whether or not the classifier can see anything. The
same file therefore carries both directions on one run — an attempt the parser
reads as dead and a later attempt of the same run it reads as completed — so a
reader anchored on the wrong record shape reports itself rather than a lane
that kills everything it is given.
"""

from __future__ import annotations

import json
from pathlib import Path

DATA = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "research"
    / "data"
    / "local-lane-deaths.json"
)


def _stated() -> dict:
    """The committed measurement, loaded whole from the one file under test."""
    return json.loads(DATA.read_text(encoding="utf-8"))


def test_the_before_block_states_a_non_zero_death_count() -> None:
    before = _stated()["before"]
    deaths = before["deaths"]
    assert deaths["at_review_effort"] > 0, "a zero death count is not a measurement"
    assert deaths["view"] == "attempt"
    assert before["population"]["review_attempts"] >= deaths["at_review_effort"]
    assert before["window"]["start"] and before["window"]["end"]


def test_the_before_block_names_the_lane_and_the_effort_it_measured() -> None:
    stated = _stated()
    before = stated["before"]
    assert stated["lane"]["backend"]
    assert stated["lane"]["model"]
    assert before["review_effort"], "the rate is only meaningful at a named effort"


def test_the_positive_control_is_present_and_classified_as_a_death() -> None:
    control = _stated()["before"]["positive_control"]
    assert control is not None, "a death count with no control is unaimed"
    assert control["run_id"]
    assert control["classification"] == "dead"
    assert control["has_result_record"] is False
    assert control["last_record_type"]


def test_the_same_run_also_carries_an_attempt_the_parser_read_as_completed() -> None:
    stated = _stated()
    control = stated["before"]["positive_control"]
    completion = stated["before"]["completion_control"]
    assert completion is not None, (
        "without a completion the classifier's negative is unread: a reader that "
        "finds no result record anywhere reports every run dead and passes the "
        "positive-control assertion"
    )
    assert completion["classification"] == "completed"
    assert completion["has_result_record"] is True
    assert completion["run_id"] == control["run_id"]


def test_an_after_block_carries_the_same_shape_when_one_exists() -> None:
    stated = _stated()
    after = stated.get("after")
    if after is None:
        assert stated["after_state"] == "not_produced"
        assert stated["after_unavailable_reason"], (
            "an unstated after-rate must say what it is waiting on"
        )
        return
    assert after["deaths"]["at_review_effort"] > 0
    control = after["positive_control"]
    assert control is not None
    assert control["classification"] == "dead"
    assert control["has_result_record"] is False