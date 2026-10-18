"""Every model of a lane gets its own row in the pick's state.

A lane's models share one backend name, so a candidate table keyed by the bare
backend name keeps only the last model offered under it: a pick at implement
offers several options and renders fewer rows, and a model that is offered but
has no row can never be reached. The candidate and return-time tables are keyed
by the same option key the questions use, so each criteria key finds exactly one
row and one return-time block, and a model's own outcomes reach its own row.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckon import flight
from reckon.crew.node import TaskNode
from reckon.crew.picker import PickRequest, prompts, snapshot

#: The models one codex lane declares, each with its own model id and effort, so
#: the pair -- not the shared backend name -- is what tells them apart.
CODEX_MODELS = {
    "sol": {"model": "gpt-6-sol", "effort": "high"},
    "astra": {"model": "gpt-6-astra", "effort": "xhigh"},
    "luna": {"model": "gpt-6-luna", "effort": "xhigh"},
}


def _request() -> PickRequest:
    return PickRequest(
        "example",
        TaskNode(
            id="work",
            goal="weigh each model of a lane",
            plan="",
            role="implement",
            spec_level="guided",
            done_when="every model renders its own row",
        ),
        capability={},
        estimated_context=100,
    )


def _row(backend: str, model: str, gate: str) -> dict[str, str]:
    return {
        "backend": backend,
        "agent": {"model": model},
        "role": "implement",
        "spec_level": "guided",
        "gate": gate,
        "completed_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
    }


def _offered(tmp_path: Path) -> list:
    """Resolve a flight with a multi-model lane and one modelless backend.

    The codex lane declares three models, each with its own model id and effort;
    a second backend declares no model and belongs to a lane whose name differs
    from its own, so its option key is ``<lane>:<backend>``. Two ledger rows
    record passed outcomes for one codex model and none for another, so the rows
    can be told apart by the history they carry.
    """

    overrides = {
        "lanes": {
            "codex": {
                "launch": "cli",
                "command": "codex",
                "default_model": "sol",
                "models": dict(CODEX_MODELS),
            }
        },
        "backends": {
            "codex": {"launch": "cli", "command": "codex"},
            "claude_lane": {"launch": "cli", "command": "claude"},
        },
    }
    absent = tmp_path / "absent.yaml"
    return flight.resolve(
        overrides=overrides,
        host_path=absent,
        project_path=absent,
        catalogue_path=absent,
    ).config


def _render(tmp_path: Path, monkeypatch) -> tuple[dict, dict, list]:
    """Pick through the real scan, and render the state and questions it builds."""

    config = _offered(tmp_path)
    monkeypatch.setattr(snapshot.routing, "_estimated_hours", lambda *a: 1)
    monkeypatch.setattr(
        snapshot.routing, "_competence_verdict", lambda **k: {"allowed": True}
    )
    monkeypatch.setattr(snapshot.routing, "_context_fit_verdict", lambda **k: None)
    monkeypatch.setattr(snapshot, "_dispatch_lane_gate", lambda *a: {"state": "open"})
    monkeypatch.setattr(
        snapshot.resumption,
        "probe_lane_availability",
        lambda *a, **k: {"status": "served"},
    )
    records = [
        _row("codex", "gpt-6-astra", "passed"),
        _row("codex", "gpt-6-astra", "passed"),
    ]
    offered = snapshot.candidates(
        _request(),
        config,
        Path(tmp_path),
        records=records,
        verdict_inputs={},
        budget_snapshot={"backends": [], "groups": []},
    )
    offered = [candidate for candidate in offered if not candidate.reasons]
    state = json.loads(
        prompts.render(
            "state.jinja",
            node=_request().node,
            capability={},
            estimated_context=100,
            comment="",
            candidates=offered,
            project="reckon",
        )
    )
    questions = json.loads(prompts.render("questions.jinja", candidates=offered))
    return state, questions, offered


def test_candidate_and_return_time_keys_equal_the_criteria_keys(tmp_path, monkeypatch):
    """Each criteria key finds exactly one candidate row and one return-time block."""

    state, questions, _ = _render(tmp_path, monkeypatch)
    keys = set(questions["route"]["criteria"]) - {"hold"}

    assert keys, "the pick offered no criteria to weigh"
    assert set(state["candidates"]) == keys
    assert set(state["return_times"]) == keys


def test_each_codex_model_has_its_own_row(tmp_path, monkeypatch):
    """A lane's three models render three rows, each carrying its own model."""

    state, _, _ = _render(tmp_path, monkeypatch)
    codex = {
        key: row for key, row in state["candidates"].items() if row["lane"] == "codex"
    }

    assert len(codex) == 3
    assert {row["model"] for row in codex.values()} == {
        "gpt-6-sol",
        "gpt-6-astra",
        "gpt-6-luna",
    }


def test_a_modelless_backend_is_keyed_by_its_option_key(tmp_path, monkeypatch):
    """A backend with no model whose lane differs from its name keys by the pair."""

    state, _, offered = _render(tmp_path, monkeypatch)
    modelless = next(candidate for candidate in offered if candidate.model is None)

    assert modelless.family != modelless.backend
    assert f"{modelless.family}:{modelless.backend}" in state["candidates"]
    assert f"{modelless.family}:{modelless.backend}" in state["return_times"]


def test_a_models_outcomes_reach_its_own_row(tmp_path, monkeypatch):
    """Two passed outcomes for astra and none for sol land on their own rows."""

    state, _, _ = _render(tmp_path, monkeypatch)
    rows = [row for row in state["candidates"].values() if row["lane"] == "codex"]
    by_model = {row["model"]: row for row in rows}

    assert by_model["gpt-6-astra"]["outcomes"]["passed"] == 2
    assert by_model["gpt-6-sol"]["outcomes"]["passed"] == 0
    assert by_model["gpt-6-luna"]["outcomes"]["passed"] == 0
