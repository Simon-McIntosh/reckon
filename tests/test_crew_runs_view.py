"""The crew runs view joins live and committed identity without fleet noise."""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import crew, ledger, mcp

PROJECT = "proj"
DEFAULT_FIELDS = {
    "run_id",
    "node",
    "plan",
    "section",
    "source",
    "classification",
    "process_alive",
    "session_id",
    "session_id_source",
    "worktree",
    "worktree_exists",
    "transcript_path",
    "transcript_exists",
    "resumable",
    "resumable_reason",
}


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    return root


def _write_live(
    home: Path,
    run_id: str,
    *,
    node: str,
    plan: str = "plan-a",
    section: str = "§2",
    session_id: str = "session-a",
    member: str = "member-a",
    resumable: bool | None = None,
) -> None:
    worktree = home / "worktrees" / run_id
    worktree.mkdir(parents=True)
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "node": {"id": node, "plan": plan, "section": section},
        "phase": "starting",
        "process_alive": True,
        "session_id": session_id,
        "member": member,
        "agent": {"backend": "alpha"},
        "base_sha": "abc123",
        "worktree": str(worktree),
        "manifest_path": str(home / "manifests" / f"{run_id}.md"),
        "log_path": str(home / "logs" / f"{run_id}.jsonl"),
    }
    if resumable is not None:
        record["resumable"] = resumable
    crew._write_json(crew.pointer_path(run_id), record)


def _write_ledger(repository: Path, run_id: str, *, node: str) -> None:
    ledger.append_run(
        PROJECT,
        ledger.build_record(
            run_id=run_id,
            plan="plan-a",
            section="§2",
            node=node,
            gate="passed",
            member_id="member-b",
            session_id="session-b",
            base_sha="def456",
            commits=["fedcba"],
        ),
        root=repository,
    )


def test_runs_view_joins_sources_orders_attempts_and_stays_compact(
    isolated_reckon_home: Path,
    repository: Path,
) -> None:
    _write_live(
        isolated_reckon_home,
        "r-20260904T100003000000-node-a",
        node="node-a",
    )
    _write_live(
        isolated_reckon_home,
        "r-20260904T100001000000-node-a",
        node="node-a",
    )
    _write_ledger(
        repository,
        "r-20260904T100002000000-node-b",
        node="node-b",
    )

    result = mcp._crew(PROJECT, view="runs", checkout_path=str(repository))

    assert result["ok"] is True
    assert [row["run_id"] for row in result["rows"]] == [
        "r-20260904T100003000000-node-a",
        "r-20260904T100002000000-node-b",
        "r-20260904T100001000000-node-a",
    ]
    assert [row["source"] for row in result["rows"]] == [
        "live",
        "ledger",
        "live",
    ]
    assert all(set(row) == DEFAULT_FIELDS for row in result["rows"])
    assert "watcher" not in result
    assert "followers" not in result

    by_node = mcp._crew(
        PROJECT,
        view="runs",
        checkout_path=str(repository),
        node="node-a",
    )
    assert [row["run_id"] for row in by_node["rows"]] == [
        "r-20260904T100003000000-node-a",
        "r-20260904T100001000000-node-a",
    ]

    newest = mcp._crew(
        PROJECT,
        view="runs",
        checkout_path=str(repository),
        newest_per_node=True,
    )
    assert [row["node"] for row in newest["rows"]] == ["node-a", "node-b"]

    assert [
        row["run_id"]
        for row in mcp._crew(
            PROJECT,
            view="runs",
            checkout_path=str(repository),
            source="live",
            limit=1,
        )["rows"]
    ] == ["r-20260904T100003000000-node-a"]
    assert {
        row["source"]
        for row in mcp._crew(
            PROJECT,
            view="runs",
            checkout_path=str(repository),
            source="ledger",
        )["rows"]
    } == {"ledger"}


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"plan": "plan-a"}, 1),
        ({"section": "2"}, 1),
        ({"session": "session-a"}, 1),
        ({"member": "member-a"}, 1),
        ({"classification": "running"}, 1),
        ({"resumable": True}, 1),
        ({"node": "unknown"}, 0),
    ],
)
def test_runs_view_applies_each_compact_filter(
    isolated_reckon_home: Path,
    repository: Path,
    filters: dict[str, object],
    expected: int,
) -> None:
    _write_live(
        isolated_reckon_home,
        "r-20260904T100001000000-node-a",
        node="node-a",
        resumable=True,
    )

    result = mcp._crew(
        PROJECT,
        view="runs",
        checkout_path=str(repository),
        **filters,
    )

    assert result["count"] == expected
    assert len(result["rows"]) == expected


def test_runs_view_adds_only_requested_optional_fields(
    isolated_reckon_home: Path,
    repository: Path,
) -> None:
    _write_live(
        isolated_reckon_home,
        "r-20260904T100001000000-node-a",
        node="node-a",
    )

    result = mcp._crew(
        PROJECT,
        view="runs",
        checkout_path=str(repository),
        fields=["member", "commits"],
    )

    assert set(result["rows"][0]) == DEFAULT_FIELDS | {"member", "commits"}
    assert result["rows"][0]["member"] == "member-a"
    assert result["rows"][0]["commits"] == []


def test_runs_view_rejects_invalid_bounds(repository: Path) -> None:
    invalid_source = mcp._crew(
        PROJECT,
        view="runs",
        checkout_path=str(repository),
        source="archive",
    )
    invalid_limit = mcp._crew(
        PROJECT,
        view="runs",
        checkout_path=str(repository),
        limit=0,
    )
    invalid_field = mcp._crew(
        PROJECT,
        view="runs",
        checkout_path=str(repository),
        fields=["prompt"],
    )

    assert invalid_source["error"] == "crew_error"
    assert invalid_limit["error"] == "crew_error"
    assert invalid_field["error"] == "crew_error"
