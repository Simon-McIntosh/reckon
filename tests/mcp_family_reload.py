"""Reload the store-backed MCP tool modules together, in dependency order.

A test points storage at a temporary project by reloading ``reckon._store``,
which replaces the module's exception and helper classes. The MCP tools catch
those store exceptions, so a tool module that still holds the pre-reload class
object no longer matches what the reloaded store raises: the exception escapes
uncaught. Reloading the tool modules keeps their references in step.

Reload order follows the import graph. ``reckon.mcp_edit_plan`` imports from
``reckon.mcp_deadlines`` and ``reckon.mcp_discovery``, so both are reloaded
first; ``reckon.mcp`` imports all three, so it is reloaded last.
"""

from __future__ import annotations

import importlib

from reckon import _store, mcp, mcp_deadlines, mcp_discovery, mcp_edit_plan


def reload_mcp_family():
    """Reload the store-backed MCP modules in dependency order.

    Returns the reloaded ``reckon.mcp`` module so a caller that needs the
    import surface can use the fresh object directly.
    """
    importlib.reload(_store)
    importlib.reload(mcp_deadlines)
    importlib.reload(mcp_discovery)
    importlib.reload(mcp_edit_plan)
    importlib.reload(mcp)
    return mcp
