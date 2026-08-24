"""Uniform dispatch: the task contract, routing, run records and worker reports.

Every test here is hermetic. ``RECKON_HOME`` moves the crew directory into a temp
tree, the repository is a real but throwaway git repo, and the one test that
needs a process substitutes a launcher — so nothing spawns a harness and nothing
reaches a network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, flight, ledger


CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        },
        "native": {"launch": "in-harness", "time_budget": "25m"},
    },
    "roles": {
        "implement": {},
        "review": {"sandbox": "read-only"},
        "inline": {"backend": "native"},
    },
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point the crew directory at a temp tree, leaving the real one alone."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path):
    """A throwaway git repository carrying the worktree fleet script."""
    root = tmp_path / "repo"
    (root / "skills" / "reckon-ship" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-ship" / "scripts" / "worktree_fleet.py").write_text(
        source.read_text()
    )
    (root / "docs" / "plans" / "plan-a.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="plan-a">
</head><body><h2 id="s3">§3 — Dispatch</h2></body></html>
"""
    )
    (root / "seed.txt").write_text("seed\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/plan-a.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def _node(**overrides) -> crew.TaskNode:
    """A well-formed node; each test spoils exactly the property it studies."""
    fields = {
        "id": "node-a",
        "goal": "record the launch matrix for one backend",
        "plan": "plan-a",
        "section": "§3",
        "done_when": "uv run pytest tests/test_backends.py reports 34 passed",
        "write_paths": ["reckon/_backends.py"],
        "time_budget": "20m",
        "manifest_path": "/tmp/node-a-manifest.md",
    }
    fields.update(overrides)
    return crew.TaskNode(**fields)


def _set_plan_hours(repo: Path, hours: float) -> None:
    plan = repo / "docs" / "plans" / "plan-a.html"
    plan.write_text(
        plan.read_text().replace(
            '<meta name="plan-slug" content="plan-a">',
            '<meta name="plan-slug" content="plan-a">\n'
            f'<meta name="plan-effort-hours" content="{hours}">',
        )
    )
    subprocess.run(
        ["git", "add", "docs/plans/plan-a.html"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "test: set plan hours"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _capability_cache(*, horizon: float | None, speed: float = 1.0) -> dict:
    agent = {
        "backend": "alpha",
        "launch": "cli",
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
    }
    return {
        "configurations": [
            {
                "key": json.dumps(agent, sort_keys=True, separators=(",", ":")),
                "competence_horizon_hours": horizon,
                "speed": {"mean": speed},
            }
        ]
    }


# ── The task-definition contract ────────────────────────────────────────────


def test_a_well_formed_node_passes_every_property() -> None:
    verdict = crew.validate_node(_node(), budget_ceiling="25m")
    assert verdict.ok, verdict.findings
    assert verdict.failed_properties == []


def test_two_goals_joined_by_a_conjunction_are_two_nodes() -> None:
    verdict = crew.validate_node(
        _node(goal="add the translation module and wire the CLI"), budget_ceiling="25m"
    )
    assert "single-goal" in verdict.failed_properties
    detail = next(
        f["detail"] for f in verdict.findings if f["property"] == "single-goal"
    )
    assert "split it" in detail


def test_conjoined_nouns_remain_one_goal_while_conjoined_actions_do_not() -> None:
    noun_phrase_goals = (
        "round-trip a section from parse and serialisation",
        "derived age and a staleness verdict",
        "sprint order and incompleteness",
    )

    for goal in noun_phrase_goals:
        verdict = crew.validate_node(_node(goal=goal), budget_ceiling="25m")
        assert verdict.ok, (goal, verdict.findings)

    action_verdict = crew.validate_node(
        _node(goal="add the translation module and wire the CLI"),
        budget_ceiling="25m",
    )
    assert "single-goal" in action_verdict.failed_properties


def test_a_subjective_done_when_is_not_a_measure() -> None:
    verdict = crew.validate_node(
        _node(done_when="the dispatch path is clean and robust"), budget_ceiling="25m"
    )
    assert "demonstrable" in verdict.failed_properties
    details = " ".join(
        f["detail"] for f in verdict.findings if f["property"] == "demonstrable"
    )
    assert "clean" in details and "robust" in details


def test_hyphenated_subjective_term_is_not_a_bare_adjective() -> None:
    compound_verdict = crew.validate_node(
        _node(done_when="a machine-readable exit code 0 is emitted"),
        budget_ceiling="25m",
    )
    assert compound_verdict.ok, compound_verdict.findings

    bare_verdict = crew.validate_node(
        _node(done_when="exit code 0 is readable"), budget_ceiling="25m"
    )
    assert "demonstrable" in bare_verdict.failed_properties
    assert any(
        "readable" in finding["detail"]
        for finding in bare_verdict.findings
        if finding["property"] == "demonstrable"
    )


def test_a_done_when_with_no_observable_fails_demonstrable() -> None:
    verdict = crew.validate_node(
        done_when_node := _node(done_when="it works"), budget_ceiling="25m"
    )
    assert done_when_node.done_when == "it works"
    assert "demonstrable" in verdict.failed_properties


def test_a_missing_write_scope_is_refused() -> None:
    verdict = crew.validate_node(_node(write_paths=[]), budget_ceiling="25m")
    assert "scoped" in verdict.failed_properties


def test_two_concurrent_nodes_may_not_share_a_file() -> None:
    verdict = crew.validate_node(
        _node(peer_scopes={"node-b": ["reckon/_backends.py", "reckon/cli.py"]}),
        budget_ceiling="25m",
    )
    detail = next(f["detail"] for f in verdict.findings if f["property"] == "scoped")
    assert "node-b" in detail and "reckon/_backends.py" in detail


def test_an_unlocked_decision_means_the_node_is_not_closed() -> None:
    verdict = crew.validate_node(
        _node(requires_decisions=["skill-topology"]), budget_ceiling="25m"
    )
    assert "closed" in verdict.failed_properties
    verdict = crew.validate_node(
        _node(requires_decisions=["skill-topology"]),
        locked_decisions=["skill-topology"],
        budget_ceiling="25m",
    )
    assert verdict.ok


def test_a_budget_over_the_fence_must_be_split_not_overrun() -> None:
    verdict = crew.validate_node(_node(time_budget="90m"), budget_ceiling="25m")
    detail = next(f["detail"] for f in verdict.findings if f["property"] == "bounded")
    assert "split the work" in detail


def test_a_malformed_budget_names_the_accepted_form() -> None:
    verdict = crew.validate_node(
        _node(time_budget="half an hour"), budget_ceiling="25m"
    )
    assert "bounded" in verdict.failed_properties


def test_an_unspecified_input_is_not_the_workers_to_infer() -> None:
    verdict = crew.validate_node(
        _node(goal="decide the manifest format and record it"), budget_ceiling="25m"
    )
    assert "fully-specified" in verdict.failed_properties


def test_a_node_naming_no_plan_has_no_semantic_authority() -> None:
    verdict = crew.validate_node(_node(plan=""), budget_ceiling="25m")
    assert "fully-specified" in verdict.failed_properties


def test_a_relative_manifest_path_would_be_invisible_to_the_orchestrator() -> None:
    """It resolves against the worktree, so a delivered node looks silent."""
    verdict = crew.validate_node(
        _node(manifest_path="manifest.md"), budget_ceiling="25m"
    )
    detail = next(
        f["detail"]
        for f in verdict.findings
        if f["property"] == "independently-verifiable"
    )
    assert "relative" in detail


def test_every_failure_is_reported_in_one_pass() -> None:
    """A caller reshaping a node wants the whole list, not the first fault."""
    verdict = crew.validate_node(
        crew.TaskNode(id="n", goal="", plan="", done_when=""), budget_ceiling="25m"
    )
    assert {
        "single-goal",
        "fully-specified",
        "demonstrable",
        "scoped",
        "bounded",
    } <= set(verdict.failed_properties)


def test_the_contract_has_exactly_seven_properties() -> None:
    assert len(crew.NODE_PROPERTIES) == 7


def test_worker_protocol_states_scope_exclusivity_is_not_sufficiency() -> None:
    protocol = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
        / "references"
        / "worker-protocol.md"
    ).read_text()
    assert "This checks exclusivity, not sufficiency" in protocol
    assert "does not prove the named paths can carry the goal" in protocol


# ── Routing ─────────────────────────────────────────────────────────────────


def test_a_role_overlays_only_the_keys_it_names() -> None:
    name, backend = crew.resolve_role(CONFIG, "review")
    assert name == "alpha"
    assert backend["sandbox"] == "read-only"
    assert backend["model"] == "some-model"
    assert backend["effort"] == "high"


def test_a_role_may_route_to_another_backend() -> None:
    name, backend = crew.resolve_role(CONFIG, "inline")
    assert name == "native"
    assert backend["launch"] == "in-harness"


def test_specification_level_may_switch_backend_and_overlay_settings() -> None:
    config = flight.deep_merge(
        CONFIG,
        {
            "roles": {
                "implement": {
                    "model": "role-model",
                    "by_spec_level": {
                        "exact": {
                            "backend": "native",
                            "model": "exact-model",
                            "effort": "low",
                            "time_budget": "3m",
                        }
                    },
                }
            }
        },
    )

    name, backend = crew.resolve_role(config, "implement", "exact")

    assert name == "native"
    assert backend["launch"] == "in-harness"
    assert backend["model"] == "exact-model"
    assert backend["effort"] == "low"
    assert backend["time_budget"] == "3m"
    assert "by_spec_level" not in backend


def test_specification_level_may_overlay_effort_without_switching_backend() -> None:
    config = flight.deep_merge(
        CONFIG,
        {"roles": {"implement": {"by_spec_level": {"guided": {"effort": "medium"}}}}},
    )

    name, backend = crew.resolve_role(config, "implement", "guided")

    assert name == "alpha"
    assert backend["model"] == "some-model"
    assert backend["effort"] == "medium"


def test_undeclared_specification_level_leaves_role_resolution_unchanged() -> None:
    config = flight.deep_merge(
        CONFIG,
        {"roles": {"implement": {"by_spec_level": {"exact": {"backend": "native"}}}}},
    )

    assert crew.resolve_role(config, "implement", "") == crew.resolve_role(
        CONFIG, "implement"
    )


def test_dispatch_override_rewrites_the_matching_specification_mapping() -> None:
    config = flight.deep_merge(
        CONFIG,
        {"roles": {"implement": {"by_spec_level": {"guided": {"effort": "medium"}}}}},
    )
    overridden = flight.deep_merge(
        config,
        flight.parse_overrides(["roles.implement.by_spec_level.guided.effort=low"]),
    )

    plan = crew.plan_dispatch(node=_node(spec_level="guided"), config=overridden)

    assert plan.backend == "alpha"
    assert plan.backend_settings["effort"] == "low"


def test_an_unconfigured_role_lists_the_configured_ones() -> None:
    with pytest.raises(crew.CrewError) as excinfo:
        crew.resolve_role(CONFIG, "nonesuch")
    assert "implement" in str(excinfo.value)


def test_a_role_routing_to_an_undefined_backend_is_an_error() -> None:
    config = {**CONFIG, "roles": {"implement": {"backend": "ghost"}}}
    with pytest.raises(crew.CrewError) as excinfo:
        crew.resolve_role(config, "implement")
    assert "ghost" in str(excinfo.value)


def test_the_budget_ceiling_prefers_the_backend_over_the_fence() -> None:
    config = {**CONFIG, "fences": {"time_budget": "10m"}}
    _, backend = crew.resolve_role(config, "implement")
    assert crew.resolved_time_budget(config, backend) == "25m"
    assert crew.resolved_time_budget(config, {}) == "10m"


def test_measured_role_defaults_are_separate_from_the_time_ceiling(home) -> None:
    from reckon import flight

    resolved = flight.resolve(host_path=Path("/nonexistent/flight.yaml"))
    implement = crew.plan_dispatch(node=_node(time_budget=""), config=resolved.config)
    review = crew.plan_dispatch(
        node=_node(id="review-node", role="review", time_budget=""),
        config=resolved.config,
    )
    declared = crew.plan_dispatch(
        node=_node(id="long-node", time_budget="45m"), config=resolved.config
    )

    assert implement.node.time_budget == "8m"
    assert review.node.time_budget == "4m"
    assert resolved.origin("roles.implement.time_budget") == "shipped"
    assert resolved.origin("roles.review.time_budget") == "shipped"
    assert declared.node.time_budget == "45m"
    assert declared.budget_ceiling == "60m"
    assert declared.validation.ok


def test_the_resolution_fills_the_defaults_a_dispatch_would_fill(home) -> None:
    """A dry run must be the same decision as a dispatch, not a second one."""
    node = _node(time_budget="", manifest_path="")
    resolution = crew.plan_dispatch(node=node, config=CONFIG)
    assert resolution.node.time_budget == "25m"
    assert Path(resolution.node.manifest_path).is_absolute()
    assert resolution.run_id in resolution.node.manifest_path
    assert resolution.validation.ok
    assert resolution.launch == "cli"


def test_a_node_serialises_its_declared_specification_level() -> None:
    assert _node(spec_level="guided").as_dict()["spec_level"] == "guided"


def test_an_unknown_specification_level_is_refused_before_resolution(home) -> None:
    with pytest.raises(crew.CrewError, match="exact, guided, open.*undeclared"):
        crew.plan_dispatch(node=_node(spec_level="prescriptive"), config=CONFIG)


@pytest.mark.parametrize("spelling", ["3", "s3", "#s3", "section 3", "§ 3"])
def test_numbered_section_spellings_normalize_at_dispatch(home, spelling) -> None:
    resolution = crew.plan_dispatch(
        node=_node(section=spelling),
        config=CONFIG,
    )

    assert resolution.node.section == "§3"


def test_tmpfs_manifest_warning_names_the_durable_default(home) -> None:
    requested = "/dev/shm/caller-manifest.md"
    resolution = crew.plan_dispatch(
        node=_node(manifest_path=requested),
        config=CONFIG,
    )

    assert resolution.node.manifest_path == requested
    assert len(resolution.warnings) == 1
    assert "tmpfs" in resolution.warnings[0]
    assert (
        str(crew.run_dir(resolution.run_id) / "manifest.md") in resolution.warnings[0]
    )


def test_dry_run_payload_reports_the_resolved_write_paths(
    home, repo, monkeypatch
) -> None:
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *args, **kwargs: CONFIG)
    node = _node()
    write_paths = ["reckon/crew.py", "tests/test_crew.py"]

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "dispatch",
            "--project",
            "proj",
            "--plan",
            "plan-a",
            "--section",
            node.section,
            "--spec-level",
            "guided",
            "--node",
            node.id,
            "--goal",
            node.goal,
            "--done-when",
            node.done_when,
            "--write-path",
            write_paths[0],
            "--write-path",
            write_paths[1],
            "--session",
            "sess",
            "--repo",
            str(repo),
            "--dry-run",
        ],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["write_paths"] == write_paths
    assert payload["node"]["spec_level"] == "guided"
    _assert_no_dispatch_artifacts(repo)


