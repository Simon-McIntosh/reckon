"""Jev weighs each lane's context window against the node's estimate.

A lane whose window cannot hold the node stays excluded; every offered
candidate carries the window, the node's estimate against it and the headroom
that estimate leaves, so the router can send an oversized node to a lane that
further fits it. These tests fix that behaviour at the candidate scan, in the
rendered state the router reads, and on the dispatch path that fills the
request's estimate.
"""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

from reckon.crew import picker
from reckon.crew.node import TaskNode
from reckon.crew.picker import prompts, snapshot

dispatch = importlib.import_module("reckon.crew.dispatch")

CONTEXT_KEYS = {
    "window_tokens",
    "estimated_tokens",
    "headroom_pct",
    "peak_utilisation_p50_pct",
    "peak_utilisation_p90_pct",
    "peak_utilisation_runs",
}

NO_UTILISATION = {
    "peak_utilisation_p50_pct": None,
    "peak_utilisation_p90_pct": None,
    "peak_utilisation_runs": None,
}

ESTIMATE = 85000


def build_node(**overrides):
    values = {
        "id": "n",
        "goal": "g",
        "plan": "",
        "role": "implement",
        "spec_level": "guided",
        "done_when": "d",
        "write_paths": ["a/b.py"],
        "negative_control": "none: nothing to refuse here",
    }
    values.update(overrides)
    return TaskNode(**values)


def build_config(local_window=100000, remote_window=200000):
    return {
        "default_backend": "remote",
        "local_backend": "local",
        "roles": {"implement": {}},
        "backends": {
            "local": {
                "launch": "cli",
                "command": "clive",
                "model": "local-model",
                "usable_input_window": local_window,
            },
            "remote": {
                "launch": "cli",
                "command": "codex",
                "model": "remote-model",
                "usable_input_window": remote_window,
            },
        },
    }


@pytest.fixture
def scan(monkeypatch, tmp_path):
    """A candidate scan whose budget, serving and estimate facts are stubbed.

    The node's estimate is fixed so a test can place it at a chosen fraction
    of a lane's window without a repository of a chosen size. The context-fit
    verdict is derived from the window each backend declares, exactly as the
    live verdict derives it.
    """

    monkeypatch.setattr(
        snapshot,
        "estimated_context_tokens",
        lambda request_node, repo, *, backend_settings=None, authority=None: ESTIMATE,
    )
    monkeypatch.setattr(
        snapshot.resumption,
        "probe_lane_availability",
        lambda project, name, backend, **k: {"status": "served"},
    )
    monkeypatch.setattr(snapshot.routing, "_context_fit_verdict", lambda **k: None)
    monkeypatch.setattr(
        snapshot, "_dispatch_lane_gate", lambda backend: {"state": "open"}
    )
    monkeypatch.setattr(
        snapshot, "_lane", lambda *a: (3, {"waiting": 0}, {"held": False})
    )

    def verdict(*, resolution, project, repo, verdict_inputs=None):
        window = resolution.backend_settings.get("usable_input_window")
        if window is None:
            return {"allowed": True}
        allowed = window >= ESTIMATE
        reason = "within-context-window" if allowed else "context-window-exceeded"
        return {
            "allowed": allowed,
            "reason": reason,
            "context": {
                "allowed": allowed,
                "window_tokens": window,
                "estimated_tokens": ESTIMATE,
                "reason": reason,
            },
        }

    monkeypatch.setattr(snapshot.routing, "_competence_verdict", verdict)
    return tmp_path


def scan_candidates(config, root):
    request = SimpleNamespace(
        project="proj",
        node=build_node(),
        capability={"class": "general"},
        estimated_context=ESTIMATE,
        session="s",
    )
    return snapshot.candidates(
        request,
        config,
        root,
        records=[],
        verdict_inputs={},
        budget_snapshot={"backends": [], "groups": []},
    )


def test_context_block_reports_headroom_and_leaves_unknown_null():
    # The block renders the once-per-pick figure it is handed, not the
    # per-backend estimate the context carries, so one backend-settings rule
    # governs the request estimate and every candidate block.
    block = snapshot._context_block(
        {"window_tokens": 100000, "estimated_tokens": 85000},
        85000,
        NO_UTILISATION,
    )
    assert block["window_tokens"] == 100000
    assert block["headroom_pct"] == pytest.approx(15.0, abs=0.1)

    overrides = snapshot._context_block(
        {"window_tokens": 100000, "estimated_tokens": 85000},
        60000,
        NO_UTILISATION,
    )
    assert overrides["estimated_tokens"] == 60000
    assert overrides["headroom_pct"] == pytest.approx(40.0, abs=0.1)

    unbounded = snapshot._context_block(None, 60000, NO_UTILISATION)
    assert unbounded["window_tokens"] is None
    assert unbounded["estimated_tokens"] == 60000
    assert unbounded["headroom_pct"] is None


