from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

import reckon.serve as serve

PROJECT = "polling"
RUN_FILES = 200


def _project(tmp_path: Path, name: str = PROJECT) -> Path:
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
    state = repo / "docs" / "state" / name
    runs = state / "runs"
    runs.mkdir(parents=True)
    for index in range(RUN_FILES):
        run_id = f"run-{index:03d}"
        (runs / f"{run_id}.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "project": name,
                    "member": "observer",
                    "node": {"plan": "visible-work"},
                }
            ),
            encoding="utf-8",
        )
    _write_aggregate(state, role="review")
    return repo


def _write_aggregate(state: Path, *, role: str) -> None:
    (state / "crew.json").write_text(
        json.dumps(
            {
                "updated": "2026-10-19T00:00:00",
                "project": state.name,
                "doc": "crew",
                "data": {
                    "members": [{"id": "observer", "role": role}],
                    "runs": [],
                    "holds": [],
                    "_version": 1,
                },
            }
        ),
        encoding="utf-8",
    )


def _pointer(project: str = PROJECT) -> dict:
    return {
        "run_id": "run-live",
        "project": project,
        "member": "observer",
        "role": "implement",
        "backend": "codex",
        "agent": {"model": "frontier", "effort": "high"},
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "phase": "working",
        "node": {
            "plan": "visible-work",
            "section": "delivery",
            "role": "implement",
            "done_when": "the route returns the live run",
        },
    }


def _under(path: object, directory: Path) -> bool:
    try:
        candidate = Path(os.path.abspath(os.fspath(path)))  # type: ignore[arg-type]
    except TypeError:
        return False
    return candidate.parent == directory


class _ReadAccount:
    """The stats under ``runs/`` and the bytes read from the aggregate."""

    def __init__(self, state: Path) -> None:
        self.runs = state / "runs"
        self.aggregate = state / "crew.json"
        self.stats = 0
        self.aggregate_bytes = 0


def _account(monkeypatch: pytest.MonkeyPatch, state: Path) -> _ReadAccount:
    account = _ReadAccount(state)
    real_stat = Path.stat
    real_read_text = Path.read_text

    def stat(path: Path, *args: object, **kwargs: object):
        if _under(path, account.runs):
            account.stats += 1
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        text = real_read_text(path, *args, **kwargs)  # type: ignore[arg-type]
        if path == account.aggregate:
            account.aggregate_bytes += len(text.encode())
        return text

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "read_text", read_text)
    return account


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = _project(tmp_path)
    monkeypatch.setattr(serve.crew, "list_live", lambda: [_pointer()])
    monkeypatch.setattr(
        serve, "_log_activity", lambda _pointer: ("2026-10-19T00:00:00Z", 0.0, [])
    )
    monkeypatch.setattr(serve, "_stream_is_terminal", lambda _pointer, _lines: False)
    monkeypatch.setattr(serve, "_ROSTER_CACHE", {}, raising=False)
    return repo


def test_a_poll_does_not_stat_every_run_file_of_an_unchanged_aggregate(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = project / "docs" / "state" / PROJECT
    mounts = {PROJECT: project / "docs"}

    with monkeypatch.context() as warm_patch:
        warm = _account(warm_patch, state)
        rows = serve._crew_rows(mounts)
    assert rows[0]["role"] == "review"
    # The instrument sees the cost it is about to report an absence of.
    assert warm.stats >= RUN_FILES
    assert warm.aggregate_bytes > 0

    with monkeypatch.context() as cold_patch:
        cold = _account(cold_patch, state)
        assert serve._crew_rows(mounts) == rows

    assert cold.stats == 0
    assert cold.aggregate_bytes == 0

    _write_aggregate(state, role="lead")
    assert serve._crew_rows(mounts)[0]["role"] == "lead"