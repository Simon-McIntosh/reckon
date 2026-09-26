"""A named member's registration reaches the repository as its own commit.

A dispatch that names no member is disposable: it runs under an identity minted
from its own run id and registers nothing, so no unrelated task is refused for a
row another run in flight happens to hold. A declared member is registered by
the registration code, which commits what it writes, so its row is readable from
the repository rather than existing only in the checkout that wrote it — an
uncommitted row is invisible to every other checkout and rides whichever
unrelated commit comes next.

These tests build a throwaway repository; the suite's autouse fixture points
the configuration home at a temp tree. They assert a registration is committed,
that the commit names only the roster file, that a caller which does not ask to
commit still does not, that a commit which fails is surfaced rather than
returned as a registration, that a dispatch naming no member neither registers
nor commits, and that the live fleet home and the shared checkout are
untouched.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import routing

PROJECT = "proj"
ROSTER = f"docs/state/{PROJECT}/crew.json"
SESSION = "session-for-a-node"

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _run(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


def _seed(repository: Path, tracked: list[str]) -> None:
    _run(repository, "init", "-q", "-b", "main")
    _run(repository, "config", "user.email", "worker@example.invalid")
    _run(repository, "config", "user.name", "Worker")
    if tracked:
        _run(repository, "add", "--", *tracked)
    _run(repository, "commit", "--allow-empty", "-q", "-m", "chore: seed fixture")


def _repository(tmp_path: Path) -> Path:
    """A throwaway checkout whose state directory is deliberately untracked.

    Nothing under ``docs/`` is committed, so every path the call touches
    outside the roster file shows up as a dirty entry rather than hiding inside
    an already-committed tree.
    """
    repository = tmp_path / "repository"
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    _seed(repository, [])
    return repository


def _register_session(repository: Path, member_id: str = SESSION) -> dict:
    return routing._register_session_member(
        PROJECT,
        member_id,
        backend="alpha",
        role="implement",
        root=repository,
    )


def _roster_at_head(repository: Path) -> list[dict]:
    envelope = json.loads(_git(repository, "show", f"HEAD:{ROSTER}"))
    return envelope["data"]["members"]


# The registration is committed


def test_a_session_registration_is_committed(tmp_path: Path) -> None:
    """The falsifier: the row is readable from the repository, not the tree."""
    repository = _repository(tmp_path)
    base = _git(repository, "rev-parse", "HEAD")

    stored = _register_session(repository)

    assert stored["id"] == SESSION
    assert _git(repository, "rev-list", "--count", f"{base}..HEAD") == "1"
    subject = _git(repository, "log", "-1", "--format=%s")
    assert subject == f"chore(roster): register {SESSION}"
    assert _git(repository, "log", "-1", "--format=%b")
    assert _git(repository, "status", "--porcelain", "--", ROSTER) == ""


def test_the_registration_commit_names_only_the_roster_file(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    unrelated = repository / "notes.txt"
    unrelated.write_text("committed\n", encoding="utf-8")
    _run(repository, "add", "--", "notes.txt")
    _run(repository, "commit", "-q", "-m", "docs: add fixture note")
    unrelated.write_text("dirty\n", encoding="utf-8")

    _register_session(repository)

    tree = _git(repository, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
    assert tree.splitlines() == [ROSTER]
    staged = _git(repository, "diff", "--cached", "--name-only")
    assert staged == ""
    dirty = _git(repository, "status", "--porcelain").splitlines()
    assert "M notes.txt" in dirty
    assert "?? " + ROSTER not in dirty


def test_the_committed_row_is_what_the_roster_serves(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    _register_session(repository)

    rows = _roster_at_head(repository)
    assert [str(e["id"]) for e in rows] == [SESSION]
    assert ledger.members(PROJECT, repository) == rows


# A caller that does not ask to commit still does not


def test_a_caller_that_does_not_ask_to_commit_leaves_the_write_untracked(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    base = _git(repository, "rev-parse", "HEAD")

    ledger.register_member(PROJECT, "internal-a", harness="alpha", root=repository)

    assert _git(repository, "rev-parse", "HEAD") == base
    assert _git(repository, "status", "--porcelain") == "?? docs/"
    untracked = _git(repository, "status", "--porcelain", "--", ROSTER)
    assert untracked == "?? " + ROSTER


def test_an_uncommitted_row_already_in_the_roster_blocks_the_registration(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    ledger.register_member(PROJECT, "internal-a", harness="alpha", root=repository)
    base = _git(repository, "rev-parse", "HEAD")

    with pytest.raises(crew.CrewError) as excinfo:
        _register_session(repository)

    assert "could not provision session member" in str(excinfo.value)
    assert "internal-a" in str(excinfo.value)
    assert _git(repository, "rev-parse", "HEAD") == base
    assert ledger.member(PROJECT, SESSION, root=repository) is None


def test_a_failed_registration_commit_is_surfaced(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    base = _git(repository, "rev-parse", "HEAD")
    calls = tmp_path / "hook-calls"
    hook = repository / ".git" / "hooks" / "pre-commit"
    script = chr(10).join(
        ["#!/bin/sh", "echo called >> " + str(calls), "exit 1"]
    ) + chr(10)
    hook.write_text(script, encoding="utf-8")
    hook.chmod(0o755)

    with pytest.raises(crew.CrewError) as excinfo:
        _register_session(repository)

    assert "not committed" in str(excinfo.value)
    assert _git(repository, "rev-parse", "HEAD") == base
    assert calls.read_text(encoding="utf-8").splitlines() == ["called"]
    assert _git(repository, "status", "--porcelain", "--", ROSTER) == "?? " + ROSTER


def _dispatch_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "checkout"
    scripts = repository / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills" / "reckon-build" / "scripts"
    fleet = scripts / "worktree_fleet.py"
    fleet.write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    state.joinpath("index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + chr(10),
        encoding="utf-8",
    )
    plan = repository / "docs" / "plans" / "plan-a.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    q = chr(34)
    meta = "<meta name=" + q + "docs-project" + q + " content=" + q + "proj" + q + ">"
    heading = "<h2 id=" + q + "session-routing" + q + ">Session routing</h2>"
    plan.write_text(
        chr(10).join(["<!doctype html>", meta, heading]) + chr(10), encoding="utf-8"
    )
    _seed(repository, ["docs", "skills"])
    home = Path(os.environ["RECKON_HOME"])
    mounts = home.joinpath("mounts.json")
    mounts.write_text(json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8")
    return repository


def _dispatch_node(node_id: str, section: str = "session-routing") -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        goal="dispatch a node under this test's member identity",
        plan="plan-a",
        section=section,
        done_when="pytest tests/test_member_registration_commits.py passes",
        write_paths=["reckon/target.py"],
        time_budget="20m",
        spec_level="exact",
    )


def test_a_dispatch_naming_no_member_is_disposable_and_commits_nothing(
    tmp_path: Path,
) -> None:
    """The unnamed dispatch is disposable: no roster row, no commit, own identity.

    A named member is registered and committed first, so the roster the unnamed
    dispatch leaves alone is one these assertions have already seen hold a row.
    """
    repository = _dispatch_repository(tmp_path)
    ledger.register_member(
        PROJECT, "named-member", harness="alpha", root=repository, commit=True
    )
    base = _git(repository, "rev-parse", "HEAD")

    record = crew.dispatch(
        node=_dispatch_node("node-a"),
        project=PROJECT,
        repo=repository,
        config=CONFIG,
        session="coordinator-a",
        launcher=lambda plan, *, log_path, stderr_path, prompt_path: os.getpid(),
        check_budget=False,
    )

    assert str(record["member"]) == "disposable-" + str(record["run_id"])
    assert record["session_id"] is None
    assert _git(repository, "rev-list", "--count", base + "..HEAD") == "0"
    assert [str(e["id"]) for e in _roster_at_head(repository)] == ["named-member"]
    assert [str(e["id"]) for e in ledger.members(PROJECT, repository)] == [
        "named-member"
    ]
    assert _git(repository, "status", "--porcelain", "--", ROSTER) == ""


def test_a_dispatch_naming_a_member_still_registers_and_serialises(
    tmp_path: Path,
) -> None:
    """A named member stays a durable route: the row exists and a second run is refused."""
    repository = _dispatch_repository(tmp_path)
    ledger.register_member(
        PROJECT, "named-member", harness="alpha", root=repository, commit=True
    )
    base = _git(repository, "rev-parse", "HEAD")
    node = _dispatch_node("node-b")

    first = crew.dispatch(
        node=node,
        project=PROJECT,
        repo=repository,
        config=CONFIG,
        session="coordinator-a",
        member="named-member",
        launcher=lambda plan, *, log_path, stderr_path, prompt_path: os.getpid(),
        check_budget=False,
    )

    assert first["member"] == "named-member"
    assert _git(repository, "rev-list", "--count", base + "..HEAD") == "0"

    with pytest.raises(crew.MemberInFlight):
        crew.dispatch(
            node=node,
            project=PROJECT,
            repo=repository,
            config=CONFIG,
            session="coordinator-a",
            member="named-member",
            launcher=lambda *args, **kwargs: pytest.fail(
                "the named member must remain serialised"
            ),
            check_budget=False,
        )


def test_the_live_home_and_the_shared_checkout_are_untouched(tmp_path: Path) -> None:
    checkout = Path(__file__).resolve().parents[1]
    live_roster = checkout / ROSTER
    before_bytes = live_roster.read_bytes() if live_roster.is_file() else None
    before_docs = _git(checkout, "status", "--porcelain", "--", "docs/state")
    live_home = Path.home() / ".config" / "reckon"
    before_home = (
        sorted(e.name for e in live_home.iterdir()) if live_home.is_dir() else []
    )
    repository = _repository(tmp_path)

    _register_session(repository)

    assert ledger.ledger_path(PROJECT, repository).is_relative_to(tmp_path)
    assert not Path(os.environ["RECKON_HOME"]).is_relative_to(Path.home())
    after_home = (
        sorted(e.name for e in live_home.iterdir()) if live_home.is_dir() else []
    )
    assert after_home == before_home
    assert _git(checkout, "status", "--porcelain", "--", "docs/state") == before_docs
    after_bytes = live_roster.read_bytes() if live_roster.is_file() else None
    assert after_bytes == before_bytes


def test_a_reap_before_a_registration_does_not_block_it(tmp_path: Path) -> None:
    """Both roster writes a dispatch makes are recorded, so neither blocks the other.

    A dispatch reaps idle session rows before it registers its own, so the two
    writes land in the same checkout one after the other. A reap whose removal
    were left in the tree would be refused by the registration's own commit, the
    way any roster write the registration did not make is refused, and the
    dispatch would then provision no member at all.
    """
    repository = _repository(tmp_path)
    ledger.register_member(
        PROJECT,
        "session-old",
        harness="alpha",
        root=repository,
        now="2020-01-01T00:00:00Z",
        commit=True,
    )
    base = _git(repository, "rev-parse", "HEAD")

    reaped = routing.reap_idle_session_members(PROJECT, root=repository)

    assert reaped["reaped"] == ["session-old"]
    assert _git(repository, "rev-list", "--count", f"{base}..HEAD") == "1"
    assert (
        _git(repository, "log", "-1", "--format=%s")
        == "chore(roster): retire session-old"
    )
    assert _git(repository, "status", "--porcelain", "--", ROSTER) == ""
    assert ledger.member(PROJECT, "session-old", root=repository) is None

    stored = _register_session(repository)

    assert stored["id"] == SESSION
    assert _git(repository, "status", "--porcelain", "--", ROSTER) == ""
    assert _roster_at_head(repository) == [
        ledger.member(PROJECT, SESSION, root=repository)
    ]
