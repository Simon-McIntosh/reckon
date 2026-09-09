"""The crew runs view joins live and committed identity without fleet noise."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew, ledger, mcp

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
    process_alive: bool = True,
) -> None:
    worktree = home / "worktrees" / run_id
    worktree.mkdir(parents=True)
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "node": {"id": node, "plan": plan, "section": section},
        "phase": "starting",
        "process_alive": process_alive,
        "session_id": session_id,
        "member": member,
        "agent": {"backend": "alpha"},
        "base_sha": "abc123",
        "worktree": str(worktree),
        "manifest_path": str(home / "manifests" / f"{run_id}.md"),
        "log_path": str(home / "logs" / f"{run_id}.jsonl"),
    }
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


def _write_plan(
    home: Path,
    repository: Path,
    *,
    slug: str,
    declarations: dict[str, str] | None = None,
) -> None:
    plans = repository / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    declaration_meta = ""
    if declarations is not None:
        declaration_meta = (
            '<meta name="plan-section-declarations" content="'
            + json.dumps(declarations).replace('"', "&quot;")
            + '">'
        )
    sections = "".join(
        f'<h2 id="{section}">{section}</h2>'
        for section in (declarations or {"s1": "implementable"})
    )
    (plans / f"{slug}.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        f"{declaration_meta}"
        f"</head><body>{sections}</body></html>",
        encoding="utf-8",
    )
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
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
    unknown = mcp._crew(
        PROJECT,
        view="runs",
        checkout_path=str(repository),
        node="unknown",
    )
    assert unknown["count"] == 0

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
        ({"resumable": False}, 1),
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
    )

    result = mcp._crew(
        PROJECT,
        view="runs",
        checkout_path=str(repository),
        **filters,
    )

    assert result["count"] == expected
    assert len(result["rows"]) == expected
    if filters == {"resumable": False}:
        assert result["rows"][0]["process_alive"] is True
        assert result["rows"][0]["resumable_reason"] == "the run's process is alive"


def test_runs_view_filters_a_recoverable_dead_process(
    isolated_reckon_home: Path,
    repository: Path,
) -> None:
    _write_live(
        isolated_reckon_home,
        "r-20260904T100001000000-node-a",
        node="node-a",
        process_alive=False,
    )

    result = mcp._crew(
        PROJECT,
        view="runs",
        checkout_path=str(repository),
        resumable=True,
    )

    assert result["count"] == 1
    assert result["rows"][0]["worktree_exists"] is True
    assert result["rows"][0]["session_id_source"] == "pointer"
    assert result["rows"][0]["resumable"] is True


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


def test_drain_surfaces_share_a_nonzero_remainder_for_a_reconciled_stop(
    isolated_reckon_home: Path,
    repository: Path,
) -> None:
    _write_plan(
        isolated_reckon_home,
        repository,
        slug="plan-a",
        declarations={"s1": "implementable", "s2": "implementable"},
    )
    _write_live(isolated_reckon_home, "r-reconciled", node="node-a")
    crew.record_run_disposition("r-reconciled", "handed-off", project=PROJECT)

    command = CliRunner().invoke(cli.main, ["crew", "drain", "--project", PROJECT])
    command_payload = json.loads(command.output)
    tool_payload = mcp._crew(PROJECT, view="drain")

    assert command.exit_code == 0, command.output
    for payload in (command_payload, tool_payload):
        assert payload["unreconciled_runs"] == 0
        assert payload["executable_remainder"] == 2
        assert payload["drained"] is False
    assert (
        command_payload["executable_remainder"] == tool_payload["executable_remainder"]
    )


def test_drain_reports_an_undeclared_plan_as_unknown(
    isolated_reckon_home: Path,
    repository: Path,
) -> None:
    _write_plan(isolated_reckon_home, repository, slug="plan-a")

    report = crew.drain(PROJECT)

    assert report["executable_remainder"] is None
    assert report["executable_remainder"] != 0
    assert report["drained"] is False


def test_drain_reports_zero_for_a_fully_done_declaration(
    isolated_reckon_home: Path,
    repository: Path,
) -> None:
    _write_plan(
        isolated_reckon_home,
        repository,
        slug="plan-a",
        declarations={"s1": "done", "s2": "deferred"},
    )

    report = crew.drain(PROJECT)

    assert report["unreconciled_runs"] == 0
    assert report["executable_remainder"] == 0
    assert report["drained"] is True


def test_drain_keeps_unreconciled_pointer_count_independent_of_remainder(
    isolated_reckon_home: Path,
    repository: Path,
) -> None:
    _write_plan(
        isolated_reckon_home,
        repository,
        slug="plan-a",
        declarations={"s1": "done"},
    )
    _write_live(isolated_reckon_home, "r-unreconciled", node="node-a")

    report = crew.drain(PROJECT)

    assert report["unreconciled_runs"] == 1
    assert report["executable_remainder"] == 0
    assert report["drained"] is False


def test_drain_sums_declared_remainders_across_project_plans(
    isolated_reckon_home: Path,
    repository: Path,
) -> None:
    _write_plan(
        isolated_reckon_home,
        repository,
        slug="plan-a",
        declarations={"s1": "implementable", "s2": "done"},
    )
    _write_plan(
        isolated_reckon_home,
        repository,
        slug="plan-b",
        declarations={"s1": "implementable", "s2": "implementable"},
    )

    report = crew.drain(PROJECT)

    assert report["executable_remainder"] == 3
    assert report["drained"] is False
