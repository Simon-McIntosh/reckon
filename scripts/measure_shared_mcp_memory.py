"""Measure one shared MCP server's resident memory at two worker counts.

Starts the node's single reckon MCP server, holds a small number of client
sessions open, samples the server's resident set over a window, then repeats
with a second worker count over the same window. The two medians are the
measure: a server that serves every worker from one process does not grow its
footprint with the number of workers it serves.

The defaults are the plan's measure — 5 workers, then 40, sampled every ten
minutes for an hour. A shorter run is the same instrument at a smaller window,
so a debugging run and the recorded measure differ only in the interval they
state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import tempfile
import time
from contextlib import AsyncExitStack, suppress
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def resident_kb(pid: int) -> int:
    status = Path(f"/proc/{pid}/status")
    for line in status.read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise RuntimeError(f"no VmRSS in {status}")


def server_pids(node_dir: Path) -> list[int]:
    """Every process whose environment names this run's node directory.

    The count is the positive control for "one server": a reading of one is
    meaningful only because the same walk finds the server at all.
    """
    marker = f"RECKON_MCP_NODE_DIR={node_dir}".encode()
    found: list[int] = []
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


async def open_session(url: str):
    """Open one worker's connection and keep it open for the window the arm runs."""
    stack = AsyncExitStack()
    await stack.__aenter__()
    read, write, _ = await stack.enter_async_context(streamablehttp_client(url))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return stack, session


async def run_arm(
    url: str, count: str | int, duration: float, interval: float, pid: int
):
    pairs = await asyncio.gather(*(open_session(url) for _ in range(int(count))))
    try:
        sessions = [session for _, session in pairs]
        answered = 0
        for session in sessions:
            result = await session.call_tool(
                "read_plan", {"project": "reckon", "slug": "index"}
            )
            answered += 0 if result.isError else 1
        samples = []
        end = time.monotonic() + duration
        while True:
            samples.append(resident_kb(pid))
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(interval, remaining))
        return {"workers": count, "answered": answered, "samples": samples}
    finally:
        for stack, _session in reversed(pairs):
            # A worker whose connection already dropped has nothing left to
            # close; the arm's measurement is complete either way.
            with suppress(Exception):
                await stack.aclose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--small", type=int, default=5)
    parser.add_argument("--large", type=int, default=40)
    parser.add_argument("--duration", type=float, default=3600.0)
    parser.add_argument("--interval", type=float, default=600.0)
    parser.add_argument("--node-dir", default=None)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args(argv)

    from reckon import mcp_shared

    node_dir = (
        Path(args.node_dir)
        if args.node_dir
        else Path(tempfile.gettempdir()) / f"reckon-mcp-measure-{os.getpid()}"
    )
    node_dir.mkdir(parents=True, exist_ok=True)
    server = mcp_shared.ensure_shared_server("reckon", directory=node_dir)
    if server is None:
        print(json.dumps({"ok": False, "error": "server did not start"}))
        return 1
    small = asyncio.run(
        run_arm(server.url, args.small, args.duration, args.interval, server.pid)
    )
    large = asyncio.run(
        run_arm(server.url, args.large, args.duration, args.interval, server.pid)
    )
    pids = server_pids(node_dir)
    payload = {
        "ok": True,
        "url": server.url,
        "server_pids": pids,
        "small": small,
        "large": large,
        "small_median_kb": int(statistics.median(small["samples"]))
        if small["samples"]
        else None,
        "large_median_kb": int(statistics.median(large["samples"]))
        if large["samples"]
        else None,
        "delta_kb": None,
    }
    if payload["small_median_kb"] is not None and payload["large_median_kb"]:
        payload["delta_kb"] = payload["large_median_kb"] - payload["small_median_kb"]
    text = json.dumps(payload, indent=2, sort_keys=True)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
