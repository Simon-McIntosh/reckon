"""Dispatch records a bounded picker answer and routes only on request."""

from __future__ import annotations

import importlib
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import reckon.crew.dispatch_picker as dispatch_picker_module
from reckon import cli, ledger
from reckon.crew import picker
from reckon.crew.picker import Candidate

dispatch = importlib.import_module("reckon.crew.dispatch")

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        name: {
            "launch": "in-harness",
            "model": f"{name}-model",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        }
        for name in ("alpha", "beta")
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    scripts = root / "skills" / "reckon-build" / "scripts"
    plans.mkdir(parents=True)
    scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / source.name).write_text(source.read_text())
    (plans / "example.html").write_text(
        '<!doctype html><html><head><meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="example"></head>'
        '<body><h2 id="dispatch">Dispatch</h2></body></html>'
    )
    (root / "seed.txt").write_text("seed\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/example.html"],
        ["commit", "-q", "-m", "chore: seed repository"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"proj": str(root / "docs")}))
    monkeypatch.setattr(cli, "_resolved_flight", lambda *_a, **_k: CONFIG)
    monkeypatch.setattr(cli, "_model_availability_refusal", lambda *_a, **_k: None)
    return root


def selection(action: str = "route", backend: str | None = "beta", **extra):
    fields = {
        "action": action,
        "backend": backend,
        "family": "test",
        "model": "beta-model" if backend == "beta" else None,
        "effort": "medium",
        "probabilities": {"beta": 0.9},
        "confidence": 0.9,
        "jev_model": "jev-test",
        "fallback_reason": None,
        "latency_ms": 1.0,
        "excluded": [],
    } | extra
    return SimpleNamespace(as_dict=lambda: fields)


def invoke(
    repo: Path,
    *,
    route: bool = False,
    dry_run: bool = False,
    comment: str = "",
    node: str = "picker-test",
    write_path: str = "result.json",
):
    args = [
        "crew",
        "dispatch",
        "--project",
        "proj",
        "--plan",
        "example",
        "--section",
        "dispatch",
        "--role",
        "implement",
        "--spec-level",
        "exact",
        "--node",
        node,
        "--goal",
        "record a backend",
        "--done-when",
        "pytest checks the selected backend",
        "--write-path",
        write_path,
        "--session",
        "session",
        "--repo",
        str(repo),
        "--no-watch",
    ]
    if route:
        args += ["--route", "picker"]
    if dry_run:
        args += ["--dry-run"]
    if comment:
        args += ["--comment", comment]
    result = CliRunner().invoke(cli.main, args)
    return result, json.loads(result.output.splitlines()[0])


def test_shadow_records_selection_without_changing_backend(repo, monkeypatch):
    calls = []

    def pick(request, config, *, repo, cached_only):
        calls.append(cached_only)
        return selection()

    monkeypatch.setattr(picker, "pick", pick)
    result, payload = invoke(repo)
    assert result.exit_code == 0, result.output
    assert calls == [True]
    assert payload["backend"] == "alpha"
    assert payload["picker_selection"]["backend"] == "beta"


def test_dispatch_hands_the_picker_its_precomputed_inputs(repo, monkeypatch):
    """The dispatch entry, not the picker helper, threads the precomputed inputs.

    A pick that reaches the picker with ``records``, ``verdict_inputs`` or
    ``budget_snapshot`` unset reloads each of them per candidate, which is the
    latency this node removes. Driving the dispatch entry proves the wiring at
    its call site rather than only inside ``dispatch_picker_selection``.
    """
    received = {}

    def pick(request, config, *, repo, cached_only, **inputs):
        received.update(inputs)
        return selection()

    monkeypatch.setattr(picker, "pick", pick)
    result, _ = invoke(repo)
    assert result.exit_code == 0, result.output
    assert received.get("records") is not None
    assert received.get("verdict_inputs") is not None
    assert received.get("budget_snapshot") is not None


