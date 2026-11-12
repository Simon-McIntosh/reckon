"""A finding on a file the reviewed run itself holds is work, not a record.

A review that raises a blocking finding against the run's own work — an evidence
fragment written one directory deeper than its declared path, a suite arm
recorded as prose in the run's manifest — reports a defect its worker can fix.
The round therefore opens on anything the run itself holds, and declines only
when a blocking finding cites nothing its worker can change.

These cases drive the reflex through the sweep with the resume stubbed, each one
against a temporary repository and crew home. The first cites a file that exists
only in the reviewed run's worktree, the second cites the run's own manifest
under the crew home, and the third cites a path outside both the repository and
the run's directory, which is the one case a round may still decline. The
negative control resolves finding paths against the main checkout alone and
requires the first case to decline there, so the check fails if the worktree is
never consulted.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery, repair, resumption, runs
from reckon.crew import review as review_module
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS

# The reflex is gated by the watch admission, so these tests arm the producer
# the gate reads rather than accepting the suite-wide waiver.
pytestmark = pytest.mark.arms_watch_producer

PROJECT = "sample"
RUN_ID = "r-work"
NODE_ID = "a-reviewed-node"

# The file exists in the reviewed run's worktree and nowhere else: it is what its
# worker wrote one directory away from the path its brief declared.
WORKTREE_ONLY = "docs/evidence/fragments/fixture/deeper/landed.html"

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


def _finding(path: str, line: str, text: str, severity: str = "blocking") -> dict:
    return {"file": path, "line": line, "text": text, "severity": severity}


@pytest.fixture()
def isolated_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path, str]:
    """A project whose review store, ledger, repo and worktree sit under a temp root."""
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
        '<h2 id="s2">A finding-bearing review dispatches its repair</h2>',
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

    worktree = tmp_path / "worktrees" / f"session-orchestrating-{NODE_ID}"
    worktree.parent.mkdir(parents=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "--detach",
            str(worktree),
            base_sha,
        ],
        check=True,
        capture_output=True,
    )
    (worktree / Path(WORKTREE_ONLY).parent).mkdir(parents=True)
    (worktree / WORKTREE_ONLY).write_text("<p>landed</p>\n", encoding="utf-8")

    def prepare_worktree(_repo: Path, session: str, node: str, base: str) -> dict:
        return {"path": str(worktree), "base": base, "base_sha": base_sha}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    return config_home, repo, worktree, base_sha


def _completed_pointer(
    config_home: Path, repo: Path, worktree: Path, **extra: object
) -> dict:
    """The reviewed run: a completed implement run whose manifest reports completion."""
    run_dir = Path(runs.run_dir(RUN_ID))
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = run_dir / "manifest.md"
    manifest.write_text(
        "node: " + RUN_ID + "\nstatus: complete\ncommits: " + RUN_ID + "\n",
        encoding="utf-8",
    )
    record = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(worktree),
        "role": "implement",
        "node": {
            "id": NODE_ID,
            "plan": "fixture",
            "section": "s2",
            "write_paths": ["reckon/queue.py"],
        },
        "backend": "alpha",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    }
    record.update(extra)
    crew._write_json(crew.pointer_path(RUN_ID), record)
    return record


def _store_review(head_sha: str, findings: list[dict]) -> None:
    scored = dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15)
    review_module.store_review(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN_ID,
            "reviewed_base_sha": head_sha,
            "reviewed_head_sha": head_sha,
            "status": "parsed",
            "scores": scored,
            "total": sum(scored.values()),
            "findings": findings,
        }
    )


def _wait_for_stopped_producer() -> None:
    deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
    while time.monotonic() < deadline:
        if not crew.watch_state(PROJECT)["watcher_live"]:
            return
        time.sleep(0.05)
    pytest.fail("watch producer did not release its seat")


@contextmanager
def _armed_fleet():
    with runs.follower_claim(PROJECT, "session-orchestrating", delivery="stream"):
        try:
            yield
        finally:
            if crew.watch_state(PROJECT)["watcher_live"]:
                recovery.unwatch(PROJECT)
                _wait_for_stopped_producer()


@contextmanager
def _stubbed_resume(monkeypatch):
    calls: list[dict] = []

    def fake_resume(run_id, record, *, config=None, launcher=None, advice=""):
        calls.append({"run_id": run_id, "advice": advice})
        return {"pid": os.getpid(), "turn": 1, "log_path": "resume-1.jsonl"}

    monkeypatch.setattr(resumption, "_resume", fake_resume)
    yield calls


def test_a_file_only_in_the_runs_worktree_opens_its_repair(
    isolated_project: tuple[Path, Path, Path, str], monkeypatch
) -> None:
    """A blocking finding citing a worktree-only file resumes the run with it."""
    config_home, repo, worktree, head_sha = isolated_project
    _completed_pointer(config_home, repo, worktree)
    _store_review(
        head_sha,
        [_finding(WORKTREE_ONLY, "4", "the landed record sits one directory deep")],
    )
    assert not (repo / WORKTREE_ONLY).exists(), (
        "the fixture must keep the cited file out of the repository"
    )
    assert (worktree / WORKTREE_ONLY).is_file()

    with _stubbed_resume(monkeypatch) as calls, _armed_fleet():
        resumption.sweep(PROJECT, config=CONFIG)

    assert len(calls) == 1, calls
    assert WORKTREE_ONLY in calls[0]["advice"]
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "resumed"


def test_a_finding_on_the_runs_manifest_opens_its_repair(
    isolated_project: tuple[Path, Path, Path, str], monkeypatch
) -> None:
    """A blocking finding citing the run's own manifest is work, not a record."""
    config_home, repo, worktree, head_sha = isolated_project
    record = _completed_pointer(config_home, repo, worktree)
    manifest = Path(record["manifest_path"])
    assert manifest.is_file()
    _store_review(
        head_sha,
        [
            _finding(
                str(manifest),
                "9",
                "the suite arms are recorded as prose rather than as the keys",
            )
        ],
    )

    with _stubbed_resume(monkeypatch) as calls, _armed_fleet():
        resumption.sweep(PROJECT, config=CONFIG)

    assert len(calls) == 1, calls
    assert str(manifest) in calls[0]["advice"]
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "resumed"


