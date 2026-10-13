"""A lane-and-model backend name is refused at dispatch, naming both dimensions.

Lane and model are two dimensions: a caller names a lane, or names a model
(which carries its lane with it), never a ``<lane>-<model>`` name such as
``codex-astra`` or ``claude-opus``. ``crew dispatch`` refuses a ``--backend``
value that resolves to a lane and model pair whose name is not the lane's own
name, naming the lane it resolves to and the ``--model`` key that replaces it,
so a caller reaching for the retired spelling is told the two-dimension form.

The read side is unchanged: ``--model`` and ``--backend <lane> --model <key>``
resolve to the same backend the combined name named, a model a lane declares
outside the lane it was asked for is refused by naming every lane that declares
it, and a resume of a record whose backend is the combined name still resolves.
"""

from __future__ import annotations

import json
import os
import socket
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from reckon import _backends, cli, crew_dispatch_commands
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
                "astra": {"model": "gpt-6-astra", "effort": "xhigh"},
            },
        },
        "clive": {
            "default_model": "flash",
            "models": {"flash": {"model": "deepseek-v4.1-flash", "effort": "high"}},
        },
    },
    "aliases": {
        "claude-opus": {"lane": "claude", "model_key": "opus"},
        "codex-astra": {"lane": "codex", "model_key": "astra"},
    },
}

# The expanded backends the resolver reads: each carries the lane and model key
# its catalogue pair resolves to, exactly as lane expansion writes them. The two
# combined names are present beside the lanes' own entries, because a host layer
# that still declares them supplies them until those declarations retire.
CONFIG: dict = {
    "default_backend": "claude",
    "local_backend": "clive",
    "backends": {
        "claude": {
            "launch": "cli",
            "command": "claude",
            "model": "claude-sonnet-5-5",
            "effort": "medium",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "lane": "claude",
            "model_key": "sonnet",
        },
        "claude-opus": {
            "launch": "cli",
            "command": "claude",
            "model": "claude-opus-5-5",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "lane": "claude",
            "model_key": "opus",
        },
        "codex": {
            "launch": "cli",
            "command": "claude",
            "model": "gpt-6-sol",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "lane": "codex",
            "model_key": "sol",
        },
        "codex-astra": {
            "launch": "cli",
            "command": "claude",
            "model": "gpt-6-astra",
            "effort": "xhigh",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "lane": "codex",
            "model_key": "astra",
        },
        "clive": {
            "launch": "cli",
            "command": "claude",
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
        "lane-model-test",
        "--goal",
        "refuse a combined name",
        "--done-when",
        "pytest checks the refusal",
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


def refusal(result) -> str:
    assert result.exit_code != 0, result.output
    body = json.loads(result.output.splitlines()[0])
    assert body["ok"] is False
    return str(body["detail"])


def _launch_argv(backend_name: str, repo: Path) -> list[str]:
    """Compose the cli command line a resolved backend composes.

    The equivalence asserted is the full argv, not the launch mode: a resolution
    that reached a different model composes a different command line while the
    launch mode stays ``cli`` either way.
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


@pytest.mark.parametrize(
    ("backend", "lane", "key"),
    [
        ("codex-astra", "codex", "astra"),
        ("claude-opus", "claude", "opus"),
    ],
)
def test_combined_backend_name_is_refused_naming_lane_and_model(
    repo, catalogue, config, backend, lane, key
) -> None:
    """A ``<lane>-<model>`` backend is refused, and the refusal names the lane
    it resolves to and the ``--model`` key that replaces it."""
    detail = refusal(dispatch(repo, backend=backend))
    assert backend in detail
    assert lane in detail
    assert key in detail
    assert f"--model {key!r}" in detail


def test_lane_and_model_pair_resolve_to_the_combined_argv(
    repo, catalogue, config
) -> None:
    """``--model`` alone and ``--backend <lane> --model <key>`` reach the same
    backend, effort and launch argv the combined name named."""
    bare = payload(dispatch(repo, model="astra"))
    paired = payload(dispatch(repo, backend="codex", model="astra"))

    assert bare["backend"] == paired["backend"] == "codex-astra"
    assert bare["agent"]["model"] == paired["agent"]["model"] == "gpt-6-astra"
    combined_argv = _launch_argv("codex-astra", repo)
    assert _launch_argv(bare["backend"], repo) == combined_argv
    assert _launch_argv(paired["backend"], repo) == combined_argv
    assert "gpt-6-astra" in combined_argv
    assert combined_argv != _launch_argv("codex", repo)


def test_a_lane_s_own_name_still_resolves(repo, catalogue, config) -> None:
    """The refusal does not over-reach: a lane named on its own still routes."""
    assert payload(dispatch(repo, backend="codex"))["backend"] == "codex"
    assert payload(dispatch(repo, backend="claude"))["backend"] == "claude"


def test_model_declared_in_two_lanes_under_a_third_is_refused_naming_both(
    repo, catalogue, config
) -> None:
    """A model two lanes declare, asked for under a third, is refused by
    naming both lanes that declare it rather than as an unknown name."""
    data = yaml.safe_load(catalogue.read_text())
    data["lanes"]["secondary"] = {
        "default_model": "astra",
        "models": {"astra": data["lanes"]["codex"]["models"]["astra"]},
    }
    catalogue.write_text(yaml.safe_dump(data))

    detail = refusal(dispatch(repo, backend="claude", model="astra"))
    assert "codex" in detail
    assert "secondary" in detail
    assert "claude" in detail


def _seed_pointer(home: Path, run_id: str, **fields: object) -> Path:
    """Write one live pointer whose backend is the combined name."""
    pointer = {
        "run_id": run_id,
        "project": "proj",
        "role": "implement",
        "backend": "codex-astra",
        "dialect": "claude",
        "launch": "cli",
        "argv": ["claude", "-p"],
        "session_id": "session-recorded",
        "agent": {"model": "gpt-6-astra", "effort": "xhigh"},
        "created_at": "2026-10-13T00:00:00+00:00",
        "pid": 0x7FFFFFFF,
        "launcher_host": socket.gethostname(),
        "sandbox": "worktree-full",
        "node": {
            "plan": "example",
            "section": "dispatch",
            "role": "implement",
            "write_paths": ["result.json"],
        },
    }
    pointer.update(fields)
    path = home / "crew" / "live" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pointer))
    return path


def test_resume_of_a_combined_backend_pointer_still_resolves(
    repo, catalogue, config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resume of a record naming the combined backend still resolves it: the
    refusal is on the dispatch request, not on reading a run's own history."""
    home = Path(str(os.environ["RECKON_HOME"]))
    run_id = "r-lane-model-astra"
    _seed_pointer(home, run_id, repo=str(repo), worktree=str(repo))

    result = CliRunner().invoke(
        cli.main,
        ["crew", "resume", "--run", run_id, "--advice", "continue", "--print-only"],
    )
    assert "lane and model pair" not in result.output, result.output
    assert result.exit_code == 0, result.output
    body = json.loads(result.output.splitlines()[0])
    assert body["backend"] == "codex-astra"
