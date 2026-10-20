"""A review round carrying many findings opens exactly one repair round.

A review *round* is the reviewed run at the head the review read, and its work
is one node: every blocking finding of the record is answered in one turn rather
than opening a repair per finding. This test drives a stored review that carries
three blocking findings and one follow-on through the reflex for an unpromoted
run whose worker has exited, and observes one repair round opened with the
reviewed run resumed once, its advice naming all three blocking findings and not
the follow-on. A second case retries a resume that ended without answering and
finds the retry recorded as another attempt of the *same* round rather than a
second round.

The finding ids are asserted against an expectation this file derives from each
finding's own file, line and text, so a case cannot pass by agreeing with
whatever the module mints. The follow-on is asserted in the absent direction, so
advice that composed every finding — the mutation this node's check must fail
against — is caught rather than accepted.

The participant ports — the resume entry point and the dispatch — are both
stubbed, so no worker is launched and no call reaches a scheduler.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery, resumption, runs
from reckon.crew import review as review_module
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS

pytestmark = pytest.mark.arms_watch_producer

PROJECT = "sample"
RUN_ID = "r-unpromoted"
NODE_ID = "a-reviewed-node"

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

# Three blocking findings naming repository source paths, so the round composes
# a repair whose advice must name all three. The severities are declared on the
# findings, so each is read as the kind it states rather than left to default.
BLOCKING_FINDINGS: list[dict[str, str]] = [
    {
        "file": "reckon/crew/alpha.py",
        "line": "10",
        "text": "the guard never fires",
        "severity": review_module.BLOCKING_FINDING_SEVERITY,
    },
    {
        "file": "reckon/crew/beta.py",
        "line": "42",
        "text": "the retry count is off by one",
        "severity": review_module.BLOCKING_FINDING_SEVERITY,
    },
    {
        "file": "reckon/crew/gamma.py",
        "line": "7",
        "text": "the None case is unhandled",
        "severity": review_module.BLOCKING_FINDING_SEVERITY,
    },
]

# One declared follow-on: recorded on the review, not commissioned as work. It
# names a repository source path, so a composer sweeping every finding would
# carry it into the advice and the case's absent-direction assertions would fail.
FOLLOW_ON_FINDING: dict[str, str] = {
    "file": "reckon/crew/later.py",
    "line": "3",
    "text": "a tidy-up left for later",
    "severity": "follow-on",
}


def _expected_id(finding: dict[str, str]) -> str:
    """The finding id the documented rule yields, re-derived here as the oracle."""
    material = "\x00".join(
        (finding["file"].strip(), finding["line"].strip(), finding["text"].strip())
    )
    return "f" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]


@pytest.fixture()
def isolated_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path, str]:
    """A project whose review store, ledger and repo all live under a temp root."""
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
        '<h2 id="s2">One repair round per review round</h2>',
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
    config_home: Path, repo: Path, *, role: str = "implement"
) -> dict:
    """The reviewed run: an exited implement run whose live pointer still stands.

    ``process_alive`` is False and the run records no pid, so the reflex reads
    its worker as exited. The node declares no write path, so the reviewed run
    holds no claim that could itself decide the reflex's path.
    """
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
        "role": role,
        "node": {"id": NODE_ID, "plan": "fixture", "section": "s2"},
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


def _write_resume_stream(*, finished: bool = True) -> None:
    """Write the resume stream a real resume leaves behind.

    An assistant record appears the moment a turn is under way and the result
    line when it ends; ``finished`` matches the shape a completed turn leaves,
    which must not read as work still in progress.
    """
    directory = Path(runs.run_dir(RUN_ID))
    directory.mkdir(parents=True, exist_ok=True)
    records = [{"type": "assistant", "message": {"content": "working"}}]
    if finished:
        records.append({"type": "result", "subtype": "success"})
    (directory / "resume-1.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )


def _stub_resume(monkeypatch, *, finished: bool = True) -> list[dict]:
    """Replace the resume entry point, writing the stream a real resume leaves."""
    calls: list[dict] = []

    def fake_resume(run_id, record, *, config=None, launcher=None, advice=""):
        calls.append({"run_id": run_id, "advice": advice, "launcher": launcher})
        _write_resume_stream(finished=finished)
        return {"pid": os.getpid(), "turn": 1, "log_path": "resume-1.jsonl"}

    monkeypatch.setattr(resumption, "_resume", fake_resume)
    return calls


def _stub_dispatch(monkeypatch) -> list[dict]:
    """Replace the dispatch so any node launch is recorded rather than run."""
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    calls: list[dict] = []

    def fake_dispatch(**kwargs):
        calls.append(kwargs)
        return {"run_id": "r-whatever"}

    monkeypatch.setattr(dispatch_module, "dispatch", fake_dispatch)
    return calls


def _wait_for_stopped_producer() -> None:
    deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
    while time.monotonic() < deadline:
        if not crew.watch_state(PROJECT)["watcher_live"]:
            return
        time.sleep(0.05)
    pytest.fail("watch producer did not release its seat")


@contextmanager
def _armed_fleet():
    """Hold the follower claim the sweep is gated on, then release the watch."""
    with runs.follower_claim(PROJECT, "session-orchestrating", delivery="stream"):
        try:
            yield
        finally:
            if crew.watch_state(PROJECT)["watcher_live"]:
                recovery.unwatch(PROJECT)
                _wait_for_stopped_producer()


def _sweep() -> dict:
    with _armed_fleet():
        return resumption.sweep(PROJECT, config=CONFIG)


def test_one_round_resumes_once_naming_every_blocking_finding(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """The three blocking findings ride one resume; the follow-on rides none."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, [*BLOCKING_FINDINGS, FOLLOW_ON_FINDING])
    resumed = _stub_resume(monkeypatch)
    dispatched = _stub_dispatch(monkeypatch)

    _sweep()

    # Exactly one resume of the reviewed run, naming every blocking finding by
    # its content-derived id and its location, and naming the follow-on nowhere.
    assert len(resumed) == 1
    assert resumed[0]["run_id"] == RUN_ID
    advice = resumed[0]["advice"]
    for finding in BLOCKING_FINDINGS:
        assert _expected_id(finding) in advice
        assert finding["file"] in advice
        assert finding["text"] in advice
    assert _expected_id(FOLLOW_ON_FINDING) not in advice
    assert FOLLOW_ON_FINDING["file"] not in advice
    assert FOLLOW_ON_FINDING["text"] not in advice

    # No node was dispatched for the round: the findings ride the resume.
    assert dispatched == []

    # Exactly one repair round is opened for the review round, not one per
    # finding, and it is recorded as resumed with the composer's own id.
    pointer = runs.read_pointer(RUN_ID)
    rounds = pointer["repair_rounds"]
    assert rounds["count"] == 1
    assert rounds["round_id"] == pointer["repair_dispatch"]["round_id"]
    assert pointer["repair_dispatch"]["status"] == "resumed"
    assert pointer["repair_dispatch"]["attempt"] == 1


def test_a_retried_resume_is_another_attempt_of_the_same_round(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """A resume that ends without answering is retried as the same round.

    The first resume leaves the finished stream every completed turn leaves, so
    the run reads free again and the next sweep resumes once more. The retry is
    another attempt of the round in hand: its round id is unchanged and the
    run's opened-round count stays at one, so a retry is never a second round.
    """
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, [*BLOCKING_FINDINGS, FOLLOW_ON_FINDING])
    resumed = _stub_resume(monkeypatch)
    _stub_dispatch(monkeypatch)

    _sweep()
    first = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert first["status"] == "resumed"
    assert first["attempt"] == 1
    assert first["round_id"]

    _sweep()

    assert len(resumed) == 2
    pointer = runs.read_pointer(RUN_ID)
    second = pointer["repair_dispatch"]
    assert second["status"] == "resumed"
    assert second["attempt"] == 2
    assert second["round_id"] == first["round_id"]
    assert pointer["repair_rounds"]["count"] == 1
    assert pointer["repair_rounds"]["round_id"] == first["round_id"]
