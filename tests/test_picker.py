"""Backend eligibility, typed answers and a replay without dispatch side effects."""

import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from reckon import budget
from reckon.cli import main
from reckon.crew.node import TaskNode
from reckon.crew.picker import PickRequest, client, pick, prompts, snapshot
from reckon.crew.picker.replay import replay


@pytest.fixture
def request_node():
    return PickRequest(
        "example",
        TaskNode(
            id="parser",
            goal="Implement a parser with clear validation errors",
            plan="",
            role="implement",
            spec_level="guided",
            done_when="Parser tests pass",
            estimated_hours=0.5,
        ),
        capability={"class": "general", "requirements": {"verification": "strict"}},
        estimated_context=32000,
        comment='Keep the API stable.\nLiteral "quotes" and {{braces}}.',
    )


@pytest.fixture
def config():
    return {
        "default_backend": "local",
        "local_backend": "local",
        "budget": {"pace_multiple": 1.1},
        "roles": {"implement": {}},
        "backends": {
            "local": {
                "launch": "cli",
                "command": "clive",
                "model": "local-model",
                "effort": "high",
            },
            "remote": {
                "launch": "cli",
                "command": "codex",
                "model": "remote-model",
                "effort": "high",
            },
            "codex-spark": {
                "launch": "cli",
                "command": "codex",
                "model": "gpt-5.3-codex-spark",
            },
        },
    }


@pytest.fixture
def live_facts(monkeypatch, tmp_path):
    monkeypatch.setenv("RECKON_LOCAL_LANE_DOCUMENT", str(tmp_path / "lane.json"))
    monkeypatch.setattr(snapshot.routing, "shared_verdict_inputs", lambda *a: {})
    monkeypatch.setattr(
        snapshot.budget,
        "latest_recorded",
        lambda *a, **k: SimpleNamespace(for_backend=lambda name: None, unattributed=[]),
    )
    monkeypatch.setattr(snapshot.budget, "recorded_windows", lambda *a, **k: {})
    monkeypatch.setattr(snapshot.budget, "group_pace", lambda *a, **k: [])
    monkeypatch.setattr(
        snapshot.budget, "state_for", lambda name, *a, **k: budget.BudgetState(name)
    )
    monkeypatch.setattr(
        snapshot.resumption,
        "probe_lane_availability",
        lambda project, name, backend, **k: {
            "status": "refused" if name == "codex-spark" else "served"
        },
    )
    monkeypatch.setattr(
        snapshot.routing,
        "_context_fit_verdict",
        lambda **k: {"allowed": True, "window_tokens": 100000},
    )
    monkeypatch.setattr(
        snapshot.routing, "_competence_verdict", lambda **k: {"allowed": True}
    )
    monkeypatch.setattr(
        snapshot, "_dispatch_lane_gate", lambda backend: {"state": "open"}
    )
    monkeypatch.setattr(
        snapshot, "_lane", lambda *a: (3, {"waiting": 0}, {"held": False})
    )


def answer(choice="remote", confidence=0.9):
    return {
        "model": "answering-snapshot",
        "answers": {
            "route": {
                "choice": choice,
                "confidence": confidence,
                "probabilities": {"local": 0.2, "remote": 0.7, "hold": 0.1},
            }
        },
    }


def run_pick(request_node, config, tmp_path, caller=None):
    return pick(
        request_node,
        config,
        repo=tmp_path,
        records=[],
        caller=caller or (lambda *a, **k: answer()),
    )


def test_refused_model_is_never_offered_to_jev(
    live_facts, request_node, config, tmp_path
):
    seen = []

    def call(state, questions, **kwargs):
        seen.append(questions["route"]["criteria"])
        return answer()

    selection = run_pick(request_node, config, tmp_path, call)
    assert len(seen) == 1 and "remote" in seen[0]
    assert "codex-spark" not in seen[0]
    assert selection.backend == "remote"
    assert selection.excluded[0]["reasons"] == ["availability: refused"]


