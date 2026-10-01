"""The fence's protected set is a declared flight key, not a compiled list.

The set a worker's fence seals read-only is composed from three sources: the
shipped default built by :func:`reckon._backends._default_protected_paths`, the
extra paths a host or project layer names under the ``protected_paths`` flight
key, and the defaults a layer names under ``unprotected_paths``. The default is
always present, so a layer augments it and can never replace it; the only way
to drop a default is to name it under ``unprotected_paths``, and a run whose
fence leaves one out carries the list on its record.

Every test here runs against a temporary operator home and a temporary flight
layer, so none reads or writes the real one.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _backends, crew, flight
from reckon import cli as cli_module

CONFIG = {
    "version": 1,
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": False,
            "time_budget": "25m",
        },
        "native": {"launch": "in-harness", "time_budget": "25m"},
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def operator_home(tmp_path, monkeypatch):
    """A temporary operator home holding the default protected directories.

    ``HOME`` is patched so both the composed fence and the built-in default
    resolve against this tree, and the paths a layer adds or removes name it
    rather than the machine's real home.
    """
    home = tmp_path / "operator-home"
    for name in (
        ".claude",
        ".codex",
        ".ssh",
        ".agents",
        ".config/reckon",
        ".config/git",
        "public",
        ".local/bin",
    ):
        (home / name).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture()
def crew_home(tmp_path, monkeypatch):
    """Point the crew config and state home at a temp tree."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path, crew_home):
    """A throwaway git repository carrying the worktree fleet script."""
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
    (crew_home / "mounts.json").write_text(json.dumps({"proj": str(root / "docs")}))
    return root


def _write_layer(tmp_path: Path, monkeypatch, layer: dict) -> Path:
    """Write a temporary host flight layer and point resolution at it."""
    import yaml

    path = tmp_path / "flight.yaml"
    path.write_text(yaml.safe_dump(layer, sort_keys=False))
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(path))
    return path


def _resolved(layer: dict, tmp_path: Path, monkeypatch) -> dict:
    """Resolve a temporary host flight layer into one config mapping."""
    _write_layer(tmp_path, monkeypatch, layer)
    return dict(flight.resolve().config)


def _node(crew_home: Path, **overrides) -> crew.TaskNode:
    fields = {
        "id": "node-a",
        "goal": "record the launch matrix for one backend",
        "plan": "plan-a",
        "section": "§3",
        "done_when": "uv run pytest tests/test_the_protected_set_is_declared.py",
        "write_paths": ["reckon/_backends.py"],
        "time_budget": "20m",
        "manifest_path": str(crew_home / "node-manifests" / "node-a-manifest.md"),
        "spec_level": "guided",
    }
    fields.update(overrides)
    (crew_home / "node-manifests").mkdir(parents=True, exist_ok=True)
    return crew.TaskNode(**fields)


def test_default_set_holds_when_no_layer_names_the_key(operator_home) -> None:
    """With no layer naming the key, the resolved set is the built-in default."""
    default = _backends._default_protected_paths(str(operator_home))
    resolved = _backends.protected_paths(str(operator_home), {})

    assert default, (
        "the temp home must expose the default set for this to mean anything"
    )
    assert resolved == default


def test_a_layer_adding_a_path_makes_the_fence_seal_it(
    operator_home, tmp_path, monkeypatch
) -> None:
    """A path a layer adds under protected_paths is mounted read-only."""
    added = operator_home / ".added-store"
    added.mkdir()
    config = _resolved({"protected_paths": [".added-store"]}, tmp_path, monkeypatch)

    assert added in _backends.protected_paths(str(operator_home), config)

    argv = _backends.fence_argv(["true"], home=str(operator_home), config=config)
    assert ["--ro-bind", str(added), str(added)] == _pair_windows(argv, str(added))


def test_a_layer_removing_a_default_leaves_it_writable_and_records_it(
    operator_home, crew_home, repo, tmp_path, monkeypatch
) -> None:
    """A default named under unprotected_paths is writable and recorded."""
    layer = dict(CONFIG)
    layer["unprotected_paths"] = [".codex"]
    config = _resolved(layer, tmp_path, monkeypatch)

    assert operator_home / ".codex" not in _backends.protected_paths(
        str(operator_home), config
    )

    record = crew.dispatch(
        node=_node(crew_home),
        project="proj",
        repo=repo,
        config=config,
        session="cli-session",
        launcher=lambda *args, **kwargs: 0,
    )
    pointer = crew.read_pointer(record["run_id"])
    recorded = pointer["fence_unprotected_paths"]
    assert str(operator_home / ".codex") in recorded

    sealed = _pair_windows(pointer["argv"], str(operator_home / ".codex"))
    assert sealed is None
    kept = _pair_windows(pointer["argv"], str(operator_home / ".claude"))
    assert kept == [
        "--ro-bind",
        str(operator_home / ".claude"),
        str(operator_home / ".claude"),
    ]


def test_dropping_a_default_from_the_key_still_protects_it(
    operator_home, tmp_path, monkeypatch
) -> None:
    """Naming only some defaults under protected_paths removes none of them."""
    config = _resolved(
        {"protected_paths": [".added-store", ".agents"]}, tmp_path, monkeypatch
    )

    resolved = _backends.protected_paths(str(operator_home), config)
    assert operator_home / ".codex" in resolved
    assert operator_home / ".claude" in resolved


def test_flight_reports_the_key_and_the_layer_that_supplied_it(
    operator_home, tmp_path, monkeypatch
) -> None:
    """``reckon flight`` reports the declared value and its origin layer."""
    _write_layer(tmp_path, monkeypatch, {"protected_paths": [".added-store"]})

    result = CliRunner().invoke(cli_module.main, ["flight"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["config"]["protected_paths"] == [".added-store"]
    assert payload["provenance"]["protected_paths"] == "host"


def _pair_windows(argv, target: str):
    """Return the ``--ro-bind`` argument triple that mounts ``target``, else None."""
    for index, word in enumerate(argv):
        if word == "--ro-bind" and argv[index + 1 : index + 3] == [target, target]:
            return argv[index : index + 3]
    return None
