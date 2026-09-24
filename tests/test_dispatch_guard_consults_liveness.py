"""Dispatch member admission follows the observed worker liveness."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger

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


@pytest.fixture()
def dispatch_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    (repo / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    (repo / "docs" / "plans").mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (repo / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").write_text(
        fleet_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (repo / "docs" / "plans" / "fixture.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
</head><body><h2 id="guard">Dispatch guard</h2></body></html>
""",
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"proj": str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo


def _node(manifest_path: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id="candidate-node",
        goal="record one dispatch guard result",
        plan="fixture",
        section="guard",
        spec_level="guided",
        done_when="pytest reports one passing guard case",
        write_paths=["src/candidate.py"],
        time_budget="20m",
        manifest_path=str(manifest_path),
    )


def _dispatch_against_holder(
    dispatch_context: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    liveness: tuple[bool | None, str],
) -> dict:
    _config_home, repo = dispatch_context
    member = "worker-a"
    ledger.register_member("proj", member, harness="codex", root=repo)
    holder = {
        "run_id": "holder-run",
        "project": "proj",
        "member": member,
        "phase": "working",
        "pid": os.getpid(),
        "launcher_host": socket.gethostname(),
    }
    dispatch_module = __import__("reckon.crew.dispatch", fromlist=["dispatch"])
    monkeypatch.setattr(dispatch_module, "list_live", lambda **_kwargs: [holder])
    monkeypatch.setattr(
        "reckon.crew.claims._worker_liveness",
        lambda _pointer: liveness,
    )
    return crew.dispatch(
        node=_node(_config_home / "manifests" / "candidate.md"),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="coordinator-session",
        member=member,
        launcher=lambda *args, **kwargs: 4242,
    )


def test_live_member_is_refused_through_dispatch(
    dispatch_context: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(crew.MemberInFlight, match="alive"):
        _dispatch_against_holder(
            dispatch_context, monkeypatch, (True, "worker is live")
        )


def test_gone_member_is_admitted_through_dispatch(
    dispatch_context: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _dispatch_against_holder(
        dispatch_context, monkeypatch, (False, "worker is gone")
    )

    assert record["member"] == "worker-a"


def test_unknown_member_liveness_is_refused_through_dispatch(
    dispatch_context: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(crew.MemberInFlight, match="unknown"):
        _dispatch_against_holder(dispatch_context, monkeypatch, (None, "cannot tell"))


def test_dispatch_wires_the_liveness_refusal(
    dispatch_context: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(crew.MemberInFlight):
        _dispatch_against_holder(
            dispatch_context, monkeypatch, (True, "worker is live")
        )

    dispatch_source = Path(crew.__file__).with_name("crew").joinpath("dispatch.py")
    assert (
        dispatch_source.read_text(encoding="utf-8").count("refuse_member_in_flight")
        >= 1
    )
