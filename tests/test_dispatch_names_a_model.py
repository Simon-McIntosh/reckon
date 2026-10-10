"""A dispatch can name a model inside a lane, resolved through the catalogue.

``--model`` reaches the lane and model key through the catalogue's one name
resolver, so a model key, a model id and an alias all name the same backend the
legacy ``<lane>-<key>`` spelling names. A bare model two lanes declare is
refused by name, and the resolved pair rides the promoted row beside
``backend``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from reckon import cli, crew_dispatch_commands, ledger
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
        }
    },
    "aliases": {"claude-opus": {"lane": "claude", "model_key": "opus"}},
}

# The expanded backends the resolver filters: each carries the lane and model
# key the catalogue pair resolves to, exactly as ``expand_lanes`` writes them.
CONFIG: dict = {
    "default_backend": "claude",
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


def dispatch(repo: Path, *, backend: str | None = None, model: str | None = None):
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
        "--dry-run",
    ]
    if backend is not None:
        args += ["--backend", backend]
    if model is not None:
        args += ["--model", model]
    return CliRunner().invoke(cli.main, args)


def payload(result) -> dict:
    assert result.exit_code == 0, result.output
    return json.loads(result.output.splitlines()[0])


def test_model_narrowing_matches_the_legacy_backend(repo, catalogue, config) -> None:
    """Naming the pair reaches the same backend, effort and launch as the
    legacy ``<lane>-<key>`` name, so a caller can spell a model either way."""
    narrowed = payload(dispatch(repo, backend="claude", model="opus"))
    legacy = payload(dispatch(repo, backend="claude-opus"))

    assert narrowed["backend"] == legacy["backend"] == "claude-opus"
    assert narrowed["agent"]["effort"] == legacy["agent"]["effort"] == "high"
    assert narrowed["agent"]["model"] == legacy["agent"]["model"]
    assert narrowed["launch"] == legacy["launch"]


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
