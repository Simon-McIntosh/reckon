"""A granted landing path is a claim the conflict report compares like any other.

Dispatch grants the plan file, its cumulative evidence record and its figures
topic as written when a node declares them, because every node on the plan
appends its own landing record to them. The refusal admits the second holder so
the appends can meet in a merge, but the report must still name the live run
that already holds the path — a silent second holder is how a whole-file write
took a predecessor's landed record once.

The report is asserted through the dry-run dispatch a coordinator actually
reads (``live_conflicts``), not by reading the pointer files back.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, crew_dispatch_commands

PROJECT = "proj"
PLAN = "plan-a"
SHARED = (
    "docs/plans/plan-a.html",
    "docs/evidence/archive/plan-a-landed.html",
    "docs/figures/plan-a",
)
EXCLUSIVE = "package/target.py"
CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "worker",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


def _snapshot(path: Path) -> tuple[str, ...]:
    if not path.is_dir():
        return ()
    return tuple(sorted(item.name for item in path.iterdir()))


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the crew config home at a temp dir and prove the real one is idle."""
    real_live = crew.live_dir()
    before = _snapshot(real_live)
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    yield config_home
    assert _snapshot(real_live) == before


@pytest.fixture()
def repo(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "package").mkdir()
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "docs" / "plans" / "plan-a.html").write_text(
        '<meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="plan-a">'
        '<h2 id="s1">s1</h2>',
        encoding="utf-8",
    )
    (root / "package" / "target.py").write_text("value = 1\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "docs", "package", "skills"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({"proj": str(root / "docs")}), encoding="utf-8"
    )
    monkeypatch.setattr(crew_dispatch_commands, "_resolved_flight", lambda *a, **k: CONFIG)
    return root


def _dispatch_arguments(repo: Path, node_id: str, paths: tuple[str, ...]) -> list[str]:
    arguments = [
        "crew",
        "dispatch",
        "--project",
        PROJECT,
        "--plan",
        PLAN,
        "--section",
        "s1",
        "--spec-level",
        "exact",
        "--node",
        node_id,
        "--goal",
        "record one dispatch readiness result",
        "--done-when",
        "pytest reports the conflict projection with zero failures",
        "--session",
        "dry-run-session",
        "--repo",
        str(repo),
        "--dry-run",
    ]
    for path in paths:
        arguments += ["--write-path", path]
    return arguments


def _dry_run(repo: Path, node_id: str, paths: tuple[str, ...]) -> dict:
    result = CliRunner().invoke(
        cli_module.main, _dispatch_arguments(repo, node_id, paths)
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _refused_dry_run(repo: Path, node_id: str, paths: tuple[str, ...]) -> dict:
    result = CliRunner().invoke(
        cli_module.main, _dispatch_arguments(repo, node_id, paths)
    )
    assert result.exit_code == 2, result.output
    return json.loads(result.output)


def _live_holder(repo: Path, run_id: str, node_id: str, paths: tuple[str, ...]) -> None:
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repo.resolve()),
            "phase": "working",
            "pid": os.getpid(),
            "launcher_host": socket.gethostname(),
            "node": {"id": node_id, "plan": PLAN, "write_paths": list(paths)},
        },
    )


def _landing_pairs(payload: dict) -> list[tuple[str, str]]:
    return [
        (entry["left_path"], entry["right_path"])
        for row in payload["live_conflicts"]
        for entry in row["paths"]
    ]


def test_a_granted_landing_collision_is_reported(home: Path, repo: Path) -> None:
    _live_holder(repo, "r-holder", "node-holder", SHARED)

    payload = _dry_run(repo, "node-later", SHARED)

    rows = payload["live_conflicts"]
    assert rows, "a granted landing collision must be reported"
    assert {row["run_id"] for row in rows} == {"r-holder"}
    assert {row["node"] for row in rows} == {"node-holder"}
    assert {row["candidate"] for row in rows} == {"node-later"}
    assert {row["claimed_path"] for row in rows} == set(SHARED)
    assert {left for left, _claim in _landing_pairs(payload)} >= set(SHARED)
    assert {claim for _left, claim in _landing_pairs(payload)} == set(SHARED)


def test_a_caller_named_collision_is_reported_as_before(home: Path, repo: Path) -> None:
    _live_holder(repo, "r-holder", "node-holder", (EXCLUSIVE,))

    result = CliRunner().invoke(
        cli_module.main, _dispatch_arguments(repo, "node-later", (EXCLUSIVE,))
    )
    payload = json.loads(result.output)

    assert result.exit_code == 7
    assert payload["ok"] is False
    assert payload["error"] == "scope-conflict"
    assert payload["conflicting_run_id"] == "r-holder"
    assert payload["candidate_path"] == EXCLUSIVE
    assert payload["claimed_path"] == EXCLUSIVE
    assert payload["live_conflicts"] == [
        {
            "candidate": "node-later",
            "run_id": "r-holder",
            "node": "node-holder",
            "claimed_path": EXCLUSIVE,
            "paths": [{"left_path": EXCLUSIVE, "right_path": EXCLUSIVE}],
        }
    ]


def test_a_promoted_holder_is_not_reported(home: Path, repo: Path) -> None:
    _live_holder(repo, "r-holder", "node-holder", SHARED)
    claimed = {claim["path"] for claim in crew.scope_claims(PROJECT, repo)}
    assert set(SHARED) <= claimed

    # Promotion removes the live pointer, which releases the claim.
    crew.pointer_path("r-holder").unlink()

    assert _dry_run(repo, "node-later", SHARED)["live_conflicts"] == []


def test_a_peer_over_the_figures_parent_is_refused_as_at_base(
    home: Path, repo: Path
) -> None:
    # The peer holds docs/figures, the parent of the granted figures topic, so
    # its claim is the coarser one and the dispatch is refused. Reporting the
    # granted collision must not turn that refusal into an admission.
    _live_holder(repo, "r-holder", "node-holder", ("docs/figures",))

    payload = _refused_dry_run(repo, "node-later", SHARED)

    assert payload["validation"]["ok"] is False
    assert "directory write path overlaps a live claim" in payload["detail"]
