"""A context-window refusal names the window it exceeded, not a horizon.

Routing refuses a node whose deterministic context estimate is above its lane's
usable input window with the structured reason ``context-window-exceeded``. That
refusal is an exit-5 ``CompetenceLimit``, whose message was written for the
other refusal shape: a node estimate above a measured competence horizon in
neutral hours. Rendering a token refusal through the hour template stated a
horizon of ``0.0`` worker-hours that nobody measured and advised splitting into
zero-hour nodes, from a verdict that recorded the real figures as tokens. The
message sends the reader to the wrong remedy, and the zero is a measurement the
verdict never made.

The verdict function is exercised directly here, so the refusal is the one
routing builds rather than a hand-written mapping, and the message is read back
from the exception the dispatch would raise.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reckon import capability, crew
from reckon.crew import routing
from reckon.crew.refusals import format_refusal

BACKEND = "clive"
DECLARED_WINDOW_TOKENS = 230_144
STANDING_INSTRUCTION_TOKENS = 20_000
REPOSITORY_FILE_BYTES = 1_000_000


def _backend_settings() -> dict[str, Any]:
    """The recorded lane settings copy a rebuilt dispatch reads the window from."""
    return {
        "backend": BACKEND,
        "launch": "cli",
        "model": "deepseek-v4-flash",
        "effort": "high",
        "sandbox": "worktree-full",
        "alias": "flash4",
        "usable_input_window": DECLARED_WINDOW_TOKENS,
        "local": True,
    }


def _context_refusal_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Build a ``context-window-exceeded`` verdict through routing's verdict function."""
    monkeypatch.setattr(
        routing,
        "_standing_context_input",
        lambda *_args, **_kwargs: (
            STANDING_INSTRUCTION_TOKENS,
            {"effective_tokens": STANDING_INSTRUCTION_TOKENS},
        ),
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    resolution = crew.DispatchPlan(
        run_id="run",
        backend=BACKEND,
        launch="cli",
        backend_settings=_backend_settings(),
        node=crew.TaskNode(
            id="node",
            goal="exercise the context-window refusal message",
            plan="plan-a",
            estimated_hours=2.0,
            write_paths=["large_module.py"],
        ),
        budget_ceiling="1h",
        validation=crew.NodeValidation(ok=True),
        execution_fit=capability.ExecutionFit(
            role="implement",
            execution_capable=True,
            matched_measure=None,
            override=False,
        ),
    )
    (repo / "large_module.py").write_text("x" * REPOSITORY_FILE_BYTES, encoding="utf-8")
    return routing._competence_verdict(
        resolution=resolution,
        project="alpha",
        repo=repo,
        verdict_inputs={
            "node_estimate": (2.0, "plan-fallback"),
            "capability_cache": {"configurations": []},
            "cache_status": "untracked",
        },
    )


def test_a_context_refusal_names_the_window_not_a_horizon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The context message cites the tokens, window, shortfall and D08 remedy."""
    verdict = _context_refusal_verdict(tmp_path, monkeypatch)

    assert verdict["reason"] == "context-window-exceeded"
    assert verdict["estimated_tokens"] > verdict["window_tokens"]
    assert verdict["shortfall_tokens"] == (
        verdict["estimated_tokens"] - verdict["window_tokens"]
    )

    with pytest.raises(crew.CompetenceLimit) as raised:
        raise crew.CompetenceLimit(verdict)
    message = str(raised.value)

    assert str(verdict["estimated_tokens"]) in message
    assert str(verdict["window_tokens"]) in message
    assert str(verdict["shortfall_tokens"]) in message
    assert verdict["recommendation"] in message
    assert "worker-hour" not in message


def test_a_context_refusal_carries_no_unmeasured_horizon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A horizon nobody measured stays absent, never a measured zero."""
    verdict = _context_refusal_verdict(tmp_path, monkeypatch)

    assert "competence_horizon_hours" not in verdict
    assert verdict.get("competence_horizon_hours") != 0.0


def test_an_hour_refusal_keeps_its_message() -> None:
    """An hour-horizon refusal's message is unchanged by the context branch."""
    verdict = {
        "allowed": False,
        "reason": "competence-horizon-exceeded",
        "estimated_hours": 12.0,
        "competence_horizon_hours": 4.0,
        "target_size_hours": 4.0,
        "recommendation": format_refusal(
            "D08",
            "split into nodes no larger than 4.0 worker-hours for this agent "
            "configuration",
        ),
    }

    message = str(crew.CompetenceLimit(verdict))

    assert message == (
        "node estimate 12.0 worker-hours exceeds the 4.0 worker-hour competence "
        "horizon after speed adjustment; split into nodes no larger than 4.0 "
        "worker-hours Resolve with `reckon crew dispatch` once for each smaller "
        "node."
    )