def test_refusal_and_serving_both_come_from_the_observation(
    live_facts, monkeypatch, request_node, config, tmp_path
):
    """Eligibility is read from the live serving probe, never a fixed name list."""

    def probe(project, name, backend, **k):
        return {"status": "refused" if name == "remote" else "served"}

    monkeypatch.setattr(snapshot.resumption, "probe_lane_availability", probe)
    options = snapshot.candidates(request_node, config, tmp_path, records=[])
    remote = next(c for c in options if c.backend == "remote")
    served = next(c for c in options if c.backend == "local")
    assert remote.reasons == ["availability: refused"]
    assert served.reasons == []
    assert served.availability == "served"


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("budget", "budget-ceiling"),
        ("context", "context-fit"),
        ("competence", "competence"),
        ("availability", "availability"),
        ("paused", "lane-gate"),
    ],
)
def test_hard_exclusions(
    live_facts, monkeypatch, request_node, config, tmp_path, kind, reason
):
    if kind == "budget":
        monkeypatch.setattr(
            snapshot.budget,
            "state_for",
            lambda name, *a, **k: budget.BudgetState(
                name, headroom="known", utilisation_pct=100
            ),
        )
    elif kind == "context":
        monkeypatch.setattr(
            snapshot.routing,
            "_context_fit_verdict",
            lambda **k: {
                "allowed": False,
                "reason": "too-large",
                "window_tokens": 1000,
            },
        )
    elif kind == "competence":
        monkeypatch.setattr(
            snapshot.routing,
            "_competence_verdict",
            lambda **k: {"allowed": False, "reason": "too-large"},
        )
    elif kind == "availability":
        monkeypatch.setattr(
            snapshot.resumption,
            "probe_lane_availability",
            lambda *a, **k: {"status": "refused"},
        )
    else:
        monkeypatch.setattr(
            snapshot, "_dispatch_lane_gate", lambda backend: {"state": "paused"}
        )
    options = snapshot.candidates(request_node, config, tmp_path, records=[])
    remote = next(c for c in options if c.backend == "remote")
    assert any(value.startswith(reason) for value in remote.reasons)


def test_exact_calls_jev(live_facts, request_node, config, tmp_path):
    request_node.node.spec_level = "exact"
    caller = Mock(return_value=answer())
    selection = run_pick(request_node, config, tmp_path, caller)
    assert selection.backend == "remote"
    assert selection.decision_source == "jev"
    assert selection.confidence == 0.9
    assert selection.jev_model == client.JEV_MODEL
    caller.assert_called_once()


def test_exact_without_slots_asks_jev(
    live_facts, monkeypatch, request_node, config, tmp_path
):
    request_node.node.spec_level = "exact"
    monkeypatch.setattr(snapshot, "_lane", lambda *a: (None, {}, {"held": False}))
    caller = Mock(return_value=answer())
    assert run_pick(request_node, config, tmp_path, caller).backend == "remote"
    caller.assert_called_once()


def test_context_estimate_is_a_hard_floor(live_facts, request_node, config, tmp_path):
    request_node.estimated_context = 100001
    selection = run_pick(request_node, config, tmp_path)
    assert selection.backend is None
    assert "no-eligible" in selection.fallback_reason


def test_representative_state_is_bounded_and_comment_verbatim(
    live_facts, request_node, config, tmp_path
):
    for index in range(7):
        config["backends"][f"remote-{index}"] = dict(config["backends"]["remote"])
    options = snapshot.candidates(request_node, config, tmp_path, records=[])
    rendered = prompts.render(
        "state.jinja",
        node=request_node.node,
        capability=request_node.capability,
        estimated_context=request_node.estimated_context,
        comment=request_node.comment,
        candidates=[c for c in options if not c.reasons],
    )
    # Each candidate now carries its own context block, so the representative
    # state is larger; the bound still holds the whole prompt to a few KB.
    assert len(rendered) <= 1500 * 5
    assert json.loads(rendered)["orchestrator_comment"] == request_node.comment
    assert set(json.loads(rendered)["candidates"]["remote"]) == {
        "backend",
        "lane",
        "model",
        "availability",
        "utilisation_pct",
        "burn_multiple",
        "pace_allowance",
        "days_to_reset",
        "resets_at",
        "worker_slots",
        "congestion",
        "outcomes",
        "context",
        "budget_source",
        "budget_age_s",
        "stale",
        "reset_available",
    }


def test_missing_live_fact_raises(request_node):
    with pytest.raises(AttributeError):
        prompts.render(
            "state.jinja",
            node=request_node.node,
            capability={},
            estimated_context=0,
            comment="",
            candidates=[SimpleNamespace(backend="missing")],
        )


@pytest.mark.parametrize("choice,confidence", [("remote", 0.1), ("local", 0.1)])
def test_confidence_is_jevs_judgment(
    live_facts, request_node, config, tmp_path, choice, confidence
):
    selection = run_pick(
        request_node, config, tmp_path, lambda *a, **k: answer(choice, confidence)
    )
    assert selection.fallback_reason is None
    assert selection.backend == choice
    assert selection.action == "route"
    assert selection.confidence == confidence
    assert selection.probabilities == {"local": 0.2, "remote": 0.7, "hold": 0.1}


