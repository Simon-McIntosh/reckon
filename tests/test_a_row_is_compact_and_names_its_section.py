"""Every crew list view names the section its run was dispatched for.

A run is dispatched for one section of one plan, and four read views list it:
``live``, ``runs``, ``obligations`` and ``directory``. Each routes its rows
through :func:`reckon.crew.query.project_run_rows`, so ``section`` is carried
wherever the run is listed, ``fields=["section"]`` is accepted by all four, and
a field a view does not carry is refused naming the fields it does.

The pointer is written under an isolated reckon home, so nothing outside the
test's own ``tmp_path`` is read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import runs
from reckon.mcp_views import serve_crew_view

PROJECT = "alpha"
RUN_ID = "r-fixture-run"
SESSION = "coordinator-session"
PLAN = "the-plan"
SECTION = "s4"


def _write_pointer(home: Path, *, session: str = SESSION) -> None:
    runs._write_json(
        runs.pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "session": session,
            "process_alive": False,
            "repo": "",
            "worktree": "",
            "node": {
                "id": "a-node",
                "plan": PLAN,
                "section": SECTION,
                "time_budget": "20m",
                "write_paths": ["docs"],
            },
        },
    )


@pytest.fixture()
def fixture(tmp_path: Path, isolated_reckon_home: Path) -> Path:
    repository = tmp_path / "repo"
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    _write_pointer(isolated_reckon_home)
    return repository


def _run_rows(payload: dict) -> list[dict]:
    """Every run row a view answers, whatever shape the view nests them in."""
    if "runs" in payload:
        return list(payload["runs"])
    rows: list[dict] = []
    for key in ("obligations", "acknowledged", "unowned_runs"):
        rows.extend(payload.get(key) or [])
    for coordinator in payload.get("coordinators") or []:
        rows.extend(coordinator.get("runs") or [])
    return rows


def _view_call(view: str, repository: Path, **kwargs) -> dict:
    return serve_crew_view(
        view,
        project=PROJECT,
        checkout_path=str(repository),
        session=SESSION,
        **kwargs,
    )


@pytest.mark.parametrize("view", ("live", "runs", "obligations", "directory"))
def test_every_run_view_names_the_section(view: str, fixture: Path) -> None:
    payload = _view_call(view, fixture)
    rows = _run_rows(payload)
    assert rows, f"{view} listed no run"
    assert [row.get("section") for row in rows] == [SECTION] * len(rows)


@pytest.mark.parametrize("view", ("live", "runs", "obligations", "directory"))
def test_every_run_view_accepts_the_section_field(view: str, fixture: Path) -> None:
    payload = _view_call(view, fixture, fields=["section"])
    rows = _run_rows(payload)
    assert rows
    for row in rows:
        assert row.get("section") == SECTION
        assert set(row) <= {"run_id", "section", "mine"}


@pytest.mark.parametrize("view", ("live", "runs", "obligations", "directory"))
def test_a_field_a_view_lacks_is_refused_naming_its_fields(
    view: str, fixture: Path
) -> None:
    payload = _view_call(view, fixture, fields=["not_a_field_here"])
    assert payload["ok"] is False
    detail = payload["detail"]
    assert "not_a_field_here" in detail
    assert "fields are" in detail