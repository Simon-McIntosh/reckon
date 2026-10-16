"""A replayed pick weighs the coordinator's assessment the original pick weighed.

``reckon crew dispatch --assess KEY=VALUE`` records the coordinator's own reading
of a node's difficulty on the run's ``node_definition``, and a live pick renders
it as ``node.orchestrator_assessment`` beside the declared capability. The replay
rebuilt its ``PickRequest`` from that same definition without the reading, so a
replay meant to show what Jev saw showed the declared capability alone. These
tests hold the replay to carrying the recorded assessment through to the state it
renders.

The declared negative control -- dropping the field from the rebuilt request, by
removing the ``orchestrator_assessment`` argument in ``replay.py`` -- fails the
first assertion: the rendered ``node`` then carries no such key.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from reckon import ledger
from reckon.crew.picker import replay as replay_module
from reckon.crew.picker.types import Candidate

NOW = datetime.now(UTC)
BACKEND = "clive"
OPTION_KEY = "local:local-model:clive"
ASSESSMENT = {"reasoning": "deep"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temporary config home, so the replay composes its view off real files."""

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    return tmp_path


def _config():
    return {
        "default_backend": BACKEND,
        "local_backend": BACKEND,
        "backends": {
            BACKEND: {
                "launch": "cli",
                "command": "clive",
                "model": "local-model",
                "sandbox": "worktree-full",
                "time_budget": "25m",
            }
        },
        "roles": {"implement": {}},
        "review_excluded_backends": [],
    }


def _candidate() -> Candidate:
    return Candidate(
        backend=BACKEND,
        family="local",
        model="local-model",
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


def _record(run_id: str, definition: dict) -> dict:
    """One committed run record carrying an authored node definition."""

    return ledger.build_record(
        run_id=run_id,
        plan="replay-assessment-fixture",
        gate="passed",
        project="fixture",
        node=run_id,
        section="dispatch",
        role="implement",
        spec_level="exact",
        session="session",
        backend=BACKEND,
        agent={"model": "local-model"},
        dispatched_at=NOW.isoformat(),
        completed_at=NOW.isoformat(),
        node_definition=definition,
    )


def _base_definition() -> dict:
    return {
        "goal": "replay the recorded pick",
        "done_when": "the assessment reaches the rendered state",
        "capability": {"requirements": {"reasoning": "standard"}},
    }


def _replay_rendering(monkeypatch, repo, config, records, *, count, seen):
    """Replay ``records`` with the picker's own render left real.

    ``pick`` is wrapped, not replaced: the replay's dataset read, view
    composition and snapshotter build run for real, and the real pick renders the
    state. Only the two boundaries a test cannot reach -- the candidate scan and
    the model client -- are stubbed, so the state captured is the state the
    replayed pick actually produced.
    """

    real_pick = replay_module.pick

    def snapshotter(*_a, **_k):
        return [_candidate()]

    def caller(state, _questions, **_kwargs):
        seen.append(state)
        return {
            "model": "stub",
            "usage": {},
            "answers": {
                "route": {
                    "choice": OPTION_KEY,
                    "confidence": 0.9,
                    "probabilities": {OPTION_KEY: 0.9, "clive:local-model:clive": 0.1},
                }
            },
        }

    def stubbed_pick(request, config, **kwargs):
        kwargs["snapshotter"] = snapshotter
        kwargs.setdefault("caller", caller)
        return real_pick(request, config, **kwargs)

    monkeypatch.setattr(replay_module, "pick", stubbed_pick)
    monkeypatch.setattr(replay_module.ledger, "runs", lambda *a, **k: records)
    return replay_module.replay("fixture", count, config, repo=repo)


def _rendered_node(payload: dict, index: int = 0) -> dict:
    row = payload["rows"][index]
    return json.loads(row["selection"]["rendered_state"])["node"]


def test_a_replay_carries_the_recorded_assessment(home, monkeypatch):
    """A recorded assessment renders in the replayed request's node block."""

    config = _config()
    definition = _base_definition()
    definition["orchestrator_assessment"] = dict(ASSESSMENT)
    records = [_record("assessed", definition)]

    seen: list[dict] = []
    payload = _replay_rendering(monkeypatch, home, config, records, count=1, seen=seen)

    assert _rendered_node(payload)["orchestrator_assessment"] == ASSESSMENT
    # The pick's client saw the same state the row records, so the reading
    # achieved the facts Jev weighed and not only the report.
    assert seen[0]["node"]["orchestrator_assessment"] == ASSESSMENT
    assert payload["rows"][0]["run_id"] == "assessed"


def test_a_replay_without_an_assessment_renders_no_key(home, monkeypatch):
    """A recorded run with no assessment renders no node.orchestrator_assessment."""

    config = _config()
    records = [_record("unassessed", _base_definition())]

    seen: list[dict] = []
    payload = _replay_rendering(monkeypatch, home, config, records, count=1, seen=seen)

    assert "orchestrator_assessment" not in _rendered_node(payload)
    assert "orchestrator_assessment" not in seen[0]["node"]
