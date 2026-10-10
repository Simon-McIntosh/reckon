"""Every billing reader resolves with the row's project.

A project's flight layer can move one of its lanes into a subscription budget
group, and that override is a property of the project rather than of the lane:
the same backend stays metered on another project's rows. Two writers read a
lane's billing without naming the project, so the override reached the read
surfaces but not these:

* the promotion writer stored the row's budget block without folding the
  project layer in, so a project that moved a lane into a subscription group
  still recorded its harness per-token figure as spend;
* the picker's outcome summary resolved each backend's subscription label
  against no project, so a project override never reached that label.

Both are exercised here by promoting a run through the same path
``reckon crew complete`` takes, and by summarizing the picker's rows, with a
project whose flight layer moves the lane into a subscription group.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew, flight, ledger
from reckon.crew.picker import outcomes
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
OTHER_PROJECT = "other"
PLAN = "plan-a"
LANE = "clive"
SUBSCRIPTION_GROUP = "claude-sub"

# A host layer declaring the lane under test, so the catalogue and the project
# layer both have a backend to name. The lane is left metered here; one
# project's flight layer moves it into a subscription group.
HOST_LAYER = """\
version: 1

backends:
  clive:
    command: /opt/backends/bin/clive
"""

# The project layer that moves the host's metered lane into a subscription
# group. The group name ends in ``-sub``, which is what the billing reader
# treats as a flat subscription rather than a metered lane.
PROJECT_FLIGHT = """\
version: 1

backends:
  clive:
    budget_group: claude-sub
"""

# The argv a run records: a resolved harness the promotion can translate.
HARNESS_EXECUTABLE = "/opt/backends/bin/clive"
ARGV = [HARNESS_EXECUTABLE, "-p", "--output-format", "stream-json", "--verbose"]


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git worktree with one mounted plan, plus a host flight we own."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    (config_home / "flight.yaml").write_text(HOST_LAYER, encoding="utf-8")
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(config_home / "flight.yaml"))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _write_project_flight(project: str, repository: Path) -> Path:
    """Write a project layer where the resolver reads it, through its mount."""
    path = flight.project_config_path(project, checkout_path=repository)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PROJECT_FLIGHT, encoding="utf-8")
    return path


def _write_stream(path: Path) -> Path:
    """A finished turn's stream carrying a harness-reported per-token cost."""
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {"type": "system", "subtype": "init", "session_id": "s-billing"},
        {
            "type": "result",
            "subtype": "success",
            "terminal_reason": "end_turn",
            "is_error": False,
            "num_turns": 1,
            "duration_api_ms": 1000,
            "total_cost_usd": 1.61,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "result": "done",
            "timestamp": "2026-09-20T00:00:00Z",
        },
    ]
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    return path


def _record(repository: Path, run_id: str, stream: Path) -> dict:
    """A run as dispatch records it, naming its lane and its translated stream."""
    return {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(repository),
        "launch": "cli",
        "role": "implement",
        "backend": LANE,
        "command": HARNESS_EXECUTABLE,
        "dialect": "claude",
        "argv": list(ARGV),
        "log_path": str(stream),
        "created_at": "2026-09-20T00:00:00Z",
        "manifest_path": "/durable/manifest.md",
        "node": {
            "id": "billing-readers",
            "plan": PLAN,
            "section": "billing-readers",
            "time_budget": "25m",
            "write_paths": [],
        },
    }


def _picker_row(name: str, backend: str) -> dict:
    return {
        "run_id": name,
        "node": name,
        "plan": "sample",
        "role": "implement",
        "spec_level": "guided",
        "backend": backend,
        "route_mode": "picker",
        "gate": "passed",
        "outcome": "",
        "review": {"total": 80},
        "wall_seconds": 100,
        "dispatched_at": "2026-09-20T04:00:00Z",
        "completed_at": "2026-09-20T04:00:00Z",
        "picker_selection": {
            "action": "route",
            "backend": backend,
            "confidence": 0.4,
            "latency_ms": 100,
            "fallback_reason": None,
            "offered": [
                {"backend": backend, "family": "local", "burn_multiple": 1.5},
            ],
        },
    }


def test_a_promotion_in_a_moved_project_stores_no_per_token_spend(
    repository: Path, tmp_path: Path
) -> None:
    """The promotion writer folds the row's project into the stored budget.

    The project's flight layer moves ``clive`` into ``claude-sub``; the host
    layer leaves it metered. A stored row for that project must carry no
    per-token spend — nulled, flagged imputed and naming the group — because the
    promotion resolves billing with the record's project rather than with no
    project at all.
    """
    _write_project_flight(PROJECT, repository)
    run_id = "r-20260920T000000000001-billing"
    stream = _write_stream(tmp_path / "stream.jsonl")
    _write_json(pointer_path(run_id), _record(repository, run_id, stream))

    crew.complete(run_id, gate="passed", root=repository)

    row = ledger.load(PROJECT, repository)[0]["runs"][0]
    budget = row["budget"]

    assert budget["cost_usd"] is None
    assert budget["cost_usd_cumulative"] is None
    assert budget["cost_usd_imputed"] is True
    assert budget["billing"] == "subscription"
    assert budget["budget_group"] == SUBSCRIPTION_GROUP
    # The harness figure survives under a name that cannot be summed as spend.
    assert budget["harness_reported_cost_usd"] == 1.61


def test_the_picker_summary_labels_the_moved_lane_for_its_own_project(
    repository: Path,
) -> None:
    """The picker's subscription label resolves with each row's project.

    ``clive`` is moved into a subscription group on ``proj`` and left metered
    elsewhere, so the summary's ``subscription_backends`` names it for the
    project whose flight says so and does not name it for another.
    """
    _write_project_flight(PROJECT, repository)
    rows_moved = {PROJECT: [_picker_row("r-moved", LANE)]}
    rows_other = {OTHER_PROJECT: [_picker_row("r-other", LANE)]}

    moved = outcomes.summarize(rows_moved, {})["metered_spend"]["billing"]
    other = outcomes.summarize(rows_other, {})["metered_spend"]["billing"]

    assert moved["subscription_backends"] == [LANE]
    assert other["subscription_backends"] == []


def test_the_ledger_reads_the_project_layer_only_when_named(
    repository: Path,
) -> None:
    """The premise: without the project, the lane reads as metered.

    This is the defect the promotion writer carried — a caller that omits the
    project resolves against the host and catalogue layers alone, where the
    lane is metered. It pins the fix to the project argument rather than to a
    backend set.
    """
    _write_project_flight(PROJECT, repository)

    assert ledger.is_subscription_backend(LANE, project=PROJECT) is True
    assert ledger.is_subscription_backend(LANE) is False
