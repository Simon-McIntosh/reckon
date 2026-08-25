"""Import-time coherence checks for the MCP server."""

from reckon import mcp as mcp_module
from reckon import roadmap as roadmap_module


def test_roadmap_symbols_load_with_mcp_module() -> None:
    assert mcp_module.GraphTargetError is roadmap_module.GraphTargetError
    assert mcp_module.build_roadmap is roadmap_module.build_roadmap
    assert mcp_module.resolve_graph_target is roadmap_module.resolve_graph_target
