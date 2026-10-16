"""A shadow's worktree path and prompt never name the primary it measures.

The worktree session a shadow runs under used to carry the primary's run id, so
the shadow's own composed prompt named the run it was supposed to be measured
against. The session is now built from the candidate backend and the run's
recorded component alone. Records written before that change keep the primary in
their path, and :func:`reckon.crew.routing._shadow_worktree_records` still
resolves them, because the ledger row does not record which naming produced it.

Every test is hermetic: ``RECKON_HOME`` moves the crew directory into a temp
tree, the repository is a throwaway git repo, and dispatch substitutes a
launcher so nothing spawns a harness.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import routing
from reckon.crew.dispatch import shadow as dispatch_shadow

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
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _node(manifest_path: str) -> crew.TaskNode:
    return crew.TaskNode(
        id="node-a",
        goal="record the launch matrix for one backend",
        plan="plan-a",
        section="dispatch",
        spec_level="exact",
        done_when="uv run pytest tests/test_backends.py reports 34 passed",
        write_paths=["reckon/_backends.py"],
        time_budget="20m",
        manifest_path=manifest_path,
    )


def _candidate_config(backend_name: str) -> dict:
    config = json.loads(json.dumps(CONFIG))
    config["backends"][backend_name] = {
        **config["backends"]["alpha"],
        "model": f"{backend_name}-model",
    }
    config["roles"]["implement"] = {"backend": backend_name}
    return config


@pytest.fixture()
def home(tmp_path, monkeypatch):
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path, home):
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
        source.read_text()
    )
    (root / "docs" / "plans" / "plan-a.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="plan-a">
</head><body><h2 id="dispatch">Dispatch</h2></body></html>
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
    (home / "mounts.json").write_text(json.dumps({"proj": str(root / "docs")}))
    return root


def _completed_primary(home, repo) -> dict:
    pointer = crew.dispatch(
        node=_node(str(home / "node-a-manifest.md")),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
        launcher=lambda *args, **kwargs: 0,
        backend_override="alpha",
    )
    return crew.complete(pointer["run_id"], gate="passed")["record"]


def _shadow(primary_run_id: str, backend_name: str, repo: Path) -> dict:
    return dispatch_shadow(
        primary_run_id,
        candidate_backend=backend_name,
        config=_candidate_config(backend_name),
        repo=repo,
        session="shadow-session",
        launcher=lambda *args, **kwargs: 0,
    )


def _commit_legacy_shadow(
    repo: Path,
    home: Path,
    *,
    run_id: str,
    primary_run_id: str,
    backend: str,
    component: str,
) -> dict:
    artifact = home / "crew" / "runs" / run_id / "shadow.patch"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("retained evidence\n")
    lineage = {
        "kind": "shadow",
        "primary_run_id": primary_run_id,
        "worktree_component": component,
        "configuration": {},
    }
    ledger.append_run(
        "proj",
        ledger.build_record(
            run_id=run_id,
            plan="plan-a",
            gate="passed",
            node="node-a",
            backend=backend,
            lineage=lineage,
            shadow_patch=str(artifact),
        ),
        root=repo,
    )
    return lineage


def test_a_shadow_worktree_and_prompt_name_no_primary(home, repo) -> None:
    primary = _completed_primary(home, repo)
    primary_id = str(primary["run_id"])

    shadow = _shadow(primary_id, "candidate-a", repo)

    worktree = str(shadow["worktree"])
    component = str(shadow["lineage"]["worktree_component"])
    prompt = (Path(shadow["prompt_path"])).read_text()

    # The primary's run id appears in neither the worktree path the shadow works
    # in nor the composed prompt that names that path.
    assert primary_id not in worktree, worktree
    assert primary_id not in prompt, prompt

    # And the session is built from exactly the candidate backend and the
    # recorded component.
    assert f"shadow-candidate-a-{component}" in worktree
    assert component


def test_a_dry_run_records_a_component_that_names_no_primary(home, repo) -> None:
    primary = _completed_primary(home, repo)
    primary_id = str(primary["run_id"])

    resolution = dispatch_shadow(
        primary_id,
        candidate_backend="candidate-a",
        config=_candidate_config("candidate-a"),
        repo=repo,
        session="shadow-session",
        dry_run=True,
    )
    component = str(resolution["lineage"]["worktree_component"])
    session = routing.shadow_worktree_session(primary_id, "candidate-a", component)

    # The worktree path a launch of this dry resolution would create names no
    # primary, whether it is reached through the session token or the fleet
    # script's own path composition.
    worktree = routing._launch_worktree_path(repo, session, "node-a")
    assert primary_id not in str(worktree), str(worktree)
    assert session == f"shadow-candidate-a-{component}"
    assert primary_id not in session


def test_a_legacy_shadow_record_with_a_component_still_resolves(home, repo) -> None:
    primary_id = "r-20261016T145650209779-xpoint-cell-owner"
    run_id = "r-20261016T165229949973-xpoint-cell-owner"
    component = "0123456789ab"

    _commit_legacy_shadow(
        repo,
        home,
        run_id=run_id,
        primary_run_id=primary_id,
        backend="candidate-a",
        component=component,
    )

    records = routing._shadow_worktree_records(repo, "proj")
    root = routing._workspace_roots(repo)[0]
    legacy = (
        root / f"shadow-{primary_id}-candidate-a-{component}" / "node-a"
    ).resolve()

    assert legacy in records
    assert records[legacy]["run_id"] == run_id


def test_a_legacy_shadow_record_without_a_component_still_resolves(home, repo) -> None:
    primary_id = "r-20261016T145650209779-xpoint-cell-owner"
    run_id = "r-20261016T165230000000-xpoint-cell-owner"

    _commit_legacy_shadow(
        repo,
        home,
        run_id=run_id,
        primary_run_id=primary_id,
        backend="candidate-a",
        component="",
    )

    records = routing._shadow_worktree_records(repo, "proj")
    root = routing._workspace_roots(repo)[0]
    legacy = (root / f"shadow-{primary_id}-candidate-a" / "node-a").resolve()

    assert legacy in records
    assert records[legacy]["run_id"] == run_id


def test_a_legacy_shadow_record_without_a_candidate_still_resolves(home, repo) -> None:
    primary_id = "r-20261016T145650209779-xpoint-cell-owner"
    run_id = "r-20261016T165231000000-xpoint-cell-owner"

    _commit_legacy_shadow(
        repo,
        home,
        run_id=run_id,
        primary_run_id=primary_id,
        backend="",
        component="",
    )

    records = routing._shadow_worktree_records(repo, "proj")
    root = routing._workspace_roots(repo)[0]
    legacy = (root / f"shadow-{primary_id}" / "node-a").resolve()

    assert legacy in records
    assert records[legacy]["run_id"] == run_id
