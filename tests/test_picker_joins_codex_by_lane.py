"""The picker joins codex runs by lane and labels a document-backed reading.

A run is judged against codex by resolving its backend through the model
catalogue rather than by a name prefix, so the lane's declared name, its models,
an old alias and a model id all reach the codex lane, while a backend whose name
merely begins with ``codex`` does not. A budget reading the paid-lanes document
published carries its own provenance label, so a reader can tell a published
figure from one a run recorded.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import flight
from reckon.crew import paid_lanes
from reckon.crew.node import TaskNode
from reckon.crew.picker import lane_context, outcomes
from reckon.crew.picker.types import Candidate

NOW = datetime(2026, 10, 2, 12, 1, 0, tzinfo=UTC)
STAMP = "2026-10-03T04:00:00Z"

# The repository's own catalogue, the subject these tests resolve names against.
_REAL_CATALOGUE = (
    Path(flight.__file__).resolve().parent.parent
    / "docs"
    / "state"
    / "reckon"
    / "model-catalogue.yaml"
)


@pytest.fixture
def catalogue(monkeypatch):
    """Resolve names against the repository's catalogue.

    The suite isolates every test from the checkout's catalogue so a fixture
    host declares its own, which leaves the resolver unknown for every name;
    these tests are about the catalogue's own names, so they point it back the
    way the catalogue-subject module does.
    """

    assert _REAL_CATALOGUE.is_file()
    monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(_REAL_CATALOGUE))


# Names the model catalogue resolves to the codex lane: the lane's own name, two
# of its models spelled by their legacy backend names, an old alias naming a
# model the lane no longer offers, and a model id.
CODEX_NAMES = ["codex", "codex-astra", "codex-luna", "codex-terra", "gpt-6-astra"]

# Backends whose name begins with ``codex`` but which the catalogue does not
# resolve to the codex lane.
LOOKALIKE_NAMES = ["codexcli", "codex-unknown"]


def _row(backend: str) -> dict:
    """One promoted, picker-routed run on ``backend`` beside a codex offer."""

    return {
        "run_id": backend,
        "node": backend,
        "plan": "sample",
        "role": "implement",
        "spec_level": "guided",
        "backend": backend,
        "route_mode": "picker",
        "gate": "passed",
        "outcome": "",
        "review": {"total": 80},
        "wall_seconds": 100,
        "dispatched_at": STAMP,
        "completed_at": STAMP,
        "picker_selection": {
            "action": "route",
            "backend": backend,
            "confidence": 0.4,
            "latency_ms": 100,
            "offered": [
                {
                    "backend": "codex",
                    "family": "codex",
                    "burn_multiple": 1.5,
                    "pace_allowance": 0.4,
                }
            ],
        },
    }


def _chosen_codex(backend: str) -> int:
    """How many routed runs at one burn level the picker sent to the codex lane."""

    report = outcomes.summarize({"demo": [_row(backend)]}, {})
    chosen = report["metered_spend"]["offered_codex_by_burn"]["1_to_2"]["chosen_codex"]
    codex_runs = report["metered_spend"]["codex_runs"]
    assert len(codex_runs) == chosen
    return chosen


@pytest.mark.parametrize("backend", CODEX_NAMES)
def test_codex_names_reach_the_codex_lane(backend, catalogue):
    assert _chosen_codex(backend) == 1


@pytest.mark.parametrize("backend", LOOKALIKE_NAMES)
def test_lookalike_names_are_not_the_codex_lane(backend, catalogue):
    assert _chosen_codex(backend) == 0


def _candidate(backend: str) -> Candidate:
    return Candidate(
        backend=backend,
        family="codex",
        model="m",
        effort="high",
        local=False,
        availability="served",
        utilisation_pct=None,
        burn_multiple=None,
        pace_allowance=None,
        resets_at=None,
        worker_slots=None,
        congestion=None,
        outcomes={"passed": 0, "failed": 0, "not-run": 0, "unknown": 0},
    )


def _node() -> TaskNode:
    return TaskNode(
        id="n",
        goal="g",
        plan="",
        role="implement",
        spec_level="guided",
        done_when="d",
        time_budget="",
    )


def _snapshot(window_source: str) -> dict:
    """A pre-flight report naming which source spoke for the codex backend.

    The backend state carries the ledger's own vocabulary, as the composed view
    writes it; the report's ``window_sources`` records that the published
    document, not a recorded run, supplied the figure.
    """

    return {
        "backends": [
            {
                "backend": "codex",
                "state": {
                    "source": "ledger",
                    "observed_at": (NOW - timedelta(minutes=5)).isoformat(),
                },
            }
        ],
        "window_sources": {"codex": window_source},
    }


def test_document_backed_reading_carries_its_own_provenance():
    document = lane_context.return_times(
        {},
        _node(),
        [_candidate("codex")],
        budget_snapshot=_snapshot("document"),
        now=NOW,
    )
    recorded = lane_context.return_times(
        {},
        _node(),
        [_candidate("codex")],
        budget_snapshot=_snapshot("recorded"),
        now=NOW,
    )
    assert document["codex"]["budget_source"] == "document"
    assert recorded["codex"]["budget_source"] == "ledger"
    assert document["codex"]["budget_source"] != recorded["codex"]["budget_source"]
    assert document["codex"]["stale"] is False


# --- The published document joins profiles to an account by declaration ---


def _rollout(root: Path, observed: datetime, used_percent: float) -> None:
    """One provider rollout reporting the week's used fraction."""

    folder = root / observed.strftime("%Y/%m/%d")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-{observed.strftime('%H%M%S')}-{used_percent}.jsonl"
    path.write_text(
        json.dumps(
            {
                "timestamp": observed.isoformat(),
                "type": "event_msg",
                "payload": {
                    "rate_limits": {
                        "limit_id": "codex",
                        "primary": {
                            "used_percent": used_percent,
                            "window_minutes": 10_080,
                            "resets_at": int(
                                (observed + timedelta(days=3)).timestamp()
                            ),
                        },
                    }
                },
            }
        )
        + "\n"
    )


