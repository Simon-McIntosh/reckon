"""Every reckon MCP storage read runs under a deadline, off the event loop.

The defect this file measures: the installed FastMCP calls a *synchronous* tool
function directly on its one event loop, so a single read blocked on a slow
filesystem stalls every later tool call and is indistinguishable from a missing
plan. The fix runs each tool body on a worker thread under a deadline and
answers a blocked call with a typed ``storage-slow`` result instead of holding
the loop.

The filesystem stand-in here is a real file whose reader blocks: a reader that
waits on an event until the test releases it models a GPFS read that never
returns, and a reader that returns immediately models a healthy path. Deadlines
are set through the environment parameter the tools read
(``RECKON_MCP_DEADLINE_SECONDS``), so the bounded-wait assertions run in
fractions of a second rather than at the 30 s production default.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time

import pytest

import reckon.mcp as mcp_module
from reckon._mcp_tools import STORAGE_SLOW

DEADLINE_ENV = mcp_module.DEADLINE_ENV
LANDING_GRACE_ENV = mcp_module.LANDING_GRACE_ENV

# Long enough that a blocked body still holds the loop when the test needs it
# to, short enough that the mutation run (no deadline, body called inline) ends
# in a few seconds instead of hanging.
BLOCK_BOUND = 3.0

REGISTERED_TOOLS = ("read_plan", "edit_plan", "roadmap", "audit", "crew")


def _sync_bodies() -> dict[str, object]:
    return {
        "read_plan": mcp_module._read_plan_tool,
        "edit_plan": mcp_module._edit_plan_tool,
        "roadmap": mcp_module._roadmap_tool,
        "audit": mcp_module._audit_tool,
        "crew": mcp_module._crew,
    }


# ── The SDK fact the fix rests on ──────────────────────────────────────────


def test_the_installed_fastmcp_runs_a_synchronous_tool_on_its_event_loop():
    """Measure, from the SDK itself, that a sync tool body blocks the loop.

    If this fails the premise is wrong and nothing should be moved on its
    strength: the SDK would already be running synchronous tools off the loop.
    """

    fastmcp_server_module = pytest.importorskip("mcp.server.fastmcp")

    server = fastmcp_server_module.FastMCP("deadline-probe")
    order: list[tuple[str, str]] = []

    def probe(label: str) -> str:
        order.append(("enter", label))
        if label == "blocked":
            time.sleep(0.4)
        order.append(("exit", label))
        return f"{label}-done"

    server.tool(name="probe")(probe)

    async def scenario() -> None:
        blocked = asyncio.create_task(
            server._tool_manager.call_tool("probe", {"label": "blocked"})
        )
        await asyncio.sleep(0.05)
        await server._tool_manager.call_tool("probe", {"label": "free"})
        await blocked

    asyncio.run(scenario())

    # A loop-free SDK would interleave: the free call enters while the blocked
    # one sleeps. Serialisation is the defect, and it is the reason the fix
    # moves the body to a worker thread rather than only bounding it.
    assert order == [
        ("enter", "blocked"),
        ("exit", "blocked"),
        ("enter", "free"),
        ("exit", "free"),
    ]


# ── The runner: a blocked read is bounded, a concurrent read is not ────────


def _blocking_reader(path, release: threading.Event):
    def read() -> str:
        release.wait(BLOCK_BOUND)
        return path.read_text(encoding="utf-8")

    return read


def test_a_blocked_read_returns_storage_slow_within_its_deadline(tmp_path, monkeypatch):
    slow = tmp_path / "slow.html"
    slow.write_text("slow-content", encoding="utf-8")
    monkeypatch.setenv(DEADLINE_ENV, "0.2")
    release = threading.Event()

    async def scenario():
        started = time.monotonic()
        result = await mcp_module._run_under_deadline(
            _blocking_reader(slow, release),
            kind="read",
            label="read_plan",
            path=str(slow),
        )
        elapsed = time.monotonic() - started
        release.set()
        return result, elapsed

    result, elapsed = asyncio.run(scenario())

    assert result["error"] == STORAGE_SLOW
    assert result["kind"] == "read"
    assert result["path"] == str(slow)
    assert result["waited_seconds"] >= 0.2
    assert result["deadline_seconds"] == pytest.approx(0.2)
    assert elapsed < 1.0, "the deadline, not the blocked read, bounds the wait"


def test_a_concurrent_read_of_another_path_answers_normally(tmp_path, monkeypatch):
    slow = tmp_path / "slow.html"
    quick = tmp_path / "quick.html"
    slow.write_text("slow-content", encoding="utf-8")
    quick.write_text("quick-content", encoding="utf-8")
    monkeypatch.setenv(DEADLINE_ENV, "0.3")
    release = threading.Event()

    async def scenario():
        blocked_at = time.monotonic()

        def quick_body() -> str:
            entered.append(time.monotonic() - blocked_at)
            return quick.read_text(encoding="utf-8")

        entered: list[float] = []
        blocked = asyncio.create_task(
            mcp_module._run_under_deadline(
                _blocking_reader(slow, release),
                kind="read",
                label="read_plan",
                path=str(slow),
            )
        )
        await asyncio.sleep(0.05)
        started = time.monotonic()
        answered = await mcp_module._run_under_deadline(
            quick_body, kind="read", label="read_plan", path=str(quick)
        )
        elapsed = time.monotonic() - started
        timed_out = await blocked
        release.set()
        return answered, elapsed, timed_out, entered

    answered, elapsed, timed_out, entered = asyncio.run(scenario())

    assert answered == "quick-content"
    assert elapsed < 0.3, "the healthy path must not wait behind the blocked one"
    # The healthy read must enter while the blocked one is still in flight, not
    # merely finish quickly once it has. Under the defect the loop is held for
    # the whole blocked read, so the healthy call enters only after it returns.
    assert entered and entered[0] < 0.3, (
        f"the healthy read entered at {entered} s, behind the blocked read"
    )
    assert timed_out["error"] == STORAGE_SLOW


# ── The write case reports its landed state in both directions ─────────────


def test_a_write_that_lands_after_its_deadline_reports_landed(tmp_path, monkeypatch):
    target = tmp_path / "plan.html"
    monkeypatch.setenv(DEADLINE_ENV, "0.2")
    monkeypatch.setenv(LANDING_GRACE_ENV, "1.5")

    def write_late() -> dict[str, object]:
        time.sleep(0.45)
        target.write_text("landed", encoding="utf-8")
        return {"ok": True}

    result = asyncio.run(
        mcp_module._run_under_deadline(
            write_late, kind="write", label="edit_plan", path=str(target)
        )
    )

    assert result["error"] == STORAGE_SLOW
    assert result["kind"] == "write"
    assert result["landed"] is True
    assert target.read_text(encoding="utf-8") == "landed"


def test_a_write_that_never_lands_reports_not_landed(tmp_path, monkeypatch):
    target = tmp_path / "plan.html"
    monkeypatch.setenv(DEADLINE_ENV, "0.2")
    monkeypatch.setenv(LANDING_GRACE_ENV, "0.3")
    release = threading.Event()

    def write_never() -> dict[str, object]:
        release.wait(BLOCK_BOUND)
        return {"ok": True}

    async def scenario():
        result = await mcp_module._run_under_deadline(
            write_never, kind="write", label="edit_plan", path=str(target)
        )
        release.set()
        return result

    result = asyncio.run(scenario())

    assert result["error"] == STORAGE_SLOW
    assert result["landed"] is False
    assert not target.exists()


def test_a_stalled_landing_stat_still_lets_a_concurrent_read_answer(
    tmp_path, monkeypatch
):
    """The landing check stats the same mount that just stalled the write.

    That stat is as unbounded as the write was, so it runs on a worker thread
    under its own deadline. If it cannot answer, the landing state is unknown —
    reported as ``landed: None``, never as false — and the loop stays free for
    the concurrent read the deadline exists to protect.
    """

    slow = tmp_path / "slow-plan.html"
    quick = tmp_path / "quick.html"
    quick.write_text("quick-content", encoding="utf-8")
    monkeypatch.setenv(DEADLINE_ENV, "0.2")
    monkeypatch.setenv(LANDING_GRACE_ENV, "0.3")
    release = threading.Event()
    real_fingerprint = mcp_module._file_fingerprint
    seen: list[str] = []

    def fingerprint(path):
        # The baseline stat answers; every later stat of the write's file is the
        # landing poll, and that one blocks on the stalled mount.
        if path == str(slow):
            seen.append(path)
            if len(seen) > 1:
                release.wait(BLOCK_BOUND)
                return None
        return real_fingerprint(path)

    monkeypatch.setattr(mcp_module, "_file_fingerprint", fingerprint)

    def stalled_write() -> dict[str, object]:
        release.wait(BLOCK_BOUND)
        return {"ok": True}

    async def scenario():
        started = time.monotonic()
        entered: list[float] = []

        def quick_body() -> str:
            entered.append(time.monotonic() - started)
            return quick.read_text(encoding="utf-8")

        timed = asyncio.create_task(
            mcp_module._run_under_deadline(
                stalled_write, kind="write", label="edit_plan", path=str(slow)
            )
        )
        # Wait past the write's deadline, so the landing check is the thing
        # holding the loop at the moment the read is dispatched.
        await asyncio.sleep(0.25)
        answered = await mcp_module._run_under_deadline(
            quick_body, kind="read", label="read_plan", path=str(quick)
        )
        result = await timed
        release.set()
        return answered, result, entered

    answered, result, entered = asyncio.run(scenario())

    assert answered == "quick-content"
    assert result["error"] == STORAGE_SLOW
    assert result["kind"] == "write"
    assert result["landed"] is None, (
        "an unanswerable landing stat is unknown, not false"
    )
    assert len(seen) > 1, "the landing stat must have been attempted"
    assert entered and entered[0] < 1.0, (
        f"the healthy read entered at {entered} s, behind the stalled landing stat"
    )


# ── The registered surface is wired to the runner ──────────────────────────


def test_every_registered_tool_runs_off_the_loop_with_its_own_signature():
    """The published callable is async; its argument model is unchanged."""

    tools = mcp_module.mcp._tool_manager._tools
    assert set(tools) == set(REGISTERED_TOOLS)

    bodies = _sync_bodies()
    for name, tool in tools.items():
        assert inspect.iscoroutinefunction(tool.fn), name
        assert list(inspect.signature(tool.fn).parameters) == list(
            inspect.signature(bodies[name]).parameters
        ), name


def test_the_read_plan_tool_answers_storage_slow_for_a_blocked_store(monkeypatch):
    """End to end through the registered adapter, not the runner alone."""

    monkeypatch.setenv(DEADLINE_ENV, "0.2")
    release = threading.Event()

    def blocked_store(project, slug, checkout_path=None):
        release.wait(BLOCK_BOUND)
        return {}, 0

    monkeypatch.setattr(mcp_module, "read_plan", blocked_store)
    resolved = "/mounted/proj/docs/slow.html"
    monkeypatch.setattr(mcp_module, "_written_path", lambda *a, **k: resolved)
    adapter = mcp_module.mcp._tool_manager._tools["read_plan"].fn

    async def scenario():
        result = await adapter(project="proj", slug="slow", with_schema=True)
        release.set()
        return result

    result = asyncio.run(scenario())

    assert result["error"] == STORAGE_SLOW
    assert result["label"] == "read_plan"
    assert result["kind"] == "read"
    assert result["path"] == resolved
