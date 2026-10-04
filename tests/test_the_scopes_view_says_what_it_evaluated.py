"""The scopes view says what it evaluated and whether each claim binds.

Two distinctions the live-claim registry owed a reader and dropped. A scopes
call carrying no wave manifest returns empty candidate collections against
nothing, and that read the same as an evaluated wave that happened to be
clean, so a caller that forgot the manifest drew a conclusion from an
unasked question; ``candidate_wave`` names which of the two the payload is.
And a listed claim carried no binding verdict, so a worker reading the
registry for itself had to choose a rule and chose a stricter one than the
dispatch scope check applies, blocking on a stopped claim that holds no
unintegrated work while dispatch walks past it.

Both are judged here against the rule dispatch itself applies, in a temporary
RECKON_HOME and a temporary repository, with the real live-pointer directory
proved untouched at teardown.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from reckon import crew, mcp
from reckon.crew.node import claim_disposition
from reckon.crew.runs import plan_scope_lanes

PROJECT = "demo"
DECLARED = "package/target.py"
CANDIDATE = "package/elsewhere.py"


def _snapshot(path: Path) -> tuple[str, ...]:
    if not path.is_dir():
        return ()
    return tuple(sorted(item.name for item in path.iterdir()))


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the crew config home at a temp dir and prove the real one is idle."""
    real_live = crew.live_dir()
    before = _snapshot(real_live)
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    yield config_home
    assert _snapshot(real_live) == before


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments], capture_output=True, check=False
    )


def _commit(repo: Path, message: str) -> None:
    ident = ("-c", "user.name=gate", "-c", "user.email=gate@example.invalid")
    _git(repo, *ident, "add", "-A")
    done = _git(repo, *ident, "commit", "-q", "-m", message)
    assert done.returncode == 0, done.stderr


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A real repository with one base commit and a readable plan index."""
    root = tmp_path / "repository"
    package = root / "package"
    package.mkdir(parents=True)
    (package / "target.py").write_text("base\n", encoding="utf-8")
    assert _git(root, "init", "-q", "-b", "main").returncode == 0
    _commit(root, "base")
    state = root / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps(
            {
                "project": PROJECT,
                "doc": "index",
                "data": {"_version": 0, "projects": [{"name": PROJECT}]},
            }
        ),
        encoding="utf-8",
    )
    return root


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.decode().strip()


def _stopped_pid() -> int:
    """Return a pid whose process has exited and been reaped."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def _write_pointer(
    run_id: str,
    *,
    repo: Path,
    paths: tuple[str, ...] = (DECLARED,),
    pid: int | None = None,
    worktree: Path | None = None,
    base_sha: str | None = None,
) -> dict[str, Any]:
    directory = crew.live_dir()
    directory.mkdir(parents=True, exist_ok=True)
    pointer: dict[str, Any] = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repo.resolve()),
        "phase": "working",
        "node": {"id": f"node-{run_id}", "write_paths": list(paths)},
    }
    if pid is not None:
        pointer["pid"] = pid
    if worktree is not None:
        pointer["worktree"] = str(worktree)
    if base_sha is not None:
        pointer["base_sha"] = base_sha
    crew._write_json(crew.pointer_path(run_id), pointer)
    return pointer


def _scopes(repo: Path, candidates: list[dict[str, Any]] | None = None) -> dict:
    return plan_scope_lanes(
        candidates or [], project=PROJECT, repo=repo, derivations={}
    )


def _scopes_view(repo: Path, candidates: list[dict[str, Any]] | None = None) -> dict:
    return mcp._crew(
        PROJECT, view="scopes", checkout_path=str(repo), candidates=candidates
    )


def _claim(report: dict, run_id: str) -> dict:
    matches = [claim for claim in report["claims"] if claim["run_id"] == run_id]
    assert matches, f"run {run_id} is not listed by the scopes view"
    return matches[0]


