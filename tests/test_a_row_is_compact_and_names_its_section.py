"""Every crew list view names the section its run was dispatched for.

A run is dispatched for one section of one plan, and four read views list it:
``live``, ``runs``, ``obligations`` and ``directory``. Each builds its rows and
routes them through :func:`reckon.crew.query.project_run_rows`, so ``section``
is carried wherever the run is listed, ``fields=["section"]`` is accepted by all
four, and a field a view does not carry is refused naming the fields it does.

The repository, the ledger and the live pointers are synthesised under the
test's own ``tmp_path`` and isolated reckon home, so nothing outside them is
read.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon.crew import runs
from reckon.crew.node import normalize_section
from reckon.mcp_views import serve_crew_view

PROJECT = "alpha"
BRANCH = "main"
RUN_ID = "r-fixture-run"
SESSION = "coordinator-session"
PLAN = "the-plan"
#: What a caller dispatched with. The row carries the canonical spelling of the
#: same identity, which is the one grammar's `#section` figure reduced to its
#: canonical form, so the four views cannot disagree about which section it is.
SECTION = "s4"
CANONICAL_SECTION = normalize_section(SECTION)

_VIEWS = ("live", "runs", "obligations", "directory")

#: The identity a worker's own git wrapper reads; dropped so the synthesised
#: repository is the only one in play.
_DISPATCH_IDENTITY = ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT")


def _git(repo: Path, *arguments: str) -> str:
    base = {**os.environ}
    for name in _DISPATCH_IDENTITY:
        base.pop(name, None)
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=base,
    )
    return result.stdout.strip()


def _write_pointer(
    run_id: str,
    *,
    session: str,
    plan: str,
    section: str,
    worktree: Path,
    manifest_path: Path,
) -> None:
    """One live pointer, reading as a run whose worker asked for help.

    A run that merely vanished owes no duty, so the obligations view would list
    nothing and the section it carries could not be read there. The manifest's
    own help line is what makes the run an outstanding duty.
    """
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": session,
            "phase": "blocked",
            "process_alive": False,
            "repo": str(worktree),
            "worktree": str(worktree),
            "manifest_path": str(manifest_path),
            "node": {
                "id": "a-node",
                "plan": plan,
                "section": section,
                "time_budget": "20m",
                "write_paths": ["docs"],
            },
        },
    )


@pytest.fixture()
def fleet(tmp_path: Path, isolated_reckon_home: Path) -> Path:
    """One mounted repository with a seeded ledger, plus one live pointer."""
    repo = tmp_path / "code" / PROJECT
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", BRANCH)
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "fixture")
    ledger = {
        "data": {
            "members": [],
            "runs": [
                {
                    "run_id": RUN_ID,
                    "node": "a-node",
                    "plan": PLAN,
                    "session": SESSION,
                    "role": "implement",
                    "gate": "passed",
                    "backend": "claude",
                    "dispatched_at": "2026-09-01T00:00:00Z",
                    "completed_at": "2026-09-01T00:01:40Z",
                    "worker_seconds": 100,
                    "lineage": {},
                    "attempt": 1,
                }
            ],
            "holds": [],
        }
    }
    state_dir = repo / "docs" / "state" / PROJECT
    state_dir.mkdir(parents=True)
    (state_dir / "crew.json").write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", f"docs/state/{PROJECT}/crew.json", "seed.txt")
    _git(repo, "commit", "-q", "-m", "chore: seed the fixture")
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    manifest = repo / "manifest.md"
    manifest.write_text(
        "NEEDS-HELP: the fixture worker cannot parse its input\n"
        "status: blocked\n\n"
        "tried: nothing\noptions: none\nleaning: none\ncost-if-wrong: none\n",
        encoding="utf-8",
    )
    _write_pointer(
        RUN_ID,
        session=SESSION,
        plan=PLAN,
        section=SECTION,
        worktree=repo,
        manifest_path=manifest,
    )
    return repo


def _run_rows(payload: dict) -> list[dict]:
    """Every run row a view answers, whatever shape the view nests them in."""
    if "rows" in payload:
        return list(payload["rows"])
    if "runs" in payload:
        return list(payload["runs"])
    rows: list[dict] = []
    for key in ("obligations", "acknowledged", "unowned_runs"):
        rows.extend(payload.get(key) or [])
    for coordinator in payload.get("coordinators") or []:
        rows.extend(coordinator.get("runs") or [])
    return rows


def _view_call(view: str, repository: Path, **kwargs) -> dict:
    """Drive one view for the fixture's single run.

    ``obligations`` is derived for one session and is read with one; ``runs``
    reads both the live pointers and the committed ledger, so it is pinned to
    the live source, where the fixture's run is the one row, rather than being
    joined with a ledger row that records no section of its own.
    """
    arguments: dict = {"project": PROJECT, "checkout_path": str(repository)}
    if view == "runs":
        arguments["source"] = "live"
    else:
        arguments["session"] = SESSION
    return serve_crew_view(view, **arguments, **kwargs)


@pytest.mark.parametrize("view", _VIEWS)
def test_every_run_view_names_the_section(view: str, fleet: Path) -> None:
    payload = _view_call(view, fleet)
    rows = _run_rows(payload)
    assert rows, f"{view} listed no run: {payload.get('error')} {payload.get('detail')}"
    assert [row.get("section") for row in rows] == [CANONICAL_SECTION] * len(rows)


@pytest.mark.parametrize("view", _VIEWS)
def test_every_run_view_accepts_the_section_field(view: str, fleet: Path) -> None:
    payload = _view_call(view, fleet, fields=["section"])
    rows = _run_rows(payload)
    assert rows, f"{view} listed no run: {payload.get('error')} {payload.get('detail')}"
    for row in rows:
        assert row.get("section") == CANONICAL_SECTION
        # The projection always carries the identity of the run it describes,
        # so a caller that asked for one field can still tell which run
        # answered. Nothing else may ride along.
        assert set(row) <= {"run_id", "node", "plan", "section", "mine"}


@pytest.mark.parametrize("view", _VIEWS)
def test_a_field_a_view_lacks_is_refused_naming_its_fields(
    view: str, fleet: Path
) -> None:
    payload = _view_call(view, fleet, fields=["not_a_field_here"])
    assert payload["ok"] is False
    detail = payload["detail"]
    assert "not_a_field_here" in detail
    assert "fields are" in detail
