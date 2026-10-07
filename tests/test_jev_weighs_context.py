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
        lambda request_node, repo, *, backend_settings=None: ESTIMATE,
    )
    monkeypatch.setattr(
        snapshot.resumption,
        "probe_lane_availability",
        lambda project, name, backend, **k: {"status": "served"},
    )
    monkeypatch.setattr(
        snapshot.routing, "_context_fit_verdict", lambda **k: None
    )
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
        allowed = ESTIMATE <= window
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
    block = snapshot._context_block(
        {"window_tokens": 100000, "estimated_tokens": 85000},
        0,
        NO_UTILISATION,
    )
    assert block["window_tokens"] == 100000
    assert block["estimated_tokens"] == 85000
    assert block["headroom_pct"] == pytest.approx(15.0, abs=0.1)

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