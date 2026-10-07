"""Pressure remains visible to the typed judgment while hard gates still refuse."""

import ast
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from reckon import budget
from reckon.crew.picker import pick, snapshot
from tests import test_picker as fixtures

config = fixtures.config
live_facts = fixtures.live_facts
request_node = fixtures.request_node


def test_candidates_are_offered_under_pressure(
    live_facts, monkeypatch, request_node, config, tmp_path
):
    monkeypatch.setattr(
        snapshot.budget,
        "state_for",
        lambda name, *a, **k: budget.BudgetState(
            name,
            headroom="known",
            utilisation_pct=34,
            burn_multiple=3.4,
            resets_at=(datetime.now(UTC) + timedelta(days=6)).isoformat(),
            source="ledger",
            observed_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        ),
    )
    monkeypatch.setattr(
        snapshot, "_lane", lambda *a: (0, {"waiting": 8}, {"held": True})
    )
    seen = []

    def caller(state, questions, **kwargs):
        seen.append(state)
        return {
            "answers": {
                "route": {
                    "choice": "remote",
                    "confidence": 0.23,
                    "probabilities": {"local": 0.1, "remote": 0.5, "hold": 0.4},
                }
            }
        }

    selection = pick(request_node, config, repo=tmp_path, records=[], caller=caller)
    assert {c["backend"] for c in selection.offered} == {"local", "remote"}
    assert seen[0]["candidates"]["remote"]["burn_multiple"] == 3.4
    assert seen[0]["candidates"]["local"]["worker_slots"] == 0
    assert selection.backend == "remote"
    assert selection.confidence == 0.23


def _answer(choice, candidates, confidence=0.2):
    keys = [*candidates, "hold"]
    return {
        "answers": {
            "route": {
                "choice": choice,
                "confidence": confidence,
                "probabilities": {key: 1 / len(keys) for key in keys},
            }
        }
    }


def test_hold_is_an_option_and_preserves_jevs_confidence(
    live_facts, request_node, config, tmp_path
):
    def caller(state, questions, **kwargs):
        assert set(questions["route"]["criteria"]) == {"local", "remote", "hold"}
        assert "wait for pressure to ease" in questions["route"]["criteria"]["hold"]
        return _answer("hold", state["candidates"], confidence=0.17)

    selection = pick(request_node, config, repo=tmp_path, records=[], caller=caller)
    assert selection.backend is None
    assert selection.model is None
    assert selection.effort is None
    assert selection.action == "hold"
    assert selection.decision_source == "jev"
    assert selection.confidence == 0.17
    assert selection.fallback_reason is None
    assert selection.probabilities["hold"] > 0


def test_only_candidate_still_goes_to_jev(live_facts, request_node, config, tmp_path):
    config["backends"].pop("remote")
    caller = Mock(return_value=_answer("local", ["local"]))
    selection = pick(request_node, config, repo=tmp_path, records=[], caller=caller)
    assert selection.backend == "local"
    caller.assert_called_once()


def test_no_candidates_refuses_without_calling_jev(
    live_facts, monkeypatch, request_node, config, tmp_path
):
    monkeypatch.setattr(
        snapshot, "_dispatch_lane_gate", lambda backend: {"state": "paused"}
    )
    caller = Mock()
    selection = pick(request_node, config, repo=tmp_path, records=[], caller=caller)
    assert selection.backend is None
    assert selection.action == "refuse"
    assert all(c["reasons"] == ["lane-gate: paused"] for c in selection.excluded)
    caller.assert_not_called()


