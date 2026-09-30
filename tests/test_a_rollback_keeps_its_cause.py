"""A rollback keeps the error it was rolling back, and a capture blocks nothing.

A dispatch that raises after its pointer and worktree exist unwinds both. If a
step of that unwind raises too, the error the caller reads is the unwind's,
naming a worktree claim — and the registration or launch failure that actually
stopped the dispatch is only reachable by re-running it by hand. These cases
inject a launch failure, inject a removal failure, and require the launch
failure to be what surfaces with the removal failure still visible on it.

The roster half is the same shape one layer down: a member row carrying a
captured session — the resumable id a run reported — is the tool's own
bookkeeping rather than another author's pending registration, so it must not
refuse the named commit that records a registration. A change to what a member
*is* still must.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger

# The package re-exports the dispatch *function* under this name, so the module
# that holds the rollback is resolved by import path rather than as an attribute.
dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "proj"
ROSTER = f"docs/state/{PROJECT}/crew.json"
SESSION = "019ff509-8a60-7723-94fd-65942a6d8faa"

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


class LaunchStepError(RuntimeError):
    """Stands in for a launch that fails after the pointer was published."""


class WorktreeRemovalError(RuntimeError):
    """Stands in for the removal refusing the worktree the run still claims."""


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


def _repository(tmp_path: Path) -> Path:
    """A throwaway checkout whose state directory is deliberately untracked."""
    repository = tmp_path / "repository"
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    _run(repository, "init", "-q", "-b", "main")
    _run(repository, "config", "user.email", "worker@example.invalid")
    _run(repository, "config", "user.name", "Worker")
    _run(repository, "commit", "--allow-empty", "-q", "-m", "chore: seed fixture")
    return repository


def _dispatch_repository(tmp_path: Path) -> Path:
    """A checkout a dispatch can cut a worktree from, with its own plan."""
    repository = tmp_path / "checkout"
    scripts = repository / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills" / "reckon-build" / "scripts"
    (scripts / "worktree_fleet.py").write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plan = repository / "docs" / "plans" / "plan-a.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    quote = '"'
    plan.write_text(
        "\n".join(
            [
                "<!doctype html>",
                f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>",
                f"<h2 id={quote}rollback{quote}>Rollback</h2>",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _run(repository, "init", "-q", "-b", "main")
    _run(repository, "config", "user.email", "worker@example.invalid")
    _run(repository, "config", "user.name", "Worker")
    _run(repository, "add", "--", "docs")
    _run(repository, "add", "--", "skills")
    _run(repository, "commit", "-q", "-m", "chore: seed checkout")
    mounts = Path(os.environ["RECKON_HOME"]) / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8")
    return repository


def _node(node_id: str) -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        goal="dispatch a node whose launch step refuses",
        plan="plan-a",
        section="rollback",
        done_when="pytest tests/test_a_rollback_keeps_its_cause.py passes",
        write_paths=["reckon/target.py"],
        time_budget="20m",
        spec_level="exact",
    )


def _failing_launcher(plan, *, log_path, stderr_path, prompt_path):
    raise LaunchStepError("the launch step refused after the pointer was published")


def _absent_executable_launcher(plan, *, log_path, stderr_path, prompt_path):
    """A launch step that refuses the way a missing harness executable does."""
    raise FileNotFoundError("the harness executable is absent")


def _failing_removal(repository, path: str) -> None:
    raise WorktreeRemovalError(
        f"refusing to remove worktree {path}: claimed by live runs r-other"
    )


def _roster_at_head(repository: Path) -> list[dict]:
    envelope = json.loads(_git(repository, "show", f"HEAD:{ROSTER}"))
    return envelope["data"]["members"]


def test_the_launch_failure_survives_its_own_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The removal's refusal must not replace the launch error that caused it."""
    repository = _dispatch_repository(tmp_path)
    monkeypatch.setattr(dispatch_module, "_remove_worktree", _failing_removal)

    with pytest.raises(LaunchStepError) as excinfo:
        crew.dispatch(
            node=_node("node-launch"),
            project=PROJECT,
            repo=repository,
            config=CONFIG,
            session="coordinator-a",
            launcher=_failing_launcher,
            check_budget=False,
        )

    surfaced = excinfo.value
    assert "the launch step refused after the pointer was published" in str(surfaced)
    assert "worktree" not in str(surfaced)
    # The rollback's own failure still reaches a reader: attached to the
    # surfaced error and printed, so the tree left behind is not silent.
    assert "claimed by live runs r-other" in str(surfaced.__cause__)
    assert "claimed by live runs r-other" in capsys.readouterr().err