def test_an_unsafe_node_id_is_refused_before_anything_is_created(home) -> None:
    with pytest.raises(crew.CrewError):
        crew.plan_dispatch(node=_node(id="../escape"), config=CONFIG)


# ── Dispatch ────────────────────────────────────────────────────────────────


def _assert_no_dispatch_artifacts(repo: Path) -> None:
    assert crew.list_live() == []
    listed = subprocess.run(
        ["git", "worktree", "list"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "node-a" not in listed.stdout


def _write_terminal_pointer(
    home: Path,
    run_id: str,
    *,
    age_seconds: int,
    status: str = "complete",
) -> dict:
    """Create one project-scoped pointer whose manifest has a known terminal age."""
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: delivered-node\nstatus: {status}\ncommits: HEAD\n"
    )
    terminal = datetime.now(tz=timezone.utc) - timedelta(seconds=age_seconds)
    os.utime(manifest, (terminal.timestamp(), terminal.timestamp()))
    record = {
        "run_id": run_id,
        "project": "proj",
        "repo": "/temporary/repository",
        "node": {
            "id": "delivered-node",
            "plan": "plan-a",
            "time_budget": "20m",
        },
        "phase": "complete",
        "created_at": (terminal - timedelta(days=2)).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(home / "absent-stream.jsonl"),
    }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def _write_running_pointer(
    home: Path,
    run_id: str,
    *,
    project: str = "proj",
    repo: str = "/temporary/repository",
    node_id: str = "working-node",
    write_paths: list[str] | None = None,
    stream_age_seconds: int = 0,
) -> dict:
    """Create one live pointer with a stream whose quiet age is controlled."""
    stream = home / "streams" / f"{run_id}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n')
    quiet_since = datetime.now(tz=timezone.utc) - timedelta(
        seconds=stream_age_seconds
    )
    os.utime(stream, (quiet_since.timestamp(), quiet_since.timestamp()))
    record = {
        "run_id": run_id,
        "project": project,
        "repo": repo,
        "node": {
            "id": node_id,
            "plan": "plan-a",
            "time_budget": "20m",
            "write_paths": list(write_paths or ()),
        },
        "phase": "working",
        "created_at": quiet_since.isoformat(),
        "manifest_path": str(home / "manifests" / f"{run_id}.md"),
        "log_path": str(stream),
        "process_alive": None,
    }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def test_project_watch_exits_on_the_first_terminal_manifest(home) -> None:
    _write_running_pointer(home, "r-alpha")
    _write_terminal_pointer(home, "r-beta", age_seconds=0, status="complete")

    result = crew.watch("proj", stall_window="5m")

    assert result["event"] == "terminal"
    assert result["run_id"] == "r-beta"
    assert result["classification"] == "completed_unpromoted"
    assert result["next_action"].startswith("reckon crew complete --run r-beta")


def test_project_watch_exits_when_a_stream_exceeds_the_stall_window(home) -> None:
    _write_running_pointer(home, "r-quiet", stream_age_seconds=61)

    result = crew.watch("proj", stall_window="1m")

    assert result["event"] == "stalled"
    assert result["run_id"] == "r-quiet"
    assert result["classification"] == "running"
    assert result["stalled_for_seconds"] >= 60
    assert result["manifest_status"] is None
    assert result["next_action"] == "reckon crew observe --run r-quiet"


def test_project_watch_exits_immediately_without_live_pointers(home) -> None:
    result = crew.watch("proj", stall_window="1h")

    assert result == {
        "project": "proj",
        "event": "empty",
        "run_id": None,
        "classification": "no_live_pointers",
        "next_action": "none — project 'proj' has no live pointers",
    }


def test_second_project_watch_reports_the_live_watcher(home) -> None:
    _write_running_pointer(home, "r-working")
    sleeping = threading.Event()
    release = threading.Event()

    def controlled_sleep(_seconds):
        sleeping.set()
        assert release.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            crew.watch,
            "proj",
            stall_window="1h",
            sleeper=controlled_sleep,
        )
        assert sleeping.wait(timeout=5)

        second = crew.watch("proj", stall_window="1h")
        assert second["event"] == "watcher-live"
        assert second["watcher_live"] is True
        assert second["watcher"]["pid"] == os.getpid()

        crew.pointer_path("r-working").unlink()
        release.set()
        assert first.result(timeout=5)["event"] == "empty"


def test_project_watch_reclaims_an_unlocked_stale_record(home) -> None:
    path = crew.watch_lock_path("proj")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project": "proj",
                "pid": 999999999,
                "started_at": "2026-08-01T00:00:00Z",
            }
        )
    )

    result = crew.watch("proj", stall_window="1h")

    assert result["event"] == "empty"
    reclaimed = json.loads(path.read_text())
    assert reclaimed["pid"] == os.getpid()
    assert reclaimed["project"] == "proj"


def test_dead_watcher_does_not_satisfy_the_dispatch_gate(home, repo) -> None:
    source_root = Path(__file__).parents[1]
    script = """
import time
from reckon.crew import _project_watch_claim
with _project_watch_claim('proj', '1h') as (acquired, _record):
    print('ready' if acquired else 'failed', flush=True)
    time.sleep(30)
"""
    watcher = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=repo,
        env={**os.environ, "PYTHONPATH": str(source_root)},
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert watcher.stdout is not None
        assert watcher.stdout.readline().strip() == "ready"
        assert crew.watch_state("proj")["watcher_live"] is True
    finally:
        watcher.terminate()
        watcher.wait(timeout=5)

    assert crew.watch_state("proj")["watcher_live"] is False
    with pytest.raises(crew.WatcherRequired):
        crew.dispatch(
            node=_node(),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="sess",
            launcher=lambda *args, **kwargs: pytest.fail("dispatch must be refused"),
            watch_required=True,
        )
    _assert_no_dispatch_artifacts(repo)


def test_live_run_listing_combines_project_and_phase_filters(home) -> None:
    records = [
        {
            "run_id": "run-alpha-running",
            "project": "alpha",
            "phase": "running",
            "node": {"id": "alpha-running", "plan": "delivery"},
        },
        {
            "run_id": "run-alpha-stopped",
            "project": "alpha",
            "phase": "stopped",
            "node": {"id": "alpha-stopped", "plan": "delivery"},
        },
        {
            "run_id": "run-beta-running",
            "project": "beta",
            "phase": "running",
            "node": {"id": "beta-running", "plan": "delivery"},
        },
    ]
    for record in records:
        crew._write_json(crew.pointer_path(record["run_id"]), record)

    project_only = CliRunner().invoke(
        cli_module.main, ["crew", "list", "--project", "alpha"]
    )
    phase_only = CliRunner().invoke(
        cli_module.main, ["crew", "list", "--phase", "running"]
    )
    combined = CliRunner().invoke(
        cli_module.main,
        ["crew", "list", "--project", "alpha", "--phase", "running"],
    )

    assert project_only.exit_code == phase_only.exit_code == combined.exit_code == 0
    assert {run["run_id"] for run in json.loads(project_only.output)["runs"]} == {
        "run-alpha-running",
        "run-alpha-stopped",
    }
    assert {run["run_id"] for run in json.loads(phase_only.output)["runs"]} == {
        "run-alpha-running",
        "run-beta-running",
    }
    assert [run["run_id"] for run in json.loads(combined.output)["runs"]] == [
        "run-alpha-running"
    ]


def test_dispatch_refuses_working_plan_changes_before_creating_anything(
    home, repo
) -> None:
    plan = repo / "docs" / "plans" / "plan-a.html"
    plan.write_text(plan.read_text().replace("Dispatch", "Changed dispatch"))

    with pytest.raises(crew.PlanVisibilityError) as excinfo:
        crew.dispatch(
            node=_node(),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="sess",
            launcher=lambda *a, **k: 1,
        )

    detail = str(excinfo.value)
    assert "docs/plans/plan-a.html" in detail
    assert "commit the plan" in detail
    _assert_no_dispatch_artifacts(repo)