def test_unreachable_jev_has_explicit_fallback(
    live_facts, request_node, config, tmp_path
):
    def unreachable(*args, **kwargs):
        raise TimeoutError("sensitive provider text")

    selection = run_pick(request_node, config, tmp_path, unreachable)
    assert selection.backend == "local"
    assert selection.action == "fallback"
    assert selection.fallback_reason == "jev-error: TimeoutError"
    assert "sensitive" not in json.dumps(selection.as_dict())


def test_fallback_cannot_select_refused_default(
    live_facts, request_node, config, tmp_path
):
    config["default_backend"] = "codex-spark"
    selection = run_pick(request_node, config, tmp_path, lambda *a, **k: {})
    assert selection.backend is None
    assert selection.action == "refuse"
    assert selection.fallback_reason.endswith("default-backend-ineligible")


@pytest.mark.parametrize(
    "payload", [{}, answer("invented"), answer(confidence=float("nan"))]
)
def test_malformed_answer_falls_back(
    live_facts, request_node, config, tmp_path, payload
):
    selection = run_pick(request_node, config, tmp_path, lambda *a, **k: payload)
    assert selection.backend == "local"
    assert selection.fallback_reason.startswith("jev-error:")


def test_client_posts_typed_shape_and_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY_RECKON", "test-credential")
    calls = []

    def open_request(request, timeout):
        calls.append((request, timeout))
        return io.BytesIO(json.dumps(answer()).encode())

    monkeypatch.setattr(client.urllib.request, "urlopen", open_request)
    assert (
        client.ask(
            {"fact": 1}, {"route": {"type": "choice"}}, env_path=tmp_path / ".env"
        )
        == answer()
    )
    request, timeout = calls[0]
    assert request.full_url == "https://openrouter.ai/api/alpha/decisions"
    assert request.method == "POST"
    assert timeout == 10
    assert json.loads(request.data) == {
        "state": {"fact": 1},
        "questions": {"route": {"type": "choice"}},
        "model": "typesafe/jev-1.13",
    }


