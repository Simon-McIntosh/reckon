"""A retried read waits on the body already answering it.

The defect this file measures: a reckon MCP read that outlives its deadline is
abandoned by ``_run_under_deadline`` — the worker thread keeps running — but a
retry of the same tool and arguments starts a *second* body, so two copies
compete for the interpreter and the coordinator that times out a ``roadmap``
never sees the first copy's answer. The fix keeps a read body's future while it
runs and lets a second call with the same tool and arguments await that future
instead of submitting another. A read whose arguments differ, and every write,
still start their own body.

The stand-in is a read body that blocks on a ``threading.Event`` until the test
releases it, modelling a storage read that never returns within its deadline.
Deadlines are set through the environment parameter the tools read
(``RECKON_MCP_DEADLINE_SECONDS``), so the bounded waits run in fractions of a
second rather than at the 30 s production default.
"""

from __future__ import annotations

import asyncio
import threading
import time

import reckon.mcp as mcp_module
from reckon._mcp_tools import STORAGE_SLOW

DEADLINE_ENV = mcp_module.DEADLINE_ENV

# Long enough that a blocked body still holds its thread while the test retries
# the call, short enough that a run that never releases it still ends. Every
# case releases the body explicitly; the bound is the safety net, not the wait.
RELEASE_BOUND = 10.0


async def _wait_until(predicate, *, bound: float) -> None:
    """Wait, bounded, for a synchronous predicate, yielding to the event loop."""

    deadline = time.monotonic() + bound
    while not predicate():
        if time.monotonic() >= deadline:
            return
        await asyncio.sleep(0.005)


def test_a_retried_read_joins_the_body_still_running(monkeypatch):
    """One body answers both calls: the first times out, the retry joins it."""

    monkeypatch.setenv(DEADLINE_ENV, "0.2")
    release = threading.Event()
    started = threading.Event()
    calls: list[tuple[str, str]] = []

    def spinning_read(project: str, slug: str) -> dict[str, object]:
        calls.append((project, slug))
        started.set()
        release.wait(RELEASE_BOUND)
        return {"ok": True, "project": project, "slug": slug, "bodies": len(calls)}

    tool = mcp_module._deadline_tool(spinning_read, kind="read", label="read_plan")

    async def scenario():
        first = asyncio.create_task(tool(project="proj", slug="plan"))
        await _wait_until(started.is_set, bound=1.0)
        first_result = await first
        # The first call's deadline has expired and its body is still running.
        retry = asyncio.create_task(tool(project="proj", slug="plan"))
        await asyncio.sleep(0.05)
        release.set()
        retry_result = await retry
        return first_result, retry_result

    first_result, retry_result = asyncio.run(scenario())

    assert first_result["error"] == STORAGE_SLOW
    assert retry_result["ok"] is True
    assert retry_result["bodies"] == 1, "the retry must be answered by the first body"
    assert len(calls) == 1, "the retry must not start a second body"


def test_a_read_with_different_arguments_starts_its_own_body(monkeypatch):
    """A call whose arguments differ is not the same read and is not joined."""

    monkeypatch.setenv(DEADLINE_ENV, "0.2")
    release = threading.Event()
    started = threading.Event()
    calls: list[tuple[str, str]] = []

    def reader(project: str, slug: str) -> dict[str, object]:
        calls.append((project, slug))
        if slug == "slow":
            started.set()
            release.wait(RELEASE_BOUND)
        return {"ok": True, "slug": slug, "bodies": len(calls)}

    tool = mcp_module._deadline_tool(reader, kind="read", label="read_plan")

    async def scenario():
        slow = asyncio.create_task(tool(project="proj", slug="slow"))
        await _wait_until(started.is_set, bound=1.0)
        blocked = await slow
        other = await tool(project="proj", slug="other")
        release.set()
        return blocked, other

    blocked, other = asyncio.run(scenario())

    assert blocked["error"] == STORAGE_SLOW
    assert other["ok"] is True
    assert other["slug"] == "other"
    assert calls == [("proj", "slow"), ("proj", "other")], (
        "the different-argument read starts its own body"
    )


def test_a_write_is_never_joined(monkeypatch):
    """A write always runs its own body, even with the same tool and arguments."""

    monkeypatch.setenv(DEADLINE_ENV, "0.2")
    release = threading.Event()
    started = threading.Event()
    calls: list[tuple[str, str]] = []

    def editor(project: str, slug: str) -> dict[str, object]:
        calls.append((project, slug))
        if len(calls) == 1:
            started.set()
            release.wait(RELEASE_BOUND)
        return {"ok": True, "bodies": len(calls)}

    tool = mcp_module._deadline_tool(editor, kind="write", label="edit_plan")

    async def scenario():
        first = asyncio.create_task(tool(project="proj", slug="plan"))
        await _wait_until(started.is_set, bound=1.0)
        first_result = await first
        second = await tool(project="proj", slug="plan")
        release.set()
        return first_result, second

    first_result, second = asyncio.run(scenario())

    assert first_result["error"] == STORAGE_SLOW
    assert second["ok"] is True
    assert second["bodies"] == 2, "the second write runs its own body"
    assert len(calls) == 2, "a write never joins another write"


def test_a_finished_read_leaves_no_join_target(monkeypatch):
    """A body is forgotten when it finishes, so a later identical read is fresh."""

    monkeypatch.setenv(DEADLINE_ENV, "1.0")
    calls: list[tuple[str, str]] = []

    def quick_read(project: str, slug: str) -> dict[str, object]:
        calls.append((project, slug))
        return {"ok": True, "bodies": len(calls)}

    tool = mcp_module._deadline_tool(quick_read, kind="read", label="read_plan")

    async def scenario():
        first = await tool(project="proj", slug="plan")
        second = await tool(project="proj", slug="plan")
        return first, second

    first, second = asyncio.run(scenario())

    assert first["bodies"] == 1
    assert second["bodies"] == 2, "the second read started its own body"
    assert not mcp_module._INFLIGHT_READS, "a finished body leaves no registration"
