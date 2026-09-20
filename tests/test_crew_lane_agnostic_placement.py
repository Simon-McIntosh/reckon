"""Placement belongs to a backend, not to the lane this host happens to serve.

The fault this module pins: a placement that quietly worked only for the locally
served backend would trade the login slice's ceiling for a narrower fleet, and a
placement whose provider the target partition cannot reach would surface as a
worker that failed rather than as a placement that was refused. Both are the
same defect seen from two sides — a lane identity leaking into placement code,
or a placement requirement that is discovered after the launch instead of before
it.

Three properties are asserted here, each against the surface a caller reaches:

* the wrap and the pre-launch check read the resolved backend's own declaration,
  so a backend name no code has ever seen is placed and refused like any other;
* an unreachable declared endpoint is refused before launch, naming the backend
  and the endpoint, in the sentence family the workspace-requirements check
  already uses rather than a second refusal;
* a dispatch that is placed is not rerouted onto another lane because of where
  it would run — an unmet requirement refuses, it does not quietly fall back.

The pre-launch site is ``plan_dispatch``, the single pre-side-effect resolver a
dry run and a real launch both reach, so a refusal raised here means no
worktree, no run directory and no live pointer exists.
"""

from __future__ import annotations

import importlib
import json
import socket
import subprocess
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from reckon import crew
from reckon._backends import LaunchPlan

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "proj"
RESOLVED_EXECUTABLE = "/opt/backends/bin/codex"

# A path on shared storage that certainly exists: this test module's directory.
VISIBLE_PATH = str(Path(__file__).resolve().parent)

QUERIES = {
    "state_query": ["squeue", "-h", "-j", "{job}", "-o", "%T"],
    "reason_query": ["squeue", "-h", "-j", "{job}", "-o", "%r"],
}

# Backend names no code knows, plus the name a config uses for its default lane.
# The wrap and the refusal must not vary across them, which is the whole claim:
# adding a backend is a configuration change, not a code change.

LOCAL_LANE = "lantern"
PLACED_LANES = ["faraway", "beyond", "third-party"]


def _placement(**overrides) -> dict:
    return {
        "scheduler": "srun",
        "options": ["--partition=metered"],
        **QUERIES,
        **overrides,
    }


def _backend(placement: dict | None = None, **overrides) -> dict:
    backend: dict = {
        "launch": "cli",
        "command": "codex",
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
        "time_budget": "25m",
        **overrides,
    }
    if placement is not None:
        backend["placement"] = placement
    return backend


def _config(backends: dict) -> dict:
    """A resolved config whose default and local lane is the unplaced one.

    The local lane declares no placement and the placed backends are separate
    keys, so a code path that placed only what this host serves would leave every
    case below unsatisfied rather than accidentally satisfied.
    """
    return {
        "default_backend": LOCAL_LANE,
        "local_backend": LOCAL_LANE,
        "backends": backends,
        "roles": {"implement": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }


def _launch_plan() -> LaunchPlan:
    return LaunchPlan(
        backend="faraway",
        dialect="codex",
        argv=[RESOLVED_EXECUTABLE, "exec", "--task", "t"],
        cwd="/work/tree",
        stdin_text="",
        environment={"PATH": "/usr/bin:/bin"},
        final_message_path=None,
        resumed_session=None,
    )


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
        id="node-lane-agnostic",
        goal="land one placed requirement from a lane that is not the local one",
        plan="fixture",
        section="scope",
        spec_level="exact",
        done_when="pytest reports one passing lane-agnostic placement case",
        write_paths=["seed.txt"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / "node-lane-agnostic.md"),
    )


def _plan(config: dict, *, config_home: Path, repository: Path, **overrides):
    return crew.plan_dispatch(
        node=_node(config_home),
        project=PROJECT,
        repo=repository,
        config=config,
        **overrides,
    )


def _live_pointers(config_home: Path) -> list[Path]:
    live = config_home / "crew" / "live"
    return sorted(live.glob("*.json")) if live.is_dir() else []


@pytest.fixture()
def answering_endpoint() -> Iterator[str]:
    """A host:port something is listening on, so a reachable probe can pass."""
    with closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        yield f"127.0.0.1:{listener.getsockname()[1]}"


# ── placement is a property of the backend, whatever it is called ───────────


def test_adding_a_backend_needs_no_placement_code(
    config_home: Path, repository: Path
) -> None:
    """The wrap and the refusal follow the resolved backend's own declaration.

    Ranging over backend names — including the lane this host serves and names
    no code has seen — a declared placement prefixes the launch with that
    backend's own partition, and an unmet requirement refuses whoever it
    resolved to. Neither behaviour is keyed on which lane the backend is, which
    is what makes adding an externally served backend a configuration change.
    """
    for name in [LOCAL_LANE, *PLACED_LANES]:
        placement = _placement(options=[f"--partition={name}-pool"])
        wrapped = dispatch_module.apply_backend_placement(
            _launch_plan(), _backend(placement)
        )
        # The argv is carried through behind this backend's own declared prefix.
        assert wrapped.argv[:2] == ["/usr/bin/srun", f"--partition={name}-pool"]
        assert wrapped.argv[2:] == _launch_plan().argv

    missing = str(Path(config_home) / "absent-storage" / "worktrees")
    for name in [LOCAL_LANE, *PLACED_LANES]:
        placement = _placement(
            options=[f"--partition={name}-pool"],
            requirements=[{"name": "worktree-root", "path": missing}],
        )
        config = _config(
            {
                LOCAL_LANE: _backend(),
                name: _backend(placement),
            }
        )
        with pytest.raises(crew.CrewError) as refusal:
            _plan(
                config,
                config_home=config_home,
                repository=repository,
                backend_override=name,
            )
        message = str(refusal.value)
        assert name in message
        assert missing in message


