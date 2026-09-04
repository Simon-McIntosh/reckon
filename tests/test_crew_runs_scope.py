"""Workstation run queries preserve project and repository ownership."""

from __future__ import annotations

import json
from pathlib import Path

from reckon import crew, ledger, mcp

PRIMARY_PROJECT = "alpha"


def _repository(root: Path, project: str) -> Path:
    repository = root / f"{project}-repository"
    (repository / "docs" / "state" / project).mkdir(parents=True)
    return repository


def _write_pointer(
    project: str,
    repository: Path,
    *,
    run_id: str,
    node: str,
) -> None:
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": project,
            "repo": str(repository),
            "node": {"id": node, "plan": "run-query"},
            "phase": "working",
            "process_alive": False,
            "worktree": str(repository / "worktree"),
        },
    )


def _write_ledger(project: str, repository: Path, *, run_id: str, node: str) -> None:
    ledger.append_run(
        project,
        ledger.build_record(
            run_id=run_id,
            plan="run-query",
            node=node,
            gate="passed",
            session_id=f"session-{project}",
        ),
        root=repository,
    )


def test_runs_scope_joins_every_configured_project_without_reading_fallback_home(
    isolated_reckon_home: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    fallback_home = tmp_path / "user-home" / ".config" / "reckon"
    fallback_live = fallback_home / "crew" / "live"
    fallback_live.mkdir(parents=True)
    (fallback_live / "unrelated.json").write_text(
        json.dumps(
            {
                "run_id": "fallback-run",
                "project": "fallback",
                "repo": str(tmp_path / "fallback-repository"),
                "node": {"id": "fallback-node"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    fallback_mtime = fallback_home.stat().st_mtime_ns

    primary_repository = _repository(tmp_path, PRIMARY_PROJECT)
    secondary_repository = _repository(tmp_path, "beta")
    absent_repository = tmp_path / "absent-repository"
    mounts = {
        PRIMARY_PROJECT: str(primary_repository / "docs"),
        "beta": str(secondary_repository / "docs"),
        "gamma": str(absent_repository / "docs"),
    }
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps(mounts), encoding="utf-8"
    )

    _write_pointer(
        PRIMARY_PROJECT,
        primary_repository,
        run_id="primary-live",
        node="primary-live-node",
    )
    _write_pointer(
        "beta",
        secondary_repository,
        run_id="secondary-live",
        node="secondary-live-node",
    )
    _write_pointer(
        "gamma",
        absent_repository,
        run_id="absent-live",
        node="absent-live-node",
    )
    _write_ledger(
        PRIMARY_PROJECT,
        primary_repository,
        run_id="primary-ledger",
        node="primary-ledger-node",
    )
    _write_ledger(
        "beta",
        secondary_repository,
        run_id="secondary-ledger",
        node="secondary-ledger-node",
    )

    project_result = mcp._crew(
        PRIMARY_PROJECT,
        view="runs",
        checkout_path=str(primary_repository),
        scope="project",
    )
    assert {row["run_id"] for row in project_result["rows"]} == {
        "primary-live",
        "primary-ledger",
    }

    workstation_result = mcp._crew(
        PRIMARY_PROJECT,
        view="runs",
        checkout_path=str(primary_repository),
        scope="workstation",
    )
    rows = {row["run_id"]: row for row in workstation_result["rows"]}

    assert workstation_result["scope"] == "workstation"
    assert set(rows) == {
        "primary-live",
        "primary-ledger",
        "secondary-live",
        "secondary-ledger",
        "absent-live",
    }
    assert all({"project", "repo", "repo_exists"} <= set(row) for row in rows.values())
    assert rows["primary-ledger"]["project"] == PRIMARY_PROJECT
    assert rows["primary-ledger"]["repo"] == str(primary_repository.resolve())
    assert rows["secondary-ledger"]["project"] == "beta"
    assert rows["secondary-ledger"]["repo"] == str(secondary_repository.resolve())
    assert rows["absent-live"]["project"] == "gamma"
    assert rows["absent-live"]["repo"] == str(absent_repository.resolve())
    assert rows["absent-live"]["repo_exists"] is False
    assert "fallback-run" not in rows
    assert fallback_home.stat().st_mtime_ns == fallback_mtime
