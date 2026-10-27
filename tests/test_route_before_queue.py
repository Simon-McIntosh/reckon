"""A stored request keeps its chosen lane until launch availability changes."""

from __future__ import annotations

import importlib
import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew_dispatch_commands
from reckon.crew import queue_admission, queue_edit, runs
from reckon.crew.dispatch_admission import LaneHeld
from reckon.crew.node import TaskNode
from tests import test_a_queued_request_waits_on_its_dependencies as dependency_tests
from tests import test_dispatch_names_its_backend as backend_tests

dispatch_module = importlib.import_module("reckon.crew.dispatch")
LOCAL = "clive"
METERED = "beta"
PROJECT = "proj"
SESSION = "session"


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return backend_tests.dispatch_repo.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture()
def routing(repository: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict]:
    lane = repository.parent / "lane.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": 1,
                "admission": {
                    "worker_slots": 0,
                    "observed_seconds": 900,
                    "sessions": {SESSION: {"worker_slots": 0}},
                    "verdict": "full",
                },
            }
        ),
        encoding="utf-8",
    )
    config = deepcopy(backend_tests.CONFIG)
    config["local_backend"] = LOCAL
    config["default_backend"] = LOCAL
    config["backends"][LOCAL]["lane_document"] = str(lane)
    config["backends"][METERED]["budget_check"] = True
    state = {"metered_held": False}

    def budget(**kwargs):
        backend = kwargs["backend_name"]
        held = backend == METERED and state["metered_held"]
        return {
            "held": held,
            "backend": backend,
            "reason": "codex pace window is held" if held else "lane open",
            "state": {
                "headroom": "known",
                "utilisation_pct": 100 if held else 20,
                "resets_at": "2026-10-27T12:00:00Z",
            },
            "warnings": [],
        }

    monkeypatch.setattr(dispatch_module, "_budget_verdict", budget)
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_args, **_kwargs: config
    )
    monkeypatch.setattr(
        queue_admission, "_dispatch_fleet_gate", lambda: {"state": "open"}
    )
    return config, state


def _node(name: str, *, section: str = "dispatch") -> TaskNode:
    return TaskNode(
        id=name,
        goal="run the routed request",
        plan="plan-a",
        section=section,
        role="implement",
        spec_level="exact",
        done_when="pytest -q tests/test_route_before_queue.py reports the routed request",
        write_paths=[f"{name}.txt"],
        time_budget="20m",
    )


def _selection(backend: str, *, model: str = "") -> dict:
    return {
        "action": "route",
        "backend": backend,
        "model": model or ("requested-model" if backend == METERED else "local-model"),
        "reason": f"fixture chooses {backend}",
        "offered": [],
    }


def _dispatch(
    repository: Path,
    config: dict,
    name: str,
    *,
    backend: str = METERED,
    section: str = "dispatch",
    named: bool = False,
    picked_model: str = "",
):
    return dispatch_module.dispatch(
        node=_node(name, section=section),
        project=PROJECT,
        repo=repository,
        config=config,
        session=SESSION,
        route="deterministic" if named else "picker",
        backend_override=backend if named else None,
        picker_selection=None if named else _selection(backend, model=picked_model),
        watch_required=False,
        watch_override=True,
    )


def _declare_wait(repository: Path) -> None:
    dependency_tests._declare_section_wait(
        repository,
        section="waiting",
        refs=["plan-a#upstream"],
        sections=("waiting", "upstream"),
    )
    path = repository / "docs" / "plans" / "plan-a.html"
    text = path.read_text(encoding="utf-8")
    declaration = json.dumps(
        {"waiting": "implementable", "upstream": "implementable"}
    ).replace('"', "&quot;")
    path.write_text(
        text.replace(
            "</head>",
            f'<meta name="plan-section-declarations" content="{declaration}"></head>',
            1,
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "--", str(path)], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "commit",
            "-q",
            "-m",
            "chore: declare section states",
            "-m",
            "Give the fixture a readable upstream declaration for admission.",
        ],
        cwd=repository,
        check=True,
    )


def _land_upstream(repository: Path) -> None:
    path = repository / "docs" / "plans" / "plan-a.html"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "&quot;upstream&quot;: &quot;implementable&quot;",
        "&quot;upstream&quot;: &quot;done&quot;",
    )
    path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "--", str(path)], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "commit",
            "-q",
            "-m",
            "chore: land upstream section",
            "-m",
            "Make the queued request ready for its admission pass.",
        ],
        cwd=repository,
        check=True,
    )


