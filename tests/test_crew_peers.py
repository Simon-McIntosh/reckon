"""Durable communication between workers whose Python scopes are adjacent."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from reckon import crew


peer_channel = importlib.import_module("reckon.crew.dispatch")

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
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "repo"
    (root / "skills" / "reckon-ship" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "package").mkdir()
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-ship" / "scripts" / "worktree_fleet.py").write_text(
        fleet_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "docs" / "plans" / "peers.html").write_text(
        '<meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="peers">'
        '<h2 id="interfaces">Adjacent interfaces</h2>',
        encoding="utf-8",
    )
    (root / "package" / "definition.py").write_text(
        "def interface(value: str) -> str:\n    return value\n", encoding="utf-8"
    )
    (root / "package" / "caller.py").write_text(
        "from package.definition import interface\n\nresult = interface('value')\n",
        encoding="utf-8",
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs", "package"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({"proj": str(root / "docs")}), encoding="utf-8"
    )
    return root


def _node(node_id: str, path: str) -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        goal="record one adjacent interface",
        plan="peers",
        section="interfaces",
        role="implement",
        spec_level="guided",
        done_when=("pytest reports 2 durable channel copies with one matching answer"),
        write_paths=[path],
        time_budget="20m",
    )


def _dispatch_pair(repo: Path) -> tuple[dict, dict]:
    definition = crew.dispatch(
        node=_node("definition-owner", "package/definition.py"),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="definition-session",
        launcher=lambda *args, **kwargs: os.getpid(),
    )
    caller = crew.dispatch(
        node=_node("caller-owner", "package/caller.py"),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="caller-session",
        launcher=lambda *args, **kwargs: os.getpid(),
    )
    return definition, caller


def test_adjacent_dispatches_wire_and_evidence_a_question_and_reply(
    home: Path, repo: Path
) -> None:
    definition, caller = _dispatch_pair(repo)

    definition_peers = peer_channel.peer_list(definition["run_id"])["peers"]
    caller_peers = peer_channel.peer_list(caller["run_id"])["peers"]
    assert list(definition_peers) == [caller["run_id"]]
    assert list(caller_peers) == [definition["run_id"]]
    assert caller["peer_channel"]["scope_transfer"] is False
    prompt = Path(caller["prompt_path"]).read_text(encoding="utf-8")
    assert "PEER CHANNEL" in prompt
    assert "definition-owner" in prompt

    question = peer_channel.peer_ask(
        caller["run_id"], "definition-owner", "What is the interface signature?"
    )
    reply = peer_channel.peer_reply(
        definition["run_id"], question["id"], "interface(value: str) -> str"
    )
    received = peer_channel.peer_read(caller["run_id"], question["id"], wait=1)

    assert received["status"] == "answered"
    assert received["question"]["reply"]["answer"] == "interface(value: str) -> str"
    assert len(question["evidence_paths"]) == 2
    assert all(Path(path).is_file() for path in reply["evidence_paths"])
    assert all(
        json.loads(Path(path).read_text(encoding="utf-8"))["reply"]["answer"]
        == "interface(value: str) -> str"
        for path in reply["evidence_paths"]
    )


def test_peer_read_blocks_on_an_event_until_the_reply_arrives(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition, caller = _dispatch_pair(repo)
    question = peer_channel.peer_ask(
        caller["run_id"], definition["run_id"], "Which name should the caller use?"
    )
    watching = threading.Event()
    original_descriptor = peer_channel._inotify_descriptor

    def observed_descriptor(directory: Path) -> int:
        descriptor = original_descriptor(directory)
        watching.set()
        return descriptor

    monkeypatch.setattr(peer_channel, "_inotify_descriptor", observed_descriptor)
    monkeypatch.setattr(
        peer_channel.time,
        "sleep",
        lambda *_args, **_kwargs: pytest.fail("peer reads must not poll"),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(
            peer_channel.peer_read, caller["run_id"], question["id"], wait=2
        )
        assert watching.wait(timeout=1)
        assert waiting.done() is False
        peer_channel.peer_reply(definition["run_id"], question["id"], "interface")
        result = waiting.result(timeout=1)

    assert result["status"] == "answered"


def test_unanswered_peer_read_delivers_a_named_needs_help_report(
    home: Path, repo: Path
) -> None:
    definition, caller = _dispatch_pair(repo)
    text = "Should the caller pass a keyword argument?"
    question = peer_channel.peer_ask(caller["run_id"], definition["run_id"], text)

    result = peer_channel.peer_read(caller["run_id"], question["id"], wait=0.05)

    manifest = Path(caller["manifest_path"]).read_text(encoding="utf-8")
    assert result["status"] == "needs-help"
    assert manifest.startswith("NEEDS-HELP:")
    assert text in manifest
    assert "status: blocked" in manifest
    assert all(
        field in manifest
        for field in ("tried:", "options:", "leaning:", "cost-if-wrong:")
    )
    assert Path(result["report_path"]).is_file()


def test_peer_channel_does_not_relax_live_scope_refusal(home: Path, repo: Path) -> None:
    definition, caller = _dispatch_pair(repo)
    before = sorted(path.name for path in crew.runs_dir().iterdir())

    with pytest.raises(crew.ScopeConflict, match=definition["run_id"]):
        crew.dispatch(
            node=_node("scope-taker", "package/definition.py"),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="third-session",
            launcher=lambda *args, **kwargs: os.getpid(),
        )

    assert sorted(path.name for path in crew.runs_dir().iterdir()) == before
    assert peer_channel.peer_list(caller["run_id"])["peers"]