def test_dispatch_refuses_a_section_absent_from_the_base(home, repo) -> None:
    with pytest.raises(crew.PlanVisibilityError) as excinfo:
        crew.dispatch(
            node=_node(section="§8"),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="sess",
            launcher=lambda *a, **k: 1,
        )

    detail = str(excinfo.value)
    assert "docs/plans/plan-a.html" in detail
    assert "does not contain section" in detail
    _assert_no_dispatch_artifacts(repo)


def test_dispatch_refuses_a_plan_absent_from_the_base(home, repo) -> None:
    plan = repo / "docs" / "plans" / "plan-b.html"
    plan.write_text(
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="plan-b"><h2 id="s3">§3</h2>'
    )

    with pytest.raises(crew.PlanVisibilityError) as excinfo:
        crew.dispatch(
            node=_node(plan="plan-b"),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="sess",
            launcher=lambda *a, **k: 1,
        )

    assert "not readable at base" in str(excinfo.value)
    _assert_no_dispatch_artifacts(repo)


@pytest.mark.parametrize(
    ("claimed_path", "candidate_path"),
    [
        ("reckon", "reckon/crew.py"),
        ("reckon/crew.py", "reckon"),
    ],
)
def test_dispatch_refuses_live_scope_containment_before_worktree_creation(
    home, repo, claimed_path, candidate_path
) -> None:
    owner = _write_running_pointer(
        home,
        "r-scope-owner",
        repo=str(repo),
        node_id="owner-node",
        write_paths=[claimed_path],
    )

    with pytest.raises(crew.ScopeConflict) as excinfo:
        crew.dispatch(
            node=_node(write_paths=[candidate_path]),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="sess",
            launcher=lambda *a, **k: pytest.fail("a conflicting node must not launch"),
        )

    refusal = excinfo.value
    assert refusal.run_id == owner["run_id"]
    assert refusal.node_id == "owner-node"
    assert refusal.candidate_path == candidate_path
    assert refusal.claimed_path == claimed_path
    assert owner["run_id"] in str(refusal)
    assert claimed_path in str(refusal)
    assert crew.list_live() == [owner]
    listed = subprocess.run(
        ["git", "worktree", "list"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "node-a" not in listed.stdout


def test_dispatch_derives_peer_scope_without_prefix_or_repository_false_positives(
    home, repo
) -> None:
    _write_running_pointer(
        home,
        "r-scope-owner",
        repo=str(repo),
        node_id="owner-node",
        write_paths=["reckon/crew.py"],
    )
    _write_running_pointer(
        home,
        "r-other-repository",
        repo=str(repo.parent / "other-repository"),
        node_id="other-owner",
        write_paths=["reckon"],
    )

    record = crew.dispatch(
        node=_node(write_paths=["reckon/crew.pyx"]),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
        launcher=lambda *a, **k: 4242,
    )

    assert record["peer_scopes"] == {"owner-node": ["reckon/crew.py"]}
    assert "owner-node → reckon/crew.py" in Path(record["prompt_path"]).read_text()
    assert Path(record["worktree"]).is_dir()


def test_scopes_view_reads_claim_owners_without_mutating_the_pointer(
    home, repo
) -> None:
    from reckon import mcp

    owner = _write_running_pointer(
        home,
        "r-scope-view-owner",
        repo=str(repo),
        node_id="owner-node",
        write_paths=["reckon/crew.py"],
    )
    pointer = crew.pointer_path(owner["run_id"])
    before = pointer.read_bytes()

    result = mcp._crew("proj", view="scopes", checkout_path=str(repo))

    assert result["ok"] is True
    assert result["claim_map"] == {
        "reckon/crew.py": [
            {
                "run_id": owner["run_id"],
                "node": "owner-node",
                "declared_path": "reckon/crew.py",
            }
        ]
    }
    assert result["claims"] == [
        {
            "path": "reckon/crew.py",
            "run_id": owner["run_id"],
            "node": "owner-node",
            "declared_path": "reckon/crew.py",
        }
    ]
    assert pointer.read_bytes() == before


def test_lane_planner_groups_a_connected_three_node_scope_graph(home, repo) -> None:
    candidates = [
        {
            "id": "refusal",
            "write_paths": ["reckon/crew.py", "reckon/cli.py"],
        },
        {
            "id": "watch",
            "write_paths": ["reckon/crew.py", "skills/reckon-ship/SKILL.md"],
        },
        {
            "id": "drain",
            "write_paths": [
                "reckon/cli.py",
                "skills/reckon-ship/SKILL.md",
                "tests/test_crew.py",
            ],
        },
    ]

    result = crew.plan_scope_lanes(candidates, project="proj", repo=repo)

    assert result["lane_count"] == 1
    assert result["lanes"] == [
        {"lane": 1, "nodes": ["refusal", "watch", "drain"]}
    ]
    assert result["conflict_graph"] == {
        "refusal": ["drain", "watch"],
        "watch": ["drain", "refusal"],
        "drain": ["refusal", "watch"],
    }


def test_scopes_view_expands_project_derivations_before_partitioning(
    home, repo
) -> None:
    from reckon import mcp

    project_dir = repo / "docs" / "state" / "proj"
    project_dir.mkdir(parents=True)
    (project_dir / "index.json").write_text(
        json.dumps(
            {
                "project": "proj",
                "doc": "index",
                "data": {
                    "_version": 0,
                    "projects": [
                        {
                            "name": "proj",
                            "derivations": {
                                "reckon/schema/flight.yaml": [
                                    "reckon/_flight_schema.py",
                                    "docs/_shared/flight.schema.json",
                                ]
                            },
                        }
                    ],
                },
            }
        )
    )

    result = mcp._crew(
        "proj",
        view="scopes",
        checkout_path=str(repo),
        candidates=[
            {
                "id": "schema-source",
                "write_paths": ["reckon/schema/flight.yaml"],
            },
            {
                "id": "generated-output",
                "write_paths": ["reckon/_flight_schema.py"],
            },
        ],
    )

    assert result["lane_count"] == 1
    assert result["lanes"] == [
        {"lane": 1, "nodes": ["schema-source", "generated-output"]}
    ]
    source = result["candidates"][0]
    assert source["derived_paths"] == [
        {
            "path": "docs/_shared/flight.schema.json",
            "declared_path": "reckon/schema/flight.yaml",
            "derived_from": "reckon/schema/flight.yaml",
        },
        {
            "path": "reckon/_flight_schema.py",
            "declared_path": "reckon/schema/flight.yaml",
            "derived_from": "reckon/schema/flight.yaml",
        },
    ]
    assert result["conflicts"][0]["paths"] == [
        {
            "left_path": "reckon/_flight_schema.py",
            "right_path": "reckon/_flight_schema.py",
        }
    ]


def test_lane_planner_returns_no_lanes_for_an_empty_candidate_set(home, repo) -> None:
    result = crew.plan_scope_lanes([], project="proj", repo=repo)

    assert result["lane_count"] == 0
    assert result["lanes"] == []
    assert result["conflict_graph"] == {}
    assert result["conflicts"] == []


def test_cli_dispatch_reports_a_live_scope_conflict_on_its_own_exit_code(
    home, repo, monkeypatch
) -> None:
    owner = _write_running_pointer(
        home,
        "r-cli-scope-owner",
        repo=str(repo),
        node_id="owner-node",
        write_paths=["reckon"],
    )
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *args, **kwargs: CONFIG)

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "dispatch",
            "--project",
            "proj",
            "--plan",
            "plan-a",
            "--section",
            "§3",
            "--node",
            "node-a",
            "--goal",
            "record the launch matrix for one backend",
            "--done-when",
            "uv run pytest tests/test_crew.py reports 0 failures",
            "--write-path",
            "reckon/crew.py",
            "--session",
            "sess",
            "--repo",
            str(repo),
        ],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 7
    assert payload == {
        "ok": False,
        "error": "scope-conflict",
        "detail": payload["detail"],
        "run_id": owner["run_id"],
        "node": "owner-node",
        "candidate_path": "reckon/crew.py",
        "claimed_path": "reckon",
    }
    assert owner["run_id"] in payload["detail"]
    assert crew.list_live() == [owner]
    listed = subprocess.run(
        ["git", "worktree", "list"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "node-a" not in listed.stdout


def test_double_dispatch_refuses_a_member_with_a_non_terminal_run(home, repo) -> None:
    ledger_member = "worker-a"
    crew.ledger.register_member("proj", ledger_member, harness="codex", root=repo)
    first = crew.dispatch(
        node=_node(),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
        member=ledger_member,
        launcher=lambda *a, **k: 4242,
    )

    with pytest.raises(crew.MemberInFlight) as excinfo:
        crew.dispatch(
            node=_node(id="node-b", manifest_path="/tmp/node-b-manifest.md"),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="sess",
            member=ledger_member,
            launcher=lambda *a, **k: pytest.fail("the member must not be launched"),
        )

    assert excinfo.value.member == ledger_member
    assert excinfo.value.run_id == first["run_id"]
    assert ledger_member in str(excinfo.value)
    assert first["run_id"] in str(excinfo.value)
    assert crew.list_live() == [first]
    listed = subprocess.run(
        ["git", "worktree", "list"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "node-a" in listed.stdout
    assert "node-b" not in listed.stdout


def test_dispatch_with_a_committed_named_section_proceeds(home, repo) -> None:
    record = crew.dispatch(
        node=_node(),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
        launcher=lambda *a, **k: 4242,
    )
    assert record["pid"] == 4242
    assert Path(record["worktree"]).is_dir()


def test_dispatch_requires_a_live_project_watcher_before_creating_a_worktree(
    home, repo
) -> None:
    with pytest.raises(crew.WatcherRequired) as excinfo:
        crew.dispatch(
            node=_node(),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="sess",
            launcher=lambda *args, **kwargs: pytest.fail("dispatch must be refused"),
            watch_required=True,
        )

    assert excinfo.value.watch == {
        "arming_line": "reckon crew watch --project proj",
        "watcher_live": False,
        "watcher": {},
    }
    assert "reckon crew watch --project proj" in str(excinfo.value)
    _assert_no_dispatch_artifacts(repo)


def test_dispatch_returns_the_project_watch_arming_line_and_live_state(
    home, repo
) -> None:
    _write_running_pointer(home, "r-existing")
    sleeping = threading.Event()
    release = threading.Event()

    def controlled_sleep(_seconds):
        sleeping.set()
        assert release.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        watcher = pool.submit(
            crew.watch,
            "proj",
            stall_window="1h",
            sleeper=controlled_sleep,
        )
        assert sleeping.wait(timeout=5)

        record = crew.dispatch(
            node=_node(id="next-node"),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="sess",
            launcher=lambda *args, **kwargs: 4242,
            watch_required=True,
        )
        assert record["watch"]["arming_line"] == "reckon crew watch --project proj"
        assert record["watch"]["watcher_live"] is True
        assert record["watch"]["watcher"]["pid"] == os.getpid()

        crew.pointer_path("r-existing").unlink()
        crew.pointer_path(record["run_id"]).unlink()
        release.set()
        assert watcher.result(timeout=5)["event"] == "empty"


def test_no_watch_dispatch_records_the_override_on_pointer_and_ledger(
    home, repo, monkeypatch
) -> None:
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *args, **kwargs: CONFIG)
    monkeypatch.setattr(crew, "_spawn", lambda *args, **kwargs: 4242)
    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "dispatch",
            "--project",
            "proj",
            "--plan",
            "plan-a",
            "--section",
            "s3",
            "--node",
            "node-a",
            "--goal",
            "record the launch matrix for one backend",
            "--done-when",
            "uv run pytest tests/test_crew.py reports 0 failures",
            "--write-path",
            "reckon/crew.py",
            "--session",
            "sess",
            "--repo",
            str(repo),
            "--no-watch",
        ],
    )

    assert result.exit_code == 0
    record = json.loads(result.output)
    waiver = record["watch_override"]
    assert waiver == {
        "requested": True,
        "arming_line": "reckon crew watch --project proj",
        "watcher_live": False,
    }
    assert crew.read_pointer(record["run_id"])["watch_override"] == waiver
    assert crew.complete(record["run_id"], gate="passed")["record"][
        "watch_override"
    ] == waiver


def test_cli_dispatch_reports_missing_watcher_on_its_own_exit_code(
    home, repo, monkeypatch
) -> None:
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *args, **kwargs: CONFIG)

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "dispatch",
            "--project",
            "proj",
            "--plan",
            "plan-a",
            "--section",
            "s3",
            "--node",
            "node-a",
            "--goal",
            "record the launch matrix for one backend",
            "--done-when",
            "uv run pytest tests/test_crew.py reports 0 failures",
            "--write-path",
            "reckon/crew.py",
            "--session",
            "sess",
            "--repo",
            str(repo),
        ],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 8
    assert payload["error"] == "watcher-required"
    assert payload["watch"]["watcher_live"] is False
    assert payload["watch"]["arming_line"] == "reckon crew watch --project proj"
    assert payload["watch"]["arming_line"] in payload["detail"]
    _assert_no_dispatch_artifacts(repo)


