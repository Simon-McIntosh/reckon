"""A hold records the exact request Jev answered, so a replay sends it again.

A hold row carried only ``confidence``, ``held_at``, ``node``, ``plan``,
``project`` and ``reasons``, so a replay had to rebuild the state from run
records, and two such rebuilds disagreed with each other and with the recorded
hold probability. Neither rebuild was what Jev saw. A hold now stores the state
and the questions it was shown, plus the template digest, and the row lets a
replay rebuild both byte-identically. The digest is what a reader compares: a
row that has been edited, or a payload reference that has been lost, is refused
rather than replayed as a different request.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from reckon.crew.node import TaskNode
from reckon.crew.picker import PickRequest, lane_context, pick
from reckon.crew.picker.outcomes import (
    HOLD_LOG_DIR,
    HOLD_LOG_FILENAME,
    HOLD_PAYLOAD_INLINE_LIMIT,
    hold_request_digest,
    record_picker_hold,
    replay_hold,
)
from reckon.crew.picker.types import Candidate
from reckon.crew.runs import crew_home

PROJECT = "reckon"

#: A config with one local backend, so a pick offers one option and the hold is
#: unambiguous over exactly the keys that option names.
CONFIG = {
    "default_backend": "clive",
    "local_backend": "clive",
    "backends": {
        "clive": {
            "launch": "cli",
            "command": "clive",
            "model": "local-model",
            "effort": "high",
        }
    },
}


@pytest.fixture(autouse=True)
def _isolated_lane(tmp_path, monkeypatch):
    """Keep the state render off the host's live lane document and live runs.

    The crew home is redirected at a temporary path too, so a hold is written
    under the test's own home rather than the operator's.
    """

    lane = tmp_path / "lane.json"
    monkeypatch.setenv("RECKON_LOCAL_LANE_DOCUMENT", str(lane))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(lane_context, "list_live", list)
    monkeypatch.setattr("reckon.crew.picker._record_pick_timings", lambda *a, **k: None)


def _candidate(backend: str = "clive", family: str = "local") -> Candidate:
    return Candidate(
        backend=backend,
        family=family,
        model="m",
        effort="high",
        local=True,
        availability="served",
        utilisation_pct=None,
        burn_multiple=None,
        pace_allowance=None,
        resets_at=None,
        worker_slots=None,
        congestion=None,
        outcomes={"passed": 0, "failed": 0, "not-run": 0, "unknown": 0},
    )


def _hold_pick(tmp_path, *, probability: float = 0.84, padded: str = ""):
    """Run one pick whose stub client holds, capturing what the client saw.

    ``padded`` lengthens the node's goal so the rendered state crosses the
    inline limit, which exercises the store-by-reference path without inventing
    a payload the picker would never render.
    """

    offered = [_candidate()]
    seen: dict[str, object] = {}

    def snapshotter(request, config, repo, **kwargs):
        return offered

    def caller(state, questions, **kwargs):
        seen["state"] = state
        seen["questions"] = questions
        criteria = questions["route"]["criteria"]
        options = [key for key in criteria if key != "hold"]
        share = (1.0 - probability) / len(options)
        probabilities = dict.fromkeys(options, share)
        probabilities["hold"] = probability
        return {
            "model": "stub",
            "usage": {"cost": 0.0},
            "answers": {
                "route": {
                    "choice": "hold",
                    "confidence": 0.66,
                    "probabilities": probabilities,
                }
            },
        }

    selection = pick(
        PickRequest(
            PROJECT,
            TaskNode(
                id="held-node",
                goal="Read the picker state" + padded,
                plan="",
                section="",
                role="implement",
                spec_level="guided",
                done_when="The hold is recorded",
            ),
        ),
        CONFIG,
        repo=tmp_path,
        records=[],
        budget_snapshot={},
        snapshotter=snapshotter,
        caller=caller,
    )
    return selection, seen


def _crew_hold_log(project: str = PROJECT):
    return crew_home() / HOLD_LOG_DIR / project / HOLD_LOG_FILENAME


def _hold_rows() -> list[dict]:
    path = _crew_hold_log()
    return [json.loads(line) for line in path.read_text().splitlines()]


def _record(tmp_path, selection) -> dict:
    docs = tmp_path / "docs"
    record_picker_hold(
        project=PROJECT,
        node="held-node",
        plan="",
        selection=selection.as_dict(),
        reason="picker selected hold",
    )
    return docs


def test_hold_row_carries_the_exact_request(tmp_path):
    """The persisted row stores the state, the questions and both digests."""

    selection, _ = _hold_pick(tmp_path)
    assert selection.action == "hold"
    _record(tmp_path, selection)
    rows = _hold_rows()
    assert len(rows) == 1
    row = rows[0]

    # The digest is over the same state and questions the selection carries, so
    # the row and the selection agree by construction.
    assert row["state"] == selection.rendered_state
    assert row["questions"] == selection.questions
    assert row["template_digest"] == selection.template_digest
    assert row["template_digest"] != ""
    assert row["request_digest"] == hold_request_digest(
        selection.rendered_state, selection.questions
    )


def test_replay_rebuilds_byte_identical_state_and_questions(tmp_path):
    """A replay of the row reproduces the request Jev was actually shown."""

    selection, seen = _hold_pick(tmp_path)
    _record(tmp_path, selection)
    row = _hold_rows()[0]

    replay = replay_hold(row, project=PROJECT)
    # Byte-identical: the state string, and the questions mapping, are the ones
    # the client saw when it answered.
    assert replay["state"].encode("utf-8") == selection.rendered_state.encode("utf-8")
    assert json.loads(replay["state"]) == seen["state"]
    assert replay["questions"] == seen["questions"]
    # Compared by digest, the replay names the recorded request exactly.
    assert replay["request_digest"] == row["request_digest"]
    assert replay["template_digest"] == row["template_digest"]


def test_oversized_request_is_stored_by_reference_and_still_replays(tmp_path):
    """A request above the inline limit is written beside the row and referenced."""

    selection, seen = _hold_pick(tmp_path, padded="x" * (HOLD_PAYLOAD_INLINE_LIMIT * 2))
    _record(tmp_path, selection)
    row = _hold_rows()[0]

    assert row["payload_bytes"] > HOLD_PAYLOAD_INLINE_LIMIT
    assert "payload_ref" in row and "state" not in row
    assert (_crew_hold_log().parent / row["payload_ref"]).is_file()

    replay = replay_hold(row, project=PROJECT)
    assert json.loads(replay["state"]) == seen["state"]
    assert replay["questions"] == seen["questions"]
    assert replay["request_digest"] == row["request_digest"]


def test_a_tampered_row_is_refused_by_its_digest(tmp_path):
    """An edited state fails the digest rather than replaying a different request."""

    selection, _ = _hold_pick(tmp_path)
    _record(tmp_path, selection)
    row = _hold_rows()[0]
    row["state"] = row["state"] + " "

    with pytest.raises(ValueError, match="digest mismatch"):
        replay_hold(row, project=PROJECT)


def test_the_recorded_size_of_a_hold_is_measurable(tmp_path):
    """The row reports the serialized size of its request, and the caller can read it."""

    selection, _ = _hold_pick(tmp_path)
    _record(tmp_path, selection)
    row = _hold_rows()[0]
    assert row["payload_bytes"] == len(
        json.dumps(
            {"state": row["state"], "questions": row["questions"]},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def test_record_carries_a_utc_timestamp_and_reason(tmp_path):
    """The row keeps the fields an existing reader already relies on."""

    selection, _ = _hold_pick(tmp_path)
    _record(tmp_path, selection)
    row = _hold_rows()[0]
    assert row["project"] == PROJECT
    assert row["node"] == "held-node"
    assert row["reasons"] == ["picker selected hold"]
    assert datetime.fromisoformat(row["held_at"]).tzinfo is not None
