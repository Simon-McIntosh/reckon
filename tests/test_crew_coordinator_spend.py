"""Dispatch attributes node-authoring cost to its coordinator session."""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger

CONFIG = {
    "default_backend": "native",
    "backends": {
        "native": {
            "launch": "in-harness",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    root = tmp_path / "repo"
    scripts = root / "skills" / "reckon-ship" / "scripts"
    scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="accounting">Coordinator accounting</h2>',
        encoding="utf-8",
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"sample": str(root / "docs")}), encoding="utf-8"
    )
    return config_home, root


def _node(config_home: Path, name: str) -> crew.TaskNode:
    return crew.TaskNode(
        id=f"coordinator-{name}",
        goal="record the coordinator cost on one dispatched node",
        plan="fixture",
        section="accounting",
        role="implement",
        spec_level="guided",
        done_when=(
            "pytest exits 0 and the live and promoted records "
            "carry one identical coordinator attribution"
        ),
        write_paths=[f"src/{name}.py"],
        time_budget="20m",
        manifest_path=str(config_home / f"{name}.md"),
    )


def _dispatch(
    repository: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    accounting: dict,
    *,
    name: str,
) -> dict:
    config_home, root = repository
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    monkeypatch.setattr(
        dispatch_module, "_coordinator_accounting", lambda _session: accounting
    )
    return crew.dispatch(
        node=_node(config_home, name),
        project="sample",
        repo=root,
        config=CONFIG,
        session="authoring-session",
    )


def test_dispatch_records_the_coordinator_and_authoring_turn_spend(
    repository: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    accounting = {
        "session_id": "authoring-session",
        "runtime_session_id": "runtime-session",
        "harness": "codex",
        "authoring_turn": {
            "status": "measured",
            "tokens": {
                "input_tokens": 125_000,
                "cached_input_tokens": 120_000,
                "output_tokens": 800,
                "total_tokens": 125_800,
            },
            "source": "codex-session-transcript",
        },
    }

    record = _dispatch(repository, monkeypatch, accounting, name="measured")
    stored = crew.read_pointer(record["run_id"])

    assert stored["coordinator"] == accounting
    assert stored["coordinator"]["session_id"] == "authoring-session"
    assert stored["coordinator"]["authoring_turn"]["tokens"]["input_tokens"] == 125_000


def test_missing_authoring_spend_is_unknown_instead_of_zero(
    repository: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    accounting = {
        "session_id": "authoring-session",
        "runtime_session_id": None,
        "harness": None,
        "authoring_turn": {
            "status": "unknown",
            "tokens": None,
            "detail": "the dispatching harness exposed no authoring-turn token usage",
        },
    }

    record = _dispatch(repository, monkeypatch, accounting, name="unknown")

    turn = crew.read_pointer(record["run_id"])["coordinator"]["authoring_turn"]
    assert turn["status"] == "unknown"
    assert turn["tokens"] is None
    assert 0 not in turn.values()


def test_coordinator_attribution_survives_promotion(
    repository: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    accounting = {
        "session_id": "authoring-session",
        "runtime_session_id": "runtime-session",
        "harness": "claude-code",
        "authoring_turn": {
            "status": "measured",
            "tokens": {
                "input_tokens": 84_000,
                "output_tokens": 500,
                "total_tokens": 84_500,
            },
            "source": "claude-code-session-transcript",
        },
    }
    _config_home, root = repository
    record = _dispatch(repository, monkeypatch, accounting, name="promoted")

    promoted = crew.complete(
        record["run_id"],
        gate="not-run",
        outcome="promotion persistence was the subject of this fixture",
        root=root,
    )["record"]
    committed = ledger.load("sample", root)[0]["runs"][0]

    assert promoted["node_definition"]["coordinator"] == accounting
    assert committed["node_definition"]["coordinator"] == accounting


def test_claude_authoring_usage_includes_cached_input(tmp_path: Path) -> None:
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "usage": {
                                "input_tokens": 2,
                                "cache_creation_input_tokens": 100,
                                "cache_read_input_tokens": 900,
                                "output_tokens": 20,
                            }
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "usage": {
                                "input_tokens": 3,
                                "cache_creation_input_tokens": 200,
                                "cache_read_input_tokens": 1_800,
                                "output_tokens": 30,
                            }
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    dispatch_module = importlib.import_module("reckon.crew.dispatch")

    tokens = dispatch_module._claude_authoring_turn(transcript)

    assert tokens["input_tokens"] == 2_003
    assert tokens["total_tokens"] == 2_033


def test_codex_authoring_usage_reads_the_current_turn_total(tmp_path: Path) -> None:
    transcript = tmp_path / "codex.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 125_000,
                            "cached_input_tokens": 120_000,
                            "output_tokens": 800,
                            "total_tokens": 125_800,
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    dispatch_module = importlib.import_module("reckon.crew.dispatch")

    tokens = dispatch_module._codex_authoring_turn(transcript)

    assert tokens == {
        "input_tokens": 125_000,
        "cached_input_tokens": 120_000,
        "output_tokens": 800,
        "total_tokens": 125_800,
    }