def test_dispatch_refuses_every_unreconciled_run_past_the_grace(home, repo) -> None:
    assert crew.live_dir() == home / "crew" / "live"
    run_ids = ("r-delivered-alpha", "r-delivered-beta")
    for run_id in run_ids:
        _write_terminal_pointer(home, run_id, age_seconds=601)
    configured = {
        **CONFIG,
        "fences": {**CONFIG["fences"], "unreconciled_run_grace": "5m"},
    }

    with pytest.raises(crew.UnreconciledRuns) as excinfo:
        crew.dispatch(
            node=_node(id="next-node"),
            project="proj",
            repo=repo,
            config=configured,
            session="sess",
            launcher=lambda *args, **kwargs: pytest.fail("dispatch must be refused"),
        )

    assert [row["run_id"] for row in excinfo.value.runs] == list(run_ids)
    for run_id in run_ids:
        expected = f"reckon crew complete --run {run_id} --gate <verdict> --commit HEAD"
        assert f"- {run_id}: {expected}" in str(excinfo.value)
    listed = subprocess.run(
        ["git", "worktree", "list"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "next-node" not in listed.stdout


def test_dispatch_grace_starts_when_the_manifest_turns_terminal(home, repo) -> None:
    _write_terminal_pointer(home, "r-recent-delivery", age_seconds=30)
    configured = {
        **CONFIG,
        "fences": {**CONFIG["fences"], "unreconciled_run_grace": "5m"},
    }

    record = crew.dispatch(
        node=_node(id="next-node"),
        project="proj",
        repo=repo,
        config=configured,
        session="sess",
        launcher=lambda *args, **kwargs: 4242,
    )

    assert record["run_id"] != "r-recent-delivery"
    assert record["unreconciled_override"] is None


def test_dispatch_override_records_and_promotes_the_waived_backlog(home, repo) -> None:
    old_run = "r-deliberately-left"
    _write_terminal_pointer(home, old_run, age_seconds=601)
    configured = {
        **CONFIG,
        "fences": {**CONFIG["fences"], "unreconciled_run_grace": "5m"},
    }

    record = crew.dispatch(
        node=_node(id="next-node"),
        project="proj",
        repo=repo,
        config=configured,
        session="sess",
        launcher=lambda *args, **kwargs: 4242,
        unreconciled_override=True,
    )

    waiver = record["unreconciled_override"]
    assert waiver["requested"] is True
    assert waiver["grace"] == "5m"
    assert [row["run_id"] for row in waiver["waived_runs"]] == [old_run]
    stored = crew.complete(record["run_id"], gate="passed")["record"]
    assert stored["unreconciled_override"] == waiver


def test_cli_dispatch_reports_unreconciled_runs_on_its_own_exit_code(
    home, repo, monkeypatch
) -> None:
    old_run = "r-cli-delivery"
    _write_terminal_pointer(home, old_run, age_seconds=601)
    configured = {
        **CONFIG,
        "fences": {**CONFIG["fences"], "unreconciled_run_grace": "5m"},
    }
    monkeypatch.setattr(
        cli_module,
        "_resolved_flight",
        lambda *args, **kwargs: configured,
    )

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "dispatch",
            "--project",
            "proj",
            "--plan",
            "plan-a",
            "--section",
            "§3",
            "--node",
            "next-node",
            "--goal",
            "record the launch matrix for one backend",
            "--done-when",
            "uv run pytest tests/test_crew.py reports 0 failures",
            "--write-path",
            "reckon/crew.py",
            "--session",
            "sess",
            "--repo",
            str(repo),
        ],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 6
    assert payload["error"] == "unreconciled-runs"
    assert payload["runs"][0]["run_id"] == old_run
    assert f"reckon crew complete --run {old_run}" in payload["detail"]


def test_dispatch_refuses_work_above_the_selected_configuration_horizon(
    home, repo, monkeypatch
) -> None:
    _set_plan_hours(repo, 4.0)
    monkeypatch.setattr(
        crew.capabilities,
        "load_capabilities",
        lambda: _capability_cache(horizon=2.5, speed=1.4),
    )

    with pytest.raises(crew.CompetenceLimit) as excinfo:
        crew.dispatch(
            node=_node(),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="sess",
            launcher=lambda *a, **k: 1,
        )

    payload = excinfo.value.verdict
    assert payload == {
        "agent_key": payload["agent_key"],
        "allowed": False,
        "cache_status": "untracked",
        "compared_hours": 4.0,
        "comparison_unit": "neutral-estimate-hours",
        "competence_horizon_hours": 2.5,
        "estimate_provenance": "plan-fallback",
        "estimated_hours": 4.0,
        "reason": "competence-horizon-exceeded",
        "recommendation": (
            "split into nodes no larger than 2.5 worker-hours for this agent "
            "configuration"
        ),
        "speed_direction": "neutral-estimate-hours-per-actual-worker-hour",
        "speed_factor": 1.4,
        "target_size_hours": 2.5,
    }
    assert "2.5 worker-hours" in str(excinfo.value)
    _assert_no_dispatch_artifacts(repo)


def test_dispatch_records_work_below_the_selected_configuration_horizon(
    home, repo, monkeypatch
) -> None:
    _set_plan_hours(repo, 1.5)
    monkeypatch.setattr(
        crew.capabilities,
        "load_capabilities",
        lambda: _capability_cache(horizon=2.5, speed=0.9),
    )

    record = crew.dispatch(
        node=_node(),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
        launcher=lambda *a, **k: 4242,
    )

    assert record["pid"] == 4242
    assert record["competence"]["allowed"] is True
    assert record["competence"]["cache_status"] == "untracked"
    assert record["competence"]["reason"] == "within-competence-horizon"
    assert record["competence"]["compared_hours"] == 1.5
    assert record["competence"]["speed_factor"] == 0.9
    assert record["competence"]["competence_horizon_hours"] == 2.5


@pytest.mark.parametrize("speed", [0.5, 2.0])
def test_competence_threshold_is_the_named_horizon_in_both_speed_directions(
    home, repo, monkeypatch, speed
) -> None:
    monkeypatch.setattr(
        crew.capabilities,
        "load_capabilities",
        lambda: _capability_cache(horizon=2.5, speed=speed),
    )
    resolution = crew.plan_dispatch(
        node=_node(estimated_hours=2.6),
        config=CONFIG,
        project="proj",
        repo=repo,
    )

    assert resolution.competence["allowed"] is False
    assert resolution.competence["target_size_hours"] == 2.5
    assert resolution.competence["estimate_provenance"] == "node"


def test_cli_competence_refusal_has_typed_dry_run_parity(
    home, repo, monkeypatch
) -> None:
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *args, **kwargs: CONFIG)
    monkeypatch.setattr(
        crew.capabilities,
        "load_capabilities",
        lambda: _capability_cache(horizon=2.5, speed=2.0),
    )
    arguments = [
        "crew",
        "dispatch",
        "--project",
        "proj",
        "--plan",
        "plan-a",
        "--section",
        "§3",
        "--node",
        "node-a",
        "--goal",
        "record the launch matrix for one backend",
        "--done-when",
        "uv run pytest tests/test_crew.py reports 0 failures",
        "--write-path",
        "reckon/crew.py",
        "--estimated-hours",
        "4",
        "--session",
        "sess",
        "--repo",
        str(repo),
    ]

    real = CliRunner().invoke(cli_module.main, arguments)
    dry = CliRunner().invoke(cli_module.main, [*arguments, "--dry-run"])
    real_payload = json.loads(real.output)
    dry_payload = json.loads(dry.output)

    assert real.exit_code == dry.exit_code == 5
    assert real_payload["error"] == dry_payload["error"] == "competence-refusal"
    assert real_payload["competence"] == dry_payload["competence"]
    assert real_payload["competence"]["target_size_hours"] == 2.5
    assert real_payload["competence"]["estimate_provenance"] == "node"
    help_text = CliRunner().invoke(cli_module.main, ["crew", "--help"]).output
    assert "5 the selected worker configuration" in " ".join(help_text.split())


def test_configuration_without_a_measured_horizon_refuses_nothing(
    home, repo, monkeypatch
) -> None:
    _set_plan_hours(repo, 100.0)
    monkeypatch.setattr(
        crew.capabilities,
        "load_capabilities",
        lambda: _capability_cache(horizon=None, speed=0.1),
    )

    record = crew.dispatch(
        node=_node(),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
        launcher=lambda *a, **k: 4242,
    )

    assert record["competence"]["allowed"] is True
    assert record["competence"]["reason"] == "no-measured-horizon"
    assert record["competence"]["estimated_hours"] == 100.0


def test_empty_capabilities_cache_cannot_block_a_dispatch(
    home, repo, monkeypatch
) -> None:
    _set_plan_hours(repo, 100.0)
    monkeypatch.setattr(
        crew.capabilities,
        "load_capabilities",
        lambda: {"configurations": []},
    )

    record = crew.dispatch(
        node=_node(),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
        launcher=lambda *a, **k: 4242,
    )

    assert record["competence"]["allowed"] is True
    assert record["competence"]["reason"] == "no-measured-horizon"


def test_cli_plan_visibility_refusal_has_its_own_exit_code(
    home, repo, monkeypatch
) -> None:
    plan = repo / "docs" / "plans" / "plan-a.html"
    plan.write_text(plan.read_text().replace("Dispatch", "Changed dispatch"))
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *args, **kwargs: CONFIG)

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "dispatch",
            "--project",
            "proj",
            "--plan",
            "plan-a",
            "--section",
            "§3",
            "--node",
            "node-a",
            "--goal",
            "record the launch matrix for one backend",
            "--done-when",
            "uv run pytest tests/test_crew.py reports 0 failures",
            "--write-path",
            "reckon/crew.py",
            "--session",
            "sess",
            "--repo",
            str(repo),
        ],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 4
    assert payload["error"] == "plan-unavailable"
    assert "docs/plans/plan-a.html" in payload["detail"]
    _assert_no_dispatch_artifacts(repo)