def test_review_node_is_never_offered_a_review_excluded_backend(
    live_facts, request_node, config, tmp_path
):
    """A backend withdrawn from review routing is filtered, not left to Jev."""
    request_node.node.role = "review"
    config["backends"] = {
        "clive": {
            "launch": "cli",
            "command": "clive",
            "model": "local-model",
            "effort": "high",
        },
        "codex": {"launch": "cli", "command": "codex", "model": "remote-model"},
        "codex-astra": {"launch": "cli", "command": "codex", "model": "astra-model"},
    }
    config["local_backend"] = "clive"
    config["default_backend"] = "clive"
    config["roles"] = {"review": {}}
    config["review_excluded_backends"] = ["codex", "codex-astra"]
    seen = []

    def caller(state, questions, **kwargs):
        seen.append(questions["route"]["criteria"])
        return _answer("clive", state["candidates"])

    selection = pick(request_node, config, repo=tmp_path, records=[], caller=caller)
    assert set(seen[0]) == {"clive", "hold"}
    assert {c["backend"] for c in selection.offered} == {"clive"}
    excluded = {c["backend"]: c["reasons"] for c in selection.excluded}
    assert excluded == {
        "codex": ["review-excluded-backend"],
        "codex-astra": ["review-excluded-backend"],
    }


def test_reserve_hold_below_hard_ceiling_remains_offered(
    live_facts, monkeypatch, request_node, config, tmp_path
):
    config["budget"].update(utilisation_ceiling_pct=90, resume_reserve_pct=20)
    monkeypatch.setattr(
        snapshot.budget,
        "state_for",
        lambda name, *a, **k: budget.BudgetState(
            name, headroom="known", utilisation_pct=80
        ),
    )
    options = snapshot.candidates(request_node, config, tmp_path, records=[])
    assert next(c for c in options if c.backend == "remote").reasons == []


@pytest.mark.parametrize("utilisation", [90, 100])
def test_hard_ceiling_refuses_even_when_serving_is_recorded(
    live_facts, monkeypatch, request_node, config, tmp_path, utilisation
):
    config["budget"]["utilisation_ceiling_pct"] = 90
    monkeypatch.setattr(
        snapshot.budget,
        "state_for",
        lambda name, *a, **k: budget.BudgetState(
            name, headroom="known", utilisation_pct=utilisation, availability="served"
        ),
    )
    options = snapshot.candidates(request_node, config, tmp_path, records=[])
    assert all(any(r.startswith("budget-ceiling:") for r in c.reasons) for c in options)


@pytest.mark.parametrize("status", ["refused", "unavailable", "logged-out"])
def test_cached_serving_refusal_is_still_a_hard_gate(
    live_facts, monkeypatch, request_node, config, tmp_path, status
):
    fresh = datetime.now(UTC).isoformat()
    monkeypatch.setattr(
        snapshot.resumption,
        "_read_lane_probe_cache",
        lambda *a: {"status": status, "observed_at": fresh},
    )
    probe = Mock(side_effect=AssertionError("Cached picks must never probe"))
    monkeypatch.setattr(snapshot.resumption, "probe_lane_availability", probe)
    selection = pick(request_node, config, repo=tmp_path, records=[], cached_only=True)
    assert selection.action == "refuse"
    assert all(c["reasons"] == [f"availability: {status}"] for c in selection.excluded)
    probe.assert_not_called()


