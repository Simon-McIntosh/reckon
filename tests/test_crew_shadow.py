"""Crew shadow dispatch: a candidate run derived from a committed primary.

The shadow tests moved here so ``tests/test_crew.py`` fits the worker-window
bound. They share that module's fixtures and helpers, imported by name so
``test_crew``'s autouse fixture and module-level cells stay scoped to the
module that owns them.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, crew_dispatch_commands, ledger
from reckon.crew.dispatch import shadow as dispatch_shadow
from tests import test_crew as _test_crew

CONFIG = _test_crew.CONFIG
_dispatched = _test_crew._dispatched
_node = _test_crew._node
home = _test_crew.home
repo = _test_crew.repo


def _candidate_config() -> dict:
    """Route implement nodes to a distinct configured candidate backend."""
    config = json.loads(json.dumps(CONFIG))
    config["backends"]["candidate"] = {
        **config["backends"]["alpha"],
        "model": "candidate-model",
    }
    config["roles"]["implement"] = {"backend": "candidate"}
    return config


def _candidate_config_with_distinct_agent() -> dict:
    """Route to a candidate whose defaults differ from the primary agent."""
    config = _candidate_config()
    config["backends"]["candidate"].update(
        {"effort": "medium", "sandbox": "read-only", "time_budget": "8m"}
    )
    return config


# ── Crew shadow ─────────────────────────────────────────────────────────────


def test_shadow_derives_the_committed_node_at_the_primary_base(home, repo) -> None:
    primary_pointer = _dispatched(home, repo, spec_level="guided")
    primary = crew.complete(primary_pointer["run_id"], gate="passed")["record"]
    live_same_scope = crew.dispatch(
        node=_node(spec_level="guided"),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="live-same-scope",
        launcher=lambda *args, **kwargs: 0,
    )

    shadow = dispatch_shadow(
        primary["run_id"],
        candidate_backend="candidate",
        config=_candidate_config(),
        repo=repo,
        session="shadow-session",
        launcher=lambda *args, **kwargs: 0,
    )

    stored_node = primary["node_definition"]
    for name in (
        "id",
        "goal",
        "plan",
        "section",
        "role",
        "spec_level",
        "done_when",
        "write_paths",
    ):
        assert shadow["node"][name] == stored_node[name]
    assert shadow["base_sha"] == primary["base_sha"]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=shadow["worktree"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == primary["base_sha"]
    assert shadow["lineage"]["kind"] == "shadow"
    assert shadow["lineage"]["primary_run_id"] == primary["run_id"]
    assert shadow["attempt"] == primary["attempt"] == 1
    assert shadow["attempt_kind"] == "shadow"
    assert shadow["peer_scopes"] == {}
    assert crew.pointer_path(live_same_scope["run_id"]).is_file()
    assert Path(shadow["manifest_path"]).parent == crew.run_dir(shadow["run_id"])
    assert "without committing" in Path(shadow["prompt_path"]).read_text()


def test_shadow_inherits_primary_agent_settings_while_switching_backend(
    home, repo
) -> None:
    primary_pointer = _dispatched(home, repo)
    primary = crew.complete(primary_pointer["run_id"], gate="passed")["record"]

    shadow = dispatch_shadow(
        primary["run_id"],
        candidate_backend="candidate",
        config=_candidate_config_with_distinct_agent(),
        repo=repo,
        session="shadow-session",
        launcher=lambda *args, **kwargs: 0,
    )

    assert shadow["agent"] == {
        "backend": "candidate",
        "launch": primary["agent"]["launch"],
        "model": "candidate-model",
        "effort": primary["agent"]["effort"],
        "sandbox": primary["agent"]["sandbox"],
    }
    comparison = shadow["lineage"]["configuration"]
    assert comparison["substituted"]["backend"] == {
        "primary": "alpha",
        "shadow": "candidate",
        "via": "backend",
    }
    assert comparison["inherited"]["effort"] == "high"
    assert comparison["inherited"]["sandbox"] == "worktree-full"


def test_shadow_explicit_effort_override_is_used_and_recorded(home, repo) -> None:
    primary_pointer = _dispatched(home, repo)
    primary = crew.complete(primary_pointer["run_id"], gate="passed")["record"]

    shadow = dispatch_shadow(
        primary["run_id"],
        candidate_backend="candidate",
        config=_candidate_config_with_distinct_agent(),
        repo=repo,
        session="shadow-session",
        configuration_overrides={"effort"},
        launcher=lambda *args, **kwargs: 0,
    )

    assert shadow["agent"]["effort"] == "medium"
    assert shadow["lineage"]["configuration"]["substituted"]["effort"] == {
        "primary": "high",
        "shadow": "medium",
        "via": "override",
    }


def test_shadow_inherits_primary_time_budget_instead_of_role_default(
    home, repo
) -> None:
    primary_pointer = _dispatched(home, repo)
    primary = crew.complete(primary_pointer["run_id"], gate="passed")["record"]

    shadow = dispatch_shadow(
        primary["run_id"],
        candidate_backend="candidate",
        config=_candidate_config_with_distinct_agent(),
        repo=repo,
        session="shadow-session",
        launcher=lambda *args, **kwargs: 0,
    )

    assert primary["time_budget"] == "20m"
    assert shadow["node"]["time_budget"] == "20m"
    assert shadow["lineage"]["configuration"]["inherited"]["time_budget"] == "20m"


def test_shadow_explicit_time_budget_override_is_used_and_recorded(home, repo) -> None:
    primary_pointer = _dispatched(home, repo)
    primary = crew.complete(primary_pointer["run_id"], gate="passed")["record"]
    config = _candidate_config_with_distinct_agent()
    config["backends"]["candidate"]["time_budget"] = "12m"

    shadow = dispatch_shadow(
        primary["run_id"],
        candidate_backend="candidate",
        config=config,
        repo=repo,
        session="shadow-session",
        configuration_overrides={"time_budget"},
        launcher=lambda *args, **kwargs: 0,
    )

    assert shadow["node"]["time_budget"] == "12m"
    assert shadow["lineage"]["configuration"]["substituted"]["time_budget"] == {
        "primary": "20m",
        "shadow": "12m",
        "via": "override",
    }


def test_shadow_without_recorded_time_budget_uses_role_default_and_records_fallback(
    home, repo
) -> None:
    primary_pointer = _dispatched(home, repo)
    primary = crew.complete(primary_pointer["run_id"], gate="passed")["record"]
    data, version = ledger.load("proj", repo)
    data["runs"][0].pop("time_budget")
    ledger.write("proj", data, version, repo)
    # Registering a session member commits the state file, and refuses while
    # the file holds content the caller has not committed; the rewritten ledger
    # is committed here so the dispatch is not the one to carry it.
    subprocess.run(
        ["git", "add", "docs/state/proj/crew.json"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "commit",
            "-q",
            "-m",
            "test: drop the recorded budget\n\nFixture state.",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    shadow = dispatch_shadow(
        primary["run_id"],
        candidate_backend="candidate",
        config=_candidate_config_with_distinct_agent(),
        repo=repo,
        session="shadow-session",
        launcher=lambda *args, **kwargs: 0,
    )

    comparison = shadow["lineage"]["configuration"]
    assert shadow["node"]["time_budget"] == "8m"
    assert comparison["inherited"]["time_budget"] == "8m"
    assert comparison["fallbacks"]["time_budget"] == {
        "source": "resolved_role_default",
        "value": "8m",
    }


def test_shadow_refuses_primary_without_recorded_agent_configuration(
    home, repo
) -> None:
    primary_pointer = _dispatched(home, repo)
    primary = crew.complete(primary_pointer["run_id"], gate="passed")["record"]
    data, version = ledger.load("proj", repo)
    data["runs"][0].pop("agent")
    ledger.write("proj", data, version, repo)

    with pytest.raises(crew.CrewError, match="no recorded agent configuration"):
        dispatch_shadow(
            primary["run_id"],
            candidate_backend="candidate",
            config=_candidate_config(),
            repo=repo,
            session="shadow-session",
            launcher=lambda *args, **kwargs: 0,
        )

    assert crew.list_live() == []


def test_shadow_accepts_historical_plan_when_current_plan_changed(home, repo) -> None:
    primary_pointer = _dispatched(home, repo)
    primary = crew.complete(primary_pointer["run_id"], gate="passed")["record"]
    plan = repo / "docs" / "plans" / "plan-a.html"
    plan.write_text(plan.read_text().replace("§3 — Dispatch", "§3 — Landed"))
    subprocess.run(
        ["git", "add", "docs/plans/plan-a.html"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "docs: update dispatch section"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    shadow = dispatch_shadow(
        primary["run_id"],
        candidate_backend="candidate",
        config=_candidate_config(),
        repo=repo,
        session="shadow-session",
        launcher=lambda *args, **kwargs: 0,
    )

    assert shadow["base_sha"] == primary["base_sha"]
    assert Path(shadow["worktree"]).is_dir()


def test_shadow_refuses_plan_unreadable_at_primary_base(home, repo) -> None:
    primary_pointer = _dispatched(home, repo)
    primary = crew.complete(primary_pointer["run_id"], gate="passed")["record"]
    plan = repo / "docs" / "plans" / "plan-a.html"
    original = plan.read_text()
    plan.unlink()
    subprocess.run(
        ["git", "add", "docs/plans/plan-a.html"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "test: remove dispatch authority"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    unreadable_base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    plan.write_text(original)
    subprocess.run(
        ["git", "add", "docs/plans/plan-a.html"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "test: restore dispatch authority"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    data, version = ledger.load("proj", repo)
    data["runs"][0]["base_sha"] = unreadable_base
    ledger.write("proj", data, version, repo)

    with pytest.raises(crew.PlanVisibilityError, match="not readable at base"):
        dispatch_shadow(
            primary["run_id"],
            candidate_backend="candidate",
            config=_candidate_config(),
            repo=repo,
            session="shadow-session",
            launcher=lambda *args, **kwargs: 0,
        )

    assert crew.list_live() == []


def test_shadow_refuses_section_absent_at_primary_base(home, repo) -> None:
    primary_pointer = _dispatched(home, repo)
    primary = crew.complete(primary_pointer["run_id"], gate="passed")["record"]
    plan = repo / "docs" / "plans" / "plan-a.html"
    original = plan.read_text()
    plan.write_text(
        original.replace('id="s3"', 'id="dispatch"').replace("§3", "Dispatch")
    )
    subprocess.run(
        ["git", "add", "docs/plans/plan-a.html"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "test: omit requested section"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    sectionless_base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    plan.write_text(original.replace("Dispatch", "Current dispatch"))
    subprocess.run(
        ["git", "add", "docs/plans/plan-a.html"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "test: restore requested section"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    data, version = ledger.load("proj", repo)
    data["runs"][0]["base_sha"] = sectionless_base
    ledger.write("proj", data, version, repo)

    with pytest.raises(crew.PlanVisibilityError, match="does not contain section"):
        dispatch_shadow(
            primary["run_id"],
            candidate_backend="candidate",
            config=_candidate_config(),
            repo=repo,
            session="shadow-session",
            launcher=lambda *args, **kwargs: 0,
        )

    assert crew.list_live() == []


def test_shadow_refuses_an_unreachable_primary_base_without_artifacts(
    home, repo
) -> None:
    primary_pointer = _dispatched(home, repo)
    primary = crew.complete(primary_pointer["run_id"], gate="passed")["record"]
    data, version = ledger.load("proj", repo)
    data["runs"][0]["base_sha"] = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    ledger.write("proj", data, version, repo)

    with pytest.raises(crew.PlanVisibilityError, match="base"):
        dispatch_shadow(
            primary["run_id"],
            candidate_backend="candidate",
            config=_candidate_config(),
            repo=repo,
            session="shadow-session",
            launcher=lambda *args, **kwargs: 0,
        )

    assert crew.list_live() == []


def test_shadow_cli_routes_the_candidate_and_derives_the_node(
    home, repo, monkeypatch
) -> None:
    primary_pointer = _dispatched(home, repo, spec_level="guided")
    primary = crew.complete(primary_pointer["run_id"], gate="passed")["record"]
    observed_overrides = []

    def resolve(_flight, project, checkout_path, overrides):
        observed_overrides.extend(overrides)
        assert project == "proj"
        assert checkout_path == repo
        config = _candidate_config_with_distinct_agent()
        config["backends"]["candidate"]["time_budget"] = "12m"
        return config

    monkeypatch.chdir(repo)
    monkeypatch.setattr(crew_dispatch_commands, "_resolved_flight", resolve)
    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "shadow",
            "--run",
            primary["run_id"],
            "--session",
            "shadow-session",
            "--backend",
            "candidate",
            "--set",
            "roles.implement.effort=medium",
            "--set",
            "roles.implement.time_budget=12m",
            "--dry-run",
        ],
    )
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["dry_run"] is True
    assert payload["primary_run_id"] == primary["run_id"]
    assert payload["backend"] == "candidate"
    assert payload["lineage"]["configuration"]["substituted"]["effort"] == {
        "primary": "high",
        "shadow": "medium",
        "via": "override",
    }
    assert payload["time_budget"] == "12m"
    assert payload["lineage"]["configuration"]["substituted"]["time_budget"] == {
        "primary": "20m",
        "shadow": "12m",
        "via": "override",
    }
    assert observed_overrides[-2:] == [
        "roles.implement.backend=candidate",
        "roles.implement.by_spec_level.guided.backend=candidate",
    ]
    assert crew.list_live() == []


def test_shadow_cli_accepts_and_persists_session_and_wave(
    home, repo, monkeypatch
) -> None:
    primary_pointer = _dispatched(home, repo, spec_level="guided")
    primary = crew.complete(primary_pointer["run_id"], gate="passed")["record"]

    def resolve(_flight, project, checkout_path, overrides):
        assert project == "proj"
        assert checkout_path == repo
        config = _candidate_config()
        config["backends"]["candidate"]["launch"] = "in-harness"
        return config

    monkeypatch.chdir(repo)
    monkeypatch.setattr(crew_dispatch_commands, "_resolved_flight", resolve)
    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "shadow",
            "--run",
            primary["run_id"],
            "--session",
            "shadow-session",
            "--wave",
            "wave-a",
            "--backend",
            "candidate",
        ],
    )
    payload = json.loads(result.output)
    pointer = crew.read_pointer(payload["run_id"])

    assert result.exit_code == 0
    assert pointer["session"] == "shadow-session"
    assert pointer["wave"] == "wave-a"


def test_shadow_completion_refuses_commits_and_measures_the_retained_patch(
    home, repo
) -> None:
    primary_pointer = _dispatched(home, repo)
    primary = crew.complete(primary_pointer["run_id"], gate="passed")["record"]
    shadow = dispatch_shadow(
        primary["run_id"],
        candidate_backend="candidate",
        config=_candidate_config(),
        repo=repo,
        session="shadow-session",
        launcher=lambda *args, **kwargs: 0,
    )
    changed = Path(shadow["worktree"]) / "reckon" / "_backends.py"
    changed.parent.mkdir()
    changed.write_text("first = 1\nsecond = 2\n")
    patch = crew.run_dir(shadow["run_id"]) / "shadow.patch"

    with pytest.raises(crew.CrewError, match="--commit is refused"):
        crew.complete(shadow["run_id"], gate="passed", commits=["HEAD"])

    assert crew.pointer_path(shadow["run_id"]).is_file()
    assert not patch.exists()
    stored = crew.complete(shadow["run_id"], gate="passed")["record"]

    assert stored["commits"] == []
    assert stored["changed_lines"] == {"added": 2, "removed": 0, "files": 1}
    assert stored["shadow_patch"] == str(patch)
    assert patch.is_file()
    assert "reckon/_backends.py" in patch.read_text()
    assert stored["lineage"] == shadow["lineage"]
    assert (
        stored["lineage"]["configuration"]["substituted"]["backend"]["shadow"]
        == "candidate"
    )
    assert stored["lineage"]["configuration"]["inherited"]["effort"] == "high"
    assert stored["lineage"]["configuration"]["inherited"]["time_budget"] == "20m"
    assert stored["attempt"] == primary["attempt"]
    assert stored["attempt_kind"] == "shadow"
    assert (
        stored["node_definition"]["done_when"]
        == primary["node_definition"]["done_when"]
    )
    assert not crew.pointer_path(shadow["run_id"]).exists()

    redispatch = crew.dispatch(
        node=_node(),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="after-shadow",
        launcher=lambda *args, **kwargs: 0,
    )
    assert redispatch["attempt"] == 2
    assert redispatch["lineage"]["previous_run_id"] == primary["run_id"]
