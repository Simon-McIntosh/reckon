"""A dispatch whose write paths leave the repository is not its own conflict.

A review node's scope is the durable review store under the crew configuration
home, which no repository contains. The admission check resolves each declared
path to the repository holding it, so a path outside every repository is one
whose claim carries no repository to match on, and the arbitration that walks
claims by path meets it by a different route than an in-repository path does.
A refusal recorded 2026-09-28 named, as the claimant of a review dispatch's two
store paths, the run that same call had created. This case measures the plain
shape of that report — one sequential dispatch, its own paths, no peer — and it
is accepted at the revision it was written against: the run id is returned and
nothing names that run as a conflict. So the recorded refusal is not reproduced
by a single sequential dispatch, and the shape that does reproduce it is still
open; see the node's manifest for the arms tried and the next probe.

A second case, not yet written, keeps the real conflict visible: a different
live run already holding one of those paths still refuses this one, naming that
run.

The real pointer directory under the user's config home is asserted untouched
across every case, because an isolated read does not prove an isolated write.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon import crew
from reckon.crew.runs import list_live, pointer_path

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "out-of-repo-claim-fixture"
NODE_ID = "review-out-of-repo"
SESSION = "session-out-of-repo"
REVIEWED_RUN = "r-20260101T000000000000-reviewed-run"
REVIEWED_HEAD = "0123456789abcdef0123456789abcdef01234567"
REAL_LIVE = Path.home() / ".config" / "reckon" / "crew" / "live"

CONFIG: dict[str, Any] = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": False,
            "time_budget": "25m",
        }
    },
    "roles": {"review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _plan_document() -> str:
    meta = (
        '<meta name="docs-project" content="%s">'
        '<meta name="reckon-type" content="plan"><meta name="plan-slug" content="fixture">'
        '<meta name="plan-title" content="fixture"><meta name="plan-status" content="active">'
        '<meta name="plan-impl" content="0"><meta name="plan-version" content="0">'
    ) % PROJECT
    body = '<body><h2 id="s5">A review writes outside the repository</h2></body>'
    return f"<!doctype html><html><head>{meta}</head>{body}</html>"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A temporary crew home and a repository that looks like a reckon mount."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(_plan_document(), encoding="utf-8")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    _git(repo, "add", "seed.txt", "docs")
    _git(repo, "commit", "-q", "-m", "chore: seed")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo


@pytest.fixture(autouse=True)
def real_live_directory_is_not_a_fixture_target() -> Any:
    """No case may write this fixture's pointer into the real crew home."""

    def fixture_pointers() -> list[str]:
        found = []
        for path in REAL_LIVE.glob("*.json"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if PROJECT in text or NODE_ID in text:
                found.append(path.name)
        return found

    assert fixture_pointers() == []
    yield
    assert fixture_pointers() == []


def _store_paths(config_home: Path) -> list[str]:
    """The two durable paths a review of one run at one head writes.

    They live under the crew configuration home, which no repository contains,
    and the role that declares them declares nothing inside the repository.
    """
    store = config_home / "crew" / "reviews" / PROJECT
    return [
        str(store / f"{REVIEWED_RUN}.json"),
        str(store / f"{REVIEWED_RUN}.at-{REVIEWED_HEAD}.json"),
    ]


def _node(config_home: Path, write_paths: list[str]) -> crew.TaskNode:
    return crew.TaskNode(
        id=NODE_ID,
        goal="review the recorded run and store the review outside the repository",
        plan="fixture",
        section="s5",
        role="review",
        spec_level="guided",
        done_when=(
            "review.read_review parses both granted paths: 2 records, each with "
            "status parsed and 0 added failures"
        ),
        write_paths=write_paths,
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{NODE_ID}.md"),
    )


def _dispatch(
    config_home: Path,
    repo: Path,
    *,
    session: str = SESSION,
    write_paths: list[str] | None = None,
) -> dict:
    """Dispatch the review node with a launcher stub standing in for the worker.

    The stub returns this live process's pid, so the pointer dispatch writes
    names a running worker and the claim it records is honoured by every read
    that judges it.
    """
    return crew.dispatch(
        node=_node(
            config_home,
            _store_paths(config_home) if write_paths is None else write_paths,
        ),
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session=session,
        launcher=lambda *_args, **_kwargs: os.getpid(),
        check_budget=False,
    )


def _prepare_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str = "seed"
):
    """Stand in for the worktree cut, so no real tree is created."""

    def prepare(_repo: Path, _session: str, _node: str, base: str) -> dict:
        path = tmp_path / "worktrees" / name
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": base, "base_sha": base}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare)


def live_pointers_naming(node_id: str) -> list[str]:
    """Run ids of live pointers of the fixture project that name the node."""
    naming = []
    for record in list_live(project=PROJECT):
        node = record.get("node") or {}
        if not isinstance(node, dict):
            continue
        if node.get("id") == node_id or node_id in str(record.get("run_id", "")):
            naming.append(str(record.get("run_id")))
    return naming


def run_directories_naming(config_home: Path, node_id: str) -> list[Path]:
    root = config_home / "crew" / "runs"
    if not root.exists():
        return []
    return [path for path in root.iterdir() if node_id in path.name]


def worktrees_naming(repo: Path, node_id: str) -> list[str]:
    listing = _git(repo, "worktree", "list", "--porcelain")
    return [
        line.removeprefix("worktree ")
        for line in listing.splitlines()
        if line.startswith("worktree ") and node_id in line
    ]


def _assert_no_trace(config_home: Path, repo: Path) -> None:
    """No live pointer, no run directory and no worktree for the node."""
    assert live_pointers_naming(NODE_ID) == []
    assert run_directories_naming(config_home, NODE_ID) == []
    assert worktrees_naming(repo, NODE_ID) == []


# ── The defect


def test_the_out_of_repo_dispatch_is_not_refused_by_its_own_claim(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured arm: a sequential out-of-repo dispatch is not refused.

    Both declared paths lie under the crew configuration home, so neither is
    contained by any repository and the arbitration matches them by overlap
    rather than by repository. This arm passes at the revision this file was
    written against, which is what narrows the recorded refusal to a shape
    other than a single sequential dispatch.
    """
    config_home, repo = home
    _prepare_worktree(monkeypatch=monkeypatch, tmp_path=tmp_path)

    try:
        record = _dispatch(config_home, repo)
    except crew.ScopeConflict as exc:
        raise AssertionError(
            "the dispatch was refused by the claim of run %r, which is the run "
            "this same call created" % (exc.run_id,)
        ) from exc

    assert record["run_id"]
    assert live_pointers_naming(NODE_ID) == [str(record["run_id"])]