def test_dispatch_launches_a_cli_backend_and_records_the_run(home, repo) -> None:
    launched: dict = {}

    def launcher(plan, *, log_path, stderr_path, prompt_path):
        launched["argv"] = plan.argv
        launched["cwd"] = plan.cwd
        log_path.write_text("")
        return 4242

    record = crew.dispatch(
        node=_node(manifest_path=""),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
        launcher=launcher,
    )
    assert record["launch"] == "cli"
    assert record["pid"] == 4242
    assert record["dialect"] == "codex"
    assert record["backend"] == "alpha"
    assert Path(record["worktree"]).is_dir()
    assert Path(record["prompt_path"]).is_file()
    assert record["budget"]["headroom"] == "unknown"
    assert launched["cwd"] == record["worktree"]
    # The record is on disk, so a fresh session can pick the run up.
    assert json.loads(crew.pointer_path(record["run_id"]).read_text()) == record


def test_read_only_dispatch_runs_in_delivery_directory_and_explains_scope(
    home, repo
) -> None:
    launched: dict = {}
    manifest = home / "review-manifest.md"

    def launcher(plan, *, log_path, stderr_path, prompt_path):
        launched["plan"] = plan
        return 4242

    record = crew.dispatch(
        node=_node(role="review", manifest_path=str(manifest)),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
        launcher=launcher,
    )
    plan = launched["plan"]
    assert plan.cwd == str(home)
    assert plan.argv[plan.argv.index("-C") + 1] == str(home)
    assert plan.argv[plan.argv.index("--sandbox") + 1] == "workspace-write"
    assert "--skip-git-repo-check" in plan.argv
    prompt = Path(record["prompt_path"]).read_text()
    assert f"working directory is the delivery directory {home}" in prompt
    assert f"repository at the assigned worktree path {record['worktree']}" in prompt
    assert "is read-only" in prompt


def test_dispatch_refuses_a_malformed_node_and_creates_nothing(home, repo) -> None:
    with pytest.raises(crew.CrewError) as excinfo:
        crew.dispatch(
            node=_node(done_when="make it better"),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="sess",
            launcher=lambda *a, **k: 1,
        )
    assert "not dispatchable" in str(excinfo.value)
    assert crew.list_live() == []
    listed = subprocess.run(
        ["git", "worktree", "list"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "node-a" not in listed.stdout


def test_dispatch_names_sync_when_the_vendored_worktree_script_is_missing(
    home, repo
) -> None:
    script = repo / "skills" / "reckon-ship" / "scripts" / "worktree_fleet.py"
    script.unlink()

    with pytest.raises(crew.CrewError, match=r"worktree fleet script.*reckon sync"):
        crew.dispatch(
            node=_node(),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="sess",
            launcher=lambda *args, **kwargs: 1,
        )

    assert crew.list_live() == []


def test_a_failed_launch_leaves_no_worktree_holding_write_scope(home, repo) -> None:
    """Atomicity matters most here: an orphan worktree owns another's scope."""

    def exploding_launcher(plan, *, log_path, stderr_path, prompt_path):
        raise OSError("no such executable")

    with pytest.raises(OSError):
        crew.dispatch(
            node=_node(),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="sess",
            launcher=exploding_launcher,
        )
    listed = subprocess.run(
        ["git", "worktree", "list"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "node-a" not in listed.stdout
    assert crew.list_live() == []


def test_pointer_write_failure_terminates_process_and_removes_dispatch_artifacts(
    home, repo, monkeypatch
) -> None:
    run_id = "r-pointer-write-failure"
    spawned: dict[str, subprocess.Popen] = {}
    events: list[str] = []
    original_signal = crew._signal_process_group
    original_remove = crew._remove_worktree
    original_write = crew._write_json

    monkeypatch.setattr(crew, "new_run_id", lambda node_id, now=None: run_id)

    def launcher(plan, *, log_path, stderr_path, prompt_path):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=plan.cwd,
            start_new_session=True,
        )
        spawned["process"] = process
        return process.pid

    def fail_pointer_write(path, payload):
        if path == crew.pointer_path(run_id):
            raise OSError("forced pointer write failure")
        return original_write(path, payload)

    def record_signal(pid, expected_start_time):
        events.append("signal")
        original_signal(pid, expected_start_time)

    def record_remove(root, path):
        events.append("remove")
        original_remove(root, path)

    monkeypatch.setattr(crew, "_write_json", fail_pointer_write)
    monkeypatch.setattr(crew, "_signal_process_group", record_signal)
    monkeypatch.setattr(crew, "_remove_worktree", record_remove)

    with pytest.raises(OSError, match="forced pointer write failure"):
        crew.dispatch(
            node=_node(),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="sess",
            launcher=launcher,
        )

    process = spawned["process"]
    try:
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert events[:2] == ["signal", "remove"]
    assert process.returncode is not None
    assert not crew.run_dir(run_id).exists()
    assert not crew.pointer_path(run_id).exists()
    _assert_no_dispatch_artifacts(repo)


def test_an_in_harness_dispatch_returns_a_directive_to_bind(home, repo) -> None:
    record = crew.dispatch(
        node=_node(role="inline"),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
    )
    assert record["launch"] == "in-harness"
    assert record["pid"] is None
    directive = record["directive"]
    assert Path(directive["worktree"]).is_dir()
    assert directive["fences"]["delivery"] == record["manifest_path"]
    assert directive["fences"]["scope"] == ["reckon/_backends.py"]
    assert directive["fences"]["time"] == "20m"
    assert "reckon crew attach" in directive["attach_with"]

    bound = crew.attach(record["run_id"], "task-77")
    assert bound["task"] == "task-77"
    assert bound["phase"] == "working"


def test_a_second_attach_is_refused(home, repo) -> None:
    """Two bindings would hide which worker holds the write scope."""
    record = crew.dispatch(
        node=_node(role="inline"),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
    )
    crew.attach(record["run_id"], "task-77")
    with pytest.raises(crew.CrewError) as excinfo:
        crew.attach(record["run_id"], "task-88")
    assert "already attached" in str(excinfo.value)


def test_attach_refuses_a_spawned_run(home, repo) -> None:
    record = crew.dispatch(
        node=_node(),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
        launcher=lambda *a, **k: 1,
    )
    with pytest.raises(crew.CrewError) as excinfo:
        crew.attach(record["run_id"], "task-1")
    assert "in-harness" in str(excinfo.value)


def test_attach_on_an_unknown_run_names_where_it_looked(home) -> None:
    with pytest.raises(crew.CrewError) as excinfo:
        crew.attach("r-nope", "task-1")
    assert "r-nope" in str(excinfo.value)


def test_interleaved_attach_and_observe_preserve_the_task_binding(
    home, repo, monkeypatch
) -> None:
    record = crew.dispatch(
        node=_node(role="inline"),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
    )
    observation_waiting = threading.Event()
    release_observation = threading.Event()
    attach_waiting = threading.Event()
    thread_role = threading.local()
    real_write = crew._write_json
    real_lock = crew._pointer_lock
    delayed = False

    def delayed_observation(path, payload):
        nonlocal delayed
        if (
            path == crew.pointer_path(record["run_id"])
            and payload.get("observed_at")
            and not delayed
        ):
            delayed = True
            observation_waiting.set()
            assert release_observation.wait(timeout=5)
        return real_write(path, payload)

    def tracked_lock(run_id):
        if getattr(thread_role, "name", None) == "attach":
            attach_waiting.set()
        return real_lock(run_id)

    def attach_worker():
        thread_role.name = "attach"
        return crew.attach(record["run_id"], "task-77")

    monkeypatch.setattr(crew, "_write_json", delayed_observation)
    monkeypatch.setattr(crew, "_pointer_lock", tracked_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        observed = pool.submit(crew.observe, record["run_id"])
        assert observation_waiting.wait(timeout=5)
        attached = pool.submit(attach_worker)
        assert attach_waiting.wait(timeout=5)
        release_observation.set()
        observed.result(timeout=5)
        attached.result(timeout=5)

    assert crew.read_pointer(record["run_id"])["task"] == "task-77"


# ── Observation ─────────────────────────────────────────────────────────────


FIXTURES = Path(__file__).parent / "fixtures" / "backends"


def _dispatched(home, repo, fixture: str | None = None, **node_kwargs) -> dict:
    record = crew.dispatch(
        node=_node(**node_kwargs),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
        launcher=lambda plan, *, log_path, stderr_path, prompt_path: 0,
    )
    if fixture:
        Path(record["log_path"]).write_text((FIXTURES / fixture).read_text())
    return record


def test_observe_folds_the_stream_into_the_record(home, repo) -> None:
    record = _dispatched(home, repo, "codex-turn.jsonl")
    observed = crew.observe(record["run_id"])
    assert observed["phase"] == "complete"
    assert observed["exit_status"] == "ok"
    assert observed["session_id"] == "019ff509-8a60-7723-94fd-65942a6d8faa"
    assert observed["final_message"] == "ready"
    assert observed["budget"]["headroom"] == "unknown"
    assert observed["manifest_present"] is False
    # Written back, so the next reader does not have to re-derive it.
    assert (
        json.loads(crew.pointer_path(record["run_id"]).read_text())["phase"]
        == "complete"
    )


def test_observe_records_a_backends_headroom_when_it_reports_one(home, repo) -> None:
    record = crew.dispatch(
        node=_node(),
        project="proj",
        repo=repo,
        config={
            **CONFIG,
            "backends": {
                **CONFIG["backends"],
                "alpha": {**CONFIG["backends"]["alpha"], "command": "claude"},
            },
        },
        session="sess",
        launcher=lambda plan, *, log_path, stderr_path, prompt_path: 0,
    )
    Path(record["log_path"]).write_text((FIXTURES / "claude-turn.jsonl").read_text())
    observed = crew.observe(record["run_id"])
    assert observed["budget"]["headroom"] == "known"
    assert observed["budget"]["utilisation_pct"] == pytest.approx(1.02)
    assert observed["budget"]["resets_at"] == "2026-09-01T00:00:00Z"


def test_observe_sees_the_manifest_that_is_the_real_delivery(
    home, repo, tmp_path
) -> None:
    manifest = tmp_path / "node-a-manifest.md"
    record = _dispatched(home, repo, "codex-turn.jsonl", manifest_path=str(manifest))
    manifest.write_text("node: node-a\nstatus: complete\n")
    assert crew.observe(record["run_id"])["manifest_present"] is True


def test_stop_observe_and_recover_preserve_the_stopped_state(home, repo) -> None:
    spawned: dict[str, subprocess.Popen] = {}

    def launcher(plan, *, log_path, stderr_path, prompt_path):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=plan.cwd,
            start_new_session=True,
        )
        spawned["process"] = process
        return process.pid

    record = crew.dispatch(
        node=_node(manifest_path=str(home / "stopped-manifest.md")),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
        launcher=launcher,
    )
    process = spawned["process"]
    try:
        assert record["pid_start_time"]
        assert crew.terminate(record["run_id"])["phase"] == "stopped"
        process.wait(timeout=5)
        assert crew.observe(record["run_id"])["phase"] == "stopped"
        recovered = crew.recover()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert recovered["counts"]["stopped"] == 1
    assert recovered["runs"][0]["phase"] == "stopped"
    assert recovered["runs"][0]["classification"] == "stopped"


@pytest.mark.parametrize("status", ["blocked", "failed"])
def test_in_harness_observation_uses_the_manifest_status(home, repo, status) -> None:
    manifest = home / f"{status}-manifest.md"
    record = crew.dispatch(
        node=_node(role="inline", manifest_path=str(manifest)),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
    )
    crew.attach(record["run_id"], "task-1")
    _deliver_manifest(record, status, blockers="delivery cannot continue")

    observed = crew.observe(record["run_id"])
    recovered = crew.recover()["runs"][0]

    assert observed["phase"] == status
    assert recovered["phase"] == status
    assert recovered["classification"] == status


def test_recycled_pid_is_refused_before_signalling(home, repo, monkeypatch) -> None:
    spawned: dict[str, subprocess.Popen] = {}

    def launcher(plan, *, log_path, stderr_path, prompt_path):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=plan.cwd,
            start_new_session=True,
        )
        spawned["process"] = process
        return process.pid

    record = crew.dispatch(
        node=_node(),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
        launcher=launcher,
    )
    process = spawned["process"]
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(crew, "_process_start_time", lambda pid: "different-start")
    monkeypatch.setattr(
        crew.os,
        "killpg",
        lambda process_group, sig: signalled.append((process_group, sig)),
    )
    try:
        with pytest.raises(crew.CrewError, match="process identity changed"):
            crew.terminate(record["run_id"])
    finally:
        process.terminate()
        process.wait(timeout=5)

    assert signalled == []
    assert crew.read_pointer(record["run_id"])["phase"] == "starting"


def test_live_view_rechecks_a_killed_worker_without_observation(home, repo) -> None:
    from reckon import mcp

    spawned: dict[str, subprocess.Popen] = {}

    def launcher(plan, *, log_path, stderr_path, prompt_path):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=plan.cwd,
            start_new_session=True,
        )
        spawned["process"] = process
        return process.pid

    record = crew.dispatch(
        node=_node(manifest_path=str(home / "killed-manifest.md")),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
        launcher=launcher,
    )
    pointer = crew.read_pointer(record["run_id"])
    pointer["process_alive"] = True
    crew._write_json(crew.pointer_path(record["run_id"]), pointer)
    process = spawned["process"]
    process.terminate()
    process.wait(timeout=5)

    row = mcp._crew("proj", view="live")["runs"][0]

    assert row["process_alive"] is False
    assert row["classification"] == "abandoned"


def test_permission_denied_process_is_not_owned(monkeypatch) -> None:
    def deny_signal(pid, sig):
        raise PermissionError("not owned")

    monkeypatch.setattr(crew.os, "kill", deny_signal)

    assert crew.process_alive(12345) is False


def test_a_dead_process_with_no_terminal_event_is_an_orphan(home, repo) -> None:
    """Not complete and not failed: recoverable, and it must say so."""
    record = _dispatched(home, repo)
    Path(record["log_path"]).write_text(
        (FIXTURES / "codex-turn.jsonl").read_text().splitlines(keepends=True)[0]
    )
    pointer = json.loads(crew.pointer_path(record["run_id"]).read_text())
    pointer["pid"] = 999999999
    crew._write_json(crew.pointer_path(record["run_id"]), pointer)
    observed = crew.observe(record["run_id"])
    assert observed["phase"] == "orphaned"
    assert "without a terminal event" in observed["detail"]


def test_a_launch_that_never_started_is_an_orphan_not_a_pending_run(home, repo) -> None:
    """An empty log plus a dead process means the worker never began.

    A launch rejected on its arguments exits before writing an event; reporting
    that as still starting would leave the orchestrator waiting forever.
    """
    record = _dispatched(home, repo)
    pointer = json.loads(crew.pointer_path(record["run_id"]).read_text())
    pointer["pid"] = 999999999
    crew._write_json(crew.pointer_path(record["run_id"]), pointer)
    observed = crew.observe(record["run_id"])
    assert observed["phase"] == "orphaned"
    assert record["stderr_path"] in observed["detail"]


def test_a_run_stays_observable_without_its_config_layer(home, repo) -> None:
    """The recorded argv holds the command, so a config change cannot orphan it."""
    record = _dispatched(home, repo, "codex-turn.jsonl")
    assert crew.observe(record["run_id"], config={})["phase"] == "complete"


def _deliver_manifest(record: dict, status: str, **fields: str) -> None:
    lines = ["node: node-a", f"status: {status}"]
    lines.extend(f"{key}: {value}" for key, value in fields.items())
    Path(record["manifest_path"]).write_text("\n".join(lines) + "\n")


def test_run_drain_counts_live_pointers_without_a_valid_disposition(home) -> None:
    for run_id, project in (("r-unowned", "proj"), ("r-foreign", "other")):
        crew._write_json(
            crew.pointer_path(run_id),
            {
                "run_id": run_id,
                "project": project,
                "phase": "working",
                "process_alive": True,
            },
        )

    report = crew.drain("proj")

    assert report["live_pointers"] == 1
    assert report["unreconciled_runs"] == 1
    assert report["disposed_runs"] == 0
    assert report["runs"][0]["run_id"] == "r-unowned"
    assert report["runs"][0]["unreconciled"] is True


def test_handed_off_disposition_removes_a_run_from_the_drain(home) -> None:
    run_id = "r-handed-off"
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "phase": "working",
            "process_alive": True,
        },
    )

    recorded = crew.record_run_disposition(run_id, "handed-off", project="proj")
    report = crew.drain("proj")

    assert recorded["closure_disposition"]["kind"] == "handed-off"
    assert recorded["closure_disposition"]["recorded_at"].endswith("Z")
    assert report["unreconciled_runs"] == 0
    assert report["disposed_runs"] == 1
    assert report["runs"][0]["disposition_valid"] is True