def test_the_launch_cause_survives_when_the_unwind_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A launch refusal that already names its own cause must keep it."""
    repository = _dispatch_repository(tmp_path)
    monkeypatch.setattr(dispatch_module, "_remove_worktree", _failing_removal)

    with pytest.raises(crew.CrewError) as excinfo:
        crew.dispatch(
            node=_node("node-cause"),
            project=PROJECT,
            repo=repository,
            config=CONFIG,
            session="coordinator-a",
            launcher=_absent_executable_launcher,
            check_budget=False,
        )

    surfaced = excinfo.value
    assert "the worker launch could not start" in str(surfaced)
    # The absent executable is why the launch refused, and it stays reachable
    # as the surfaced error's cause rather than only as text inside its message.
    assert isinstance(surfaced.__cause__, FileNotFoundError)
    assert "harness executable is absent" in str(surfaced.__cause__)
    # The unwind's own failure is still visible to a reader — printed, and
    # carried on the surfaced error as a note.
    assert "claimed by live runs r-other" in "\n".join(surfaced.__notes__)
    assert "claimed by live runs r-other" in capsys.readouterr().err


def test_the_rollback_releases_its_own_claim_before_removing(
    tmp_path: Path,
) -> None:
    """The pointer a dispatch published is given back, so its own tree can go."""
    repository = _dispatch_repository(tmp_path)

    with pytest.raises(LaunchStepError):
        crew.dispatch(
            node=_node("node-clean"),
            project=PROJECT,
            repo=repository,
            config=CONFIG,
            session="coordinator-a",
            launcher=_failing_launcher,
            check_budget=False,
        )

    assert crew.list_live() == []
    worktrees = _git(repository, "worktree", "list", "--porcelain")
    assert "node-clean" not in worktrees


def _capture_the_session(repository: Path) -> None:
    """Leave an uncommitted capture — and nothing else — on the roster."""
    data, version = ledger.load(PROJECT, repository)
    data["members"][0].update(
        {
            "session_id": SESSION,
            "session_model": "some-model",
            "sessions": {"some-model": SESSION},
        }
    )
    ledger.write(PROJECT, data, version, repository)


def test_a_capture_only_ledger_admits_a_registration(tmp_path: Path) -> None:
    """A captured session is the tool's bookkeeping, not a pending registration."""
    repository = _repository(tmp_path)
    ledger.register_member(
        PROJECT, "worker-a", harness="alpha", root=repository, commit=True
    )
    _capture_the_session(repository)
    assert _git(repository, "status", "--porcelain", "--", ROSTER) == f"M {ROSTER}"
    assert ledger.member(PROJECT, "worker-a", root=repository)["session_id"] == SESSION

    entry = ledger.register_member(
        PROJECT, "worker-b", harness="alpha", root=repository, commit=True
    )

    assert entry["id"] == "worker-b"
    assert _git(repository, "status", "--porcelain", "--", ROSTER) == ""
    rows = {str(row["id"]): row for row in _roster_at_head(repository)}
    assert sorted(rows) == ["worker-a", "worker-b"]
    assert rows["worker-a"]["session_id"] == SESSION


def test_a_real_member_change_still_refuses_a_registration(tmp_path: Path) -> None:
    """A row that is not what it was committed as is another author's write."""
    repository = _repository(tmp_path)
    ledger.register_member(
        PROJECT, "worker-a", harness="alpha", root=repository, commit=True
    )
    _capture_the_session(repository)
    ledger.register_member(PROJECT, "worker-a", harness="beta", root=repository)

    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.register_member(
            PROJECT, "worker-b", harness="alpha", root=repository, commit=True
        )

    assert "uncommitted member registration(s): worker-a" in str(excinfo.value)
    assert sorted(row["id"] for row in _roster_at_head(repository)) == ["worker-a"]


def test_a_new_uncommitted_member_still_refuses_a_registration(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    ledger.register_member(PROJECT, "internal-a", harness="alpha", root=repository)

    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.register_member(
            PROJECT, "public-b", harness="alpha", root=repository, commit=True
        )

    assert "uncommitted member registration(s): internal-a" in str(excinfo.value)
    assert ledger.member(PROJECT, "public-b", root=repository) is None
