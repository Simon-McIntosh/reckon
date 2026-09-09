"""Shadow worktree paths retain several comparison arms for one primary.

A shadow worktree's session token names its primary, candidate, and a unique
recorded component. The dispatcher and reclamation site both use the same helper,
so different candidates and repeated arms on one candidate keep separate retained
evidence.

Every test here is hermetic: ``RECKON_HOME`` moves the crew directory into a
temp tree, the repository is a real but throwaway git repo, and dispatch
substitutes a launcher so nothing spawns a harness or reaches a network.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew, ledger
from reckon.crew import routing
from reckon.crew.dispatch import shadow as dispatch_shadow
from reckon.crew.runs import pointer_path

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
    """Route implement nodes to the named candidate backend."""
    config = json.loads(json.dumps(CONFIG))
    config["backends"][backend_name] = {
        **config["backends"]["alpha"],
        "model": f"{backend_name}-model",
    }
    config["roles"]["implement"] = {"backend": backend_name}
    return config


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point the crew directory at a temp tree, leaving the real one alone."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path, home):
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


def _shadow(
    primary_run_id: str,
    backend_name: str,
    repo: Path,
    *,
    config: dict | None = None,
    configuration_overrides: set[str] | None = None,
    session: str = "shadow-session",
    wave: str = "",
) -> dict:
    return dispatch_shadow(
        primary_run_id,
        candidate_backend=backend_name,
        config=config or _candidate_config(backend_name),
        repo=repo,
        configuration_overrides=configuration_overrides or set(),
        session=session,
        wave=wave,
        launcher=lambda *args, **kwargs: 0,
    )


def _commit_shadow_record(repo, home, *, run_id, lineage, backend) -> Path:
    artifact = home / "crew" / "runs" / run_id / "shadow.patch"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("retained evidence\n")
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
    return artifact


def test_shadow_pointer_keeps_the_dispatching_session_and_wave(home, repo) -> None:
    primary = _completed_primary(home, repo)

    shadow = _shadow(
        str(primary["run_id"]),
        "candidate-a",
        repo,
        session="dispatching-session",
        wave="wave-a",
    )
    pointer = crew.read_pointer(shadow["run_id"])

    assert pointer["session"] == shadow["session"] == "dispatching-session"
    assert pointer["wave"] == shadow["wave"] == "wave-a"
    assert not {"contamination", "excluded", "exclusion"}.intersection(pointer)


def test_many_candidates_shadow_one_primary_without_colliding(home, repo) -> None:
    primary = _completed_primary(home, repo)
    primary_id = str(primary["run_id"])

    shadows = [
        _shadow(primary_id, backend, repo)
        for backend in ("candidate-a", "candidate-b", "candidate-c")
    ]

    # Three distinct candidates against one primary all create a worktree; none
    # is refused, because each path names its candidate.
    paths = [Path(record["worktree"]).resolve() for record in shadows]
    assert len({str(path) for path in paths}) == 3
    assert all(path.is_dir() for path in paths)

    for record, backend in zip(
        shadows, ("candidate-a", "candidate-b", "candidate-c"), strict=True
    ):
        assert record["backend"] == backend
        assert record["session"] == "shadow-session"
        # Attribution is shared while the internal worktree identity retains
        # the primary, candidate, and unique component used for isolation.
        component = record["lineage"]["worktree_component"]
        worktree_identity = routing.shadow_worktree_session(
            primary_id, backend, component
        )
        assert worktree_identity in str(Path(record["worktree"]))


def test_two_effort_arms_on_one_backend_keep_separate_patches(home, repo) -> None:
    primary = _completed_primary(home, repo)
    primary_id = str(primary["run_id"])

    inherited_config = _candidate_config("candidate-a")
    inherited_config["backends"]["candidate-a"]["effort"] = "xhigh"
    first = _shadow(primary_id, "candidate-a", repo, config=inherited_config)
    overridden_config = _candidate_config("candidate-a")
    overridden_config["backends"]["candidate-a"]["effort"] = "medium"
    second = _shadow(
        primary_id,
        "candidate-a",
        repo,
        config=overridden_config,
        configuration_overrides={"effort"},
    )

    assert first["agent"]["effort"] == primary["agent"]["effort"] == "high"
    assert second["agent"]["effort"] == "medium"
    assert first["session"] == second["session"] == "shadow-session"
    assert first["worktree"] != second["worktree"]
    assert (
        first["lineage"]["worktree_component"]
        != second["lineage"]["worktree_component"]
    )

    for record, content in ((first, "inherited\n"), (second, "overridden\n")):
        changed = Path(record["worktree"]) / "reckon" / "_backends.py"
        changed.parent.mkdir(parents=True)
        changed.write_text(content)

    stored = [
        crew.complete(record["run_id"], gate="passed")["record"]
        for record in (first, second)
    ]
    patches = [Path(record["shadow_patch"]) for record in stored]
    assert patches[0] != patches[1]
    assert all(path.is_file() for path in patches)
    assert "inherited" in patches[0].read_text()
    assert "overridden" in patches[1].read_text()
    assert stored[0]["lineage"]["configuration"]["resolved"]["effort"] == "high"
    assert stored[0]["lineage"]["configuration"]["overrides"] == {}
    assert stored[1]["lineage"]["configuration"]["resolved"]["effort"] == "medium"
    assert stored[1]["lineage"]["configuration"]["overrides"]["effort"] == {
        "layers": {"backend": "medium"},
        "resolved": "medium",
    }


def test_each_shadow_is_reclaimed_against_its_own_committed_record(home, repo) -> None:
    primary = _completed_primary(home, repo)
    primary_id = str(primary["run_id"])

    shadows = [
        _shadow(primary_id, backend, repo)
        for backend in ("candidate-a", "candidate-b", "candidate-c")
    ]
    for record, backend in zip(
        shadows, ("candidate-a", "candidate-b", "candidate-c"), strict=True
    ):
        _commit_shadow_record(
            repo,
            home,
            run_id=str(record["run_id"]),
            lineage=record["lineage"],
            backend=backend,
        )
        # A terminal shadow run's live pointer is retired once the record is
        # committed; without that the worktree reads as still claimed.
        pointer_path(str(record["run_id"])).unlink(missing_ok=True)

    # The reclamation site resolves the location the dispatcher created, both
    # through the one shared helper: every committed shadow record maps to the
    # exact worktree path its dispatch produced.
    reclaimed = routing._shadow_worktree_records(repo, "proj")
    for record in shadows:
        path = Path(record["worktree"]).resolve()
        assert path in reclaimed
        assert reclaimed[path]["run_id"] == record["run_id"]

    # And each stays reclaimable exactly as a single shadow is today: dry-run GC
    # classifies every one of them disposable alongside its retained patch.
    result = CliRunner().invoke(
        cli.main, ["crew", "gc", "--repo", str(repo), "--project", "proj"]
    )
    payload = json.loads(result.output)
    assert result.exit_code == 0, result.output
    by_path = {item["path"]: item for item in payload["worktrees"]}
    for record in shadows:
        path = str(Path(record["worktree"]).resolve())
        assert by_path[path]["classification"] == "disposable"
        assert by_path[path]["shadow_run_id"] == record["run_id"]
    assert payload["counts"]["disposable"] >= len(shadows)
