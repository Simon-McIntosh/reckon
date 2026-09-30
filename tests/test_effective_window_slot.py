"""The declared window, and the boundary the lane's endpoint is recorded refusing above.

A lane's configuration states the input window its endpoint accepts; the lane's
own run streams record the smaller input its endpoint has actually refused.
Dispatch gates on the smaller of the two, so a node sized inside the band
between them is refused by reckon before a worktree is created, rather than by
the endpoint after three records and no deliverable.

The boundary is read from the resolved backend mapping, so the slot has to
exist in the flight schema for a production configuration to declare it, and
the settings copy a run record keeps has to carry it beside the declared
window. Both are asserted here over a configuration this module writes and
resolves through the dispatch path, rather than over a hand-built mapping.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reckon import capability, crew
from reckon.crew import routing
from reckon.flight import FlightConfigError, resolve

LANE = "clive"
DECLARED_WINDOW_TOKENS = 480_000
EFFECTIVE_BOUNDARY_TOKENS = 200_000
STANDING_INSTRUCTION_TOKENS = 20_000
REPOSITORY_FILE_BYTES = 1_000_000


@pytest.fixture(autouse=True)
def isolated_host_config(monkeypatch, tmp_path):
    """Resolve against the layer this module writes, never the workstation's.

    A real host layer would contribute a second definition of the same backend,
    and the verdict under test would then depend on the machine it ran on.
    """
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(tmp_path / "absent" / "flight.yaml"))


def _window_lines(
    *,
    declared: Any = DECLARED_WINDOW_TOKENS,
    effective: Any = EFFECTIVE_BOUNDARY_TOKENS,
) -> str:
    """Render the two window keys a lane may declare, omitting those passed None."""
    lines = ""
    if declared is not None:
        lines += f"    usable_input_window: {declared}\n"
    if effective is not None:
        lines += f"    effective_input_window: {effective}\n"
    return lines


def _lane_settings(tmp_path: Path, window_lines: str) -> dict[str, Any]:
    """Resolve one lane's settings through the path a dispatch resolves them on."""
    host = tmp_path / "host" / "flight.yaml"
    host.parent.mkdir(parents=True, exist_ok=True)
    host.write_text(
        f"default_backend: {LANE}\n"
        "backends:\n"
        f"  {LANE}:\n"
        "    launch: cli\n"
        "    command: claude\n" + window_lines,
        encoding="utf-8",
    )
    resolved = resolve(
        host_path=host,
        project_path=tmp_path / "project" / "flight.yaml",
    )
    backend_name, settings = routing.resolve_role(resolved.config, "implement", "")
    assert backend_name == LANE
    return settings


def _fixed_standing(tokens: int):
    def _stub(*_args: Any, **_kwargs: Any) -> tuple[int, dict[str, Any]]:
        return tokens, {"effective_tokens": tokens}

    return _stub


def _estimated_tokens() -> int:
    """The estimate this module's repository fixture produces."""
    return STANDING_INSTRUCTION_TOKENS + routing._tokens_for_bytes(
        REPOSITORY_FILE_BYTES
    )


def _lane_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, window_lines: str
) -> crew.DispatchPlan:
    """Build the resolution a dispatch would check, from the resolved lane."""
    monkeypatch.setattr(
        routing,
        "_standing_context_input",
        _fixed_standing(STANDING_INSTRUCTION_TOKENS),
    )
    settings = _lane_settings(tmp_path, window_lines)
    return crew.DispatchPlan(
        run_id="run",
        backend=LANE,
        launch="cli",
        # The recorded settings copy, which is what a rebuilt dispatch reads the
        # lane back from.
        backend_settings=routing._agent_configuration(LANE, "cli", settings),
        node=crew.TaskNode(
            id="node",
            goal="exercise the lane's effective input boundary",
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


def _repository_with_a_measured_file(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "large_module.py").write_text("x" * REPOSITORY_FILE_BYTES, encoding="utf-8")
    return repo


def test_the_resolved_lane_carries_both_windows(tmp_path: Path) -> None:
    """A configuration declaring the boundary reaches resolution with it intact."""
    settings = _lane_settings(tmp_path, _window_lines())

    assert settings["usable_input_window"] == DECLARED_WINDOW_TOKENS
    assert settings["effective_input_window"] == EFFECTIVE_BOUNDARY_TOKENS


def test_the_settings_copy_carries_the_boundary_beside_the_window(
    tmp_path: Path,
) -> None:
    """The settings a run record keeps hold the boundary, as they hold the window."""
    settings = _lane_settings(tmp_path, _window_lines())

    recorded = routing._agent_configuration(LANE, "cli", settings)

    assert recorded["usable_input_window"] == DECLARED_WINDOW_TOKENS
    assert recorded["effective_input_window"] == EFFECTIVE_BOUNDARY_TOKENS


def test_an_estimate_between_the_boundary_and_the_window_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The smaller figure gates, and a refusal names the boundary that killed it.

    A node sized between the two would pass a check reading only the declared
    window and then die at the endpoint, so the refusal has to cite both figures:
    a reader who sees only the declared window looks for the fault in the node.
    """
    resolution = _lane_resolution(tmp_path, monkeypatch, _window_lines())
    repo = _repository_with_a_measured_file(tmp_path)

    estimated = _estimated_tokens()
    assert EFFECTIVE_BOUNDARY_TOKENS < estimated < DECLARED_WINDOW_TOKENS

    verdict = routing._context_fit_verdict(resolution=resolution, repo=repo)

    assert verdict is not None
    assert verdict["estimated_tokens"] == estimated
    assert verdict["allowed"] is False
    assert verdict["window_tokens"] == EFFECTIVE_BOUNDARY_TOKENS
    assert verdict["declared_window_tokens"] == DECLARED_WINDOW_TOKENS
    assert verdict["effective_boundary_tokens"] == EFFECTIVE_BOUNDARY_TOKENS
    assert verdict["shortfall_tokens"] == estimated - EFFECTIVE_BOUNDARY_TOKENS
    assert verdict["reason"].startswith("context-window-exceeded")
    assert str(EFFECTIVE_BOUNDARY_TOKENS) in verdict["reason"]
    assert str(DECLARED_WINDOW_TOKENS) in verdict["reason"]


def test_the_same_estimate_passes_when_the_boundary_is_undeclared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane recording no boundary keeps the window it declares.

    Absence is undeclared, not zero: the same node that the recorded boundary
    refuses is admitted when the configuration declares only its window.
    """
    resolution = _lane_resolution(tmp_path, monkeypatch, _window_lines(effective=None))
    repo = _repository_with_a_measured_file(tmp_path)

    verdict = routing._context_fit_verdict(resolution=resolution, repo=repo)

    assert verdict is not None
    assert verdict["estimated_tokens"] == _estimated_tokens()
    assert verdict["allowed"] is True
    assert verdict["reason"] == "within-context-window"
    assert verdict["window_tokens"] == DECLARED_WINDOW_TOKENS
    assert verdict["effective_boundary_tokens"] is None


def test_a_non_integer_effective_window_is_refused_at_load(tmp_path: Path) -> None:
    """The slot is an integer, so a lane cannot declare a boundary it cannot be read at."""
    with pytest.raises(FlightConfigError) as excinfo:
        _lane_settings(tmp_path, _window_lines(effective='"two hundred thousand"'))

    assert excinfo.value.key_path == f"backends.{LANE}.effective_input_window"
