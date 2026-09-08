"""Wire-name contract tests for the reckon MCP server."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import reckon.mcp as mcp_module

WIRE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def _published_tools():
    assert mcp_module.mcp is not None
    return mcp_module.mcp._tool_manager._tools


def _private_tool_functions():
    return (
        mcp_module._read_plan,
        mcp_module._edit_plan,
        mcp_module._roadmap,
        mcp_module._audit,
        mcp_module._crew,
    )


def test_published_tool_names_follow_the_regular_wire_name_contract():
    """Wire-name contract: every tool is addressed as mcp__reckon__<tool-name>."""

    private_functions = _private_tool_functions()
    expected_names = {
        function.__name__.removeprefix("_") for function in private_functions
    }
    published_tools = _published_tools()

    assert all(function.__name__.startswith("_") for function in private_functions)
    assert len(published_tools) == len(expected_names)
    assert set(published_tools) == expected_names
    assert all(name == tool.name for name, tool in published_tools.items())
    assert all(WIRE_NAME_PATTERN.fullmatch(name) for name in published_tools)
    assert all(not name.startswith("_") for name in published_tools)
    assert {f"mcp__reckon__{name}" for name in published_tools} == {
        f"mcp__reckon__{name}" for name in expected_names
    }


def test_host_approval_keys_follow_the_published_tool_names():
    """The metered harness approves the two configured published wire names."""

    config_path = Path.home() / ".codex" / "config.toml"
    with config_path.open("rb") as config_file:
        config = tomllib.load(config_file)

    approval_keys = config["mcp_servers"]["reckon"]["tools"]
    assert approval_keys["audit"]["approval_mode"] == "approve"
    assert approval_keys["read_plan"]["approval_mode"] == "approve"
