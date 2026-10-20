"""A new model id starts from its key's record.

Every outcome tally a pick reads is keyed by the backend and the exact model
id, so when a lane's model key is re-pointed to a new generation -- ``sol``
from ``gpt-6-sol`` to ``gpt-6.1-sol`` -- that key's whole record falls out of
the pick and the new id starts from nothing, although the key's earlier ids
carry hundreds of runs. A lane-model candidate whose own model id has fewer
recorded outcomes than a threshold therefore also carries a prior block: the
tallies of the runs recorded under the same lane and model key at a different
model id, naming each of those ids.

Runs are matched by the ``lane`` and ``model_key`` each run recorded, read
through :func:`ledger.normalize_identity`, so a recorded pair is authoritative
and a row is never matched by today's lane default. At or above the threshold a
candidate carries no prior block, so a candidate with enough history of its own
renders exactly as it did before prior blocks existed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml

from reckon.crew import routing
from reckon.crew.node import TaskNode
from reckon.crew.picker import prompts, snapshot
from reckon.crew.picker.types import PickRequest

#: The key ``sol`` is re-pointed to ``gpt-6.1-sol``; ``astra`` stays where it
#: was. The catalogue is the resolver's own view of what each name means now,
#: so a row whose id it no longer declares is exactly the case this node reads.
CATALOGUE = {
    "version": 1,
    "lanes": {
        "codex": {
            "default_model": "sol",
            "models": {
                "sol": {"model": "gpt-6.1-sol", "effort": "high"},
                "astra": {"model": "gpt-6-astra", "effort": "xhigh"},
            },
        }
    },
}

CONFIG = {
    "local_backend": "clive",
    "roles": {"implement": {}, "review": {}},
    "backends": {
        "clive": {"model": "m-clive", "lane": "clive", "model_key": "flash"},
        "codex": {"model": "gpt-6.1-sol", "lane": "codex", "model_key": "sol"},
    },
    "lanes": {
        "codex": {
            "default_model": "sol",
            "models": {
                "sol": {"model": "gpt-6.1-sol", "effort": "high"},
                "astra": {"model": "gpt-6-astra", "effort": "xhigh"},
            },
        }
    },
}

ZERO = {"passed": 0, "failed": 0, "not-run": 0, "unknown": 0}


@pytest.fixture(autouse=True)
def catalogue(monkeypatch, tmp_path):
    """Point the resolver at a catalogue whose ``sol`` key has moved on."""

    path = tmp_path / "catalogue.yaml"
    path.write_text(yaml.safe_dump(CATALOGUE), encoding="utf-8")
    monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(path))
    return path


@pytest.fixture
def picker_env(monkeypatch, tmp_path):
    """Isolate candidates() from the ledger, the repository and the network."""

    monkeypatch.setattr(snapshot, "estimated_context_tokens", lambda *a, **k: 0)
    monkeypatch.setattr(snapshot, "_fit", lambda request, name, backend, repo, **k: [])
    monkeypatch.setattr(
        snapshot,
        "_serving_observation",
        lambda backend: {"status": "served", "detail": ""},
    )
    monkeypatch.setattr(
        routing, "_estimated_hours", lambda *a, **k: (None, "unavailable")
    )
    return tmp_path


def _request() -> PickRequest:
    return PickRequest(
        project="reckon",
        node=TaskNode(
            id="key-record-node",
            goal="start a new model id from its key's record",
            plan="lane-and-model-routing",
            role="implement",
            spec_level="exact",
            done_when="the prior block carries the key's earlier ids",
            write_paths=[],
        ),
        session="s",
        capability={},
    )


def _row(
    *,
    model: str,
    gate: str,
    model_key: str = "sol",
    backend: str = "codex",
    days_ago: float = 1.0,
) -> dict[str, Any]:
    """One ledger row as the ledger stores it: the pair beside the id it ran as."""

    return {
        "backend": backend,
        "lane": backend,
        "model_key": model_key,
        "agent": {"model": model},
        "gate": gate,
        "role": "implement",
        "spec_level": "exact",
        "completed_at": (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(),
    }


def _candidates(rows: list[dict[str, Any]], picker_env):
    return snapshot.candidates(
        _request(),
        CONFIG,
        picker_env,
        records=rows,
        budget_snapshot={"backends": [], "model": "", "groups": []},
        verdict_inputs={},
        cached_only=True,
    )


def _by_model(candidates) -> dict[tuple[str, str], Any]:
    return {(candidate.backend, candidate.model): candidate for candidate in candidates}


def _moved_key_fixture() -> list[dict[str, Any]]:
    """Twenty runs under the key's old id, two under the id it now points at."""

    return [
        *(_row(model="gpt-6-sol", gate="passed") for _ in range(17)),
        *(_row(model="gpt-6-sol", gate="failed") for _ in range(2)),
        _row(model="gpt-6-sol", gate="not-run"),
        _row(model="gpt-6.1-sol", gate="passed"),
        _row(model="gpt-6.1-sol", gate="failed"),
    ]