def _admit(config: dict):
    return queue_admission.admit_once(lane=LOCAL, config_for=lambda _pointer: config)


def _open_local(config: dict) -> None:
    lane_path = Path(config["backends"][LOCAL]["lane_document"])
    lane = json.loads(lane_path.read_text())
    lane["admission"]["worker_slots"] = 1
    lane["admission"]["sessions"][SESSION]["worker_slots"] = 1
    lane_path.write_text(json.dumps(lane), encoding="utf-8")


def test_ready_metered_requests_launch_without_local_slots(
    repository: Path, routing: tuple[dict, dict]
) -> None:
    config, _state = routing
    first = _dispatch(repository, config, "metered-first")
    second = _dispatch(repository, config, "metered-second")
    with pytest.raises(LaneHeld) as local_hold:
        _dispatch(repository, config, "local-third", backend=LOCAL)

    assert [first["backend"], second["backend"]] == [METERED, METERED]
    assert [first["route_mode"], second["route_mode"]] == ["picker", "picker"]
    assert local_hold.value.queued["phase"] == "queued"
    assert runs.read_pointer(local_hold.value.queued["run_id"])["backend"] == LOCAL


def test_local_only_choice_queues_every_request(
    repository: Path, routing: tuple[dict, dict]
) -> None:
    config, _state = routing
    for name in ("local-first", "local-second", "local-third"):
        with pytest.raises(LaneHeld) as held:
            _dispatch(repository, config, name, backend=LOCAL)
        assert held.value.queued["phase"] == "queued"
    assert len(runs.list_live(project=PROJECT, phase="queued")) == 3


