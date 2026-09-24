"""A placement declares what its workers need visible, and is refused early.

The fault: a backend could be placed into a partition where the worktree root,
the crew state directory, the backend binary, the shared project environment,
the served model endpoint or a lane's credential path was not visible, and the
launch succeeded anyway. The worker then failed on a path that exists on the
dispatcher, which reads as a worker defect rather than as a placement that
pointed at the wrong storage. The per-user runtime directory is the sharpest
case, because it exists on every node and names different bytes on each.

The check is coordinator-side and reads the declaration only, so a dry run
reaches the verdict a real dispatch reaches and no worktree, pointer or job
exists when the refusal is raised.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from reckon import crew, flight
from reckon.crew import runs

PROJECT = "proj"

# A path on the shared filesystem that certainly exists and is not node-local:
# this test module's own directory.
VISIBLE_PATH = str(Path(__file__).resolve().parent)

QUERIES = {
    "state_query": ["squeue", "-h", "-j", "{job}", "-o", "%T"],
    "reason_query": ["squeue", "-h", "-j", "{job}", "-o", "%r"],
}


def _placement(**overrides) -> dict:
    return {
        "scheduler": "srun",
        "options": ["--partition=compute"],
        **QUERIES,
        **overrides,
    }


def _config(placement: dict | None = None) -> dict:
    backend: dict = {
        "launch": "cli",
        "command": "codex",
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
        "time_budget": "25m",
    }
    if placement is not None:
        backend["placement"] = placement
    return {
        "default_backend": "alpha",
        "backends": {"alpha": backend},
        "roles": {"implement": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True, text=True
    )


@pytest.fixture()
def repository(config_home: Path, tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        "<!doctype html>\n<html><head>\n"
        f'<meta name="docs-project" content="{PROJECT}">\n'
        '<meta name="reckon-type" content="plan">\n'
        '<meta name="plan-slug" content="fixture">\n'
        '</head><body><h2 id="scope">Scope</h2></body></html>\n',
        encoding="utf-8",
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(root, "add", "seed.txt", "docs/plans/fixture.html")
    _git(root, "commit", "-q", "-m", "chore: seed")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _node(config_home: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id="node-placement",
        goal="land one placed requirement",
        plan="fixture",
        section="scope",
        spec_level="exact",
        done_when="pytest reports one passing placement requirement case",
        write_paths=["seed.txt"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / "node-placement.md"),
    )


def _plan(config: dict, *, config_home: Path, repository: Path, node=None):
    return crew.plan_dispatch(
        node=node or _node(config_home),
        project=PROJECT,
        repo=repository,
        config=config,
    )


@pytest.fixture()
def answering_endpoint() -> Iterator[str]:
    """A host:port something is listening on, so a reachable probe can pass."""
    with closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        yield f"127.0.0.1:{listener.getsockname()[1]}"


# ── the refusal, reached the way dispatch reaches it ────────────────────────


def test_an_unreachable_requirement_path_is_refused_before_launch(
    config_home: Path, repository: Path, tmp_path: Path
) -> None:
    """A synthesised path that no node can see refuses the placement.

    The path is built under a directory that does not exist, so nothing in the
    test environment can accidentally satisfy it.
    """
    missing = str(tmp_path / "absent-storage" / "worktrees")
    config = _config(
        _placement(requirements=[{"name": "worktree-root", "path": missing}])
    )

    with pytest.raises(crew.CrewError) as refusal:
        _plan(config, config_home=config_home, repository=repository)

    message = str(refusal.value)
    assert "worktree-root" in message
    assert missing in message
    assert "compute" in message


def test_an_unreachable_endpoint_is_refused_naming_the_endpoint(
    config_home: Path, repository: Path
) -> None:
    """A declared endpoint nothing answers on refuses, and the refusal names it."""
    endpoint = "127.0.0.1:1"
    config = _config(
        _placement(requirements=[{"name": "model-endpoint", "endpoint": endpoint}])
    )

    with pytest.raises(crew.CrewError) as refusal:
        _plan(config, config_home=config_home, repository=repository)

    assert endpoint in str(refusal.value)
    assert "model-endpoint" in str(refusal.value)


def test_a_node_local_path_is_refused_by_name_with_its_own_sentence(
    config_home: Path, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-node runtime directory gets its own refusal, not a missing one.

    Two spellings, and the refusal must read the same for both. The first is a
    directory created under this process's own runtime root, so it certainly
    exists here — the silent case, where a check that only asked whether the
    path exists would admit it and the failure would surface as a worker defect
    on another node. The second is spelled under ``/run/user``, the same shape
    reached on a host where that directory names a different user or nothing at
    all, so the classification cannot be an existence check on either side.
    """
    runtime = tmp_path / "runtime"
    (runtime / "crew-state").mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    existing = str(runtime / "crew-state")
    assert Path(existing).is_dir()
    absent = str(Path("/run/user") / str(os.getuid()) / "crew-state")

    for node_local in (existing, absent):
        config = _config(
            _placement(requirements=[{"name": "crew-state", "path": node_local}])
        )

        with pytest.raises(crew.CrewError) as refusal:
            _plan(config, config_home=config_home, repository=repository)

        message = str(refusal.value)
        assert node_local in message
        assert "per-node storage" in message
        # Its own sentence, distinct from the not-visible refusal.
        assert "is not visible from there" not in message