def _assert_uniform(prior: dict[str, Any] | None) -> None:
    """A prior block that is present must say what it rests on."""

    assert prior is not None
    assert prior["model_ids"]


def test_the_new_id_counts_its_own_runs_and_priors_the_keys_record(picker_env):
    """The two runs under gpt-6.1-sol are its own; the twenty under gpt-6-sol
    are its key's earlier record and are named as such."""

    by_model = _by_model(_candidates(_moved_key_fixture(), picker_env))
    sol = by_model[("codex", "gpt-6.1-sol")]

    assert sol.outcomes == {"passed": 1, "failed": 1, "not-run": 0, "unknown": 0}
    _assert_uniform(sol.prior_outcomes)
    assert sol.prior_outcomes == {
        "passed": 17,
        "failed": 2,
        "not-run": 1,
        "unknown": 0,
        "model_ids": ["gpt-6-sol"],
    }


def test_enough_own_outcomes_means_no_prior_block(picker_env):
    """At the threshold of its own outcomes the candidate stands on them alone,
    so its rendered state is unchanged from before prior blocks existed."""

    rows = [
        *(_row(model="gpt-6-sol", gate="passed") for _ in range(20)),
        *(_row(model="gpt-6.1-sol", gate="passed") for _ in range(4)),
        _row(model="gpt-6.1-sol", gate="failed"),
    ]
    sol = _by_model(_candidates(rows, picker_env))[("codex", "gpt-6.1-sol")]

    assert sum(sol.outcomes.values()) == 5
    assert sol.prior_outcomes is None


def test_another_lane_gains_nothing_from_this_keys_runs(picker_env):
    """Sol's runs belong to sol's key, so the astra candidate is untouched."""

    by_model = _by_model(_candidates(_moved_key_fixture(), picker_env))
    astra = by_model[("codex", "gpt-6-astra")]

    assert astra.outcomes == ZERO
    assert astra.prior_outcomes is None


def test_a_recorded_key_outranks_the_ids_current_default(picker_env):
    """A row whose recorded key differs from the key its id resolves to now is
    counted under the key it recorded, never under today's default."""

    rows = [
        *(_row(model="gpt-6-sol", gate="passed") for _ in range(3)),
        # Its id is the codex lane's default model id, whose key is ``sol``;
        # the row recorded ``astra``, and the record is what counts.
        _row(model="gpt-6.1-sol", gate="passed", model_key="astra"),
    ]
    by_model = _by_model(_candidates(rows, picker_env))
    sol = by_model[("codex", "gpt-6.1-sol")]
    astra = by_model[("codex", "gpt-6-astra")]

    _assert_uniform(astra.prior_outcomes)
    assert astra.prior_outcomes["model_ids"] == ["gpt-6.1-sol"]
    assert astra.prior_outcomes["passed"] == 1

    _assert_uniform(sol.prior_outcomes)
    assert sol.prior_outcomes["model_ids"] == ["gpt-6-sol"]
    assert sol.prior_outcomes["passed"] == 3


def test_the_state_carries_the_prior_block_and_the_glossary_defines_it(picker_env):
    """Jev reads the block as the key's earlier generations, below the
    candidate's own outcomes, and a candidate standing on its own record
    renders no such block."""

    rows = _moved_key_fixture()
    candidates = _candidates(rows, picker_env)
    state = prompts.build_state(
        node=_request().node,
        capability={},
        estimated_context=0,
        comment="",
        candidates=candidates,
        lane={"return_times": {}, "lanes": {}, "local_lane": {}},
        attempts=0,
    )
    entries = state["candidates"]
    sol_key = prompts.option_key(
        next(c for c in candidates if c.model == "gpt-6.1-sol")
    )
    astra_key = prompts.option_key(
        next(c for c in candidates if c.model == "gpt-6-astra")
    )
    assert entries[sol_key]["prior_outcomes"]["model_ids"] == ["gpt-6-sol"]
    assert entries[sol_key]["prior_outcomes"]["passed"] == 17
    assert "prior_outcomes" not in entries[astra_key]

    glossary = json.loads(prompts.render("questions.jinja", candidates=candidates))[
        "route"
    ]["glossary"]
    assert "prior_outcomes" in glossary
    assert "earlier generations" in glossary["prior_outcomes"]


def test_a_declared_threshold_moves_the_point_a_prior_stops_being_shown(picker_env):
    """The threshold is a flight knob: a candidate with one outcome of its own
    stands on it once the layer declares a threshold of one."""

    rows = [
        _row(model="gpt-6-sol", gate="passed"),
        _row(model="gpt-6.1-sol", gate="passed"),
    ]
    config = {**CONFIG, "routing": {"prior_outcome_threshold": 1}}
    sol = _by_model(
        snapshot.candidates(
            _request(),
            config,
            picker_env,
            records=rows,
            budget_snapshot={"backends": [], "groups": []},
            verdict_inputs={},
            cached_only=True,
        )
    )[("codex", "gpt-6.1-sol")]

    assert sol.outcomes["passed"] == 1
    assert sol.prior_outcomes is None
