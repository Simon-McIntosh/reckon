"""MCP read responses stay inside their configured transport ceiling."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import reckon.mcp as mcp_module
from reckon.mcp_budget import (
    DEFAULT_RESPONSE_CEILING,
    RESPONSE_CEILING_ENV,
    bound_response,
    response_ceiling,
    serialised_characters,
)

TEST_CEILING = 1_000


def _call_registered(tool_name: str, **arguments: Any) -> dict[str, Any]:
    tool = mcp_module.mcp._tool_manager._tools[tool_name].fn
    return asyncio.run(tool(**arguments))


def _assert_bounded(result: dict[str, Any]) -> None:
    assert serialised_characters(result) <= TEST_CEILING
    assert result["truncated"]["narrow_with"]
    assert result["truncated"]["omitted"]["characters"] > 0


def _roadmap_detail_fixture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    response = {
        "project": "sample",
        "view": "detail",
        "pending_work": [
            {"slug": f"plan-{index}", "summary": "detail " * 80} for index in range(20)
        ],
    }
    monkeypatch.setattr(mcp_module, "_roadmap", lambda *args, **kwargs: response)
    return _call_registered("roadmap", project="sample", view="detail")


def _crew_scopes_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> dict[str, Any]:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    claims = [
        {
            "run_id": f"run-{index}",
            "node": f"worker-{index}",
            "declared_path": f"src/component_{index}.py",
            "path": f"src/component_{index}.py",
        }
        for index in range(40)
    ]
    monkeypatch.setattr(mcp_module, "_docs_dir_for_project", lambda *args: docs_dir)
    monkeypatch.setattr(mcp_module, "read_plan", lambda *args: ({"projects": [{}]}, 1))
    monkeypatch.setattr(
        mcp_module.crew_module,
        "plan_scope_lanes",
        lambda *args, **kwargs: {
            "claims": claims,
            "conflicts": [],
            "live_conflicts": [],
            "lanes": [],
        },
    )
    return _call_registered("crew", project="sample", view="scopes")


def _crew_live_fixture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    live = [{"project": "sample", "run_id": f"run-{index}"} for index in range(30)]
    rows = [
        {
            "run_id": f"run-{index}",
            "classification": "working",
            "checkpoint": "synthetic live detail " * 40,
        }
        for index in range(30)
    ]
    monkeypatch.setattr(mcp_module.crew_module, "list_live", lambda: live)
    monkeypatch.setattr(mcp_module, "project_live_rows", lambda *args, **kwargs: rows)
    monkeypatch.setattr(
        mcp_module,
        "project_watch_visibility",
        lambda *args, **kwargs: {"producer": "synthetic"},
    )
    return _call_registered("crew", project="sample", view="live")


@pytest.mark.parametrize(
    "fixture",
    [_roadmap_detail_fixture, _crew_scopes_fixture, _crew_live_fixture],
    ids=["roadmap-detail", "crew-scopes", "crew-live"],
)
def test_oversized_coordinator_reads_return_a_bounded_narrowing_payload(
    fixture: Callable[..., dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(RESPONSE_CEILING_ENV, str(TEST_CEILING))

    result = (
        fixture(monkeypatch, tmp_path)
        if fixture is _crew_scopes_fixture
        else fixture(monkeypatch)
    )

    _assert_bounded(result)


@pytest.mark.parametrize("tool_name", ["read_plan", "audit"])
def test_every_other_read_tool_uses_the_same_ceiling(
    tool_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(RESPONSE_CEILING_ENV, str(TEST_CEILING))
    response = {"data": [{"detail": "large answer " * 100} for _ in range(10)]}
    if tool_name == "read_plan":
        monkeypatch.setattr(mcp_module, "_read_plan", lambda **kwargs: response)
        result = _call_registered(tool_name, project="sample", slug="large")
    else:
        monkeypatch.setattr(mcp_module, "_audit", lambda **kwargs: response)
        result = _call_registered(tool_name, project="sample")

    _assert_bounded(result)


def test_an_under_budget_read_is_exactly_its_pre_ceiling_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(RESPONSE_CEILING_ENV, str(TEST_CEILING))
    expected = {"ok": True, "project": "sample", "view": "detail", "rows": []}
    monkeypatch.setattr(mcp_module, "_roadmap", lambda *args, **kwargs: expected)

    result = _call_registered("roadmap", project="sample", view="detail")

    assert result == expected


def test_the_default_ceiling_is_configurable() -> None:
    assert response_ceiling({}) == DEFAULT_RESPONSE_CEILING == 80_000
    assert response_ceiling({RESPONSE_CEILING_ENV: "1234"}) == 1_234


def test_even_the_smallest_supported_ceiling_returns_a_bounded_receipt() -> None:
    result = bound_response(
        {"rows": ["large " * 200]},
        tool="roadmap",
        arguments={"view": "detail"},
        ceiling=128,
    )

    assert serialised_characters(result) <= 128
    assert result["truncated"]["narrow_with"]
