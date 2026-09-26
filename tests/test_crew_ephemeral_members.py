"""Disposable per-dispatch identities for unnamed dispatches.

A dispatch that names no member carries an identity minted from its own run id
and registers no roster row, so two unnamed dispatches of one coordinator never
serialise against each other. The session a run captures still lands on its own
pointer, and no later dispatch composes it.
"""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from reckon import crew, ledger


CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
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


@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").write_text(
        fleet_script.read_text()
    )
    (root / "docs" / "plans" / "dispatch.html").write_text(
        '<meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="dispatch">'
        '<h2 id="concurrency">Concurrent dispatch</h2>'
    )
    (root / "seed.txt").write_text("seed\n")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/dispatch.html"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    return root


def _node(node_id: str, manifest: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        goal="record one isolated launch",
        plan="dispatch",
        section="concurrency",
        spec_level="exact",
        done_when="launch record count equals 2 and member refusal count equals 0",
        write_paths=[f"records/{node_id}.json"],
        time_budget="20m",
        manifest_path=str(manifest),
    )


def test_unnamed_dispatches_are_isolated_and_never_reuse_a_captured_session(
    isolated_home: Path, repository: Path
) -> None:
    """Two unnamed dispatches are disposable; neither registers nor composes a session.

    One named member is registered and committed first, so the roster the
    unnamed dispatches leave alone is one these assertions have already seen
    hold a row.
    """
    ledger.register_member(
        "proj", "keeper", harness="worker", root=repository, commit=True
    )
    roster_path = ledger.ledger_path("proj", repository)
    roster_before = roster_path.read_bytes()

    def launch(node_id: str, coordinator_session: str) -> dict:
        return crew.dispatch(
            node=_node(node_id, isolated_home / f"{node_id}.md"),
            project="proj",
            repo=repository,
            config=CONFIG,
            session=coordinator_session,
            launcher=lambda *args, **kwargs: 4242,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(launch, "left-node", "left-coordinator"),
            pool.submit(launch, "right-node", "right-coordinator"),
        ]
        refusals = [future.exception(timeout=10) for future in futures]
        first, second = [future.result(timeout=10) for future in futures]

    assert refusals == [None, None]
    assert first["member"] == "disposable-" + str(first["run_id"])
    assert second["member"] == "disposable-" + str(second["run_id"])
    assert first["member"] != second["member"]
    assert first["session_id"] is None
    assert second["session_id"] is None
    assert {entry["id"] for entry in ledger.members("proj", root=repository)} == {
        "keeper"
    }
    assert roster_path.read_bytes() == roster_before

    captured_session = "019ff509-8a60-7723-94fd-65942a6d8faa"
    Path(first["log_path"]).write_text(
        "\n".join(
            [
                json.dumps({"thread_id": captured_session, "type": "thread.started"}),
                json.dumps({"type": "turn.started"}),
                json.dumps({"type": "turn.completed", "usage": {}}),
            ]
        )
        + "\n"
    )
    observed = crew.observe(first["run_id"])
    assert observed["phase"] == "complete"
    assert observed["session_capture"]["captured"] is True

    captured = crew.read_pointer(first["run_id"])
    assert captured["session_id"] == captured_session
    assert captured["session_harness"] == "codex"
    assert captured["session_model"] == "some-model"
    assert roster_path.read_bytes() == roster_before

    # A later dispatch from the same coordinator is a third disposable identity:
    # it composes no session, and the record names the task rule as the reason.
    resumed = launch("followup-node", "left-coordinator")
    assert resumed["member"] == "disposable-" + str(resumed["run_id"])
    assert resumed["member"] != first["member"]
    assert resumed["session_id"] is None
    assert resumed["session_id_absent"]["point"] == "dispatch-no-same-task-session"
