"""A picker hold that offered the local lane queues or launches there.

When the picker holds a node it is usually waiting for the local lane. A hold
whose offered candidates include the local lane is not a reason to wait for a
coordinator to re-dispatch: the node is validated and continued on that lane,
and the lane allowance then launches it if the lane has room or queues it. A
hold that offered no local candidate stays a budget-hold, and a node that fails
validation is refused as not-dispatchable rather than held.
"""

from __future__ import annotations

import importlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew_dispatch_commands
from reckon.crew import queue_admission, runs
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
    session_slots: float,
) -> dict:
    """Point the local lane at a document granting the session a slot count."""
    lane = repo.parent / "lane.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": 4,
                "admission": {
                    "worker_slots": session_slots,
                    "observed_seconds": 900,
                    "sessions": {"session": {"worker_slots": session_slots}},
                    "verdict": "full",
                    "headroom": 4,
                },
            }
        ),
        encoding="utf-8",
    )
    config = deepcopy(backend_tests.CONFIG)
    config["local_backend"] = LOCAL
    config["backends"][LOCAL]["lane_document"] = str(lane)
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    return config


def _hold_selection(*, offered_local: bool) -> dict:
    """A picker answer that held, with or without the local lane offered."""
    offered = [
        {
            "backend": "beta",
            "family": "metered",
            "model": "requested-model",
            "local": False,
            "burn_multiple": 1.4,
        }
    ]
    if offered_local:
        offered.append(
            {
                "backend": LOCAL,
                "family": "local",
                "model": "local-model",
                "local": True,
                "burn_multiple": None,
            }
        )
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
        "offered": offered,
        "excluded": [],
        "rendered_token_estimate": 0,
        "latency_ms": 1.0,
        "decision_source": "jev",
        "comment": "",
        "rendered_state": "{}",
        "questions": {},
        "template_digest": "template",
    }


def _stub_hold(monkeypatch: pytest.MonkeyPatch, *, offered_local: bool) -> None:
    selection = _hold_selection(offered_local=offered_local)

    def stub(**_kwargs):
        return selection

    monkeypatch.setattr(dispatch_module, "dispatch_picker_selection", stub)


def _invoke(
    repo: Path,
    *,
    node: str,
    extra: list[str] | None = None,
    dry_run: bool = False,
):
    arguments = [
        *backend_tests._arguments(repo, node=node, dry_run=dry_run),
        "--route",
        "picker",
        "--no-watch",
        *(extra or []),
    ]
    return CliRunner().invoke(cli.main, arguments)


def _payload(result) -> dict:
    return backend_tests._payload(result)


def _holds(repo: Path) -> list[dict]:
    path = repo / "docs" / "state" / PROJECT / "picker-holds.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_a_held_local_candidate_queues_when_the_lane_holds(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(dispatch_repo, monkeypatch, session_slots=0)
    _stub_hold(monkeypatch, offered_local=True)

    result = _invoke(dispatch_repo, node="held-local")
    payload = _payload(result)

    assert result.exit_code == 0, result.output
    assert payload["ok"] is True
    assert payload["queued"] is True
    assert payload["run_id"]
    pointers = runs.list_live(project=PROJECT)
    assert len(pointers) == 1
    assert pointers[0]["phase"] == "queued"
    assert pointers[0]["run_id"] == payload["run_id"]

    holds = _holds(dispatch_repo)
    assert holds[-1]["run_id"] == payload["run_id"]
    assert holds[-1]["queued"] is True


def test_a_held_local_candidate_launches_when_the_lane_has_room(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(dispatch_repo, monkeypatch, session_slots=4)
    _stub_hold(monkeypatch, offered_local=True)

    result = _invoke(dispatch_repo, node="launched-local")
    payload = _payload(result)

    assert result.exit_code == 0, result.output
    assert payload.get("queued") is not True
    assert payload["backend"] == LOCAL
    assert payload["launch"] == "in-harness"
    # Jev's hold still rides the record, so the outcome history keeps it.
    assert payload["route_mode"] == "overridden-hold"
    assert payload["picker_selection"]["action"] == "hold"
    assert runs.list_live(project=PROJECT, phase="queued") == []


def test_a_hold_without_a_local_candidate_stays_a_budget_hold(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(dispatch_repo, monkeypatch, session_slots=4)
    _stub_hold(monkeypatch, offered_local=False)

    result = _invoke(dispatch_repo, node="held-metered")
    payload = _payload(result)

    assert result.exit_code == 3, result.output
    assert payload["error"] == "budget-hold"
    assert runs.list_live(project=PROJECT) == []


def test_a_malformed_node_offered_the_local_lane_is_refused_not_queued(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(dispatch_repo, monkeypatch, session_slots=4)
    _stub_hold(monkeypatch, offered_local=True)

    result = _invoke(
        dispatch_repo,
        node="malformed-local",
        extra=["--write-path", "tests/test_malformed_hold_node.py"],
    )
    payload = _payload(result)

    assert result.exit_code == 2, result.output
    assert payload["error"] == "not-dispatchable"
    assert runs.list_live(project=PROJECT) == []


def test_an_admission_relaunch_of_a_held_run_launches_on_local(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _configure(dispatch_repo, monkeypatch, session_slots=1)
    _stub_hold(monkeypatch, offered_local=True)

    from reckon.crew.node import TaskNode

    node = TaskNode(
        id="queued-held",
        goal="queue on the local lane",
        plan="plan-a",
        section="dispatch",
        done_when="pytest reports the queued run admitted",
        write_paths=["result.json"],
        spec_level="exact",
        time_budget="20m",
    )
    runs.queue_dispatch(
        {
            "run_id": "r-queued-held",
            "project": PROJECT,
            "session": "session",
            "repo": str(dispatch_repo),
            "node": node.as_dict(),
            "backend": LOCAL,
            "launch": "in-harness",
            "local": True,
            "phase": "queued",
            "queued_at": "2026-10-13T12:00:00+00:00",
            "created_at": "2026-10-13T12:00:00+00:00",
            "hold": {"held": True},
            "dispatch_options": {
                "project": PROJECT,
                "plan_slug": "plan-a",
                "section": "dispatch",
                "session": "session",
                "local": True,
                "no_watch": True,
                "route": "picker",
                "overrides": [],
            },
        }
    )

    result = queue_admission.admit_once(config_for=lambda _pointer: config)

    assert result["launched"], result
    assert runs.read_pointer(result["launched"][0])["phase"] != "queued"
    assert runs.list_live(project=PROJECT, phase="queued") == []
