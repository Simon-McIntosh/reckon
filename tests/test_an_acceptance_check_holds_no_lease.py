"""Acceptance leaves the promotion lease available until it is ready to write."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import promotion, resumption, runs
from reckon.crew import recovery_review_acceptance as acceptance
from tests.helpers.git import git
from tests.test_reflex_acceptance_reads_the_remote_primary import (
    PROJECT,
    RUN_ID,
    _land_run_commit,
    _store_review,
    _write_run,
)
from tests.test_reflex_acceptance_reads_the_remote_primary import (
    origin_and_repository as origin_and_repository,
)


@pytest.fixture()
def candidate(origin_and_repository, tmp_path):
    _origin, repository = origin_and_repository
    base, commit = _land_run_commit(repository, push=False)
    _write_run(repository, tmp_path, base=base, commit=commit)
    _store_review(base=base, head=commit)
    return repository, base, commit


def _sweep():
    return acceptance.dispatch_awaiting_reviews(
        project=PROJECT,
        session="",
        promotions=resumption.PromotionWorkers(width=1, wait_seconds=30),
    )


def _coordinator_complete(repository, commit):
    """A separate coordinator process must claim the real cross-host lease."""
    command = [
        sys.executable,
        "-c",
        "from reckon.cli import main; main()",
        "crew",
        "complete",
        "--run",
        RUN_ID,
        "--gate",
        "failed",
        "--failure-classification",
        "work-rejected",
        "--commit",
        commit,
        "--outcome",
        "the coordinator rejects the unmerged deliverable",
        "--checkout-path",
        str(repository),
    ]
    return subprocess.run(
        command,
        env={**os.environ, "PYTHONPATH": str(Path(acceptance.__file__).parents[2])},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_coordinator_completes_while_the_sweep_checks_unmerged_commits(
    candidate, monkeypatch
):
    repository, _base, commit = candidate
    checking, release = threading.Event(), threading.Event()
    original = acceptance._commits_missing_from

    def blocked(*args):
        checking.set()
        assert release.wait(30), "the test never released the commit check"
        return original(*args)

    monkeypatch.setattr(acceptance, "_commits_missing_from", blocked)
    result = {}
    worker = threading.Thread(target=lambda: result.update(_sweep()))
    worker.start()
    try:
        assert checking.wait(15), "the sweep never reached its commit check"
        completed = _coordinator_complete(repository, commit)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert not runs.pointer_path(RUN_ID).exists()
        rows = ledger.runs(PROJECT, root=repository)
        assert [row["run_id"] for row in rows] == [RUN_ID]
        assert rows[0]["gate"] == "failed"
    finally:
        release.set()
        worker.join(35)
    assert not worker.is_alive()
    assert result["accepted"] == []


def test_unchanged_refusal_is_skipped_and_a_new_head_is_judged(candidate, monkeypatch):
    repository, base, commit = candidate
    calls = []
    original = acceptance._commits_missing_from

    def counted(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(acceptance, "_commits_missing_from", counted)
    first = _sweep()
    assert first["reports"][-1]["unmerged_commits"] == [commit]
    _sweep()
    _sweep()
    assert len(calls) == 1

    (repository / "candidate.txt").write_text("revised\n")
    git(repository, "add", "candidate.txt")
    git(repository, "commit", "-q", "-m", "fix: revise the deliverable")
    head = git(repository, "rev-parse", "HEAD")
    _store_review(base=base, head=head)
    _sweep()
    assert len(calls) == 2


def test_a_changed_remote_releases_the_refusal_and_promotes(candidate):
    repository, _base, _commit = candidate
    first = _sweep()
    assert first["accepted"] == []
    git(repository, "push", "-q", "origin", "main")

    report = _sweep()

    assert report["accepted"] == [RUN_ID], report
    assert not runs.pointer_path(RUN_ID).exists()
    rows = ledger.runs(PROJECT, root=repository)
    assert [row["run_id"] for row in rows] == [RUN_ID]
    assert rows[0]["promoted_by"] == "review-acceptance"


def test_coordinator_completion_between_judgement_and_lease_is_not_repeated(
    candidate, monkeypatch
):
    repository, _base, commit = candidate
    git(repository, "push", "-q", "origin", "main")
    original = promotion._promotion_host_lease
    intercepted = []

    @contextmanager
    def coordinator_first(run_id):
        if not intercepted:
            intercepted.append(run_id)
            completed = _coordinator_complete(repository, commit)
            assert completed.returncode == 0, completed.stdout + completed.stderr
        with original(run_id) as lease:
            yield lease

    monkeypatch.setattr(promotion, "_promotion_host_lease", coordinator_first)
    report = _sweep()

    assert intercepted == [RUN_ID]
    assert report["accepted"] == [], report
    assert not runs.pointer_path(RUN_ID).exists()
    rows = ledger.runs(PROJECT, root=repository)
    assert [row["run_id"] for row in rows] == [RUN_ID]
    assert rows[0]["gate"] == "failed"
