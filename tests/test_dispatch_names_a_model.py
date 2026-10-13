"""A dispatch can name a model inside a lane, resolved through the catalogue.

``--model`` reaches the lane and model key through the catalogue's one name
resolver, so a model key, a model id and an alias all name the same backend the
legacy ``<lane>-<key>`` spelling names. A bare model two lanes declare is
refused by name, and a model named outside the lane it was asked for is refused
naming the model and both lanes. The resolved pair rides the promoted row
beside ``backend`` and the live pointer.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from reckon import _backends, cli, crew, crew_dispatch_commands, ledger
from tests.test_picker_in_dispatch import (
    repo as repo,  # noqa: PLC0414 - re-export fixture
)

CATALOGUE: dict = {
    "version": 1,
    "lanes": {
        "claude": {
            "default_model": "sonnet",
            "models": {
                "sonnet": {"model": "claude-sonnet-5-5", "effort": "medium"},
                "opus": {
                    "model": "claude-opus-5-5",
                    "effort": "high",
                    "alias": "opus 5.5",
                },
            },
        },
        "codex": {
            "default_model": "sol",
            "models": {
                "sol": {"model": "gpt-6-sol", "effort": "high"},
                "astra": {"model": "gpt-6-astra", "effort": "high"},
            },
        },
        "clive": {
            "default_model": "flash",
            "models": {"flash": {"model": "deepseek-v4.1-flash", "effort": "high"}},
        },
    },
    "aliases": {"claude-opus": {"lane": "claude", "model_key": "opus"}},
}

# The expanded backends the resolver filters: each carries the lane and model
# key the catalogue pair resolves to, exactly as ``expand_lanes`` writes them.
CONFIG: dict = {
    "default_backend": "claude",
    "local_backend": "clive",
    "backends": {
        "claude": {
            "launch": "in-harness",
            "model": "claude-sonnet-5-5",
            "effort": "medium",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "lane": "claude",
            "model_key": "sonnet",
        },
        "claude-opus": {
            "launch": "in-harness",
            "model": "claude-opus-5-5",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "lane": "claude",
            "model_key": "opus",
        },
        "codex": {
            "launch": "in-harness",
            "model": "gpt-6-sol",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "lane": "codex",
            "model_key": "sol",
        },
        "clive": {
            "launch": "in-harness",
            "model": "deepseek-v4.1-flash",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "lane": "clive",
            "model_key": "flash",
        },
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture
def catalogue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "catalogue.yaml"
    path.write_text(yaml.safe_dump(CATALOGUE))
    monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(path))
    return path


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: CONFIG
    )


def dispatch(
    repo: Path,
    *,
    backend: str | None = None,
    model: str | None = None,
    local: bool = False,
    dry_run: bool = True,
):
    args = [
        "crew",
        "dispatch",
        "--project",
        "proj",
        "--plan",
        "example",
        "--section",
        "dispatch",
        "--role",
        "implement",
        "--spec-level",
        "exact",
        "--node",
        "model-test",
        "--goal",
        "record a model",
        "--done-when",
        "pytest checks the model",
        "--write-path",
        "result.json",
        "--session",
        "session",
        "--repo",
        str(repo),
        "--no-watch",
    ]
    if backend is not None:
        args += ["--backend", backend]
    if model is not None:
        args += ["--model", model]
    if local:
        args += ["--local"]
    if dry_run:
        args += ["--dry-run"]
    return CliRunner().invoke(cli.main, args)


def payload(result) -> dict:
    assert result.exit_code == 0, result.output
    return json.loads(result.output.splitlines()[0])


def _launch_argv(backend_name: str, repo: Path) -> list[str]:
    """Compose the cli command line a resolved backend would spawn.

    Used so the equivalence a test asserts is the full launch argv, not the
    launch mode: a resolution that reached a different model composes a
    different command line, while the launch mode stays ``cli`` either way.
    """
    backend = deepcopy(CONFIG["backends"][backend_name])
    backend["launch"] = "cli"
    backend["command"] = "claude"
    return _backends.launch_plan(
        backend_name=backend_name,
        backend=backend,
        prompt="p",
        worktree=str(repo),
        fence=False,
    ).argv


def test_model_narrowing_matches_the_legacy_backend(repo, catalogue, config) -> None:
    """Naming the pair reaches the same backend, effort and launch as the
    ``<lane>-<key>`` backend entry, so a caller can spell a model either way
    through the two dimensions.

    The combined spelling is refused at dispatch now (lane and model are two
    dimensions), so the legacy side is the entry's own launch argv, built from
    the fixture's ``claude-opus`` backend rather than by dispatching the
    retired spelling.
    """
    narrowed = payload(dispatch(repo, backend="claude", model="opus"))

    assert narrowed["backend"] == "claude-opus"
    assert narrowed["agent"]["effort"] == "high"
    assert narrowed["agent"]["model"] == "claude-opus-5-5"
    # The equivalence is the full launch argv, not the launch mode: the pair must
    # compose the same command line the ``claude-opus`` entry composes, carrying
    # the model id that entry names. A regression that resolved a different model
    # would change the argv while leaving the launch mode ``cli`` unchanged.
    narrowed_argv = _launch_argv(narrowed["backend"], repo)
    legacy_argv = _launch_argv("claude-opus", repo)
    assert narrowed_argv == legacy_argv
    assert "claude-opus-5-5" in narrowed_argv
    assert narrowed_argv != _launch_argv("claude", repo)


@pytest.mark.parametrize("model", ["opus", "claude-opus-5-5", "opus 5.5"])
def test_model_key_id_and_alias_reach_the_same_backend(
    repo, catalogue, config, model
) -> None:
    """A key, a model id and an alias each resolve through the same resolver."""
    resolved = payload(dispatch(repo, backend="claude", model=model))
    assert resolved["backend"] == "claude-opus"
    assert resolved["agent"]["effort"] == "high"


def test_bare_model_implies_its_one_lane(repo, catalogue, config) -> None:
    """A model exactly one lane declares carries its lane with it."""
    resolved = payload(dispatch(repo, model="opus"))
    assert resolved["backend"] == "claude-opus"


def test_ambiguous_bare_model_is_refused_naming_both_lanes(
    repo, catalogue, config
) -> None:
    """Two lanes declaring one model refuse, and the refusal names both."""
    data = yaml.safe_load(catalogue.read_text())
    data["lanes"]["secondary"] = {
        "default_model": "opus",
        "models": {"opus": data["lanes"]["claude"]["models"]["opus"]},
    }
    catalogue.write_text(yaml.safe_dump(data))

    result = dispatch(repo, model="opus")

    assert result.exit_code != 0
    body = json.loads(result.output.splitlines()[0])
    assert body["ok"] is False
    assert "ambiguous" in body["detail"]
    assert "claude" in body["detail"] and "secondary" in body["detail"]


def test_unknown_model_is_refused(repo, catalogue, config) -> None:
    """A name the catalogue does not declare is refused, never guessed."""
    result = dispatch(repo, backend="claude", model="not-a-model")
    assert result.exit_code != 0
    body = json.loads(result.output.splitlines()[0])
    assert body["ok"] is False
    assert "not-a-model" in body["detail"]


def test_model_outside_the_named_lane_is_refused_naming_both(
    repo, catalogue, config
) -> None:
    """A model declared by another lane is refused, and the refusal names the
    model, the lane it was asked for and the lane that declares it."""
    result = dispatch(repo, backend="codex", model="opus")

    assert result.exit_code != 0
    body = json.loads(result.output.splitlines()[0])
    assert body["ok"] is False
    detail = body["detail"]
    # ``opus`` is declared by the claude lane, not by the codex lane asked for:
    # the refusal must name the model, the requested lane and the declaring lane.
    assert "opus" in detail
    assert "codex" in detail
    assert "claude" in detail


def test_local_model_outside_the_local_lane_is_refused_naming_both(
    repo, catalogue, config
) -> None:
    """A model in the local lane resolves under ``--local``; one outside it is
    refused, naming the local lane and the lane that declares the model."""
    in_lane = payload(dispatch(repo, model="flash", local=True))
    assert in_lane["backend"] == "clive"
    assert in_lane["local"] is True

    result = dispatch(repo, model="opus", local=True)
    assert result.exit_code != 0
    body = json.loads(result.output.splitlines()[0])
    assert body["ok"] is False
    assert "opus" in body["detail"]
    assert "clive" in body["detail"]
    assert "claude" in body["detail"]


def test_live_pointer_records_lane_and_model_key(repo, catalogue, config) -> None:
    """A dispatched run's live pointer carries the lane and model key the pair
    resolved to, beside the backend name."""
    result = dispatch(repo, backend="claude", model="opus", dry_run=False)
    body = payload(result)
    pointer = crew.read_pointer(body["run_id"])
    assert pointer["backend"] == "claude-opus"
    assert (pointer["lane"], pointer["model_key"]) == ("claude", "opus")


def test_promoted_row_carries_lane_and_model_key(catalogue) -> None:
    """A row records the pair beside ``backend`` on the row and its bytes."""
    row = ledger.build_record(
        run_id="r-model",
        plan="plan-a",
        gate="passed",
        backend="claude-opus",
        agent={"backend": "claude-opus"},
    )
    assert (row["lane"], row["model_key"]) == ("claude", "opus")
    assert '"lane": "claude"' in ledger.serialize_run(row)


def test_sibling_surfaces_name_a_model() -> None:
    """The surfaces that already take ``--backend`` take ``--model`` too."""
    for args in (["crew", "preflight", "--help"], ["crew", "shadow", "--help"]):
        result = CliRunner().invoke(cli.main, args)
        assert result.exit_code == 0, result.output
        assert "--model" in result.output