def test_still_working_disposition_expires_when_the_run_turns_terminal(
    home,
) -> None:
    run_id = "r-working"
    manifest = home / "working-manifest.md"
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "phase": "working",
            "process_alive": True,
            "manifest_path": str(manifest),
        },
    )
    crew.record_run_disposition(run_id, "still-working", project="proj")
    assert crew.drain("proj")["unreconciled_runs"] == 0

    manifest.write_text("node: node-a\nstatus: complete\ncommits: HEAD\n")
    report = crew.drain("proj")

    assert report["unreconciled_runs"] == 1
    assert report["runs"][0]["classification"] == "completed_unpromoted"
    assert report["runs"][0]["disposition_valid"] is False


def test_run_drain_refuses_a_disposition_outside_the_closed_set(home) -> None:
    run_id = "r-invalid-disposition"
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "phase": "working",
            "process_alive": True,
        },
    )

    with pytest.raises(crew.CrewError, match="is not one of handed-off, still-working"):
        crew.record_run_disposition(run_id, "probably-fine", project="proj")

    assert "closure_disposition" not in crew.read_pointer(run_id)
    assert crew.drain("proj")["unreconciled_runs"] == 1


def test_cli_drain_records_dispositions_and_mcp_reads_the_same_projection(
    home,
) -> None:
    from reckon import mcp

    run_id = "r-command-drain"
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "phase": "working",
            "process_alive": True,
        },
    )

    command = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "drain",
            "--project",
            "proj",
            "--leave",
            f"{run_id}=handed-off",
        ],
    )
    payload = json.loads(command.output)
    read_view = mcp._crew("proj", view="drain")

    assert command.exit_code == 0
    assert payload["unreconciled_runs"] == 0
    assert payload["recorded"][0]["run_id"] == run_id
    assert read_view["ok"] is True
    assert read_view["view"] == "drain"
    assert read_view["unreconciled_runs"] == payload["unreconciled_runs"]


def test_promoted_run_leaves_the_drain_by_losing_its_pointer(home, repo) -> None:
    manifest = home / "promoted-manifest.md"
    record = _dispatched(home, repo, manifest_path=str(manifest))
    _deliver_manifest(record, "complete", commits="HEAD")
    assert crew.drain("proj")["unreconciled_runs"] == 1

    promoted = crew.complete(record["run_id"], gate="passed", commits=["HEAD"])

    assert promoted["pointer_removed"] is True
    assert crew.drain("proj")["live_pointers"] == 0
    assert crew.drain("proj")["unreconciled_runs"] == 0


def test_terminal_phase_without_a_manifest_is_abandoned(home, repo) -> None:
    record = _dispatched(home, repo, "codex-turn.jsonl")
    observed = crew.observe(record["run_id"])

    row = crew.classify_pointer(observed)

    assert observed["phase"] == "complete"
    assert row["classification"] == "abandoned"
    assert row["manifest_status"] is None
    assert record["stderr_path"] in row["next_action"]
    assert "complete --run" not in row["next_action"]


def test_blocked_manifest_is_not_promotable(home, repo) -> None:
    manifest = home / "blocked-manifest.md"
    record = _dispatched(
        home,
        repo,
        "codex-turn.jsonl",
        manifest_path=str(manifest),
    )
    _deliver_manifest(
        record,
        "blocked",
        blockers="the schema choice needs direction",
    )

    row = crew.classify_pointer(crew.observe(record["run_id"]))

    assert row["classification"] == "blocked"
    assert row["manifest_status"] == "blocked"
    assert "schema choice" in row["detail"]
    assert "complete --run" not in row["next_action"]


def test_failed_manifest_is_not_promotable(home, repo) -> None:
    manifest = home / "failed-manifest.md"
    record = _dispatched(
        home,
        repo,
        "codex-turn.jsonl",
        manifest_path=str(manifest),
    )
    _deliver_manifest(record, "failed", blockers="the focused test failed")

    row = crew.classify_pointer(crew.observe(record["run_id"]))

    assert row["classification"] == "failed"
    assert row["manifest_status"] == "failed"
    assert "focused test failed" in row["detail"]
    assert record["stderr_path"] in row["next_action"]
    assert "complete --run" not in row["next_action"]


def test_only_a_complete_manifest_returns_promotion_advice(home, repo) -> None:
    manifest = home / "complete-manifest.md"
    record = _dispatched(
        home,
        repo,
        "codex-turn.jsonl",
        manifest_path=str(manifest),
    )
    _deliver_manifest(record, "complete", commits="HEAD")

    row = crew.classify_pointer(crew.observe(record["run_id"]))

    assert row["classification"] == "completed_unpromoted"
    assert row["manifest_status"] == "complete"
    assert row["manifest_commits"] == ["HEAD"]
    assert row["next_action"].endswith("--commit HEAD")


def test_crew_read_and_recovery_command_share_classification(home, repo) -> None:
    from reckon import mcp

    manifest = home / "shared-manifest.md"
    record = _dispatched(
        home,
        repo,
        "codex-turn.jsonl",
        manifest_path=str(manifest),
    )
    _deliver_manifest(record, "blocked", blockers="waiting for direction")
    crew.observe(record["run_id"])

    read_row = mcp._crew("proj", view="live")["runs"][0]
    command = CliRunner().invoke(cli_module.main, ["crew", "recover"])
    command_row = json.loads(command.output)["runs"][0]

    assert command.exit_code == 0
    assert command_row["classification"] == read_row["classification"] == "blocked"
    assert command_row["manifest_status"] == read_row["manifest_status"] == "blocked"
    assert command_row["next_action"] == read_row["next_action"]


def test_no_reader_promotes_a_run_without_commit_or_manifest(home, repo) -> None:
    from reckon import mcp

    record = _dispatched(home, repo, "codex-turn.jsonl")
    observed = crew.observe(record["run_id"])
    observed["commits"] = []
    crew._write_json(crew.pointer_path(record["run_id"]), observed)

    direct = crew.classify_pointer(observed)
    read_row = mcp._crew("proj", view="live")["runs"][0]
    recovered = crew.recover()["runs"][0]

    assert {
        direct["classification"],
        read_row["classification"],
        recovered["classification"],
    } == {"abandoned"}
    for row in (direct, read_row, recovered):
        assert row["manifest_present"] is False
        assert "complete --run" not in row["next_action"]


def test_liveness_is_reported_not_inferred() -> None:
    assert crew.process_alive(None) is None
    assert crew.process_alive(999999999) is False


