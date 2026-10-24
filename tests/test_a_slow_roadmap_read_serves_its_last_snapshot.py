"""A roadmap read that cannot recompose in time answers from its last snapshot.

On slow storage a roadmap composition can outlive the roadmap read's deadline,
and the plan writes it is waiting on arrive continuously, so its key moves
before the composition that would answer under it has finished. A snapshot
kept only when its key held then stores nothing at all, and the read reports
its deadline with a whole composition's work discarded.

What this file measures is the pair of behaviours that replace that: every
composition is kept as its tree's last answer for the request shape it
composed, marked with whether its key held; and a read whose recompute has not
arrived with the reserve it keeps before its deadline is answered from the last
stored answer, marked stale with that answer's age and key.

Every test builds its own temporary home and docs tree and points each
environment-resolved directory at a temporary path, so nothing here reads or
writes the machine's real crew home, state root or node-local cache. The
composition itself is stood in for: a roadmap scan over a real corpus costs
more than the behaviour under test and none of it is being measured here.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

import reckon.mcp as mcp_module
from reckon import mcp_deadlines, roadmap_snapshot, serve
from reckon._mcp_tools import STORAGE_SLOW
from reckon._store import advance_write_generation

PROJECT = "p1"
DEADLINE_ENV = "RECKON_MCP_READ_DEADLINE_SECONDS"
RESERVE_ENV = "RECKON_ROADMAP_STALE_RESERVE_SECONDS"

# The shortened deadline and reserve the timing cases run under: a fraction of
# a second each, so a case that would reach the deadline is bounded in a second
# rather than in the production 30.
SHORT_DEADLINE = 1.0
SHORT_RESERVE = 0.4


def _plan_html(slug: str) -> str:
    return (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-title" content="{slug}">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-impl" content="0.25">'
        '<meta name="plan-version" content="1">'
        f"</head><body><p>{slug}</p></body></html>\n"
    )


class Home:
    """One temporary home and the project tree the mounted project names."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.docs = root / "repo" / "docs"
        self.home = root / "home"

    @property
    def mounts(self) -> dict[str, Path]:
        return {PROJECT: self.docs}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Home:
    """A home whose every resolved directory is inside the test's own tree."""
    plans = tmp_path / "repo" / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "alpha.html").write_text(_plan_html("alpha"), encoding="utf-8")
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "mounts.json").write_text(
        json.dumps({PROJECT: str(tmp_path / "repo" / "docs")}), encoding="utf-8"
    )
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    # Wide enough that no case recomposes because a revalidation window
    # expired: every recomposition below is caused by the fact under test.
    monkeypatch.setenv("RECKON_ROADMAP_REVALIDATE_S", "3600")
    monkeypatch.delenv(DEADLINE_ENV, raising=False)
    monkeypatch.delenv(RESERVE_ENV, raising=False)
    # The in-process snapshot memo is keyed on the tree, the home and the
    # shape; a case's tree is new every time, and clearing it keeps a record
    # from a previous case from being servable in this one.
    roadmap_snapshot._ROADMAP_SNAPSHOTS.clear()
    mcp_deadlines._INFLIGHT_READS.clear()
    serve._resolve_paths(tmp_path / "home" / "mounts.json")
    return Home(tmp_path)