@pytest.mark.parametrize("cache_state", ["served", "missing", "corrupt"])
def test_cached_only_reads_the_existing_cache_without_writing(
    live_facts, monkeypatch, request_node, config, tmp_path, cache_state
):
    monkeypatch.setattr(snapshot.resumption, "crew_home", lambda: tmp_path / "crew")
    fresh = datetime.now(UTC).isoformat()
    for name in config["backends"]:
        path = snapshot.resumption.lane_probe_cache_path(request_node.project, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if cache_state == "served":
            path.write_text(json.dumps({"status": "served", "observed_at": fresh}))
        elif cache_state == "corrupt":
            path.write_text("{")
    before = {p: p.read_bytes() for p in (tmp_path / "crew").rglob("*") if p.is_file()}
    probe = Mock(side_effect=AssertionError("Cached picks must never probe"))
    monkeypatch.setattr(snapshot.resumption, "probe_lane_availability", probe)
    selection = pick(
        request_node,
        config,
        repo=tmp_path,
        records=[],
        cached_only=True,
        caller=lambda state, *a, **k: _answer("remote", state["candidates"]),
    )
    # A served observation is offered as served; an absent or unreadable cache
    # is an absence of evidence, so the candidate is offered as unknown.
    by_backend = {c["backend"]: c for c in selection.offered}
    assert selection.action == "route"
    assert selection.backend == "remote"
    assert by_backend["remote"]["availability"] == (
        "served" if cache_state == "served" else "unknown"
    )
    after = {p: p.read_bytes() for p in (tmp_path / "crew").rglob("*") if p.is_file()}
    assert before == after
    probe.assert_not_called()


def test_cached_only_budget_refresh_cannot_start_a_serving_probe(
    live_facts, monkeypatch, request_node, config, tmp_path
):
    config["backends"]["remote"]["budget_check"] = True
    monkeypatch.setattr(
        snapshot.budget,
        "state_for",
        lambda name, *a, **k: budget.BudgetState(
            name, headroom="known", utilisation_pct=100, threshold_status="rejected"
        ),
    )
    probe = Mock(side_effect=AssertionError("No live serving request"))
    monkeypatch.setattr(snapshot.resumption, "probe_lane_availability", probe)
    view = snapshot.budget_view(
        request_node.project, config, tmp_path, [], cached_only=True
    )
    assert view["held"] is True
    probe.assert_not_called()
    assert config["backends"]["remote"]["budget_check"] is True


def test_public_pick_renders_size_matched_return_time_and_budget_provenance(
    live_facts, monkeypatch, request_node, config, tmp_path
):
    now = datetime.now(UTC)
    request_node.node.time_budget = "45m"
    request_node.attempts = 2
    row = {
        "backend": "remote",
        "agent": {"model": "remote-model", "effort": "high"},
        "role": "implement",
        "spec_level": "guided",
        "gate": "passed",
        "time_budget": "45m",
        "completed_at": (now - timedelta(hours=1)).isoformat(),
    }
    rows = [row | {"wall_seconds": seconds} for seconds in [100, 200, 300, 400]]
    rows += [row | {"wall_seconds": 9999, "time_budget": "5m"}]
    rows += [
        row
        | {"wall_seconds": 9999, "completed_at": (now - timedelta(days=15)).isoformat()}
    ]
    rows += [
        row
        | {"wall_seconds": 9999, "completed_at": (now + timedelta(days=1)).isoformat()}
    ]
    reader = Mock(return_value=rows)
    monkeypatch.setattr(snapshot.ledger, "runs", reader)
    budget_snapshot = {
        "backends": [
            {
                "backend": "remote",
                "held": False,
                "state": {
                    "headroom": "known",
                    "source": "ledger",
                    "observed_at": (now - timedelta(hours=2)).isoformat(),
                    "burn_multiple": 3.4,
                    "utilisation_pct": 34,
                    "resets_at": (now + timedelta(days=6)).isoformat(),
                },
            }
        ],
        "groups": [{"members": ["remote"], "allowance": {"effective_limit": 0.11}}],
    }
    view = Mock(return_value=budget_snapshot)
    monkeypatch.setattr(snapshot, "budget_view", view)
    seen = []

    def caller(state, questions, **kwargs):
        seen.append(state)
        return _answer("remote", state["candidates"])

    selection = pick(request_node, config, repo=tmp_path, caller=caller)
    assert selection.backend == "remote"
    reader.assert_called_once_with(request_node.project, root=tmp_path)
    assert view.call_args.args[3] is rows
    state = seen[0]
    assert state["return_times"]["remote"] == {
        "p50_s": 250.0,
        "p90_s": 400.0,
        "runs": 4,
        "size_key": "time_budget",
        "size_bucket": "30m_to_60m",
        "budget_source": "ledger",
        "budget_age_s": pytest.approx(7200, abs=5),
        "stale": True,
    }
    assert state["candidates"]["remote"]["pace_allowance"] == 0.11
    assert state["candidates"]["remote"]["days_to_reset"] == pytest.approx(6, abs=0.001)
    assert state["node"]["estimated_hours"] == 0.5
    assert state["node"]["attempts"] == 2
    assert state["node"]["spec_level"] == "guided"
    assert state["node"]["capability"] == request_node.capability
    assert state["candidates"]["remote"]["outcomes"]["passed"] == 5


def test_pressure_alignment_has_no_coded_or_numeric_threshold():
    from reckon.crew.picker import prompts

    def comparisons(source):
        tree = ast.parse(source)
        return [
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare)
            and any(
                isinstance(op, (ast.Gt, ast.GtE, ast.Lt, ast.LtE)) for op in node.ops
            )
            and re.search(r"burn|pace", ast.unparse(node), re.IGNORECASE)
        ]

    assert comparisons("if state['burn_multiple'] > pace_multiple: refuse()")
    root = Path(snapshot.__file__).parent
    for path in root.glob("*.py"):
        assert comparisons(path.read_text()) == [], path
    questions = json.loads(prompts.render("questions.jinja", candidates=[]))
    alignment = questions["route"]["instructions"]
    for phrase in [
        "sliding scale",
        "low-risk",
        "Deferrable",
        "deep-reasoning",
        "pressed metered lane",
    ]:
        assert phrase in alignment
    for path in (root / "templates").glob("*.jinja"):
        assert not re.search(
            r"(?:burn|pace)[^.!?\n]*?(?:[<>]=?|threshold|exceeds)\s*\d",
            path.read_text(),
            re.IGNORECASE,
        )
    for path in root.glob("*.py"):
        assert "sliding scale" not in path.read_text()


