"""Queue depth is a project's bound, and one request is stored once.

A coordinator loads a plan's whole width in one pass, so the queue's depth is
a property of the project's plan rather than a ration of a session's share.
Depth is ``queue.max_requests`` in flight configuration (500 by default), a
request is identified by its project, plan, section and node id, and a review
of a delivered run is never refused by the bound.

The last arm measures what the bound costs a reader: the crew MCP reads of the
live view and the obligations view answer within the 30 s deadline the tool
wrapper enforces, cold and warm, with 500 requests queued.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon import flight
from reckon.crew import runs
from reckon.crew.runs import QueueFull

PROJECT = "queue-depth-fixture"
SESSION = "coordinator-session"
PLAN = "fixture-plan"
# The read deadline the MCP wrapper applies to a crew read view.
READ_DEADLINE_SECONDS = 30.0


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private crew home, so no queued pointer lands anywhere shared."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


@pytest.fixture()
def repository(crew_home: Path, tmp_path: Path) -> Path:
    """A mounted project checkout, so a crew view resolves its docs root."""
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (crew_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _request(
    run_id: str,
    *,
    section: str = "s1",
    project: str = PROJECT,
    session: str = SESSION,
    role: str | None = "implement",
    repo: str = "/repo",
) -> dict[str, Any]:
    node: dict[str, Any] = {"id": run_id, "plan": PLAN, "section": section}
    if role is not None:
        node["role"] = role
    moment = datetime.now(UTC).isoformat()
    return {
        "run_id": run_id,
        "project": project,
        "plan": PLAN,
        "section": section,
        "repo": repo,
        "session": session,
        "backend": "local",
        "launch": "cli",
        "local": True,
        "phase": "queued",
        "queued_at": moment,
        "created_at": moment,
        "node": node,
    }


def _seed_queued(count: int, *, session: str = SESSION) -> list[str]:
    """Write ``count`` queued pointers straight to the live directory.

    Seeding through :func:`runs.queue_dispatch` would rescan the queue on every
    write — quadratic in the fixture's size, and a measurement of the fixture
    rather than of the read under test — so the pointers are written as the
    dispatch that stored them would leave them.
    """
    runs.live_dir().mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for index in range(count):
        run_id = f"r-fixture-{index:04d}"
        record = _request(f"node-{index:04d}", section=f"s{index:04d}", session=session)
        record["run_id"] = run_id
        runs._write_json(runs.pointer_path(run_id), record)
        written.append(run_id)
    return written


def _live_names() -> list[str]:
    return sorted(path.name for path in runs.live_dir().glob("*.json"))


# ── depth: one session loads a plan's width, and one request is stored once


def test_one_session_queues_two_hundred_requests_without_a_refusal(
    crew_home: Path,
) -> None:
    """A session's depth is bounded by its project, never by its own share.

    The arm that fails if the per-session clamp returns: at a floor of two the
    ninth dispatch is refused, and this loop reaches two hundred.
    """
    for index in range(200):
        queued, position = runs.queue_dispatch(
            _request(f"node-{index:03d}", section=f"s{index:03d}")
        )
        assert queued["phase"] == "queued"
        assert position is not None

    assert len(runs.list_live(project=PROJECT)) == 200


def test_a_duplicate_request_returns_the_existing_id_and_stores_nothing(
    crew_home: Path,
) -> None:
    first, position = runs.queue_dispatch(_request("node-a", section="s1"))
    before = _live_names()

    again, again_position = runs.queue_dispatch(_request("node-a", section="s1"))

    assert again["run_id"] == first["run_id"]
    assert again_position == position
    assert _live_names() == before


def test_another_sessions_copy_of_one_request_is_the_same_request(
    crew_home: Path,
) -> None:
    """Identity is the work, not the session that asked for it."""
    first, _ = runs.queue_dispatch(_request("node-a", session="one"))

    second, _ = runs.queue_dispatch(_request("node-a", session="two"))

    assert second["run_id"] == first["run_id"]
    assert len(runs.list_live(project=PROJECT)) == 1


# ── the bound: queue.max_requests, named in the refusal


def test_the_501st_request_in_a_project_is_refused_by_the_bound(
    crew_home: Path,
) -> None:
    _seed_queued(500)

    with pytest.raises(QueueFull) as refused:
        runs.queue_dispatch(_request("node-overflow", section="s-overflow"))

    assert refused.value.cap == flight.DEFAULT_QUEUE_MAX_REQUESTS == 500
    assert refused.value.held == 500
    assert "queue.max_requests" in str(refused.value)
    assert len(runs.list_live(project=PROJECT)) == 500


def test_the_bound_is_the_projects_configured_depth() -> None:
    """The depth is a flight setting, so a project layer can lower it."""
    assert flight.queue_max_requests({}) == 500
    assert flight.queue_max_requests({"queue": {"max_requests": 7}}) == 7
    assert flight.queue_max_requests({"queue": {"max_requests": "many"}}) == 500


def test_a_review_of_a_delivered_run_is_never_refused_by_the_bound(
    crew_home: Path,
) -> None:
    _seed_queued(500)

    review, position = runs.queue_dispatch(
        _request("review-of-delivered", section="delivered", role="review")
    )

    assert review["phase"] == "queued"
    assert position is not None
    assert len(runs.list_live(project=PROJECT)) == 501


def test_a_queued_review_does_not_shrink_the_projects_room(
    crew_home: Path,
) -> None:
    _seed_queued(499)
    runs.queue_dispatch(_request("review-of-earlier", section="earlier", role="review"))

    queued, _ = runs.queue_dispatch(_request("node-last", section="s-last"))

    assert queued["phase"] == "queued"


# ── what the depth costs a reader: the MCP reads answer inside the deadline


def _crew_read_tool() -> Any:
    """The registered crew tool, synchronous body wrapped in its deadline."""
    import reckon.mcp as mcp_module
    from reckon import mcp_deadlines

    return mcp_deadlines._deadline_tool(
        mcp_module._crew,
        kind=mcp_deadlines._crew_call_kind,
        response_budget=True,
    )


def test_the_mcp_crew_reads_answer_inside_their_deadline_on_five_hundred(
    crew_home: Path, repository: Path
) -> None:
    _seed_queued(500)
    tool = _crew_read_tool()

    for view in ("live", "obligations"):
        for attempt in ("cold", "warm"):
            started = time.monotonic()
            answer = asyncio.run(
                tool(view=view, project=PROJECT, checkout_path=str(repository))
            )
            elapsed = time.monotonic() - started
            assert answer.get("error") != "storage-slow", (view, attempt, answer)
            assert elapsed < READ_DEADLINE_SECONDS, (view, attempt, elapsed)
