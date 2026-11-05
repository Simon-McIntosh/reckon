"""A dispatch dry run reaches the verdict the real dispatch reaches.

The preview exists so a coordinator can ask what a dispatch would do before it
happens. A preview that resolves its own gates answers a different question: it
reports a node ready that the real dispatch then refuses, and it reports a hold
for a node the real dispatch routes. Both arms of each case are exercised here,
so a preview that diverges from the launch is caught before an operator plans
around it.
"""

from __future__ import annotations

import importlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew_dispatch_commands
from tests import test_dispatch_names_its_backend as backend_tests

dispatch_module = importlib.import_module("reckon.crew.dispatch")

LOCAL = "clive"
PROJECT = "proj"


@pytest.fixture()
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return backend_tests.dispatch_repo.__wrapped__(tmp_path, monkeypatch)


def _configure(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan_review_gate: str = "report",
) -> dict:
    """Point the local lane at a document granting four worker slots."""
    lane = repo.parent / "lane.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": 4,
                "admission": {
                    "worker_slots": 4,
                    "observed_seconds": 900,
                    "sessions": {"session": {"worker_slots": 4}},
                    "verdict": "open",
                    "headroom": 4,
                },
            }
        ),
        encoding="utf-8",
    )
    config = deepcopy(backend_tests.CONFIG)
    config["local_backend"] = LOCAL
    config["plan_review_gate"] = plan_review_gate
    config["backends"][LOCAL]["lane_document"] = str(lane)
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    return config


def _hold_selection() -> dict:
    """A picker answer that held, with the local lane among the offered."""
    return {
        "action": "hold",
        "backend": None,
        "family": None,
        "model": None,
        "effort": None,
        "probabilities": {},
        "confidence": 0.4,
        "jev_model": "jev-test",
        "fallback_reason": None,
        "offered": [
            {
                "backend": "beta",
                "family": "metered",
                "model": "requested-model",
                "local": False,
                "burn_multiple": 1.4,
            },
            {
                "backend": LOCAL,
                "family": "local",
                "model": "local-model",
                "local": True,
                "burn_multiple": None,
            },
        ],
        "excluded": [],
        "rendered_token_estimate": 0,
        "latency_ms": 1.0,
        "decision_source": "jev",
        "comment": "",
        "rendered_state": "{}",
        "questions": {},
        "template_digest": "template",
    }


def _stub_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    selection = _hold_selection()
    monkeypatch.setattr(
        dispatch_module, "dispatch_picker_selection", lambda **_kwargs: selection
    )


def _invoke(repo: Path, *, node: str, dry_run: bool, extra: list[str] | None = None):
    arguments = [
        *backend_tests._arguments(repo, node=node, dry_run=dry_run),
        "--no-watch",
        *(extra or []),
    ]
    return CliRunner().invoke(cli.main, arguments)


def _payload(result) -> dict:
    return json.loads(result.output.splitlines()[0])


def test_a_dry_run_refuses_an_unreviewed_plan_where_the_launch_does(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(dispatch_repo, monkeypatch, plan_review_gate="enforce")

    dry = _invoke(dispatch_repo, node="unreviewed-plan", dry_run=True)
    real = _invoke(dispatch_repo, node="unreviewed-plan", dry_run=False)

    assert dry.exit_code == 4, dry.output
    assert _payload(dry)["error"] == "plan-review-missing"
    assert real.exit_code == 4, real.output
    assert _payload(real)["error"] == "plan-review-missing"


def test_a_dry_run_routes_a_held_node_where_the_launch_routes_it(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(dispatch_repo, monkeypatch)
    _stub_hold(monkeypatch)

    dry = _invoke(
        dispatch_repo, node="held-local", dry_run=True, extra=["--route", "picker"]
    )
    real = _invoke(
        dispatch_repo, node="held-local", dry_run=False, extra=["--route", "picker"]
    )

    dry_payload = _payload(dry)
    real_payload = _payload(real)
    assert real.exit_code == 0, real.output
    assert real_payload["backend"] == LOCAL
    assert dry.exit_code == 0, dry.output
    assert dry_payload["ok"] is True
    assert dry_payload["backend"] == real_payload["backend"] == LOCAL
    assert dry_payload["local"] is True
    # Jev's own answer stays on both documents beside the lane it landed on, so
    # the preview reads as the decision the launch made rather than as a hold
    # that silently evaporated.
    assert dry_payload["route_mode"] == real_payload["route_mode"]
    assert dry_payload["picker_selection"]["action"] == "hold"
    assert real_payload["picker_selection"]["action"] == "hold"