def test_live_classification_flags_a_budget_overrun_in_one_poll() -> None:
    started = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    record = {
        "run_id": "r-overrun",
        "created_at": started.isoformat(),
        "node": {"id": "slow-node", "plan": "plan-a", "time_budget": "10m"},
        "phase": "working",
        "process_alive": True,
    }

    row = crew.classify_pointer(
        record, now_seconds=(started + timedelta(seconds=601)).timestamp()
    )

    assert row["classification"] == "running"
    assert row["budget_seconds"] == 600
    assert row["elapsed_seconds"] == 601
    assert row["budget_overrun"] is True
    assert row["budget_overrun_seconds"] == 1


def test_opt_in_budget_watchdog_stops_and_records_the_run_phase(
    home, monkeypatch
) -> None:
    started = datetime.now(tz=timezone.utc) - timedelta(seconds=21)
    run_id = "r-watchdog"
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "launch": "cli",
            "backend": "alpha",
            "argv": ["codex"],
            "pid": 4242,
            "pid_start_time": "start",
            "phase": "working",
            "created_at": started.isoformat(),
            "node": {"id": "slow-node", "plan": "plan-a", "time_budget": "10s"},
            "manifest_path": str(home / "absent-manifest.md"),
            "log_path": str(home / "absent-stream.jsonl"),
        },
    )
    signalled = []
    monkeypatch.setattr(crew, "process_alive", lambda pid: True)
    monkeypatch.setattr(
        crew, "_signal_process_group", lambda pid, started_at: signalled.append(pid)
    )
    config = {
        "fences": {
            "enforce_budget_watchdog": True,
            "budget_grace_multiple": 2.0,
        }
    }

    observed = crew.observe(run_id, config=config)
    row = crew.classify_pointer(observed)

    assert signalled == [4242]
    assert observed["watchdog_enforced"] is True
    assert observed["phase"] == "stopped"
    assert row["classification"] == "stopped"


def test_resume_answers_in_the_same_session(home, repo) -> None:
    """Advice only makes sense to a worker that remembers what it tried."""
    record = _dispatched(home, repo, "codex-turn.jsonl")
    plan = crew.resume_plan(record["run_id"], "take the second option")
    subcommand = plan.argv.index("resume")
    assert plan.argv[subcommand + 1] == "019ff509-8a60-7723-94fd-65942a6d8faa"
    assert plan.stdin_text == "take the second option"
    assert crew.read_pointer(record["run_id"])["session_id"] == (
        "019ff509-8a60-7723-94fd-65942a6d8faa"
    )


