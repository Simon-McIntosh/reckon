"""One MCP server on a node serves every worker that names it.

A worker whose MCP set names a shared server connects to that node's single
process rather than starting its own copy. The tests here start the real
server and real clients: the positive control is a tool call answered by every
worker, and the process count is read from ``/proc`` by the node directory
stamped into each server's environment, so a reading of one is meaningful.

The negative control is the base revision, which has no shared declaration at
all: there each worker's configuration carries the server's own command, so N
workers start N servers. ``_worker_config_at_base`` reproduces that shape and
the run script below writes the red log.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from reckon import _worker_fence, mcp_shared

WORKERS = 40
SMALL = 5


def server_pids(node_dir: Path) -> list[int]:
    """Every process whose environment names this node's runtime directory."""
    marker = f"{mcp_shared.NODE_DIR_ENV}={node_dir}".encode()
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if marker in environ:
            found.append(int(entry.name))
    return sorted(found)


def test_a_second_launch_reuses_the_node_server(tmp_path, monkeypatch):
    monkeypatch.setenv(mcp_shared.NODE_DIR_ENV, str(tmp_path))
    first = mcp_shared.ensure_shared_server("reckon", directory=tmp_path)
    second = mcp_shared.ensure_shared_server("reckon", directory=tmp_path)
    assert first is not None and second is not None
    assert first.pid == second.pid
    live = server_pids(tmp_path)
    assert live == [first.pid], "the node must run exactly one server"


def test_a_node_that_cannot_start_a_server_keeps_the_worker_declaration(tmp_path):
    endpoints, failures = mcp_shared.shared_server_urls(
        ["reckon"], {"reckon": {"command": ["/bin/false"]}}, directory=tmp_path
    )
    assert endpoints == {}
    assert "reckon" in failures


def test_worker_config_points_at_the_node_server(tmp_path):
    config = _worker_fence.claude_worker_mcp_config(
        tmp_path, tmp_path, ["reckon"], {"reckon": "http://127.0.0.1:9/mcp"}
    )
    servers = json.loads(config)["mcpServers"]
    assert servers["reckon"] == {"type": "http", "url": "http://127.0.0.1:9/mcp"}


def test_at_base_a_worker_carries_its_own_server_command(tmp_path):
    """The negative control: without the shared declaration the worker starts one."""
    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": {"reckon": {"command": "reckon-mcp"}}})
    )
    config = _worker_fence.claude_worker_mcp_config(tmp_path, tmp_path, ["reckon"])
    servers = json.loads(config)["mcpServers"]
    assert servers["reckon"] == {"command": "reckon-mcp"}


async def _answered(url: str, count: int) -> tuple[int, list]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def one():
        async with streamablehttp_client(url) as (read, write, _), ClientSession(
            read, write
        ) as session:
                await session.initialize()
                result = await session.call_tool(
                    "read_plan", {"project": "reckon", "slug": "index"}
                )
                return result.isError

    errors = await asyncio.gather(*(one() for _ in range(count)))
    return len(errors), [error for error in errors if error]


def test_every_worker_tool_call_is_answered_by_one_server(tmp_path):
    server = mcp_shared.ensure_shared_server("reckon", directory=tmp_path)
    assert server is not None
    answered, errors = asyncio.run(_answered(server.url, WORKERS))
    assert errors == []
    assert answered == WORKERS
    assert server_pids(tmp_path) == [server.pid]
