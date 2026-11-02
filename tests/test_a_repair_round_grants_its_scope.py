"""A repair round's out-of-scope finding paths reach the run's claim.

A repair round is composed from a review's blocking findings and rides a resume
of the reviewed run. A finding may name a path the run was never dispatched to
write. The round edits it because the advice says to, so the claim must carry
that path before the worker resumes: promotion accepts the granted change with
no exception passed by hand, and a peer sees the path held while the round edits
it. These cases drive the reflex and read back the two facts a reader depends
on — the grant on the run's declared scope, and the scope promotion measures.

A path a peer's claim already holds is the other half of the same rule. The
grant is refused, the path is left out of the advice's scope line, and the
advice names the peer that holds it, so a worker is told which file it must not
edit rather than discovering the refusal at promotion.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery, repair, resumption, runs
from reckon.crew import review as review_module
from reckon.crew.dispatch_claims import declared_write_grants
from reckon.crew.promotion_checks import _landing_scope_products

pytestmark = pytest.mark.arms_watch_producer

PROJECT = "sample"
RUN_ID = "r-unpromoted"
NODE_ID = "a-reviewed-node"
PEER_RUN_ID = "r-peer-holding-the-path"

DECLARED = "reckon/crew/declared.py"
GRANTED = "reckon/crew/ticker.py"

CONFIG = {
    "default_backend": "alpha",
    "local_backend": "alpha",
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
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

FINDING = {
    "file": GRANTED,
    "line": "10",
    "text": "off-by-one in the loop",
}


@pytest.fixture()
def isolated_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path, str]:
    """A project whose review store, ledger and repository live under a temp root."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s2">A repair round grants its scope</h2>',
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
        '{"sample": "' + str(repo / "docs") + '"}', encoding="utf-8"
    )

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def prepare_worktree(_repo: Path, session: str, node: str, base: str) -> dict:
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": base, "base_sha": base_sha}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    return config_home, repo, base_sha


def _completed_pointer(
    config_home: Path,
    repo: Path,
    *,
    write_paths: list[str] | None = None,
) -> dict:
    """The reviewed run: an exited implement run whose live pointer still stands."""
    manifest = config_home / "manifests" / (RUN_ID + ".md")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: " + RUN_ID + "\nstatus: complete\ncommits: " + RUN_ID + "\n",
        encoding="utf-8",
    )
    record = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(repo),
        "role": "implement",
        "node": {
            "id": NODE_ID,
            "plan": "fixture",
            "section": "s2",
            "write_paths": list(write_paths or []),
        },
        "backend": "alpha",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    }
    crew._write_json(crew.pointer_path(RUN_ID), record)
    return record


def _peer_pointer(repo: Path, *, write_paths: list[str]) -> dict:
    """A live peer run that holds the named paths, so the grant must refuse."""
    record = {
        "run_id": PEER_RUN_ID,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(repo),
        "role": "implement",
        "node": {
            "id": "a-peer-node",
            "plan": "fixture",
            "section": "s3",
            "write_paths": list(write_paths),
        },
        "backend": "alpha",
        "launch": "cli",
        "phase": "working",
        "process_alive": True,
        "session": "session-peer",
    }
    crew._write_json(crew.pointer_path(PEER_RUN_ID), record)
    return record


def _store_review(head_sha: str, findings: list[dict[str, str]]) -> None:
    record = {
        "project": PROJECT,
        "reviewed_run_id": RUN_ID,
        "reviewed_base_sha": head_sha,
        "reviewed_head_sha": head_sha,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15),
        "total": 75,
        "findings": findings,
    }
    review_module.store_review(record)


def _stub_resume(monkeypatch) -> list[dict]:
    """Replace the resume entry point and keep every advice it was handed."""
    calls: list[dict] = []

    def fake_resume(run_id, record, *, config=None, launcher=None, advice=""):
        calls.append({"run_id": run_id, "advice": advice, "launcher": launcher})
        directory = Path(runs.run_dir(RUN_ID))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "resume-1.jsonl").write_text(
            json.dumps({"type": "assistant", "message": {"content": "working"}})
            + "\n"
            + json.dumps({"type": "result", "subtype": "success"})
            + "\n",
            encoding="utf-8",
        )
        return {"pid": os.getpid(), "turn": 1, "log_path": "resume-1.jsonl"}

    monkeypatch.setattr(resumption, "_resume", fake_resume)
    return calls