class Composer:
    """A stand-in for the roadmap scan, counted and holdable.

    ``gate`` is open while a composition runs and cleared to hold one open;
    ``on_run`` runs inside each composition, which is how a case moves the
    key while a composition is under way.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.started = threading.Event()
        self.gate = threading.Event()
        self.gate.set()
        self.on_run = None

    def __call__(self, project: str, **kwargs: object) -> dict[str, object]:
        self.calls += 1
        number = self.calls
        self.started.set()
        # A composition held past its deadline is abandoned rather than
        # cancelled, so the hold always releases; the bound is what keeps a
        # failed case from hanging rather than a timing assertion.
        self.gate.wait(15)
        if self.on_run is not None:
            self.on_run()
        return {"ok": True, "project": project, "composition": number}


def _tool():
    """The registered roadmap read: the real deadline wrapper and fallback."""
    return mcp_module._roadmap_deadline_tool()


@pytest.fixture
def loop():
    """One event loop for a whole case.

    A case holds a composition open past its deadline, and that composition's
    thread is abandoned rather than cancelled — it keeps running until the case
    releases it. ``asyncio.run`` shuts its loop's executor down on the way out,
    which would join that thread before the case could release it, so the loop
    here outlives the individual calls and the case releases the hold itself.
    """
    created = asyncio.new_event_loop()
    yield created
    created.close()


def _call(loop: asyncio.AbstractEventLoop, tool, **kwargs) -> dict:
    return loop.run_until_complete(tool(project=PROJECT, **kwargs))


def _timed_call(loop: asyncio.AbstractEventLoop, tool, **kwargs) -> tuple[dict, float]:
    async def measure() -> tuple[dict, float]:
        started = time.monotonic()
        answer = await tool(project=PROJECT, **kwargs)
        return answer, time.monotonic() - started

    return loop.run_until_complete(measure())


def _shape(view: str = "summary") -> dict:
    scoped = mcp_module._roadmap_snapshot_scope(
        PROJECT, None, None, 5, view, None, None
    )
    assert scoped is not None, "the fixture's own project has no snapshot scope"
    return scoped[2]


def _key(view: str = "summary") -> str:
    scoped = mcp_module._roadmap_snapshot_scope(
        PROJECT, None, None, 5, view, None, None
    )
    assert scoped is not None
    return scoped[0]


def _last(home: Home, view: str = "summary") -> dict | None:
    return roadmap_snapshot.roadmap_snapshot_last(PROJECT, home.docs, _shape(view))


def _wait_until(condition, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("the condition never held within its bound")


@pytest.fixture
def composer(monkeypatch: pytest.MonkeyPatch) -> Composer:
    stand_in = Composer()
    monkeypatch.setattr(mcp_module, "_roadmap_scoped", stand_in)
    return stand_in


def test_a_read_past_its_reserve_answers_from_the_last_snapshot(
    home: Home, composer: Composer, loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held recompute is answered from the stored composition, marked stale.

    The answer names the composition's age, the key it was made under and why
    no fresher one was had, and it lands inside the deadline — after the
    reserve the deadline keeps, and before the deadline itself.
    """
    monkeypatch.setenv(DEADLINE_ENV, str(SHORT_DEADLINE))
    monkeypatch.setenv(RESERVE_ENV, str(SHORT_RESERVE))
    tool = _tool()

    first = _call(loop, tool)
    assert first["composition"] == 1
    assert "stale" not in first, "an in-time composition is not a stale answer"
    stored = _last(home)
    assert stored is not None, "the composition was not stored"

    # The key moves, so the answer needs a recomposition, and that
    # recomposition is held past the shortened deadline.
    advance_write_generation(PROJECT)
    composer.gate.clear()
    answered, elapsed = _timed_call(loop, tool)
    composer.gate.set()

    assert answered.get("error") != STORAGE_SLOW, "the read reported its deadline"
    assert answered["composition"] == 1, "the answer is not the stored composition"
    stale = answered["stale"]
    assert stale["key"] == stored["key"]
    assert stale["shape"] == _shape()
    assert stale["reason"] == mcp_module._ROADMAP_STALE_REASON
    assert stale["age_seconds"] >= SHORT_RESERVE / 2, stale
    assert elapsed < SHORT_DEADLINE, f"answered after its deadline: {elapsed:.3f}s"
    assert elapsed >= SHORT_DEADLINE - SHORT_RESERVE, (
        f"gave up before its reserve: {elapsed:.3f}s"
    )