def _published_accounts(tmp_path, monkeypatch, backends: dict) -> dict:
    """Publish the document from one fresh rollout and a stub config."""

    now = datetime.now(UTC)
    crew_home = tmp_path / "crew-home"
    _rollout(crew_home / "codex-home" / "sessions", now - timedelta(minutes=2), 42.0)
    monkeypatch.setenv("RECKON_HOME", str(crew_home))
    monkeypatch.setattr(
        flight,
        "resolve",
        lambda *_a, **_k: SimpleNamespace(config={"backends": backends}),
    )
    target = tmp_path / "paid-lanes.json"
    assert paid_lanes.main(["--once", "--path", str(target)]) == 0
    return json.loads(target.read_text())["accounts"]


def test_paid_lanes_joins_profiles_by_catalogue_lane(tmp_path, monkeypatch, catalogue):
    """Profiles the catalogue resolves to the codex lane share its window."""

    accounts = _published_accounts(
        tmp_path,
        monkeypatch,
        {name: {} for name in [*CODEX_NAMES, *LOOKALIKE_NAMES]},
    )
    for name in [*CODEX_NAMES, *LOOKALIKE_NAMES]:
        assert name in accounts
    for name in CODEX_NAMES:
        assert accounts[name]["state"] == "observed"
        assert accounts[name]["windows"]["seven_day"]["utilisation"] == 0.42
    for name in LOOKALIKE_NAMES:
        assert accounts[name]["windows"]["seven_day"]["utilisation"] is None


def test_paid_lanes_joins_profiles_by_budget_group(tmp_path, monkeypatch):
    """A declared budget group joins a profile without a catalogue to read."""

    backends = {
        "codex": {"budget_group": "codex-sub"},
        "codex-astra": {"budget_group": "codex-sub"},
        "codexcli": {},
    }
    accounts = _published_accounts(tmp_path, monkeypatch, backends)
    assert accounts["codex"]["state"] == "observed"
    assert accounts["codex-astra"]["state"] == "observed"
    assert accounts["codexcli"]["windows"]["seven_day"]["utilisation"] is None