def _stub_dispatch(monkeypatch) -> list[dict]:
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    calls: list[dict] = []

    def fake_dispatch(**kwargs):
        calls.append(kwargs)
        return {"run_id": "r-whatever"}

    monkeypatch.setattr(dispatch_module, "dispatch", fake_dispatch)
    return calls


@contextmanager
def _armed_fleet():
    """Hold the follower claim the sweep is gated on, then release the watch."""
    with runs.follower_claim(PROJECT, "session-orchestrating", delivery="stream"):
        try:
            yield
        finally:
            if crew.watch_state(PROJECT)["watcher_live"]:
                recovery.unwatch(PROJECT)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if not crew.watch_state(PROJECT)["watcher_live"]:
                        break
                    time.sleep(0.05)


def _sweep() -> dict:
    with _armed_fleet():
        return resumption.sweep(PROJECT, config=CONFIG)


def _scope_line(advice: str) -> str:
    from reckon.crew.recovery_review_dispatch import REPAIR_ADVICE_SCOPE_LINE

    for line in advice.splitlines():
        if line.startswith(REPAIR_ADVICE_SCOPE_LINE):
            return line
    return ""


def _commit_a_change_to(path: str, repo: Path) -> str:
    """Commit a change to one path in the reviewed run's repository."""
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "add", path], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "fix: answer the finding"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_a_round_grants_the_path_its_finding_names(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """A finding outside the run's write paths lands on the claim as a grant."""
    config_home, repo, head_sha = isolated_project
    dispatched_record = _completed_pointer(config_home, repo, write_paths=[DECLARED])
    assert GRANTED not in dispatched_record["node"]["write_paths"]
    _store_review(head_sha, [FINDING])
    resumed = _stub_resume(monkeypatch)
    _stub_dispatch(monkeypatch)

    _sweep()

    assert len(resumed) == 1
    assert GRANTED in resumed[0]["advice"]
    pointer = runs.read_pointer(RUN_ID)
    assert pointer["node"]["write_paths"] == [DECLARED, GRANTED]
    grants = declared_write_grants(pointer)
    assert {
        "path": GRANTED,
        "source": "repair-round",
        "reason": repair.finding_id(FINDING),
    } in grants
    assert {"path": DECLARED, "source": "dispatch", "reason": ""} in grants


def test_promotion_accepts_a_head_that_changed_the_granted_path(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """The granted path is in the declared scope promotion measures, so it passes."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo, write_paths=[DECLARED])
    _store_review(head_sha, [FINDING])
    _stub_resume(monkeypatch)
    _stub_dispatch(monkeypatch)

    _sweep()
    work_sha = _commit_a_change_to(GRANTED, repo)
    record = runs.read_pointer(RUN_ID)

    products = _landing_scope_products(
        RUN_ID,
        record,
        shadow=False,
        node=record["node"],
        commits=[work_sha],
        accepted_paths=None,
    )

    assert products["scope_acceptances"] == []


def test_a_peer_held_path_is_not_granted_and_the_advice_names_the_peer(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """A claim the peer holds refuses the grant; the advice drops and names it."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo, write_paths=[DECLARED])
    _peer_pointer(repo, write_paths=[GRANTED])
    _store_review(head_sha, [FINDING])
    resumed = _stub_resume(monkeypatch)
    _stub_dispatch(monkeypatch)

    _sweep()

    assert len(resumed) == 1
    advice = resumed[0]["advice"]
    pointer = runs.read_pointer(RUN_ID)
    assert GRANTED not in pointer["node"]["write_paths"]
    assert [
        grant for grant in declared_write_grants(pointer) if grant["path"] == GRANTED
    ] == []
    assert PEER_RUN_ID in advice
    assert GRANTED not in _scope_line(advice)
