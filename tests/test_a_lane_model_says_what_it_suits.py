"""A lane model declares what work it suits, and its row carries that statement.

A model's row names the work the operator intends it for, so a pick can weigh
the declared standing of a model with no measured outcomes beside the models
that have some. The declaration is read from the same merged per-model mapping
as the model's effort: a model's own ``suits`` overrides one declared on its
lane block, and a lane-level ``suits`` reaches a model that declares none. It is
declared, never measured, and nothing routes or refuses on it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import flight
from reckon.crew.node import TaskNode
from reckon.crew.picker import PickRequest, prompts, snapshot
from reckon.flight import FlightConfigError

#: sol and luna each declare their own statement; astra declares none, so it
#: reads the codex lane block's statement. The second lane declares none, and
#: the outside backend declares its own on its block.
CODEX_LANE_SUITS = "the codex lane's default, for any model that declares none"
SOL_SUITS = "guided implementation and full reviews"
LUNA_SUITS = "well-defined, less difficult work and reviews"
SOLO_SUITS = "a backend outside any lane, declared on its own block"


def _request() -> PickRequest:
    return PickRequest(
        "example",
        TaskNode(
            id="work",
            goal="agree a pick with its declared suits",
            plan="",
            role="implement",
            spec_level="guided",
            done_when="each row carries the statement its model or lane declares",
        ),
        capability={},
        estimated_context=100,
    )


def _overrides() -> dict:
    """A codex lane, a second lane declaring no suits, and an outside backend.

    The codex lane declares sol (its own statement), luna (its own statement)
    and astra (none, so the lane block's statement applies). The ``claude_lane``
    lane declares no suits on either its block or its model, so its row carries
    none. The ``solo`` backend sits outside every lane and declares its own.
    """

    return {
        "lanes": {
            "codex": {
                "launch": "cli",
                "command": "codex",
                "default_model": "astra",
                "suits": CODEX_LANE_SUITS,
                "models": {
                    "sol": {"model": "gpt-6-sol", "effort": "high", "suits": SOL_SUITS},
                    "astra": {"model": "gpt-6-astra", "effort": "xhigh"},
                    "luna": {
                        "model": "gpt-6-luna",
                        "effort": "xhigh",
                        "suits": LUNA_SUITS,
                    },
                },
            },
            "claude_lane": {
                "launch": "cli",
                "command": "claude",
                "default_model": "opus",
                "models": {"opus": {"model": "claude-opus-5"}},
            },
        },
        "backends": {
            "codex": {"launch": "cli", "command": "codex"},
            "claude_lane": {"launch": "cli", "command": "claude"},
            "solo": {
                "launch": "cli",
                "command": "codex",
                "suits": SOLO_SUITS,
            },
        },
    }


def _render(tmp_path: Path, monkeypatch) -> tuple[dict, dict, list]:
    """Pick through the real scan, and render the state and questions it builds."""

    config = _resolve(tmp_path, _overrides())
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
    offered = snapshot.candidates(
        _request(),
        config,
        Path(tmp_path),
        records=[],
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


def _resolve(tmp_path: Path, overrides: dict):
    absent = tmp_path / "absent.yaml"
    return flight.resolve(
        overrides=overrides,
        host_path=absent,
        project_path=absent,
        catalogue_path=absent,
    ).config


def _rows_by_model(state: dict, lane: str) -> dict[str, dict]:
    return {
        row["model"]: row for row in state["candidates"].values() if row["lane"] == lane
    }


def test_a_models_own_suits_reaches_its_row(tmp_path, monkeypatch):
    """sol and luna each render the statement their model declares."""

    state, _, _ = _render(tmp_path, monkeypatch)
    codex = _rows_by_model(state, "codex")

    assert codex["gpt-6-sol"]["suits"] == SOL_SUITS
    assert codex["gpt-6-luna"]["suits"] == LUNA_SUITS


def test_a_model_declaring_none_reads_its_lane_statement(tmp_path, monkeypatch):
    """astra declares no suits, so the lane block's statement is what it carries."""

    state, _, _ = _render(tmp_path, monkeypatch)
    codex = _rows_by_model(state, "codex")

    assert codex["gpt-6-astra"]["suits"] == CODEX_LANE_SUITS


def test_a_lane_declaring_none_carries_no_suits_key(tmp_path, monkeypatch):
    """The second lane declares no suits anywhere, so its row carries no key."""

    state, _, _ = _render(tmp_path, monkeypatch)
    claude = _rows_by_model(state, "claude")

    assert claude, "the second lane rendered no row to inspect"
    assert all("suits" not in row for row in claude.values())


def test_a_backend_outside_any_lane_carries_its_own_suits(tmp_path, monkeypatch):
    """A suits on a backend block outside any lane reaches that backend's row."""

    state, _, offered = _render(tmp_path, monkeypatch)
    modelless = next(candidate for candidate in offered if candidate.model is None)

    assert modelless.backend == "solo"
    row = next(row for row in state["candidates"].values() if row["backend"] == "solo")
    assert row["suits"] == SOLO_SUITS


def test_a_non_string_suits_is_refused_by_flight_validation(tmp_path):
    """A suits that is not a string is refused at the configuration boundary."""

    overrides = {
        "backends": {
            "odd": {"launch": "cli", "command": "codex", "suits": 123},
        }
    }

    with pytest.raises(FlightConfigError) as excinfo:
        _resolve(tmp_path, overrides)

    assert excinfo.value.key_path == "backends.odd.suits"


def test_the_glossary_defines_suits_and_the_instructions_name_it(tmp_path, monkeypatch):
    """The rendered glossary defines suits, and the instructions name it."""

    _, questions, _ = _render(tmp_path, monkeypatch)

    assert "suits" in questions["route"]["glossary"]
    assert "suits" in questions["route"]["instructions"]
