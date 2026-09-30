"""Both discovery and the roadmap summary list the sprints a reader needs now.

The two surfaces answer "what could I pick up next" from the same project, so a
reader who asks one and then the other must not get two different answers. The
list they share is: every sprint a crew is working right now, every sprint whose
stored status is not ``done`` or ``shipped``, and every sprint closed within the
configured window. Rows are ordered live first, then open, then recently closed.

The row list rides the ``summary_sprints`` key, present on both surfaces and
absent on both when the project has no sprint to list. Discovery's ``sprints``
key is left as the count it has always been, so each key carries one shape.

The fixture is a synthetic project under a temporary config home with one live
sprint, one open idle sprint, one ``archived`` and one ``closed`` sprint (both
open, because neither is ``done`` or ``shipped``), one sprint closed three days
ago and one closed forty days ago. The close dates are derived from the
fixture's own clock rather than written down, so the case cannot pass on the
day it is written and fail a month later.
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
EMPTY_PROJECT = "sprint-summary-empty"

LIVE = "S-live"
ARCHIVED = "S-archived"
CLOSED = "S-closed"
OPEN = "S-open"
RECENT = "S-closed-recent"
OLDER = "S-closed-old"

# Live first, then the open bucket ordered by id, then the recently closed one.
EXPECTED_ORDER = [LIVE, ARCHIVED, CLOSED, OPEN, RECENT]

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


def _mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    docs: Path,
    *,
    extra: dict[str, Path] | None = None,
) -> None:
    mounts = tmp_path / "mounts.json"
    registry = {PROJECT: str(docs), **(extra or {})}
    mounts.write_text(json.dumps(registry), encoding="utf-8")
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
    # ``archived`` and ``closed`` are not statuses the sprint writer accepts, so
    # they are written as resources already on disk that carry them. Neither is
    # ``done`` or ``shipped``, so the section lists both with the open work.
    _store_raw_sprint(docs, ARCHIVED, "archived", "archived theme")
    _store_raw_sprint(
        docs,
        CLOSED,
        "closed",
        "closed theme",
        closed_at=(now - timedelta(days=2)).isoformat(),
    )
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
    # A second project that declares no sprint at all, beside the sprinted one,
    # so a key's shape can be compared across the two.
    empty_docs = tmp_path / "empty-repo" / "docs"
    create_project_state(empty_docs, EMPTY_PROJECT)

    _mount(tmp_path, monkeypatch, docs, extra={EMPTY_PROJECT: str(empty_docs)})
    _live_pointer(home, "run-live", "live-plan", "sess-live")
    return home, docs


def _store_sprint(docs: Path, sprint_id: str, payload: dict) -> None:
    write_resource(docs, PROJECT, "sprint", sprint_id, payload, 0, create=True)


def _store_raw_sprint(
    docs: Path,
    sprint_id: str,
    status: str,
    theme: str,
    *,
    closed_at: str | None = None,
) -> None:
    """Write a sprint resource directly, carrying a status the writer refuses.

    The sprint writer accepts only planned, open, active, done and shipped, so a
    resource already on disk with ``archived`` or ``closed`` is the only way a
    reader meets one. The file is a normal sprint resource in every other
    respect, so discovery reads it exactly as it reads a written one.
    """

    state: dict = {
        "description": "",
        "ends": "",
        "id": sprint_id,
        "starts": "",
        "status": status,
        "summary": "",
        "theme": theme,
        "type": "sprint",
        "version": 1,
    }
    if closed_at is not None:
        state["closed_at"] = closed_at
    path = docs / "sprints" / f"{sprint_id}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '  <meta charset="utf-8">\n'
        f'  <meta name="docs-project" content="{PROJECT}">\n'
        '  <meta name="reckon-type" content="sprint">\n'
        f'  <meta name="reckon-id" content="{sprint_id}">\n'
        '  <meta name="reckon-version" content="1">\n'
        f"  <title>{theme} | sprint</title>\n</head>\n<body>\n"
        f'<main class="reckon-resource" data-type="sprint" data-id="{sprint_id}">\n'
        '  <ol data-reckon="sprint-items">\n  </ol>\n'
        '  <script type="application/json" id="reckon-resource-state">'
        f"{json.dumps(state, sort_keys=True)}</script>\n"
        "</main>\n</body>\n</html>\n",
        encoding="utf-8",
    )


def _discovery_sprints(project: str = PROJECT) -> list[dict]:
    data = _read_plan_tool(project=project, view="raw")["data"]
    return data["summary"]["summary_sprints"]


def _roadmap_sprints(project: str = PROJECT) -> list[dict]:
    return _roadmap_tool(project=project, view="summary")["summary_sprints"]


def test_both_summaries_list_live_open_and_recently_closed(project) -> None:
    """The live, open and recently closed sprints, in that order, on both surfaces."""

    assert [row["id"] for row in _discovery_sprints()] == EXPECTED_ORDER
    assert [row["id"] for row in _roadmap_sprints()] == EXPECTED_ORDER


def test_archived_and_closed_sprints_are_listed_with_the_open_work(project) -> None:
    """A sprint is closed for the list only when it is done or shipped.

    ``archived`` and ``closed`` are not ``done`` or ``shipped``, so the section
    lists them with the open work. A reader that folds them into the closed set
    drops the one with no close date and files the other beside the recently
    closed, which the order shows.
    """

    for rows in (_discovery_sprints(), _roadmap_sprints()):
        by_id = {row["id"]: row for row in rows}
        assert ARCHIVED in by_id
        assert CLOSED in by_id
        assert by_id[ARCHIVED]["status"] == "archived"
        assert by_id[CLOSED]["status"] == "closed"
        assert by_id[ARCHIVED]["closed_at"] is None
        assert rows.index(by_id[CLOSED]) < rows.index(by_id[RECENT])


def test_summary_keys_have_one_shape_across_the_projects(project) -> None:
    """Every key this node touched carries one type, sprinted or not.

    ``sprints`` is discovery's count and never a row list; the rows ride
    ``summary_sprints``, a list on both surfaces for the sprinted project and
    absent on both for the project that declares no sprint.
    """

    sprinted_discovery = _read_plan_tool(project=PROJECT, view="raw")["data"]["summary"]
    sprinted_roadmap = _roadmap_tool(project=PROJECT, view="summary")
    assert isinstance(sprinted_discovery["sprints"], int)
    assert isinstance(sprinted_discovery["summary_sprints"], list)
    assert isinstance(sprinted_roadmap["summary_sprints"], list)
    assert [row["id"] for row in sprinted_discovery["summary_sprints"]] == [
        row["id"] for row in sprinted_roadmap["summary_sprints"]
    ]

    empty_discovery = _read_plan_tool(project=EMPTY_PROJECT, view="raw")["data"][
        "summary"
    ]
    empty_roadmap = _roadmap_tool(project=EMPTY_PROJECT, view="summary")
    assert isinstance(empty_discovery["sprints"], int)
    assert "summary_sprints" not in empty_discovery
    assert "summary_sprints" not in empty_roadmap


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

    expected = [LIVE, ARCHIVED, CLOSED, OPEN]
    assert [row["id"] for row in _discovery_sprints()] == expected
    assert [row["id"] for row in _roadmap_sprints()] == expected


def test_both_summaries_window_through_the_roadmaps_resolver(
    project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One owner for the window: both surfaces follow the roadmap's resolver.

    The resolver is replaced with one returning a wide sentinel window, so the
    sprint closed forty days ago — outside the default window and outside the
    fixture's configured one — appears only if both summaries read the window
    through that resolver rather than resolving a copy of their own.
    """

    import reckon.roadmap as roadmap_module

    seen: list[str] = []

    def _wide(project_name: str, docs_dir) -> int:
        seen.append(project_name)
        return 100

    monkeypatch.setattr(roadmap_module, "_sprint_recent_days", _wide)

    # Closed rows are ordered by id within their bucket, so the long closed
    # sprint sorts ahead of the recently closed one once the window admits it.
    expected = [LIVE, ARCHIVED, CLOSED, OPEN, OLDER, RECENT]
    assert [row["id"] for row in _discovery_sprints()] == expected
    assert [row["id"] for row in _roadmap_sprints()] == expected
    assert PROJECT in seen


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