# ── an unreachable provider is refused before launch, in the existing sentence ─


def test_an_unreachable_provider_endpoint_is_refused_before_launch(
    config_home: Path, repository: Path
) -> None:
    """A provider the target cannot reach refuses at the pre-launch site.

    The refusal names both the backend that declared it and the endpoint it
    cannot reach, and it is composed by the same formatter as the invisible-path
    refusal rather than as a second, differently-worded check. Nothing is
    launched: the raise comes from ``plan_dispatch``, and no live pointer is
    written.
    """
    endpoint = "127.0.0.1:1"
    partition = "faraway-pool"
    placed = _placement(
        options=[f"--partition={partition}"],
        requirements=[{"name": "provider", "endpoint": endpoint}],
    )
    config = _config({LOCAL_LANE: _backend(), "faraway": _backend(placed)})

    with pytest.raises(crew.CrewError) as refusal:
        _plan(
            config,
            config_home=config_home,
            repository=repository,
            backend_override="faraway",
        )

    message = str(refusal.value)
    assert "faraway" in message
    assert endpoint in message
    assert "provider" in message
    assert _live_pointers(config_home) == []

    # The same sentence the invisible-path refusal uses, with the same target
    # quoted: one refusal family, not a placement-specific second one. The
    # control path is a top-level directory that does not exist and is not on
    # per-node storage, so it exercises the not-visible sentence rather than the
    # node-local one.
    absent = "/awcj-absent-provider/model"
    assert not Path(absent).exists()
    path_config = _config(
        {
            LOCAL_LANE: _backend(),
            "faraway": _backend(
                _placement(
                    options=[f"--partition={partition}"],
                    requirements=[{"name": "provider", "path": absent}],
                )
            ),
        }
    )
    with pytest.raises(crew.CrewError) as path_refusal:
        _plan(
            path_config,
            config_home=config_home,
            repository=repository,
            backend_override="faraway",
        )
    path_message = str(path_refusal.value)

    for phrase in (
        "places its workers on",
        "but requirement",
        "is not visible from there",
    ):
        assert phrase in message
        assert phrase in path_message
    assert partition in message
    assert partition in path_message


def test_a_reachable_provider_leaves_the_placement_admitted(
    config_home: Path, repository: Path, answering_endpoint: str
) -> None:
    """The positive control: an answered endpoint admits the same placement."""
    placed = _placement(
        options=["--partition=faraway-pool"],
        requirements=[{"name": "provider", "endpoint": answering_endpoint}],
    )
    config = _config({LOCAL_LANE: _backend(), "faraway": _backend(placed)})

    resolution = _plan(config, config_home=config_home, repository=repository)

    assert resolution.validation.ok, resolution.validation.findings


# ── a placed dispatch is not rerouted by where it would run ─────────────────


def test_a_placed_dispatch_is_never_rerouted_to_another_lane(
    config_home: Path, repository: Path, answering_endpoint: str
) -> None:
    """Where a placement sends a worker never decides which lane it runs on.

    A satisfiable placement stays on the backend the caller named, carrying that
    backend's own settings, and an unsatisfiable one is refused rather than
    quietly moved onto the default lane. A reroute would report a run that
    landed somewhere the caller never asked for, which is the silent failure
    this asserts against.
    """
    satisfiable = _placement(
        options=["--partition=faraway-pool"],
        requirements=[{"name": "provider", "endpoint": answering_endpoint}],
    )
    config = _config({LOCAL_LANE: _backend(), "faraway": _backend(satisfiable)})

    resolution = _plan(
        config,
        config_home=config_home,
        repository=repository,
        backend_override="faraway",
    )

    assert resolution.backend == "faraway"
    assert resolution.backend != LOCAL_LANE
    assert resolution.local is False
    # The placement carried is the resolved backend's own declaration.
    assert resolution.backend_settings["placement"]["options"] == [
        "--partition=faraway-pool"
    ]

    # Unsatisfiable: refused, not rerouted onto the default lane.
    unsatisfiable = _placement(
        options=["--partition=faraway-pool"],
        requirements=[{"name": "provider", "endpoint": "127.0.0.1:1"}],
    )
    unmet = _config({LOCAL_LANE: _backend(), "faraway": _backend(unsatisfiable)})
    with pytest.raises(crew.CrewError):
        _plan(
            unmet,
            config_home=config_home,
            repository=repository,
            backend_override="faraway",
        )
