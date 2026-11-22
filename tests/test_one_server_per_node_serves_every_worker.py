"""One MCP server on a node serves every worker that names it.

A worker whose MCP set names a shared server connects to that node's single
process rather than starting its own copy. The tests here start the real
server and real clients: the positive control is a tool call answered by every
worker, and the process count is read from ``/proc`` by the node directory
stamped into each server's environment, so a reading of one is meaningful.

The negative control is the base revision, which has no shared declaration at
all: there is no shared server to start, so each worker's configuration carries
the server's own command and N workers start N servers. The base-shape test
below reproduces that shape; the run script that exercises it with live servers
is described in this node's evidence record.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from contextlib import suppress
from pathlib import Path

import pytest

from reckon import _worker_fence, mcp_shared

WORKERS = 40
SMALL = 5
STOP_TIMEOUT = 15.0


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


def stop_server(record: mcp_shared.SharedServer | None) -> None:
    """Terminate a server this process started, and reap it.

    The server is spawned detached, so nothing else will collect it. It is a
    child of the test process, so waiting on it both removes its process-table
    entry and makes the "no process survives" reading meaningful.
    """
    if record is None:
        return
    with suppress(ProcessLookupError):
        os.kill(record.pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        with suppress(ChildProcessError):
            reaped, _ = os.waitpid(record.pid, os.WNOHANG)
            if reaped == record.pid:
                return
        time.sleep(0.05)
    with suppress(ProcessLookupError):
        os.kill(record.pid, signal.SIGKILL)
    with suppress(ChildProcessError):
        os.waitpid(record.pid, 0)


@pytest.fixture
def node(tmp_path, monkeypatch):
    """This test's node directory and the one server serving it."""
    monkeypatch.setenv(mcp_shared.NODE_DIR_ENV, str(tmp_path))
    record = mcp_shared.ensure_shared_server("reckon", directory=tmp_path)
    assert record is not None
    try:
        yield tmp_path, record
    finally:
        stop_server(record)


def test_a_second_launch_reuses_the_node_server(node):
    directory, first = node
    second = mcp_shared.ensure_shared_server("reckon", directory=directory)
    assert second is not None
    assert first.pid == second.pid
    live = server_pids(directory)
    assert live == [first.pid], "the node must run exactly one server"
    record_file = mcp_shared.record_path("reckon", directory)
    assert record_file.stat().st_mode & 0o777 == 0o600
    assert json.loads(record_file.read_text())["pid"] == first.pid


def test_no_server_outlives_the_test_that_started_it(tmp_path, monkeypatch):
    monkeypatch.setenv(mcp_shared.NODE_DIR_ENV, str(tmp_path))
    record = mcp_shared.ensure_shared_server("reckon", directory=tmp_path)
    assert record is not None
    assert server_pids(tmp_path) == [record.pid]
    stop_server(record)
    assert server_pids(tmp_path) == [], "a stopped server must leave no process"


def test_the_node_directory_follows_the_node_runtime_root(tmp_path, monkeypatch):
    monkeypatch.delenv(mcp_shared.NODE_DIR_ENV, raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert mcp_shared.node_dir() == tmp_path / "reckon-mcp"


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


def test_a_list_shaped_server_store_is_filtered_and_takes_the_endpoint(tmp_path):
    """A harness may keep ``mcpServers`` as a list of named entries."""
    path = tmp_path / "worker.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": [
                    {"name": "reckon", "command": "reckon-mcp"},
                    {"name": "other", "command": "other-mcp"},
                ]
            }
        )
    )
    _worker_fence._seed_worker_mcp_exclusions(
        tmp_path,
        path.name,
        {"reckon"},
        refusal_list=True,
        shared_endpoints={"reckon": "http://127.0.0.1:9/mcp"},
    )
    config = json.loads(path.read_text())
    assert [entry["name"] for entry in config["mcpServers"]] == ["reckon"]
    assert config["mcpServers"][0] == {
        "name": "reckon",
        "command": "reckon-mcp",
        "type": "http",
        "url": "http://127.0.0.1:9/mcp",
    }
    assert config["disabledServers"] == ["other"]


async def _answers(url: str, count: int) -> list:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def one():
        async with (
            streamablehttp_client(url) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return await session.call_tool(
                "read_plan", {"project": "reckon", "slug": "index"}
            )

    return await asyncio.gather(*(one() for _ in range(count)))


def answer_text(result) -> str:
    return "".join(
        block.text
        for block in result.content
        if isinstance(getattr(block, "text", None), str)
    )


def test_a_composed_worker_config_serves_a_real_tool_call(node):
    """The declaration a worker is handed reaches the live node server."""
    directory, server = node
    (directory / ".claude.json").write_text(
        json.dumps({"mcpServers": {"reckon": {"command": "reckon-mcp"}}})
    )
    composed = json.loads(
        _worker_fence.claude_worker_mcp_config(
            directory, directory, ["reckon"], {"reckon": server.url}
        )
    )
    declaration = composed["mcpServers"]["reckon"]
    assert declaration == {"type": "http", "url": server.url}
    result = asyncio.run(_answers(declaration["url"], 1))[0]
    assert result.isError is False
    assert answer_text(result).strip(), "the composed declaration must answer"
    assert server_pids(directory) == [server.pid]


def test_every_worker_tool_call_is_answered_by_one_server(node):
    directory, server = node
    results = asyncio.run(_answers(server.url, WORKERS))
    answers = [answer_text(result) for result in results]
    assert len(answers) == WORKERS
    assert [result.isError for result in results] == [False] * WORKERS
    assert all(answer.strip() for answer in answers), "every call must be answered"
    assert len(set(answers)) == 1, "every worker must receive the same answer"
    assert server_pids(directory) and server_pids(directory) == [server.pid]