def test_key_falls_back_to_project_env(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY_RECKON", raising=False)
    env = tmp_path / ".env"
    env.write_text('OPENROUTER_API_KEY_RECKON="project-credential"\n')
    assert client.load_key(env) == "project-credential"


def test_outcomes_match_model_role_spec_and_fourteen_days(request_node):
    now = datetime(2026, 1, 20, tzinfo=UTC)
    row = {
        "backend": "remote",
        "agent": {"model": "remote-model"},
        "role": "implement",
        "spec_level": "guided",
        "gate": "passed",
        "completed_at": now.isoformat(),
    }
    records = [
        row,
        row | {"gate": "failed"},
        row | {"role": "review"},
        row | {"spec_level": "exact"},
        row | {"agent": {"model": "other-model"}},
        row | {"completed_at": (now - timedelta(days=15)).isoformat()},
    ]
    assert snapshot.recent_outcomes(
        records, request_node, "remote", "remote-model", now=now
    ) == {
        "passed": 1,
        "failed": 1,
        "not-run": 0,
        "unknown": 0,
    }


def test_lane_zero_share_wins_over_global_room(tmp_path):
    path = tmp_path / "lane.json"
    path.write_text(
        json.dumps(
            {
                "headroom": 20,
                "admission": {
                    "observed_seconds": 600,
                    "worker_slots": 20,
                    "sessions": {"mine": {"worker_slots": 0}},
                },
            }
        )
    )
    slots, _, allowance = snapshot._lane({"lane_document": str(path)}, "mine")
    assert slots == 0 and allowance["held"]


def test_cli_emits_one_selection_without_dispatch(
    monkeypatch, live_facts, config, tmp_path
):
    from reckon import cli
    from reckon.crew import picker

    monkeypatch.setattr(cli, "_dispatch_resolved_flight", lambda *a: config)
    monkeypatch.setattr(picker.snapshot.ledger, "runs", lambda *a, **k: [])
    monkeypatch.setattr(client, "load_key", Mock(side_effect=TimeoutError))
    result = CliRunner().invoke(
        main,
        [
            "crew",
            "pick",
            "--project",
            "example",
            "--role",
            "implement",
            "--spec-level",
            "exact",
            "--goal",
            "A parser",
            "--done-when",
            "Parser tests pass",
            "--checkout-path",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["backend"] == "local"


def test_replay_sorts_by_dispatch_not_promotion(monkeypatch, tmp_path, config):
    import importlib

    module = importlib.import_module("reckon.crew.picker.replay")
    rows = [
        {
            "run_id": str(index),
            "dispatched_at": f"2026-01-{index:02d}T00:00:00Z",
            "backend": "local",
            "role": "implement",
            "spec_level": "exact",
            "gate": "passed",
        }
        for index in (3, 1, 2)
    ]
    monkeypatch.setattr(module.ledger, "runs", lambda *a, **k: rows)
    monkeypatch.setattr(module.snapshot, "budget_view", lambda *a, **k: {})
    selection = {
        "backend": "local",
        "model": "local-model",
        "fallback_reason": None,
        "latency_ms": 1,
        "jev_latency_ms": 0,
        "usage": {},
    }
    monkeypatch.setattr(
        module, "pick", lambda *a, **k: SimpleNamespace(as_dict=lambda: selection)
    )
    result = replay("example", 2, config, repo=tmp_path)
    assert [row["run_id"] for row in result["rows"]] == ["3", "2"]
    assert result["summary"]["agreement_rate"] == 1


def test_replay_reuses_one_serving_observation_per_model(
    live_facts, monkeypatch, request_node, config, tmp_path
):
    probe = Mock(
        side_effect=lambda project, name, backend, **k: {
            "status": "refused" if name == "codex-spark" else "served",
            "observed_at": "2026-01-20T00:00:00Z",
        }
    )
    monkeypatch.setattr(snapshot.resumption, "probe_lane_availability", probe)
    observations = {}
    for _ in range(2):
        options = snapshot.candidates(
            request_node, config, tmp_path, records=[], availability_cache=observations
        )
        assert len([c for c in options if not c.reasons]) == 2
    assert probe.call_count == 3
    assert len(observations) == 3


def test_expired_budget_is_not_rendered_as_current(
    live_facts, monkeypatch, request_node, config, tmp_path
):
    monkeypatch.setattr(
        snapshot.budget,
        "state_for",
        lambda name, *a, **k: budget.BudgetState(
            name, expired=True, utilisation_pct=90, resets_at="2000-01-01T00:00:00Z"
        ),
    )
    candidates = snapshot.candidates(request_node, config, tmp_path, records=[])
    remote = next(c for c in candidates if c.backend == "remote")
    assert remote.utilisation_pct is None
    assert remote.resets_at is None


def test_serving_probe_does_not_inherit_task_sandbox(
    live_facts, monkeypatch, request_node, config, tmp_path
):
    config["roles"]["implement"]["sandbox"] = "read-only"
    config["backends"]["remote"]["sandbox"] = "exec"
    probe = Mock(return_value={"status": "served"})
    monkeypatch.setattr(snapshot.resumption, "probe_lane_availability", probe)
    snapshot.candidates(request_node, config, tmp_path, records=[])
    remote = next(
        call.args[2] for call in probe.call_args_list if call.args[1] == "remote"
    )
    assert remote["sandbox"] == "exec"
    assert remote["model"] == "remote-model"


def test_budget_snapshot_is_shared_only_when_explicit(
    live_facts, monkeypatch, request_node, config, tmp_path
):
    view = snapshot.budget_view(request_node.project, config, tmp_path, [])
    reader = Mock(side_effect=AssertionError("Do not re-read a supplied snapshot"))
    monkeypatch.setattr(snapshot, "budget_view", reader)
    candidates = snapshot.candidates(
        request_node, config, tmp_path, records=[], budget_snapshot=view
    )
    assert any(
        candidate.backend == "remote" and not candidate.reasons
        for candidate in candidates
    )
    reader.assert_not_called()


def test_account_and_budget_exclusions_skip_repository_census(
    live_facts, monkeypatch, request_node, config, tmp_path
):
    monkeypatch.setattr(
        snapshot.budget,
        "state_for",
        lambda name, *a, **k: budget.BudgetState(
            name, headroom="known", utilisation_pct=100 if name == "remote" else 0
        ),
    )
    fit = Mock(return_value=[])
    monkeypatch.setattr(snapshot, "_fit", fit)
    snapshot.candidates(request_node, config, tmp_path, records=[])
    assert [call.args[1] for call in fit.call_args_list] == ["local", "codex-spark"]
