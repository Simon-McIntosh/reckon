"""Uniform dispatch: the task contract, routing, run records and worker reports.

Every test here is hermetic. ``RECKON_HOME`` moves the crew directory into a temp
tree, the repository is a real but throwaway git repo, and the one test that
needs a process substitutes a launcher — so nothing spawns a harness and nothing
reaches a network.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew


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
        "done_when": "uv run pytest tests/test_backends.py reports 28 passed",
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


def test_the_resolution_fills_the_defaults_a_dispatch_would_fill(home) -> None:
    """A dry run must be the same decision as a dispatch, not a second one."""
    node = _node(time_budget="", manifest_path="")
    resolution = crew.plan_dispatch(node=node, config=CONFIG)
    assert resolution.node.time_budget == "25m"
    assert Path(resolution.node.manifest_path).is_absolute()
    assert resolution.run_id in resolution.node.manifest_path
    assert resolution.validation.ok
    assert resolution.launch == "cli"


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
        "adjusted_hours": pytest.approx(2.857143),
        "agent_key": payload["agent_key"],
        "allowed": False,
        "competence_horizon_hours": 2.5,
        "estimated_hours": 4.0,
        "reason": "competence-horizon-exceeded",
        "recommendation": (
            "split into nodes no larger than 3.5 worker-hours for this agent "
            "configuration"
        ),
        "speed_factor": 1.4,
        "target_size_hours": 3.5,
    }
    assert "3.5 worker-hours" in str(excinfo.value)
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
    assert record["competence"]["reason"] == "within-competence-horizon"
    assert record["competence"]["adjusted_hours"] == pytest.approx(1.666667)
    assert record["competence"]["speed_factor"] == 0.9
    assert record["competence"]["competence_horizon_hours"] == 2.5


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


def test_resume_answers_in_the_same_session(home, repo) -> None:
    """Advice only makes sense to a worker that remembers what it tried."""
    record = _dispatched(home, repo, "codex-turn.jsonl")
    crew.observe(record["run_id"])
    plan = crew.resume_plan(record["run_id"], "take the second option")
    subcommand = plan.argv.index("resume")
    assert plan.argv[subcommand + 1] == "019ff509-8a60-7723-94fd-65942a6d8faa"
    assert plan.stdin_text == "take the second option"


def test_resume_before_a_session_id_exists_says_to_observe_first(home, repo) -> None:
    record = _dispatched(home, repo)
    with pytest.raises(crew.CrewError) as excinfo:
        crew.resume_plan(record["run_id"], "advice")
    assert "observe it first" in str(excinfo.value)


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


def test_promotion_refuses_an_unresolvable_commit_value(home, repo) -> None:
    record = _dispatched(home, repo)
    revision = "not-a-revision"

    with pytest.raises(crew.CrewError, match=revision):
        crew.complete(record["run_id"], gate="passed", commits=[revision])

    assert crew.pointer_path(record["run_id"]).is_file()


def test_outside_repository_scope_records_absent_changed_lines(
    home, repo, tmp_path
) -> None:
    record = _dispatched(home, repo)
    pointer = json.loads(crew.pointer_path(record["run_id"]).read_text())
    pointer["node"]["write_paths"] = [str(tmp_path / "outside.py")]
    crew._write_json(crew.pointer_path(record["run_id"]), pointer)

    stored = crew.complete(record["run_id"], gate="passed", commits=["HEAD"])["record"]

    assert stored["changed_lines"] is None


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