def test_resumed_attempt_owns_its_stream_phase_and_manifest(home, repo) -> None:
    manifest = home / "resume-manifest.md"
    record = _dispatched(
        home,
        repo,
        "codex-turn.jsonl",
        manifest_path=str(manifest),
    )
    _deliver_manifest(record, "blocked", blockers="waiting for direction")
    manifest_baseline = manifest.stat().st_mtime_ns

    plan = crew.resume_plan(record["run_id"], "continue")
    assert "resume" in plan.argv

    resumed_stream = crew.run_dir(record["run_id"]) / "resume-1.jsonl"
    resumed_stream.write_text(
        "".join(
            (FIXTURES / "codex-turn.jsonl").read_text().splitlines(keepends=True)[:2]
        )
    )
    resumed_stderr = crew.run_dir(record["run_id"]) / "resume-1.stderr.log"
    resumed = crew.record_resumption(
        record["run_id"],
        pid=os.getpid(),
        turn=1,
        log_path=resumed_stream,
        stderr_path=resumed_stderr,
        manifest_baseline_mtime_ns=manifest_baseline,
    )

    assert resumed["attempt"] == 2
    assert resumed["attempt_kind"] == "resume"
    assert resumed["log_path"] == str(resumed_stream)
    assert resumed["phase"] == "working"
    observed = crew.observe(record["run_id"])
    assert observed["phase"] == "working"
    assert observed["process_alive"] is True
    assert observed["manifest_file_present"] is True
    assert observed["manifest_fresh"] is False

    sleeps = 0

    def deliver_current_manifest(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        _deliver_manifest(resumed, "complete", commits="HEAD")
        fresh = manifest_baseline + 1_000_000
        os.utime(manifest, ns=(fresh, fresh))

    event = crew.watch(
        "proj",
        stall_window="1h",
        poll_interval=0,
        sleeper=deliver_current_manifest,
    )

    assert sleeps == 1
    assert event["event"] == "terminal"
    assert event["manifest_status"] == "complete"
    assert event["manifest_fresh"] is True


def test_cli_resume_resolves_the_run_projects_budget_policy(
    home, repo, monkeypatch
) -> None:
    record = _dispatched(home, repo, "codex-turn.jsonl")
    observed = crew.observe(record["run_id"])
    observed["budget"] = {
        **observed["budget"],
        "headroom": "known",
        "utilisation_pct": 4.0,
        "threshold_status": "spent",
    }
    crew._write_json(crew.pointer_path(record["run_id"]), observed)
    configured = {
        **CONFIG,
        "budget": {
            "utilisation_ceiling_pct": 83,
            "resume_reserve_pct": 17,
            "exhausted_statuses": ["spent"],
        },
    }
    resolved_calls = []
    passed_configs = []
    original_resume = crew.resume_plan

    def resolve(_module, project, checkout_path, overrides):
        resolved_calls.append((project, checkout_path, overrides))
        return configured

    def resume(run_id, advice, *, config=None):
        passed_configs.append(config)
        return original_resume(run_id, advice, config=config)

    monkeypatch.setattr(cli_module, "_resolved_flight", resolve)
    monkeypatch.setattr(crew, "resume_plan", resume)

    result = CliRunner().invoke(
        cli_module.main,
        ["crew", "resume", "--run", record["run_id"], "--advice", "continue"],
    )
    payload = json.loads(result.output)

    assert result.exit_code == 3
    assert resolved_calls == [("proj", str(repo), ())]
    assert passed_configs == [configured]
    assert payload["hold"]["ceiling_pct"] == 83
    assert payload["hold"]["effective_ceiling_pct"] == 83
    assert "threshold status 'spent'" in payload["hold"]["reason"]


def test_resume_refuses_while_the_previous_process_is_alive(home, repo) -> None:
    record = _dispatched(home, repo, "codex-turn.jsonl")
    observed = crew.observe(record["run_id"])
    observed["pid"] = os.getpid()
    crew._write_json(crew.pointer_path(record["run_id"]), observed)

    with pytest.raises(crew.CrewError, match="still has a live process"):
        crew.resume_plan(record["run_id"], "continue")


def test_read_only_resume_reuses_the_manifest_delivery_directory(home, repo) -> None:
    manifest = home / "review-manifest.md"
    record = _dispatched(
        home,
        repo,
        "codex-turn.jsonl",
        role="review",
        manifest_path=str(manifest),
    )
    crew.observe(record["run_id"])
    plan = crew.resume_plan(record["run_id"], "write the redelivery")
    assert plan.cwd == str(home)
    assert plan.argv[plan.argv.index("-C") + 1] == str(home)
    assert plan.argv[plan.argv.index("--sandbox") + 1] == "workspace-write"
    assert "--skip-git-repo-check" in plan.argv


def test_resume_without_a_session_in_the_stream_names_the_missing_evidence(
    home, repo
) -> None:
    record = _dispatched(home, repo)
    with pytest.raises(crew.CrewError) as excinfo:
        crew.resume_plan(record["run_id"], "advice")
    assert "no session id in its current stream" in str(excinfo.value)


# ── Prompt composition ──────────────────────────────────────────────────────


def test_the_prompt_carries_four_fences_and_points_at_the_plan(home, repo) -> None:
    record = _dispatched(home, repo)
    prompt = Path(record["prompt_path"]).read_text()
    for fence in crew.FENCES:
        assert f"FENCE — {fence.upper()}" in prompt
    assert "proj:plan-a §3" in prompt
    assert record["manifest_path"] in prompt
    assert crew.NEEDS_HELP_MARKER in prompt
    for field in crew.NEEDS_HELP_FIELDS:
        assert f"{field}:" in prompt


def test_the_prompt_copies_no_plan_prose(home, repo) -> None:
    """A copied brief becomes a second source of truth that drifts."""
    record = _dispatched(home, repo)
    prompt = Path(record["prompt_path"]).read_text()
    assert "semantic authority" in prompt
    assert len(prompt.splitlines()) < 70


@pytest.mark.parametrize(
    ("spec_level", "guidance"),
    [
        ("exact", "implement as written and run the named check"),
        ("guided", "the plan fixes the design; derive the implementation"),
        ("open", "the plan fixes the goal and measure; design and implement"),
    ],
)
def test_the_prompt_names_declared_specification_ownership(
    home, repo, spec_level, guidance
) -> None:
    record = _dispatched(home, repo, spec_level=spec_level)
    prompt = Path(record["prompt_path"]).read_text()

    assert f"SPEC     {spec_level} — {guidance}" in prompt


def test_the_prompt_omits_specification_guidance_when_undeclared(home, repo) -> None:
    record = _dispatched(home, repo)

    assert "\nSPEC     " not in Path(record["prompt_path"]).read_text()


def test_the_prompt_names_concurrent_scopes(home, repo) -> None:
    record = crew.dispatch(
        node=_node(),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
        peer_scopes={"node-b": ["reckon/cli.py"]},
        launcher=lambda *a, **k: 1,
    )
    prompt = Path(record["prompt_path"]).read_text()
    assert "node-b → reckon/cli.py" in prompt
    assert record["peer_scopes"] == {"node-b": ["reckon/cli.py"]}


# ── Promotion measurements ─────────────────────────────────────────────────


def test_promotion_without_a_commit_records_absent_changed_lines(home, repo) -> None:
    record = _dispatched(home, repo)

    stored = crew.complete(record["run_id"], gate="passed")["record"]

    assert stored["commits"] == []
    assert stored["changed_lines"] is None


def test_promotion_forwards_the_declared_specification_level(home, repo) -> None:
    record = _dispatched(home, repo, spec_level="guided")

    stored = crew.complete(record["run_id"], gate="passed")["record"]

    assert stored["spec_level"] == "guided"


@pytest.mark.parametrize("gate", ["failed", "not-run"])
def test_non_passing_promotion_requires_a_diagnostic_outcome(home, repo, gate) -> None:
    record = _dispatched(home, repo)

    with pytest.raises(crew.CrewError, match="--outcome.*what failed"):
        crew.complete(record["run_id"], gate=gate)

    assert crew.pointer_path(record["run_id"]).exists()


def test_redispatched_node_records_its_run_lineage(home, repo) -> None:
    first = crew.dispatch(
        node=_node(),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="first",
        launcher=lambda *args, **kwargs: 0,
    )
    crew.complete(first["run_id"], gate="passed")

    second = crew.dispatch(
        node=_node(),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="second",
        launcher=lambda *args, **kwargs: 0,
    )

    expected = {
        "kind": "redispatch",
        "attempt": 2,
        "root_run_id": first["run_id"],
        "previous_run_id": first["run_id"],
    }
    assert second["lineage"] == expected
    assert (
        crew.complete(second["run_id"], gate="passed")["record"]["lineage"] == expected
    )


def test_redispatch_does_not_inherit_a_terminal_delivery(home, repo) -> None:
    manifest = home / "shared-node-manifest.md"
    first = crew.dispatch(
        node=_node(manifest_path=str(manifest)),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="first",
        launcher=lambda *args, **kwargs: 0,
    )
    _deliver_manifest(first, "complete", commits="HEAD")
    crew.complete(first["run_id"], gate="passed")

    second = crew.dispatch(
        node=_node(manifest_path=str(manifest)),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="second",
        launcher=lambda *args, **kwargs: os.getpid(),
    )
    row = crew.classify_pointer(second)

    assert second["attempt"] == 2
    assert second["attempt_kind"] == "redispatch"
    assert second["phase"] == "starting"
    assert second["log_path"] != first["log_path"]
    assert row["classification"] == "running"
    assert row["manifest_file_present"] is True
    assert row["manifest_fresh"] is False
    assert row["manifest_status"] is None


def test_unresolvable_commit_records_a_typed_diff_absence(home, repo) -> None:
    record = _dispatched(home, repo)
    revision = "not-a-revision"

    stored = crew.complete(record["run_id"], gate="passed", commits=[revision])[
        "record"
    ]

    assert stored["commits"] == [revision]
    assert stored["changed_lines"] == {
        "available": False,
        "reason": "unresolvable_revision",
    }
    assert "fatal:" not in json.dumps(stored["changed_lines"])
    assert not crew.pointer_path(record["run_id"]).exists()


def test_complete_clears_a_pointer_when_the_run_is_already_ledgered(home, repo) -> None:
    record = _dispatched(home, repo)
    first = crew.complete(record["run_id"], gate="passed")
    crew._write_json(crew.pointer_path(record["run_id"]), record)

    recovered = crew.complete(record["run_id"], gate="passed")

    assert first["already_promoted"] is False
    assert recovered["already_promoted"] is True
    assert recovered["pointer_removed"] is True
    assert len(ledger.runs("proj", repo)) == 1


def test_discard_prints_and_removes_a_ledgered_pointer_without_promoting(
    home, repo
) -> None:
    record = _dispatched(home, repo)
    crew.complete(record["run_id"], gate="passed")
    crew._write_json(crew.pointer_path(record["run_id"]), record)

    command = CliRunner().invoke(
        cli_module.main, ["crew", "discard", "--run", record["run_id"]]
    )
    payload = json.loads(command.output)

    assert command.exit_code == 0
    assert payload["pointer_removed"] is True
    assert payload["removed"]["run_id"] == record["run_id"]
    assert not crew.pointer_path(record["run_id"]).exists()
    assert len(ledger.runs("proj", repo)) == 1


def test_discard_refuses_while_the_recorded_pid_is_alive(home, repo) -> None:
    record = _dispatched(home, repo)
    pointer = crew.read_pointer(record["run_id"])
    pointer["pid"] = os.getpid()
    crew._write_json(crew.pointer_path(record["run_id"]), pointer)

    with pytest.raises(crew.CrewError, match="recorded pid .* is alive"):
        crew.discard(record["run_id"])

    assert crew.pointer_path(record["run_id"]).is_file()


def test_outside_repository_scope_records_absent_changed_lines(
    home, repo, tmp_path
) -> None:
    record = _dispatched(home, repo)
    pointer = json.loads(crew.pointer_path(record["run_id"]).read_text())
    pointer["node"]["write_paths"] = [str(tmp_path / "outside.py")]
    crew._write_json(crew.pointer_path(record["run_id"]), pointer)

    stored = crew.complete(record["run_id"], gate="passed", commits=["HEAD"])["record"]

    assert stored["changed_lines"] == {
        "available": False,
        "reason": "diff_unavailable",
    }


# ── Worker reports ──────────────────────────────────────────────────────────

MANIFEST = """\
node: node-a
status: complete
commits: 1a2b3c4
changed_paths: reckon/_backends.py
tests: uv run pytest tests/test_backends.py -q -> 28 passed
test_logs: /tmp/backends.log
artifacts: none
evidence_inputs: 28 new tests, all four fixtures parsed
follow_ons: the observe command needs a --wait flag, the fixture README needs a diagram
blockers: none
"""


def test_a_manifest_parses_into_structured_fields() -> None:
    manifest = crew.parse_manifest(MANIFEST)
    assert manifest["status"] == "complete"
    assert manifest["commits"] == ["1a2b3c4"]
    assert manifest["changed_paths"] == ["reckon/_backends.py"]
    assert manifest["blockers"] == []
    assert manifest["artifacts"] == []
    assert len(manifest["follow_ons"]) == 2


def test_an_audited_manifest_catches_out_of_scope_changes() -> None:
    text = MANIFEST.replace(
        "changed_paths: reckon/_backends.py",
        "changed_paths: reckon/_backends.py, reckon/cli.py",
    )
    audit = crew.audit_manifest(text, _node())
    assert audit["ok"] is False
    assert "reckon/cli.py" in " ".join(audit["findings"])


def test_a_complete_manifest_without_a_commit_is_incomplete() -> None:
    text = MANIFEST.replace("commits: 1a2b3c4", "commits: none")
    audit = crew.audit_manifest(text, _node())
    assert any("no commit" in finding for finding in audit["findings"])


def test_a_clean_manifest_audits_clean() -> None:
    assert crew.audit_manifest(MANIFEST, _node())["ok"] is True


NEEDS_HELP = """\
NEEDS-HELP: the schema rejects the enum value the config file needs
tried: set gates.enforce to off; the layer parsed it as the boolean false and
  validation rejected it against the enum
options: spell the value disabled in the schema; or accept the boolean and
  coerce it
leaning: spell it disabled, because the coercion would be invisible in the file
cost-if-wrong: the generated model and its committed JSON Schema regenerate
"""


def test_an_escape_hatch_report_becomes_a_decision_brief() -> None:
    parsed = crew.parse_needs_help(NEEDS_HELP)
    assert parsed["complete"] is True
    assert parsed["missing"] == []
    assert parsed["headline"].startswith("the schema rejects")
    assert "boolean false" in parsed["fields"]["tried"]
    assert parsed["fields"]["leaning"]


def test_an_incomplete_escape_hatch_names_the_missing_fields() -> None:
    """A vague plea wastes as much time as thrashing."""
    parsed = crew.parse_needs_help("NEEDS-HELP: stuck\ntried: a few things\n")
    assert parsed["complete"] is False
    assert parsed["missing"] == ["options", "leaning", "cost-if-wrong"]


def test_a_manifest_carrying_an_escape_hatch_reports_it() -> None:
    manifest = crew.parse_manifest(NEEDS_HELP + "\nstatus: blocked\n")
    assert manifest["needs_help"]["complete"] is True
    assert manifest["status"] == "blocked"


def test_a_manifest_without_one_reports_no_escape_hatch() -> None:
    assert crew.parse_manifest(MANIFEST)["needs_help"] is None


# ── Continuation, worker altitude ───────────────────────────────────────────


def test_a_workers_candidate_follow_on_becomes_a_plan_followup() -> None:
    """Work a worker was fenced out of otherwise has nowhere to go but prose."""
    ops = crew.followup_ops_from_manifest(MANIFEST, slug="plan-a", section="§3")
    assert len(ops) == 2
    for op in ops:
        assert op["op"] == "append"
        assert op["target"] == "followups"
        assert op["item"]["prompt"] == "/reckon-ship plan-a §3"
        assert op["item"]["status"] == "open"
        assert op["item"]["title"]
    assert {op["item"]["id"] for op in ops} == {op["item"]["id"] for op in ops}


def test_a_manifest_with_no_follow_ons_produces_no_ops() -> None:
    text = MANIFEST.replace(
        "follow_ons: the observe command needs a --wait flag, the fixture README needs a diagram",
        "follow_ons: none",
    )
    assert crew.followup_ops_from_manifest(text, slug="s") == []


# ── The summary reflex ──────────────────────────────────────────────────────

DISPATCH_SUMMARY = """\
Dispatching wave 1 — 2 workers
WHAT   §3 translation module (impl-a) · §3 CLI primitive (impl-b)
WHY    both read the recorded fixtures; no shared files
HOW    detached worktrees, scopes below, manifests on disk
WHEN   ~20 min each; the end-to-end gate closes the wave
"""

COMPLETION_SUMMARY = """\
Wave 1 complete — 2/2 landed
WHAT   translation module + CLI primitive (1a2b3c4, 5d6e7f8)
WHY    gate evidence: 28 backend tests green, all 4 recorded fixtures parsed
HOW    both scoped clean on git show --stat; no out-of-scope paths
WHEN   next the escape-hatch round-trip; nothing blocks it
"""


def test_a_dispatch_summary_needs_all_four_axes() -> None:
    assert crew.validate_summary(DISPATCH_SUMMARY, occasion="dispatch")["ok"] is True
    missing = crew.validate_summary("WHAT a thing\nWHY because\n", occasion="dispatch")
    assert missing["ok"] is False
    assert "axis HOW is missing" in missing["findings"]


def test_a_completion_summary_must_carry_the_gate_evidence() -> None:
    """The discipline that makes the format earn its place."""
    assert (
        crew.validate_summary(COMPLETION_SUMMARY, occasion="completion")["ok"] is True
    )
    vague = COMPLETION_SUMMARY.replace(
        "WHY    gate evidence: 28 backend tests green, all 4 recorded fixtures parsed",
        "WHY    gate evidence: the tests look good",
    )
    verdict = crew.validate_summary(vague, occasion="completion")
    assert verdict["ok"] is False
    assert "quantitative" in " ".join(verdict["findings"])


def test_an_axis_may_not_run_past_two_lines() -> None:
    verbose = DISPATCH_SUMMARY.replace(
        "WHY    both read the recorded fixtures; no shared files",
        "WHY    one\n  two\n  three",
    )
    verdict = crew.validate_summary(verbose, occasion="dispatch")
    assert any("runs to 3 lines" in finding for finding in verdict["findings"])


def test_the_reflex_has_four_axes() -> None:
    assert crew.SUMMARY_AXES == ("WHAT", "WHY", "HOW", "WHEN")


def test_a_subjective_word_qualifying_an_input_is_not_the_measure() -> None:
    """An adjective on an input does not erase a concrete measure beside it.

    The refusal exists to stop a done-when whose *verdict* is a feeling. When the
    verdict is a counted row and the flagged word merely describes the fixture that
    produces it, refusing is ceremony: it teaches wording, not measurability.
    """
    verdict = crew.validate_node(
        _node(
            done_when=(
                "a correctly spelled property produces no violation row and a "
                "misspelled one produces exactly 1"
            )
        ),
        budget_ceiling="25m",
    )
    assert verdict.ok, verdict.findings


def test_a_subjective_word_as_the_predicate_is_still_refused() -> None:
    """A copula complement is the measure itself, so it must still fail."""
    for spoiled in (
        "the dispatch path is clean",
        "exit code 0 is readable",
        "the replacement reads better than 3 prior revisions",
        "the emitted file is correctly formatted",
    ):
        verdict = crew.validate_node(_node(done_when=spoiled), budget_ceiling="25m")
        assert "demonstrable" in verdict.failed_properties, spoiled


def test_a_subjective_done_when_with_no_observable_still_fails() -> None:
    """Without any evidence signal the adjective is all there is."""
    verdict = crew.validate_node(
        _node(done_when="the resulting module feels tidy and appropriate"),
        budget_ceiling="25m",
    )
    assert "demonstrable" in verdict.failed_properties


def test_describing_machinery_that_decides_is_not_unspecified_intent() -> None:
    """``decide`` describing code under test does not hand the worker a choice."""
    verdict = crew.validate_node(
        _node(
            done_when=(
                "the report states whether the resolver decides a relation "
                "reproduces its declared unit, citing 2 file:line pairs"
            )
        ),
        budget_ceiling="25m",
    )
    assert verdict.ok, verdict.findings


def test_handing_the_decision_to_the_worker_is_still_unspecified() -> None:
    for spoiled in (
        "you decide which model to use and report 3 rows",
        "decide as appropriate and record the exit code",
        "the worker decides the threshold, then reports 5 counts",
    ):
        verdict = crew.validate_node(_node(done_when=spoiled), budget_ceiling="25m")
        assert "fully-specified" in verdict.failed_properties, spoiled