def test_the_held_recompute_stores_and_the_next_call_answers_fresh(
    home: Home, composer: Composer, loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The abandoned recompute runs on and stores, and the next read serves it."""
    monkeypatch.setenv(DEADLINE_ENV, str(SHORT_DEADLINE))
    monkeypatch.setenv(RESERVE_ENV, str(SHORT_RESERVE))
    tool = _tool()

    assert _call(loop, tool)["composition"] == 1
    advance_write_generation(PROJECT)
    composer.gate.clear()
    assert _call(loop, tool)["stale"]["key"]
    composer.gate.set()

    # The recompute is not cancelled by the wait that abandoned it: it
    # finishes and stores the composition it was making.
    key_now = _key()
    shape = _shape()
    _wait_until(
        lambda: (
            roadmap_snapshot.roadmap_snapshot_load(
                PROJECT, home.docs, key_now, shape=shape
            )
            is not None
        )
    )
    mcp_deadlines._INFLIGHT_READS.clear()
    calls_before = composer.calls

    fresh = _call(loop, tool)

    assert "stale" not in fresh, "a stored, held composition is answered fresh"
    assert fresh["composition"] == 2, "the answer is not the recompute's"
    assert composer.calls == calls_before, "the fresh answer recomposed again"


def test_a_recompute_inside_the_deadline_is_answered_fresh(
    home: Home, composer: Composer, loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored composition shortens the wait but never marks a fresh answer."""
    monkeypatch.setenv(DEADLINE_ENV, str(SHORT_DEADLINE))
    monkeypatch.setenv(RESERVE_ENV, str(SHORT_RESERVE))
    tool = _tool()

    assert _call(loop, tool)["composition"] == 1
    advance_write_generation(PROJECT)

    answered = _call(loop, tool)

    assert "stale" not in answered
    assert answered["composition"] == 2


def test_a_composition_whose_key_moved_is_kept_as_the_last_snapshot(
    home: Home, composer: Composer, loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key that moves mid-composition still stores, marked not held.

    The record does not answer an exact-key read under its own key — the tree
    has moved past it — but it is the freshest answer the tree has for that
    shape, so a later read held past its deadline is served from it.
    """
    monkeypatch.setenv(DEADLINE_ENV, str(SHORT_DEADLINE))
    monkeypatch.setenv(RESERVE_ENV, str(SHORT_RESERVE))
    tool = _tool()
    shape = _shape()
    compose_key = _key()

    composer.on_run = lambda: advance_write_generation(PROJECT)
    composed = _call(loop, tool)
    composer.on_run = None

    assert composed["composition"] == 1
    record = _last(home)
    assert record is not None, "a composition whose key moved stored nothing"
    assert record["held"] is False
    assert record["key"] == compose_key
    assert (
        roadmap_snapshot.roadmap_snapshot_load(
            PROJECT, home.docs, compose_key, shape=shape
        )
        is None
    ), "a record whose key moved answered an exact-key read"

    # The key has moved, so this read recomposes; it is held past the reserve
    # and answered from the record the moved-key composition stored.
    mcp_deadlines._INFLIGHT_READS.clear()
    composer.gate.clear()
    answered = _call(loop, tool)
    composer.gate.set()

    assert answered["composition"] == 1, "the stale answer is not that record"
    assert answered["stale"]["key"] == compose_key
    assert answered.get("error") != STORAGE_SLOW


def test_a_snapshot_is_not_served_to_a_read_of_another_shape(
    home: Home, composer: Composer, loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A detail composition does not answer a summary read."""
    monkeypatch.setenv(DEADLINE_ENV, "0.5")
    monkeypatch.setenv(RESERVE_ENV, "0.1")
    tool = _tool()

    detail = _call(loop, tool, view="detail")
    assert detail["composition"] == 1
    assert _last(home, "detail") is not None
    assert _last(home, "summary") is None, "one shape was filed under another"

    advance_write_generation(PROJECT)
    composer.gate.clear()
    answered, elapsed = _timed_call(loop, tool)
    composer.gate.set()

    assert answered.get("error") == STORAGE_SLOW, answered
    assert "stale" not in answered
    assert elapsed < 2.0, f"the shortened deadline did not bound the wait: {elapsed}"


def test_with_no_stored_snapshot_a_held_recompute_reports_the_deadline(
    home: Home, composer: Composer, loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing stored means nothing to fall back to: the deadline is reported."""
    monkeypatch.setenv(DEADLINE_ENV, "0.4")
    monkeypatch.setenv(RESERVE_ENV, "0.1")
    composer.gate.clear()

    answered = _call(loop, _tool())
    composer.gate.set()

    assert answered.get("error") == STORAGE_SLOW, answered
    assert answered["deadline_seconds"] == pytest.approx(0.4)
    assert composer.calls == 1


def test_two_reads_during_one_held_recompute_start_one_recompute(
    home: Home, composer: Composer, loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second read of the same request joins the body already answering it."""
    monkeypatch.setenv(DEADLINE_ENV, "0.4")
    monkeypatch.setenv(RESERVE_ENV, "0.1")
    tool = _tool()
    composer.gate.clear()

    async def scenario() -> list[dict]:
        first = asyncio.create_task(tool(project=PROJECT))
        second = asyncio.create_task(tool(project=PROJECT))
        return await asyncio.gather(first, second)

    answers = loop.run_until_complete(scenario())
    composer.gate.set()

    assert all(answer.get("error") == STORAGE_SLOW for answer in answers), answers
    assert composer.calls == 1, "two reads started two recompositions"