def test_a_placement_naming_a_wrapper_with_no_query_is_refused(
    config_home: Path, repository: Path
) -> None:
    """A wrapper declared without a way to ask it is refused, not left silent.

    Without this the liveness read falls through to a pid on another host, and
    a job that ended is indistinguishable from a worker that is running.
    """
    config = _config(
        {
            "scheduler": "srun",
            "options": ["--partition=compute"],
            "requirements": [{"name": "worktree-root", "path": VISIBLE_PATH}],
        }
    )

    with pytest.raises(crew.CrewError) as refusal:
        _plan(config, config_home=config_home, repository=repository)

    message = str(refusal.value)
    assert "state_query" in message
    assert "reason_query" in message


def test_a_half_declared_query_set_is_refused(
    config_home: Path, repository: Path
) -> None:
    """One query without the other is still a placement reckon cannot follow."""
    config = _config(
        _placement(
            reason_query=None,
            requirements=[{"name": "worktree-root", "path": VISIBLE_PATH}],
        )
    )

    with pytest.raises(crew.CrewError) as refusal:
        _plan(config, config_home=config_home, repository=repository)

    assert "state_query" in str(refusal.value)


# ── the controls: a satisfied placement, and one declaring nothing ──────────


def test_a_satisfied_placement_is_admitted(
    config_home: Path, repository: Path, answering_endpoint: str
) -> None:
    """Every requirement visible: the launched node is admitted unchanged."""
    config = _config(
        _placement(
            requirements=[
                {"name": "worktree-root", "path": VISIBLE_PATH},
                {"name": "model-endpoint", "endpoint": answering_endpoint},
            ]
        )
    )

    resolution = _plan(config, config_home=config_home, repository=repository)

    assert resolution.validation.ok, resolution.validation.findings


def test_a_backend_declaring_no_placement_is_untouched(
    config_home: Path, repository: Path
) -> None:
    """Absence is not an empty placement: nothing is required and nothing runs."""
    resolution = _plan(_config(), config_home=config_home, repository=repository)

    assert resolution.validation.ok, resolution.validation.findings


def test_a_requirement_declaring_no_target_is_refused(
    config_home: Path, repository: Path
) -> None:
    """A malformed requirement is refused rather than quietly passing."""
    config = _config(_placement(requirements=[{"name": "nowhere"}]))

    with pytest.raises(crew.CrewError) as refusal:
        _plan(config, config_home=config_home, repository=repository)

    assert "nowhere" in str(refusal.value)


# ── the declaration is the authority, not a table in code ──────────────────


def test_the_declared_queries_are_the_ones_runs_asks() -> None:
    """A wrapper's reporting verb comes from the placement that named it."""
    placement = _placement()
    assert runs._scheduler_state_argv(placement, "77") == [
        "squeue",
        "-h",
        "-j",
        "77",
        "-o",
        "%T",
    ]
    assert (
        runs.scheduler_job_state(placement, "77", lambda argv: "RUNNING") == "RUNNING"
    )


def test_an_undeclared_query_answers_none_rather_than_guessing() -> None:
    """A wrapper with no declared query is unqueryable, not silently mapped."""
    assert runs._scheduler_state_argv({"scheduler": "srun"}, "77") is None


def test_a_placement_survives_the_flight_layer_with_its_requirements() -> None:
    """The schema carries the set and the read path returns it unchanged."""
    data = {
        "version": 1,
        "default_backend": "alpha",
        "backends": {
            "alpha": {
                "launch": "cli",
                "command": "codex",
                "placement": _placement(
                    requirements=[
                        {"name": "worktree-root", "path": VISIBLE_PATH},
                        {"name": "model-endpoint", "endpoint": "host:1234"},
                    ]
                ),
            }
        },
    }

    flight.validate_layer(data, "test")

    placement = flight.placement_for(data["backends"]["alpha"])
    entries = flight.placement_requirement_entries(placement)
    assert [entry["name"] for entry in entries] == ["worktree-root", "model-endpoint"]
    assert flight.placement_scheduler_queries(placement) == QUERIES


def test_a_requirement_declaring_both_targets_is_refused_by_the_schema(
    tmp_path: Path,
) -> None:
    """A requirement naming both a path and an endpoint is a config error.

    The rule needs the merged config to judge, so it is exercised through a
    written host layer rather than a bare layer check.
    """
    host = tmp_path / "flight.yaml"
    host.write_text(
        "version: 1\n"
        "default_backend: alpha\n"
        "backends:\n"
        "  alpha:\n"
        "    launch: cli\n"
        "    command: codex\n"
        "    placement:\n"
        "      scheduler: srun\n"
        "      state_query: [squeue, -h, -j, '{job}', -o, '%T']\n"
        "      reason_query: [squeue, -h, -j, '{job}', -o, '%r']\n"
        "      requirements:\n"
        "        - name: confused\n"
        f"          path: {VISIBLE_PATH}\n"
        "          endpoint: host:1234\n",
        encoding="utf-8",
    )

    with pytest.raises(flight.FlightConfigError) as refusal:
        flight.resolve(host_path=host)

    assert "exactly one of path and endpoint" in str(refusal.value)
