"""A review round waits for the previous round's blocking finding to be answered.

The reflex dispatches a review for every run it classifies ``scoring``, and it
read nothing about the review the run already carried: a run resumed onto a new
head whose previous review still owed an answer got a fresh round certain to
repeat the same findings. The refusal reads the run's newest review through
``review_lifecycle.lifecycle`` — the one derivation of whether a stored review
is still ``open`` — and names each undisposed blocking finding with the
``crew dispose`` command that answers it, so the coordinator can lift the
refusal rather than lose a round to findings already known.

Every case drives the real dispatch against a real git history: the reviewed
run's pointer, its stored review and the reflex's own dispatch are the real
ones, and the worktree creator is captured so a refusal is shown to compose
nothing.
"""

from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path

import pytest

from reckon.crew import review as review_module
from reckon.crew import review_dispositions, runs
from reckon.crew import review_lifecycle as lifecycle_module

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "review-waits-for-its-repair"
SESSION = "coordinator-fixture"

LOCAL_BACKEND = "clive"

# The severity a finding states to block the node's landing, taken from the
# review parser's own vocabulary rather than invented here.
BLOCKING = review_module.BLOCKING_FINDING_SEVERITY


def _backend(command: str) -> dict:
    return {
        "launch": "cli",
        "command": command,
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
        "session_reuse": True,
        "time_budget": "25m",
    }


CONFIG = {
    "default_backend": LOCAL_BACKEND,
    "local_backend": LOCAL_BACKEND,
    "backends": {LOCAL_BACKEND: _backend("codex")},
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _commit(repository: Path, paths: dict[str, str], message: str) -> str:
    for name, body in paths.items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    subprocess.run(
        ["git", "add", *paths], cwd=repository, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A repository, an isolated crew home and a reviewed implement run."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repo = tmp_path / "repo"
    repo.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(repo, *arguments)
    _commit(repo, {"seed.txt": "seed\n"}, "chore: seed")
    reviewed = _commit(
        repo,
        {
            "pkg/mod.py": "VALUE = 1\n",
            "docs/plans/fixture-plan.html": (
                '<meta name="docs-project" content="' + PROJECT + '">'
                '<meta name="reckon-type" content="plan">'
                '<meta name="plan-slug" content="fixture-plan">'
                '<h2 id="s2">A review waits for its repair</h2>'
            ),
        },
        "feat: the reviewed change",
    )
    (config_home / "mounts.json").write_text(
        '{"' + PROJECT + '": "' + str(repo / "docs") + '"}'
    )
    monkeypatch.setattr(
        review_module,
        "review_store_root",
        lambda base_dir=None: config_home / "reviews",
    )
    return {
        "tmp": tmp_path,
        "config_home": config_home,
        "repo": repo,
        "reviewed": reviewed,
    }


def _write_run(world: dict, *, run_id: str, head: str) -> dict:
    """One complete, unpromoted implement run whose worktree is the repo."""
    repo = world["repo"]
    manifest = world["config_home"] / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n", encoding="utf-8"
    )
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repo),
        "worktree": str(repo),
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": run_id,
            "plan": "fixture-plan",
            "write_paths": ["pkg/mod.py"],
        },
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _store_review(
    run_id: str, *, base: str, head: str, finding: str | None = None
) -> dict:
    """Store a review of ``run_id`` at the revision it read.

    ``finding`` adds one blocking finding to the record, stated in the parser's
    own emitted grammar so the stored severity is the one a reviewer would have
    written rather than a field this test set by hand.
    """
    emitted = [
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    ]
    if finding is not None:
        emitted.append(f"FINDING pkg/mod.py:1 {BLOCKING}: {finding}")
    return review_module.store_review(
        {
            **review_module.parse_review("\n".join(emitted)),
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )


def _resumed_run(world: dict, run_id: str, *, finding: str | None) -> dict:
    """A run reviewed at one head and resumed onto a new source head.

    This is the §1 shape: the run moved past the head its review read, so the
    reflex sees it as awaiting review and would dispatch a fresh round —
    over a previous review still holding the finding.
    """
    reviewed = world["reviewed"]
    _write_run(world, run_id=run_id, head=reviewed)
    _store_review(run_id, base=reviewed, head=reviewed, finding=finding)
    _commit(world["repo"], {"pkg/mod.py": "VALUE = 2\n"}, "fix: a source move")
    return runs.read_pointer(run_id)


def _drive(
    world: dict, record: dict, monkeypatch: pytest.MonkeyPatch, captured: dict
) -> dict:
    """Run the real dispatch for one run, capturing any worktree it asked for."""
    repo = world["repo"]
    worktrees = world["tmp"] / "worktrees"

    def prepare(_repo, session, worker, base):
        path = worktrees / str(session) / str(worker)
        path.mkdir(parents=True, exist_ok=True)
        captured["worker"] = worker
        captured["path"] = str(path)
        return {
            "path": str(path),
            "base": base,
            "base_sha": _git(repo, "rev-parse", "HEAD"),
        }

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare)
    from reckon import crew
    from reckon.crew import recovery
    from reckon.crew.recovery_review_dispatch import dispatch_review_for_run

    try:
        with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
            return dispatch_review_for_run(
                record, config=CONFIG, launcher=lambda *a, **k: os.getpid()
            )
    finally:
        if crew.watch_state(PROJECT)["watcher_live"]:
            recovery.unwatch(PROJECT)