def test_node_over_the_local_window_is_excluded_and_a_lane_with_room_is_offered(
    scan,
):
    config = build_config(local_window=60000, remote_window=200000)
    options = scan_candidates(config, scan)
    local = next(c for c in options if c.backend == "local")
    remote = next(c for c in options if c.backend == "remote")
    assert any(reason.startswith("context-fit") for reason in local.reasons)
    assert remote.reasons == []
    assert remote.context["window_tokens"] == 200000
    assert remote.context["headroom_pct"] > 0


def test_rendered_state_carries_the_context_block_on_every_candidate(scan):
    config = build_config(local_window=100000, remote_window=200000)
    options = [c for c in scan_candidates(config, scan) if not c.reasons]
    assert options
    rendered = prompts.render(
        "state.jinja",
        node=build_node(),
        capability={"class": "general"},
        estimated_context=ESTIMATE,
        comment="",
        candidates=options,
    )
    payload = json.loads(rendered)
    assert payload["candidates"]
    for name, entry in payload["candidates"].items():
        assert set(entry["context"]) == CONTEXT_KEYS, name
    local = payload["candidates"]["local"]
    assert local["context"]["headroom_pct"] == pytest.approx(15.0, abs=0.5)


def test_dispatch_pick_carries_a_non_zero_estimated_context(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "foo.py").write_text("x" * 400)
    seen: dict[str, int] = {}

    def pick(request, _config, *, repo, cached_only):
        seen["estimated_context"] = request.estimated_context
        return SimpleNamespace(as_dict=lambda: {"backend": "remote"})

    monkeypatch.setattr(picker, "pick", pick)
    dispatch.dispatch_picker_selection(
        node=build_node(write_paths=["src/foo.py"]),
        config={},
        project="proj",
        repo=repo,
    )
    assert seen["estimated_context"] > 0


def _granted_authority(repo):
    return {
        "plan": {
            "project": "proj",
            "repository": str(repo),
            "docs": str(repo / "docs"),
        }
    }


def test_node_and_candidate_estimates_agree_with_granted_paths(tmp_path):
    """One estimate: the request-level figure equals the candidate block's.

    A dispatcher-granted landing fragment is exempt from the context charge
    only when the census can see the grant. Measured without the authority the
    node-level figure charges that fragment and reads larger than the estimate
    the same node's candidate block carries, so the two figures Jev weighs for
    one node disagree. Both paths are given the same authority here and must
    return one estimate.
    """

    from reckon import capability
    from reckon.crew.dispatch import DispatchPlan, _grant_landing_write_paths
    from reckon.crew.node import NodeValidation
    from reckon.crew.routing import _context_fit_verdict

    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    node = build_node(id="n", plan="p", write_paths=["src/target.py"])
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "target.py").write_text("x" * 400)
    authority = _granted_authority(repo)
    _grant_landing_write_paths(node, project="proj", authority=authority, warnings=[])
    # The granted fragment exists and is large: exempt only if the census can
    # see the grant, so the two estimates diverge when they disagree on it.
    for granted in node.write_paths:
        resolved = repo / granted
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text("g" * 4000)
    settings = {"launch": "cli", "command": "codex", "usable_input_window": 1_000_000}

    node_tokens = snapshot.estimated_context_tokens(
        node, repo, backend_settings=settings, authority=authority
    )
    without_authority = snapshot.estimated_context_tokens(
        node, repo, backend_settings=settings
    )
    execution = capability.assess_execution_fit(
        node.done_when, role=node.role, execution_capable=None
    )
    resolution = DispatchPlan(
        run_id="",
        backend="remote",
        launch="cli",
        backend_settings=settings,
        node=node,
        budget_ceiling="",
        validation=NodeValidation(ok=True),
        execution_fit=execution,
        authority=authority,
    )
    verdict = _context_fit_verdict(resolution=resolution, repo=repo)

    # The granted fragment is exempt, so the two estimates for one node agree.
    assert node_tokens == verdict["estimated_tokens"]
    # Without the grant the same fragment is charged, which is the split this
    # authority threading removes.
    assert without_authority > node_tokens