def test_a_scopes_call_without_a_wave_reports_unevaluated(home, repo: Path) -> None:
    report = _scopes(repo)

    assert report["candidate_wave"]["state"] == "unevaluated"
    assert report["candidate_wave"]["detail"]
    assert report["conflicts"] == []
    assert report["live_conflicts"] == []
    assert report["lanes"] == []


def test_a_supplied_wave_reports_evaluated_with_empty_conflicts(
    home, repo: Path
) -> None:
    report = _scopes(repo, [{"id": "solo", "write_paths": [CANDIDATE]}])

    assert report["candidate_wave"]["state"] == "evaluated"
    assert report["conflicts"] == []
    assert report["live_conflicts"] == []
    assert report["lane_count"] == 1


def test_the_scopes_view_carries_the_wave_state(home, repo: Path) -> None:
    unasked = _scopes_view(repo)
    asked = _scopes_view(repo, candidates=[{"id": "solo", "write_paths": [CANDIDATE]}])

    assert unasked["candidate_wave"]["state"] == "unevaluated"
    assert asked["candidate_wave"]["state"] == "evaluated"
    assert asked["conflicts"] == []


def test_a_stopped_claim_with_no_work_left_is_listed_non_binding(
    home, repo: Path
) -> None:
    pointer = _write_pointer(
        "r-stopped-clean",
        repo=repo,
        pid=_stopped_pid(),
        worktree=repo,
        base_sha=_head(repo),
    )

    claim = _claim(_scopes(repo), "r-stopped-clean")

    assert claim["binding"] is False
    assert claim_disposition(pointer).binding is False
    assert "disregarded" in claim["disposition_reason"]


def test_a_running_claim_is_listed_binding(home, repo: Path) -> None:
    pointer = _write_pointer("r-running", repo=repo, pid=os.getpid())

    claim = _claim(_scopes(repo), "r-running")

    assert claim["binding"] is True
    assert claim_disposition(pointer).binding is True


def test_an_unintegrated_commit_makes_the_stopped_claim_binding(
    home, repo: Path
) -> None:
    base_sha = _head(repo)
    assert _git(repo, "checkout", "-q", "-b", "worker-branch").returncode == 0
    (repo / "package" / "target.py").write_text("work\n", encoding="utf-8")
    _commit(repo, "unintegrated work")
    pointer = _write_pointer(
        "r-stopped-loaded",
        repo=repo,
        pid=_stopped_pid(),
        worktree=repo,
        base_sha=base_sha,
    )

    claim = _claim(_scopes(repo), "r-stopped-loaded")

    assert claim["binding"] is True
    assert claim_disposition(pointer).binding is True
    assert "promote or recover" in claim["disposition_reason"]


def test_the_scopes_view_marks_each_claim_owner(home, repo: Path) -> None:
    _write_pointer("r-running", repo=repo, pid=os.getpid())
    _write_pointer(
        "r-stopped-clean",
        repo=repo,
        pid=_stopped_pid(),
        worktree=repo,
        base_sha=_head(repo),
    )

    result = _scopes_view(repo)
    owners = {owner["run_id"]: owner for owner in result["claim_map"][DECLARED]}

    assert owners["r-running"]["binding"] is True
    assert owners["r-stopped-clean"]["binding"] is False
    for owner in result["claim_map"][DECLARED]:
        assert "binding" in owner
        assert "disposition_reason" in owner


def test_an_unverdicted_claim_still_lists_a_binding_verdict(
    home, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader never has to guess what an absent verdict means."""
    monkeypatch.setattr(
        mcp.crew_module,
        "plan_scope_lanes",
        lambda *args, **kwargs: {
            "claims": [
                {
                    "path": CANDIDATE,
                    "run_id": "r-unverdicted",
                    "node": "worker",
                    "declared_path": CANDIDATE,
                }
            ],
            "conflicts": [],
            "live_conflicts": [],
            "lanes": [],
        },
    )

    owner = _scopes_view(repo)["claim_map"][CANDIDATE][0]

    assert owner["binding"] is True
    assert owner["disposition_reason"] == ""