def test_dispatch_survives_a_raising_picker(repo, monkeypatch):
    def raise_pick(*_args, **_kwargs):
        raise RuntimeError("picker failed")

    monkeypatch.setattr(picker, "pick", raise_pick)
    result, payload = invoke(repo)
    assert result.exit_code == 0, result.output
    assert payload["backend"] == "alpha"
    assert (
        payload["picker_selection"]["fallback_reason"] == "RuntimeError: picker failed"
    )


def test_dispatch_survives_a_picker_past_the_bound(repo, monkeypatch):
    monkeypatch.setattr(dispatch_picker_module, "PICKER_DISPATCH_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(picker, "pick", lambda *_a, **_k: time.sleep(0.2))
    started = time.monotonic()
    direct = dispatch.dispatch_picker_selection(
        node=SimpleNamespace(), config=CONFIG, project="proj", repo=repo
    )
    assert time.monotonic() - started < 0.2
    assert direct["fallback_reason"] == "timeout"
    result, payload = invoke(repo)
    assert result.exit_code == 0, result.output
    assert payload["backend"] == "alpha"
    assert payload["picker_selection"]["fallback_reason"] == "timeout"


def test_picker_route_uses_selected_backend(repo, monkeypatch):
    monkeypatch.setattr(picker, "pick", lambda *_a, **_k: selection())
    result, payload = invoke(repo, route=True)
    assert result.exit_code == 0, result.output
    assert payload["backend"] == "beta"
    assert payload["picker_selection"]["action"] == "route"


def test_picker_fallback_uses_default_backend(repo, monkeypatch):
    monkeypatch.setattr(
        picker,
        "pick",
        lambda *_a, **_k: selection("fallback", None, fallback_reason="low confidence"),
    )
    result, payload = invoke(repo, route=True)
    assert result.exit_code == 0, result.output
    assert payload["backend"] == "alpha"
    assert payload["picker_selection"]["fallback_reason"] == "low confidence"


def test_picker_refusal_names_exclusions_before_worktree(repo, monkeypatch):
    """A refused selection falls through to the default backend, exclusions kept.

    The picker found no eligible candidate, so it names no backend the dispatch
    can use: the dispatch continues as deterministic routing would, resolves the
    configured default, and records the excluded candidate and its reason on the
    run — a refusal narrows nothing the deterministic routing would have run.
    """
    monkeypatch.setattr(
        picker,
        "pick",
        lambda *_a, **_k: selection(
            "refuse", None, excluded=[{"backend": "beta", "reasons": ["logged out"]}]
        ),
    )
    result, payload = invoke(repo, route=True)
    assert result.exit_code == 0, result.output
    assert payload["backend"] == "alpha"
    assert payload["route"] == "picker"
    assert payload["picker_selection"]["action"] == "refuse"
    assert "logged out" in payload["picker_selection"]["excluded"][0]["reasons"]


def test_null_route_names_exclusions(repo, monkeypatch):
    """A route naming no backend resolves deterministically, exclusions kept.

    An action of "route" with a null backend names no backend the dispatch can
    route to, so it stands in for the default exactly as a refusal does, and the
    excluded candidate and its reason stay recorded on the run.
    """
    monkeypatch.setattr(
        picker,
        "pick",
        lambda *_a, **_k: selection(
            "route", None, excluded=[{"backend": "alpha", "reasons": ["hard ceiling"]}]
        ),
    )
    result, payload = invoke(repo, route=True)
    assert result.exit_code == 0, result.output
    assert payload["backend"] == "alpha"
    assert payload["picker_selection"]["action"] == "route"
    assert "hard ceiling" in payload["picker_selection"]["excluded"][0]["reasons"]


def test_picker_hold_uses_budget_code_before_worktree(repo, monkeypatch):
    monkeypatch.setattr(
        picker,
        "pick",
        lambda *_a, **_k: selection(
            "hold",
            None,
            confidence=0.17,
            probabilities={"hold": 0.17, "beta": 0.83},
        ),
    )
    result, payload = invoke(repo, route=True)
    assert result.exit_code == 3
    assert payload["error"] == "budget-hold"
    assert "picker selected hold at confidence 0.17" in payload["detail"]
    assert "hold probability 0.17" in payload["detail"]
    assert not list(repo.parent.glob("**/picker-test/.git"))


def test_comment_reaches_the_picker_verbatim(repo, monkeypatch):
    seen = []
    real_pick = picker.pick

    def pick(request, config, *, repo, cached_only):
        candidate = Candidate(
            backend="beta",
            family="test",
            model="beta-model",
            effort="medium",
            local=False,
            availability="served",
            utilisation_pct=20,
            burn_multiple=1,
            pace_allowance=50,
            resets_at=None,
            worker_slots=None,
            congestion=None,
            outcomes={},
        )

        def caller(state, _questions, *, env_path):
            seen.append(state["orchestrator_comment"])
            return {
                "answers": {
                    "route": {
                        "choice": "beta",
                        "confidence": 0.9,
                        "probabilities": {"beta": 1.0},
                    }
                }
            }

        return real_pick(
            request,
            config,
            repo=repo,
            snapshotter=lambda *_a, **_k: [candidate],
            caller=caller,
        )

    monkeypatch.setattr(picker, "pick", pick)
    comment = "Lead says: preserve \\n and 'quotes' verbatim"
    result, payload = invoke(repo, comment=comment)
    assert result.exit_code == 0, result.output
    assert seen == [comment]
    assert payload["backend"] == "alpha"


def test_routed_dispatch_skips_pace_but_obeys_hard_ceiling(repo, monkeypatch):
    monkeypatch.setattr(picker, "pick", lambda *_a, **_k: selection())
    observed = []

    def budget_verdict(*, config, backend_name, **_kwargs):
        observed.append((backend_name, config["budget"]))
        return {
            "held": False,
            "backend": backend_name,
            "reason": "served observation",
            "state": {
                "resets_at": "2026-10-03T00:00:00Z",
                "headroom": "known",
                "availability": "served",
                "utilisation_pct": config["budget"]["utilisation_pct"],
            },
        }

    monkeypatch.setattr(dispatch, "_budget_verdict", budget_verdict)

    def pace_hold(**_kwargs):
        raise dispatch.CrewError("deterministic pace hold")

    monkeypatch.setattr(dispatch, "_refuse_against_the_bookend_reserve", pace_hold)
    CONFIG["budget"] = {
        "utilisation_ceiling_pct": 90,
        "utilisation_pct": 80,
        "resume_reserve_pct": 10,
        "coordinator_reserve_pct": 10,
    }
    try:
        result, payload = invoke(
            repo, route=True, node="picker-ceiling", write_path="ceiling.json"
        )
        assert result.exit_code == 0, result.output
        assert payload["backend"] == "beta"
        assert observed[-1][1]["resume_reserve_pct"] == 0
        assert observed[-1][1]["coordinator_reserve_pct"] == 0
        result, payload = invoke(repo, node="pace-held", write_path="pace.json")
        assert result.exit_code == 1
        assert "deterministic pace hold" in payload["detail"]
        CONFIG["budget"]["utilisation_pct"] = 90
        result, payload = invoke(repo, route=True)
        assert result.exit_code == 3
        assert payload["error"] == "budget-hold"
        assert "hard ceiling" in payload["detail"]
    finally:
        CONFIG.pop("budget", None)


def test_every_promoted_row_declares_nullable_selection():
    assert "picker_selection" in ledger.RECORD_FIELDS
    row = ledger.build_record(run_id="not-live", plan="example", gate="not-run")
    assert "picker_selection" in row
    assert row["picker_selection"] is None


def test_promotion_carries_the_live_selection(monkeypatch):
    from reckon.crew import runs

    monkeypatch.setattr(
        runs,
        "read_pointer",
        lambda _run_id: {"picker_selection": selection().as_dict()},
    )
    row = ledger.build_record(run_id="live", plan="example", gate="not-run")
    assert row["picker_selection"]["backend"] == "beta"
