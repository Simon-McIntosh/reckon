"""A block's estimate and headroom are the context-window gate's own figures.

The context-window gate in ``reckon/crew/routing.py`` decides on each backend's
own estimate — that backend's standing instruction chain plus the repository
files the brief loads — and refuses a lane whose declared window that estimate
overflows. A candidate block once carried the request's harness-independent
figure whatever lane it named, so an offered lane whose standing chain was
larger than the request's reported headroom the gate had already spent. These
tests fix each block's ``estimated_tokens`` to the estimate the gate measured
for that backend, ``headroom_pct`` to the share of the gating window that
estimate leaves free, and leave a lane declaring no window on the request figure
with a null headroom.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from reckon.crew.node import TaskNode
from reckon.crew.picker import snapshot

routing = importlib.import_module("reckon.crew.routing")

WINDOW = 200000
REQUEST_TOKENS = 55000
CHAIN_TOKENS = {"claude": 60000, "codex": 51000}


def build_node():
    return TaskNode(
        id="n",
        goal="g",
        plan="",
        role="implement",
        spec_level="guided",
        done_when="PASS",
        # Absent on disk, so the census charges no repository file and the
        # gate's estimate for a backend is exactly its standing chain.
        write_paths=["src/absent.py"],
        negative_control="none: nothing to refuse here",
    )


def build_config():
    return {
        "default_backend": "claude_lane",
        "roles": {"implement": {}},
        "backends": {
            "claude_lane": {
                "launch": "cli",
                "command": "claude",
                "model": "claude-model",
                "usable_input_window": WINDOW,
            },
            "codex_lane": {
                "launch": "cli",
                "command": "codex",
                "model": "codex-model",
                "usable_input_window": WINDOW,
            },
            "unbounded_lane": {
                "launch": "cli",
                "command": "other",
                "model": "other-model",
            },
        },
    }


def scan(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def standing(_repo, backend):
        tokens = CHAIN_TOKENS.get((backend or {}).get("command"), 40000)
        return tokens, {"effective_tokens": tokens, "files": []}

    monkeypatch.setattr(routing, "_standing_context_input", standing)
    monkeypatch.setattr(
        snapshot.resumption,
        "probe_lane_availability",
        lambda project, name, backend, **k: {"status": "served"},
    )
    monkeypatch.setattr(
        snapshot, "_dispatch_lane_gate", lambda backend: {"state": "open"}
    )
    monkeypatch.setattr(
        snapshot, "_lane", lambda *a, **k: (3, {"waiting": 0}, {"held": False})
    )
    # The competence gate is neutral here; the real context-fit verdict derives
    # the estimate from each backend's window, exactly as the live gate does.
    monkeypatch.setattr(
        routing,
        "_competence_verdict",
        lambda **k: {"allowed": True, "reason": "", "context": None},
    )

    request = SimpleNamespace(
        project="proj",
        node=build_node(),
        capability={"class": "general"},
        estimated_context=REQUEST_TOKENS,
        session="s",
    )
    return snapshot.candidates(
        request,
        build_config(),
        repo,
        records=[],
        verdict_inputs={},
        budget_snapshot={"backends": [], "groups": []},
    )


def test_block_estimate_and_headroom_are_the_gates_for_that_backend(
    tmp_path, monkeypatch
):
    options = scan(tmp_path, monkeypatch)
    blocks = {candidate.backend: candidate.context for candidate in options}
    assert set(blocks) == {"claude_lane", "codex_lane", "unbounded_lane"}

    claude = blocks["claude_lane"]
    codex = blocks["codex_lane"]
    unbounded = blocks["unbounded_lane"]

    # The two chains genuinely differ, so one request-level figure cannot
    # satisfy both blocks: a shared rule would put the same number on each.
    assert CHAIN_TOKENS["claude"] != CHAIN_TOKENS["codex"]

    # Each block carries the gate's estimate for its own backend's chain.
    assert claude["estimated_tokens"] == CHAIN_TOKENS["claude"]
    assert codex["estimated_tokens"] == CHAIN_TOKENS["codex"]
    assert claude["estimated_tokens"] != codex["estimated_tokens"]

    # headroom_pct is the share of the gating window the estimate leaves free.
    assert claude["window_tokens"] == WINDOW
    assert codex["window_tokens"] == WINDOW
    assert claude["headroom_pct"] == pytest.approx(
        100.0 * (WINDOW - CHAIN_TOKENS["claude"]) / WINDOW, abs=0.1
    )
    assert codex["headroom_pct"] == pytest.approx(
        100.0 * (WINDOW - CHAIN_TOKENS["codex"]) / WINDOW, abs=0.1
    )

    # A lane declaring no window carries the request figure and no headroom,
    # rather than a zero that would read as a spent window.
    assert unbounded["window_tokens"] is None
    assert unbounded["estimated_tokens"] == REQUEST_TOKENS
    assert unbounded["headroom_pct"] is None