def test_metered_wait_keeps_its_pick_and_launches_after_upstream_lands(
    repository: Path, routing: tuple[dict, dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _state = routing
    _declare_wait(repository)
    with pytest.raises(LaneHeld) as held:
        _dispatch(repository, config, "metered-wait", section="waiting")
    request_id = held.value.queued["run_id"]
    waiting = runs.read_pointer(request_id)
    assert waiting["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
    assert waiting["backend"] == METERED
    assert waiting["picker_selection"]["reason"] == "fixture chooses beta"

    _land_upstream(repository)
    order = queue_admission.fair_share_admission_order
    with monkeypatch.context() as negative_arm:
        negative_arm.setattr(
            queue_admission,
            "fair_share_admission_order",
            lambda queued, live, **kwargs: order(
                [pointer for pointer in queued if pointer["backend"] == LOCAL],
                live,
                **kwargs,
            ),
        )
        assert _admit(config)["launched"] == []
        assert runs.read_pointer(request_id)["phase"] == "queued"
    result = _admit(config)
    assert result["launched"] == [request_id]
    launched = runs.read_pointer(request_id)
    assert launched["backend"] == METERED
    assert launched["picker_selection"]["reason"] == "fixture chooses beta"
    assert launched["admit_request"]["backend"] == METERED


def test_queued_pick_launches_the_selected_nondefault_model(
    repository: Path,
    routing: tuple[dict, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _state = routing
    config["backends"][METERED].update(
        {"lane": METERED, "model_key": "sol", "model": "model-sol"}
    )
    config["lanes"] = {
        METERED: {
            "launch": "in-harness",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "budget_check": True,
            "default_model": "sol",
            "models": {
                "sol": {"model": "model-sol"},
                "astra": {"model": "model-astra"},
            },
        }
    }
    catalogue = repository.parent / "model-catalogue.yaml"
    catalogue.write_text(
        json.dumps({"version": 1, "lanes": config["lanes"]}), encoding="utf-8"
    )
    monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(catalogue))
    _declare_wait(repository)

    with pytest.raises(LaneHeld) as held:
        _dispatch(
            repository,
            config,
            "picked-model-wait",
            section="waiting",
            picked_model="astra",
        )
    request_id = held.value.queued["run_id"]
    assert runs.read_pointer(request_id)["picker_selection"]["model"] == "astra"

    _land_upstream(repository)
    assert _admit(config)["launched"] == [request_id]
    launched = runs.read_pointer(request_id)
    assert launched["backend"] == METERED
    assert launched["model_key"] == "astra"
    assert launched["agent"]["model"] == "model-astra"


def test_a_move_launches_on_its_explicit_destination(
    repository: Path,
    routing: tuple[dict, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _state = routing
    _declare_wait(repository)
    with pytest.raises(LaneHeld) as held:
        _dispatch(repository, config, "moved-pick", section="waiting")
    request_id = held.value.queued["run_id"]
    _land_upstream(repository)
    order = queue_admission.fair_share_admission_order
    with monkeypatch.context() as no_launch:
        no_launch.setattr(
            queue_admission,
            "fair_share_admission_order",
            lambda queued, live, **kwargs: order([], live, **kwargs),
        )
        assert _admit(config)["launched"] == []
    assert runs.read_pointer(request_id)["phase"] == "queued"

    _open_local(config)
    moved = queue_admission.move_queued_run(
        request_id, LOCAL, config=config, reason="operator chose local"
    )
    assert moved["backend"] == LOCAL
    assert moved["lane_move"]["to_backend"] == LOCAL
    assert moved["picker_selection"]["backend"] == METERED


def test_pace_hold_falls_back_when_a_picked_request_becomes_ready(
    repository: Path, routing: tuple[dict, dict]
) -> None:
    config, state = routing
    _declare_wait(repository)
    with pytest.raises(LaneHeld) as held:
        _dispatch(repository, config, "falling-back", section="waiting")
    request_id = held.value.queued["run_id"]
    _land_upstream(repository)
    state["metered_held"] = True
    _open_local(config)
    result = _admit(config)
    assert result["launched"] == [request_id]
    run = runs.read_pointer(request_id)
    assert run["backend"] == LOCAL
    assert run["route_fallback"]["from_backend"] == METERED
    assert "hard ceiling" in run["route_fallback"]["reason"]
    assert run["admit_request"]["route_fallback"] == run["route_fallback"]


def test_ready_pace_hold_launches_on_open_local_lane(
    repository: Path, routing: tuple[dict, dict]
) -> None:
    config, state = routing
    _open_local(config)
    state["metered_held"] = True
    run = _dispatch(repository, config, "ready-open-fallback")
    assert run["backend"] == LOCAL
    assert run["route_mode"] == "fallback"
    assert run["route_fallback"]["from_backend"] == METERED
    assert "hard ceiling" in run["route_fallback"]["reason"]


def test_ready_pace_hold_falls_back_on_dispatch(
    repository: Path, routing: tuple[dict, dict]
) -> None:
    config, state = routing
    state["metered_held"] = True
    with pytest.raises(LaneHeld) as held:
        _dispatch(repository, config, "ready-fallback")
    request = runs.read_pointer(held.value.queued["run_id"])
    assert request["backend"] == LOCAL
    assert request["route_fallback"]["from_backend"] == METERED
    assert "hard ceiling" in request["route_fallback"]["reason"]


def test_named_metered_routes_hold_and_never_fall_back(
    repository: Path, routing: tuple[dict, dict]
) -> None:
    config, state = routing
    state["metered_held"] = True
    with pytest.raises(LaneHeld) as held:
        _dispatch(repository, config, "named-ready", named=True)
    request_id = held.value.queued["run_id"]
    request = runs.read_pointer(request_id)
    assert request["backend"] == METERED
    assert request["named_route"]["set_by"] == "dispatch"
    assert "codex pace window is held" in request["reason"]
    result = _admit(config)
    assert request_id in result["held"]
    assert runs.read_pointer(request_id)["backend"] == METERED
    assert not runs.read_pointer(request_id).get("route_fallback")


def test_named_metered_cli_reports_the_queued_backend(
    repository: Path, routing: tuple[dict, dict]
) -> None:
    _config, state = routing
    state["metered_held"] = True
    result = CliRunner().invoke(
        cli.main,
        [
            *backend_tests._arguments(repository, node="named-cli", dry_run=False),
            "--backend",
            METERED,
            "--no-watch",
        ],
    )
    payload = backend_tests._payload(result)
    assert result.exit_code == 0, result.output
    assert payload["queued"] is True
    assert "backend 'beta' queued run" in payload["detail"]
    assert runs.read_pointer(payload["run_id"])["named_route"]["set_by"] == "dispatch"


def test_edited_metered_route_holds_without_fallback(
    repository: Path, routing: tuple[dict, dict]
) -> None:
    config, state = routing
    _declare_wait(repository)
    with pytest.raises(LaneHeld) as held:
        _dispatch(repository, config, "edited-route", section="waiting")
    request_id = held.value.queued["run_id"]
    queue_edit.edit(request_id, backend=METERED, project=PROJECT)
    _land_upstream(repository)
    state["metered_held"] = True
    result = _admit(config)
    assert request_id in result["held"]
    request = runs.read_pointer(request_id)
    assert request["backend"] == METERED
    assert request["named_route"]["set_by"] == "queue-edit"
    assert "codex pace window is held" in request["reason"]
    assert not request.get("route_fallback")