def test_live_attempts_are_counted_from_the_matching_node(
    live_facts, request_node, config, tmp_path
):
    row = {"node": request_node.node.id, "plan": request_node.node.plan}
    rows = [row, row, row | {"node": "other"}, row | {"plan": "other"}]
    seen = []

    def caller(state, questions, **kwargs):
        seen.append(state)
        return _answer("local", state["candidates"])

    pick(request_node, config, repo=tmp_path, records=rows, caller=caller)
    assert seen[0]["node"]["attempts"] == 2


def test_representative_live_state_fits_the_token_bound(
    live_facts, monkeypatch, request_node, config, tmp_path
):
    now = datetime.now(UTC)
    config["backends"].pop("codex-spark")
    for index in range(7):
        config["backends"][f"remote-{index}"] = dict(config["backends"]["remote"])
    request_node.node.time_budget = "45m"
    rows = [
        {
            "backend": name,
            "agent": {"model": backend["model"], "effort": "high"},
            "role": "implement",
            "spec_level": "guided",
            "gate": "passed",
            "time_budget": "45m",
            "completed_at": (now - timedelta(hours=1)).isoformat(),
            "wall_seconds": wall,
        }
        for name, backend in config["backends"].items()
        for wall in [100, 200, 300]
    ]
    monkeypatch.setattr(
        snapshot,
        "_lane",
        lambda *a: (
            0,
            {
                "running": 16,
                "waiting": 8,
                "admission_verdict": "congested",
                "stale": False,
                "slots_state": "congested",
            },
            {"held": True},
        ),
    )
    view = {
        "backends": [
            {
                "backend": name,
                "held": False,
                "state": {
                    "headroom": "known",
                    "source": "ledger",
                    "observed_at": (now - timedelta(hours=2)).isoformat(),
                    "burn_multiple": 3.4,
                    "utilisation_pct": 34,
                    "resets_at": (now + timedelta(days=6)).isoformat(),
                },
            }
            for name in config["backends"]
        ],
        "groups": [
            {
                "members": list(config["backends"]),
                "allowance": {"effective_limit": 0.11},
            }
        ],
    }
    seen = []

    def caller(state, questions, **kwargs):
        seen.append(state)
        return _answer("hold", state["candidates"])

    selection = pick(
        request_node,
        config,
        repo=tmp_path,
        records=rows,
        budget_snapshot=view,
        caller=caller,
    )
    assert len(selection.offered) == 9
    assert selection.rendered_token_estimate <= 1500
    assert all(c["p50_s"] == 200 for c in seen[0]["return_times"].values())
    assert all(c["stale"] for c in seen[0]["return_times"].values())
    print(f"representative_state_tokens={selection.rendered_token_estimate}/1500")
    print("representative_state=" + json.dumps(seen[0], separators=(",", ":")))
