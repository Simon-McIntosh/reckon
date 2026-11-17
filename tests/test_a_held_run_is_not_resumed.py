"""A station pass leaves a run under a coordinator's delivery hold alone.

A coordinator holds a delivered run when its head has to land through the
coordinator's own merge. The hold lived only in the run's live pointer, and the
station's repair pass never read it: it resumed the held run, the resumed
worker committed, and the station reviewed the new head -- a delivery and a
review nobody asked for, on a run the coordinator had taken by hand. The
pointer is deleted by the promotion that ends a run's life, so the hold left no
record that the run ever waited; the delay had to be reconstructed from a
coordinator's session.

These cases drive the station's own entry point over a temporary repository and
crew home and read three things from it: the attempt reservations the run's
directory carries, the reviews the sweep reports dispatching for the run, and
the hold record the run's directory keeps after a hold and its release. The
first case's negative control is the base pass: with the guard removed it
reserves a resume for the held run, so the absence asserted here is one the
instrument shows can be present.

A resume's own hold is the other shape: it names the head it was issued
against, and once the run delivers a new head there is nothing left to defer,
so the pass acts exactly as it does at base.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew import resume as resume_module
from reckon.crew import review as review_module

PROJECT = "sample"
RUN_ID = "r-held-run"
NODE_ID = "a-held-node"


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
}


def _git(tree: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class Harness:
    """A temporary repository, worktree and crew home for one case."""

    def __init__(self, root: Path, monkeypatch) -> None:
        self.root = root
        self.home = root / "config"
        self.home.mkdir()
        monkeypatch.setenv("RECKON_HOME", str(self.home))
        self.repo = root / "repo"
        plans = self.repo / "docs" / "plans"
        plans.mkdir(parents=True)
        (plans / "fixture.html").write_text(
            '<meta name="docs-project" content="sample">'
            '<meta name="reckon-type" content="plan">'
            '<meta name="plan-slug" content="fixture">'
            '<h2 id="s2">A held run is left alone</h2>',
            encoding="utf-8",
        )
        (self.repo / "reckon").mkdir()
        (self.repo / "reckon" / "thing.py").write_text("VALUE = 1\n", encoding="utf-8")
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "worker@example.invalid")
        _git(self.repo, "config", "user.name", "Worker")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "chore: seed")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        self.worktree = root / "worktree"
        _git(self.repo, "worktree", "add", "-q", str(self.worktree), self.head)
        (self.home / "mounts.json").write_text(
            json.dumps({PROJECT: str(self.repo / "docs")}), encoding="utf-8"
        )
        self.run_dir = Path(runs.run_dir(RUN_ID))
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def pointer(self, **extra) -> dict:
        """The delivered run: a completed implement run whose manifest agrees."""
        manifest = self.run_dir / "manifest.md"
        manifest.write_text(
            "node: " + NODE_ID + "\nstatus: complete\ncommits: " + self.head + "\n",
            encoding="utf-8",
        )
        record = {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(self.repo),
            "worktree": str(self.worktree),
            "role": "implement",
            "node": {"id": NODE_ID, "plan": "fixture", "section": "s2"},
            "backend": "alpha",
            "launch": "cli",
            "argv": ["codex"],
            "phase": "complete",
            "process_alive": False,
            "session": "session-orchestrating",
            "session_id": "sess-held-run",
            "manifest_path": str(manifest),
        }
        record.update(extra)
        runs._write_json(runs.pointer_path(RUN_ID), record)
        return record

    def deliver_new_head(self) -> str:
        """Commit once more in the run's worktree, as a resumed worker would."""
        (self.worktree / "reckon" / "thing.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )
        _git(self.worktree, "config", "user.email", "worker@example.invalid")
        _git(self.worktree, "config", "user.name", "Worker")
        _git(self.worktree, "add", "reckon/thing.py")
        _git(self.worktree, "commit", "-q", "-m", "fix: the reviewed finding")

        return _git(self.worktree, "rev-parse", "HEAD")

    def store_review(self, head: str, findings: list[dict], dispositions=None) -> None:
        record = {
            "project": PROJECT,
            "reviewed_run_id": RUN_ID,
            "reviewed_base_sha": head,
            "reviewed_head_sha": head,
            "status": "parsed",
            "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15),
            "total": 80,
            "findings": findings,
        }
        if dispositions is None:
            record["finding_dispositions"] = {}
        else:
            record["finding_dispositions"] = dispositions
        review_module.store_review(record)

    def record_a_finished_worker(self) -> None:
        """Record the run's worker as ended, as its supervisor would on exit.

        A resume rests on an observed end; a delivered run whose end nothing
        observed is refused rather than resumed, so the fixture records the
        receipt the supervisor writes when the worker it launched exits.
        """
        (self.run_dir / "exit.json").write_text(
            json.dumps(
                {
                    "run_id": RUN_ID,
                    "exit_code": 0,
                    "at": "2026-11-16T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )

    def reservations(self) -> list[str]:
        return sorted(path.name for path in self.run_dir.glob("attempt-*.reservation"))

    def hold_record(self) -> dict:
        path = self.run_dir / resume_module.HOLD_RECORD_NAME
        assert path.is_file(), f"the run directory records no hold at {path}"
        return json.loads(path.read_text(encoding="utf-8"))["delivery_hold"]


class Launcher:
    """A launcher that reports success without spawning a worker.

    An attempt reservation is taken before a launch and released once the
    worker exists, so it is observable only while the launcher runs: the record
    names the reservation the run directory held at the moment of the call.
    """

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs) -> int:
        self.calls.append(
            {
                "reservations": sorted(
                    path.name for path in self.run_dir.glob("attempt-*.reservation")
                )
            }
        )
        return os.getpid()


def _passes(launcher: Launcher, run: int = 3) -> list[dict]:
    """The station's repair and review passes, run over the live fleet.

    ``dispatch_awaiting_reviews`` is the station's own entry point and carries
    both: the review pass that composes a settled run's review and the repair
    pass that answers a review round's findings.
    """
    return [
        recovery.dispatch_awaiting_reviews(
            project=PROJECT,
            config=CONFIG,
            launcher=launcher,
            session="session-orchestrating",
        )
        for _ in range(run)
    ]


def _dispatched_reviews(reports: list[dict]) -> list[dict]:
    found = []
    for report in reports:
        for entry in report.get("reports") or ():
            if str(entry.get("run_id") or "") != RUN_ID:
                continue
            if entry.get("dispatched") or entry.get("review_run_id"):
                found.append(entry)
    return found


FINDINGS = [
    {
        "file": "reckon/thing.py",
        "line": "1",
        "text": "the module answers a stale value",
        "severity": "blocking",
    },
    {
        "file": "reckon/thing.py",
        "line": "1",
        "text": "a branch no test reaches",
        "severity": "blocking",
    },
]

# The recorded shape: the newest review's first blocking finding is disposed as
# folded into the node, and the round still carries an open blocking finding
# the base pass composes a resume for.
DISPOSITIONS = {"0": {"kind": "folded", "node": NODE_ID}}


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


def _reserved_attempts(launcher: Launcher) -> list[str]:
    """Every attempt reservation a launch was issued under, in launch order.

    The attempt number is the run directory's own count and is not the subject
    here: what the held run must not have is a reservation at all.
    """
    return [name for call in launcher.calls for name in call["reservations"]]


def test_a_held_run_is_not_resumed_reviewed_or_redispatched(harness: Harness) -> None:
    """Three station passes leave the held run as it was: no attempt, no review."""
    harness.pointer()
    harness.record_a_finished_worker()
    harness.store_review(harness.head, FINDINGS, DISPOSITIONS)
    reason = "the head can land only through the coordinator's own merge"
    resume_module.hold_delivery(RUN_ID, reason)
    launcher = Launcher(harness.run_dir)

    reports = _passes(launcher)

    assert launcher.calls == [], "the station launched a worker for the held run"
    assert harness.reservations() == []
    assert _dispatched_reviews(reports) == []
    report = recovery.dispatch_repair_for_run(
        runs.read_pointer(RUN_ID), config=CONFIG, launcher=launcher
    )
    assert report.get("held") is True, report
    assert launcher.calls == []
    hold = harness.hold_record()
    assert hold["reason"] == reason
    assert not hold.get("released_at")

    resume_module.release_hold(RUN_ID, "the coordinator merged the head")
    released = harness.hold_record()
    assert released["released_at"]
    assert released["release_reason"] == "the coordinator merged the head"
    _passes(launcher, 1)
    # The pass after the release acts as the base pass does: it composes the
    # round and resumes the run, taking its attempt reservation first.
    reserved = _reserved_attempts(launcher)
    assert len(reserved) == 1 and reserved[0].endswith(".reservation"), reserved


def test_a_resume_hold_stops_covering_a_newly_delivered_head(harness: Harness) -> None:
    """A head-scoped hold defers only the delivery it was issued against."""
    harness.pointer()
    harness.record_a_finished_worker()
    harness.store_review(harness.head, FINDINGS, DISPOSITIONS)
    resume_module.hold_delivery(
        RUN_ID, "a coordinator is resuming this delivered run", head=harness.head
    )
    new_head = harness.deliver_new_head()
    harness.store_review(new_head, FINDINGS, DISPOSITIONS)
    launcher = Launcher(harness.run_dir)
    assert harness.hold_record().get("head") == harness.head

    _passes(launcher)

    reserved = _reserved_attempts(launcher)
    assert len(reserved) == 1 and reserved[0].endswith(".reservation"), reserved
