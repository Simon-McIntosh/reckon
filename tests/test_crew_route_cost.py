from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from reckon import ledger
import reckon.serve as serve


def _project(tmp_path: Path, name: str = "active") -> Path:
    repo = tmp_path / name
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "visible-work.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{name}">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="visible-work">'
        '<meta name="plan-sprint" content="current">'
        '<meta name="plan-effort-hours" content="3.25">'
        "<title>Visible work</title>"
        "</head><body></body></html>",
        encoding="utf-8",
    )
    return repo


def _pointer(project: str = "active") -> dict:
    return {
        "run_id": "run-live",
        "project": project,
        "member": "observer",
        "role": "implement",
        "backend": "codex",
        "agent": {"model": "frontier", "effort": "high"},
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "node": {
            "plan": "visible-work",
            "section": "delivery",
            "role": "implement",
            "done_when": "the route returns the live run",
        },
    }


def _isolate_pointer_work(monkeypatch: pytest.MonkeyPatch, pointers: list[dict]) -> None:
    monkeypatch.setattr(serve.crew, "list_live", lambda: pointers)
    monkeypatch.setattr(
        serve,
        "_log_activity",
        lambda _pointer: ("2026-08-25T07:30:00Z", 0.0, []),
    )
    monkeypatch.setattr(serve, "_stream_is_terminal", lambda _pointer, _lines: False)
    monkeypatch.setattr(
        serve,
        "discover_plans",
        lambda *_args, **_kwargs: pytest.fail("crew routes must not discover plans"),
    )


def test_crew_rows_preserve_fields_without_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _project(tmp_path)
    _isolate_pointer_work(monkeypatch, [_pointer()])
    monkeypatch.setattr(
        ledger,
        "load",
        lambda _project, _root: (
            {"members": [{"id": "observer", "role": "review"}]},
            1,
        ),
    )

    rows = serve._crew_rows({"active": repo / "docs"})

    assert len(rows) == 1
    row = rows[0]
    expected_fields = {
        "run_id": "run-live",
        "project": "active",
        "member": "observer",
        "role": "review",
        "plan": "visible-work",
        "section": "delivery",
        "backend": "codex",
        "model": "frontier",
        "effort": "high",
        "effort_hours": 3.25,
        "phase": "working",
        "last_activity": "2026-08-25T07:30:00Z",
        "gate": "the route returns the live run",
        "plan_href": "/active/#plan/visible-work",
        "sprint_href": "/active/#sprint/current",
    }
    assert row.items() >= expected_fields.items()
    assert row["elapsed_seconds"] >= 0


def test_crew_rows_cost_ignores_unreferenced_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _project(tmp_path)
    _isolate_pointer_work(monkeypatch, [_pointer()])
    loaded_projects: list[str] = []

    def load_roster(project: str, _root: Path):
        loaded_projects.append(project)
        return {"members": []}, 1

    monkeypatch.setattr(ledger, "load", load_roster)
    active_mount = {"active": repo / "docs"}
    many_mounts = {
        **active_mount,
        **{
            f"unreferenced-{index}": tmp_path / f"unreferenced-{index}" / "docs"
            for index in range(100)
        },
    }

    one_mount_rows = serve._crew_rows(active_mount)
    many_mount_rows = serve._crew_rows(many_mounts)

    assert many_mount_rows == one_mount_rows
    assert loaded_projects == ["active", "active"]