def _live_run_ids() -> list[str]:
    return [
        str(pointer.get("run_id") or "") for pointer in runs.list_live(project=PROJECT)
    ]


FINDING = "the fit ignores the converter's behaviour at DC, so the model is wrong"


def test_a_review_is_refused_while_a_blocking_finding_is_undisposed(
    world: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reflex refuses, names the finding and its dispose command, composes nothing."""
    record = _resumed_run(world, "r-undisposed", finding=FINDING)
    captured: dict = {}

    report = _drive(world, record, monkeypatch, captured)

    assert report["dispatched"] is False, report
    assert report.get("refused") is True, report
    reason = report["reason"]
    assert "blocking finding(s) with no disposition" in reason, reason
    assert "pkg/mod.py:1" in reason, reason
    assert "--finding 0" in reason, reason
    assert "--kind folded" in reason, reason
    assert "--kind exempted" in reason, reason
    assert "crew dispose" in reason, reason
    # No review round was composed: no worktree was asked for, and the only
    # live run is the one that was refused.
    assert captured == {}, captured
    assert _live_run_ids() == ["r-undisposed"], _live_run_ids()


def test_the_same_review_dispatches_once_the_finding_is_disposed(
    world: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disposing the finding through the named command lifts the refusal."""
    reviewed = world["reviewed"]
    record = _resumed_run(world, "r-disposed", finding=FINDING)
    review_dispositions.record_finding_disposition(
        PROJECT,
        "r-disposed",
        0,
        kind="folded",
        node="repair-node",
        reviewed_head_sha=reviewed,
    )
    captured: dict = {}

    report = _drive(world, record, monkeypatch, captured)

    assert report.get("refused") is not True, report.get("reason")
    assert report["dispatched"] is True, report.get("reason")
    assert captured.get("worker"), captured


def test_the_refusal_lifts_when_the_review_is_no_longer_open(
    world: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal reads ``_run_open``: stubbed shut, the same record dispatches.

    This is the proof that the refusal reads ``review_lifecycle``'s one
    derivation rather than a second one of its own: the finding is still
    undisposed, and only the derivation's answer changes.
    """
    record = _resumed_run(world, "r-not-open", finding=FINDING)
    monkeypatch.setattr(lifecycle_module, "_run_open", lambda _record: False)
    captured: dict = {}

    report = _drive(world, record, monkeypatch, captured)

    assert report.get("refused") is not True, report.get("reason")
    assert report["dispatched"] is True, report.get("reason")
