"""A regenerated path is claimed like a write and charged a fixed reference cost.

A node whose deliverable is a large generated record overwrites it wholesale and
never reads it, yet the context census charged the whole file because it was
declared as a write path. Declaring the same path with ``--regenerate-path``
holds it as a write path for every scope purpose — the peer check, the scope
conflict name and the live pointer all see it — while the census charges one
fixed reference cost independent of the file's size, recorded with provenance
``regenerated`` so a reader sees what was charged and why.

The fit measure exercises the routing verdict that emits the refusal
(``_competence_verdict`` through ``_context_fit_verdict``), so the accepted and
refused arms are the ones a real dispatch builds rather than hand-written
mappings.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import capability, crew, crew_dispatch_commands
from reckon import cli as cli_module
from reckon.crew import routing
from reckon.crew.routing import _context_file_inputs

PROJECT = "proj"
PLAN = "plan-a"
LARGE_PATH = "docs/research/generated.html"
LARGE_BYTES = 1_000_000
REFERENCE_TOKENS = routing._REGENERATED_PATH_REFERENCE_TOKENS
STANDING_TOKENS = 20_000
WINDOW_TOKENS = 230_144
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


def _node(*, write_paths: list[str], regenerate_paths: list[str]) -> crew.TaskNode:
    return crew.TaskNode(
        id=PLAN,
        goal="regenerate a large record",
        plan=PLAN,
        section="s9",
        role="implement",
        spec_level="exact",
        done_when="the census charges the regenerated path its reference cost",
        write_paths=write_paths,
        regenerate_paths=regenerate_paths,
        time_budget="20m",
    )


def _authority(repo: Path) -> dict[str, object]:
    return {
        "plan": {
            "project": PROJECT,
            "repository": str(repo),
            "docs": str(repo / "docs"),
        }
    }


def _records_by_path(records: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(record["declared"]): record for record in records}


def _seed_large_file(repo: Path) -> None:
    target = repo / LARGE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x" * LARGE_BYTES)


def test_a_regenerated_path_is_charged_its_reference_cost(tmp_path: Path) -> None:
    """The census charges the reference cost and records regenerated provenance."""
    _seed_large_file(tmp_path)
    node = _node(write_paths=[], regenerate_paths=[LARGE_PATH])

    tokens, inputs = _context_file_inputs(tmp_path, node, _authority(tmp_path))
    record = _records_by_path(inputs["write_paths"])[LARGE_PATH]

    assert record["provenance"] == "regenerated"
    assert record["estimated_tokens"] == REFERENCE_TOKENS
    assert record["reference_tokens"] == REFERENCE_TOKENS
    assert record["counted"] is True
    # The real size is recorded beside the charge, so the exemption is visible
    # rather than reading as a file that happens to be small.
    assert record["bytes"] == LARGE_BYTES
    assert tokens == REFERENCE_TOKENS


def test_an_ordinary_write_path_is_still_charged_at_full(tmp_path: Path) -> None:
    """Declaring the same file as a plain write path charges every byte."""
    _seed_large_file(tmp_path)
    node = _node(write_paths=[LARGE_PATH], regenerate_paths=[])

    tokens, inputs = _context_file_inputs(tmp_path, node, _authority(tmp_path))
    record = _records_by_path(inputs["write_paths"])[LARGE_PATH]

    assert record["provenance"] == "declared"
    assert record["estimated_tokens"] > LARGE_BYTES // 4
    assert tokens == record["estimated_tokens"]
    assert tokens > REFERENCE_TOKENS


def _backend_settings() -> dict[str, Any]:
    return {
        "backend": "worker",
        "launch": "cli",
        "model": "deepseek-v4-flash",
        "effort": "high",
        "sandbox": "worktree-full",
        "usable_input_window": WINDOW_TOKENS,
    }


def _fit_verdict(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    node: crew.TaskNode,
) -> dict[str, Any]:
    """Build the routing context-fit verdict a dispatch reads for one node."""
    monkeypatch.setattr(
        routing,
        "_standing_context_input",
        lambda *_args, **_kwargs: (
            STANDING_TOKENS,
            {"effective_tokens": STANDING_TOKENS},
        ),
    )
    resolution = crew.DispatchPlan(
        run_id="run",
        backend="worker",
        launch="cli",
        backend_settings=_backend_settings(),
        node=node,
        budget_ceiling="1h",
        validation=crew.NodeValidation(ok=True),
        execution_fit=capability.ExecutionFit(
            role="implement",
            execution_capable=True,
            matched_measure=None,
            override=False,
        ),
    )
    return routing._competence_verdict(
        resolution=resolution,
        project=PROJECT,
        repo=repo,
        verdict_inputs={
            "node_estimate": (2.0, "plan-fallback"),
            "capability_cache": {"configurations": []},
            "cache_status": "untracked",
        },
    )


def test_a_regenerated_path_fits_a_window_the_write_path_exceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same 1 MB file is admitted as regenerated and refused as a write."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_large_file(repo)

    regenerated = _fit_verdict(
        repo, monkeypatch, _node(write_paths=[], regenerate_paths=[LARGE_PATH])
    )
    written = _fit_verdict(
        repo, monkeypatch, _node(write_paths=[LARGE_PATH], regenerate_paths=[])
    )

    # Both arms read the same file at the same size, so the two verdicts differ
    # only in what each declaration was charged.
    assert (repo / LARGE_PATH).stat().st_size == LARGE_BYTES
    assert regenerated["allowed"] is True
    assert not str(regenerated["reason"]).startswith("context-window-exceeded")
    assert written["allowed"] is False
    assert str(written["reason"]).startswith("context-window-exceeded")
    assert written["estimated_tokens"] > WINDOW_TOKENS


def _snapshot(path: Path) -> tuple[str, ...]:
    if not path.is_dir():
        return ()
    return tuple(sorted(item.name for item in path.iterdir()))


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
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
    _seed_large_file(root)
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "docs", "skills"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({"proj": str(root / "docs")}), encoding="utf-8"
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *a, **k: CONFIG
    )
    return root


def _dispatch_arguments(
    repo: Path, node_id: str, *, regenerate: tuple[str, ...]
) -> list[str]:
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
        "regenerate one large record",
        "--done-when",
        "pytest reports the census charged the regenerated path its reference cost",
        "--session",
        "dry-run-session",
        "--repo",
        str(repo),
        "--dry-run",
    ]
    for path in regenerate:
        arguments += ["--regenerate-path", path]
    return arguments


def _live_holder(
    repo: Path, run_id: str, node_id: str, regenerated: tuple[str, ...]
) -> None:
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repo.resolve()),
            "phase": "working",
            "pid": os.getpid(),
            "launcher_host": socket.gethostname(),
            "node": {
                "id": node_id,
                "plan": PLAN,
                "write_paths": list(regenerated),
                "regenerate_paths": list(regenerated),
            },
        },
    )


def test_a_live_claim_on_a_regenerated_path_refuses_a_second_dispatch(
    home: Path, repo: Path
) -> None:
    """A peer holding the regenerated path is named in a scope-conflict refusal."""
    _live_holder(repo, "r-holder", "node-holder", (LARGE_PATH,))

    result = CliRunner().invoke(
        cli_module.main,
        _dispatch_arguments(repo, "node-later", regenerate=(LARGE_PATH,)),
    )
    payload = json.loads(result.output)

    assert result.exit_code == 7, result.output
    assert payload["ok"] is False
    assert payload["error"] == "scope-conflict"
    assert payload["conflicting_run_id"] == "r-holder"
    assert payload["candidate_path"] == LARGE_PATH


def test_the_live_pointer_carries_the_regenerated_paths(home: Path, repo: Path) -> None:
    """The pointer node records the regenerated set, and it is a claim."""
    node = _node(write_paths=[LARGE_PATH], regenerate_paths=[LARGE_PATH])

    definition = node.as_dict()
    assert definition["regenerate_paths"] == [LARGE_PATH]
    # A regenerated path is a write path for every scope purpose.
    assert LARGE_PATH in definition["write_paths"]

    _live_holder(repo, "r-holder", "node-holder", (LARGE_PATH,))
    pointer = json.loads(crew.pointer_path("r-holder").read_text(encoding="utf-8"))
    assert pointer["node"]["regenerate_paths"] == [LARGE_PATH]
    claimed = {claim["path"] for claim in crew.scope_claims(PROJECT, repo)}
    assert LARGE_PATH in claimed
