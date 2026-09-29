"""Both discovery and the roadmap summary list the sprints a reader needs now.

The two surfaces answer "what could I pick up next" from the same project, so a
reader who asks one and then the other must not get two different answers. The
list they share is: every sprint a crew is working right now, every sprint whose
stored status is not ``done`` or ``shipped``, and every sprint closed within the
configured window. Rows are ordered live first, then open, then recently closed.

The fixture is a synthetic project under a temporary config home with one live
sprint, one open idle sprint, one sprint closed three days ago and one closed
forty days ago. The close dates are derived from the fixture's own clock rather
than written down, so the case cannot pass on the day it is written and fail a
month later.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.mcp import _read_plan_tool, _roadmap_tool
from reckon.mcp_budget import response_ceiling, serialised_characters
from reckon.project_state import create_project_state, write_resource

# A project name no test elsewhere uses, so discovery cannot pick up a peer's
# mount or a real fleet's live pointers.
PROJECT = "sprint-summary-sample"

LIVE = "S-live"
OPEN = "S-open"
RECENT = "S-closed-recent"
OLDER = "S-closed-old"

ROW_KEYS = {
    "id",
    "theme",
    "status",
    "live",
    "live_runs",
    "live_sessions",
    "last_activity_at",
    "members",
    "pending",
    "completion_pct",
    "closed_at",
}


def _plan(docs: Path, slug: str, sprint: str) -> None:
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-sprint" content="{sprint}">'
        f"<title>{slug}</title></head><body></body></html>",
        encoding="utf-8",
    )


def _live_pointer(home: Path, run_id: str, plan: str, session: str) -> None:
    """Write one live pointer for a plan, reading as working on this host."""
    run_dir = home / "crew" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    stream = run_dir / "stdout.jsonl"
    stream.write_text('{"type":"thread.started"}\n', encoding="utf-8")
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": session,
        "phase": "working",
        "node": {"plan": plan, "section": "s1"},
        "log_path": str(stream),
        "process_alive": True,
    }
    live_dir = home / "crew" / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    (live_dir / f"{run_id}.json").write_text(json.dumps(record), encoding="utf-8")


def _mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, docs: Path) -> None:
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(docs)}), encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config-home"))
    host_config = tmp_path / "host-flight.yaml"
    host_config.write_text("version: 1\n", encoding="utf-8")
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(host_config))
    import reckon.serve as serve_module

    serve_module._MOUNTS_FILE = mounts
    serve_module._STATE_ROOT = state


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """One live, one open idle, one recently closed and one long closed sprint."""

    home = tmp_path / "config-home"
    docs = tmp_path / "repo" / "docs"
    now = datetime.now(UTC)

    _plan(docs, "live-plan", LIVE)
    _plan(docs, "idle-plan", OPEN)
    create_project_state(docs, PROJECT)
    _store_sprint(
        docs,
        LIVE,
        {
            "status": "active",
            "theme": "live theme",
            "items": [{"slug": "live-plan"}, {"slug": "idle-plan"}],
        },
    )
    _store_sprint(docs, OPEN, {"status": "planned", "theme": "open theme", "items": []})
    _store_sprint(
        docs,
        RECENT,
        {
            "status": "done",
            "theme": "recent theme",
            "closed_at": (now - timedelta(days=3)).isoformat(),
        },
    )
    _store_sprint(
        docs,
        OLDER,
        {
            "status": "shipped",
            "theme": "old theme",
            "closed_at": (now - timedelta(days=40)).isoformat(),
        },
    )
    _mount(tmp_path, monkeypatch, docs)
    _live_pointer(home, "run-live", "live-plan", "sess-live")
    return home, docs


def _store_sprint(docs: Path, sprint_id: str, payload: dict) -> None:
    write_resource(docs, PROJECT, "sprint", sprint_id, payload, 0, create=True)


def _discovery_sprints() -> list[dict]:
    data = _read_plan_tool(project=PROJECT, view="raw")["data"]
    return data["summary"]["sprints"]


def _roadmap_sprints() -> list[dict]:
    return _roadmap_tool(project=PROJECT, view="summary")["sprints"]


def test_both_summaries_list_live_open_and_recently_closed(project) -> None:
    """The live, open and recently closed sprints, in that order, on both surfaces."""

    expected = [LIVE, OPEN, RECENT]
    assert [row["id"] for row in _discovery_sprints()] == expected
    assert [row["id"] for row in _roadmap_sprints()] == expected


def test_the_long_closed_sprint_is_absent(project) -> None:
    """A close outside the window drops out of both summaries."""

    assert OLDER not in [row["id"] for row in _discovery_sprints()]
    assert OLDER not in [row["id"] for row in _roadmap_sprints()]


def test_a_row_carries_the_row_contract(project) -> None:
    """Each row gives identity, liveness, counts, completion and close date."""

    rows = {row["id"]: row for row in _discovery_sprints()}
    for row in rows.values():
        assert set(row) >= ROW_KEYS

    live = rows[LIVE]
    assert live["live"] is True
    assert live["live_runs"] == ["run-live"]
    assert live["live_sessions"] == ["sess-live"]
    assert live["last_activity_at"] is not None
    assert live["members"] == 2
    assert live["closed_at"] is None

    closed = rows[RECENT]
    assert closed["live"] is False
    assert closed["closed_at"] is not None


def test_the_window_is_read_from_flight_config(project) -> None:
    """A project flight layer that narrows the window drops the 3-day close."""

    _home, docs = project
    config = docs / "state" / PROJECT / "flight.yaml"
    config.write_text("version: 1\nsprint_recent_days: 1\n", encoding="utf-8")

    assert [row["id"] for row in _discovery_sprints()] == [LIVE, OPEN]
    assert [row["id"] for row in _roadmap_sprints()] == [LIVE, OPEN]


def test_reckon_discovery_summary_stays_under_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The discovery summary block at the worktree head fits the response budget.

    The repository under test is mounted through its own ``docs`` tree, so the
    measured project is this checkout rather than whatever the workstation
    happens to have registered.
    """

    repo_root = Path(__file__).resolve().parents[1]
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({"reckon": str(repo_root / "docs")}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))

    summary = _read_plan_tool(project="reckon", view="raw")["data"]["summary"]
    length = serialised_characters(summary)
    assert length < response_ceiling(), (
        f"the discovery summary is {length} characters, over the "
        f"{response_ceiling()} budget"
    )