def test_a_path_outside_the_repository_and_the_run_is_declined_and_named(
    isolated_project: tuple[Path, Path, Path, str], tmp_path: Path, monkeypatch
) -> None:
    """A finding citing nothing the run holds declines, and the reason names it."""
    config_home, repo, worktree, head_sha = isolated_project
    _completed_pointer(config_home, repo, worktree)
    elsewhere = tmp_path / "elsewhere" / "notes.md"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("nothing the run holds\n", encoding="utf-8")
    _store_review(
        head_sha, [_finding(str(elsewhere), "2", "this note is wrong somehow")]
    )

    with _stubbed_resume(monkeypatch) as calls, _armed_fleet():
        resumption.sweep(PROJECT, config=CONFIG)

    assert calls == []
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "decline-only"
    reason = recorded["reason"]
    assert str(elsewhere) in reason
    assert (
        repair.finding_id(
            {"file": str(elsewhere), "line": "2", "text": "this note is wrong somehow"}
        )
        in reason
    )


def test_negative_control_resolving_against_the_main_checkout_alone_declines(
    isolated_project: tuple[Path, Path, Path, str], monkeypatch
) -> None:
    """With resolution against the main checkout alone, the worktree case declines.

    This is the mutation the check is declared against: the same worktree-only
    finding, resolved against the repository the run was cut from rather than the
    tree the run wrote into. Nothing is found there, so the round must decline —
    which is what shows the first case passes because the worktree is consulted,
    not because every citation is accepted.
    """
    config_home, repo, worktree, head_sha = isolated_project
    _completed_pointer(config_home, repo, worktree)
    _store_review(
        head_sha,
        [_finding(WORKTREE_ONLY, "4", "the landed record sits one directory deep")],
    )

    def resolve_against_main_checkout(path: str, run_record) -> bool:
        cited = Path(str(path or "")).expanduser()
        return not cited.is_absolute() and (repo / cited).is_file()

    monkeypatch.setattr(
        repair, "finding_cites_run_own_file", resolve_against_main_checkout
    )

    with _stubbed_resume(monkeypatch) as calls, _armed_fleet():
        resumption.sweep(PROJECT, config=CONFIG)

    assert calls == []
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "decline-only"
    assert WORKTREE_ONLY in recorded["reason"]
