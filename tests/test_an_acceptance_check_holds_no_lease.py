"""Acceptance leaves the promotion lease available until it is ready to write."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import landing_measure, merge, promotion, review, runs
from reckon.crew import recovery_review_acceptance as acceptance
from tests.helpers.git import git
from tests.helpers.landing_repository import in_place_scheduler
from tests.test_reflex_acceptance_reads_the_remote_primary import (
    PROJECT,
    RUN_ID,
    _land_run_commit,
    _store_review,
    _write_run,
)
from tests.test_reflex_acceptance_reads_the_remote_primary import (
    origin_and_repository as origin_and_repository,  # noqa: PLC0414
)

#: A gate that reports its own failure the way the reader of a measured arm
#: expects: a ``FAILED`` line naming an id, and a non-zero exit. The seed gate
#: greps quietly, and an arm that fails without naming what failed is recorded
#: as unreadable rather than as a failed gate.
FAILING_GATE = (
    "#!/bin/sh\n"
    'if grep -q "satisfied" node.txt; then exit 0; fi\n'
    'printf "%s\\n" "FAILED node.txt::satisfied"\n'
    "exit 1\n"
)


def _fixture_run(repository, tmp_path, *, push):
    """Write the run's pointer, manifest and clean review over its own head."""
    base, commit = _land_run_commit(repository, push=push)
    _write_run(repository, tmp_path, base=base, commit=commit)
    _store_review(base=base, head=commit)
    return base, commit


@pytest.fixture()
def candidate(origin_and_repository, tmp_path, isolated_reckon_home):
    """A reviewed implementation the primary branch already carries.

    Acceptance promotes this run on its own: its commits are on the branch the
    remote carries, so the gate the run recorded is re-run where the repository
    stands and the promotion takes the cross-host lease — the discipline every
    test holding a lease question here measures.
    """
    assert runs.crew_home().is_relative_to(isolated_reckon_home)
    real_pointer = Path.home() / ".config/reckon/crew/live" / f"{RUN_ID}.json"
    assert not real_pointer.exists()
    _origin, repository = origin_and_repository
    base, commit = _fixture_run(repository, tmp_path, push=True)
    yield repository, base, commit
    assert not real_pointer.exists()


@pytest.fixture()
def refusing_candidate(
    origin_and_repository, tmp_path, isolated_reckon_home, monkeypatch
):
    """A reviewed implementation whose delivered head breaks the gate it recorded.

    An implementation whose commits are still missing from the primary branch is
    an implementation acceptance lands itself, so it is measured at its merge
    candidate rather than refused. ``node.txt`` no longer says ``satisfied`` at
    the delivered head, so that measure fails and the run is refused before
    anything is written.
    """
    assert runs.crew_home().is_relative_to(isolated_reckon_home)
    real_pointer = Path.home() / ".config/reckon/crew/live" / f"{RUN_ID}.json"
    assert not real_pointer.exists()
    monkeypatch.setattr(landing_measure, "_RUN", in_place_scheduler())
    _origin, repository = origin_and_repository
    base, _commit = _land_run_commit(repository, push=False)
    (repository / "node.txt").write_text("broken\n", encoding="utf-8")
    (repository / "gate.sh").write_text(FAILING_GATE, encoding="utf-8")
    git(repository, "add", "node.txt", "gate.sh")
    git(repository, "commit", "-q", "-m", "test: break the gate the run recorded")
    head = git(repository, "rev-parse", "HEAD")
    _write_run(repository, tmp_path, base=base, commit=head)
    _store_review(base=base, head=head)
    yield repository, base, head, head
    assert not real_pointer.exists()


def _sweep():
    """One sweep, with each run's own outcome in the returned report.

    A sweep that hands its promotions to workers reports only those that
    finished inside its wait, and a worker still running keeps that run's claim
    for the next sweep to find in flight instead of judging it again. These
    tests ask about one run's own outcome, which the report carries when the
    promotion runs where the run is read.
    """
    return acceptance.dispatch_awaiting_reviews(project=PROJECT, session="")


def _coordinator_complete(repository, commit):
    """A separate coordinator process must claim the real cross-host lease."""
    command = [
        sys.executable,
        "-c",
        "from reckon.cli import main; main()",
        "crew",
        "complete",
        "--wait",
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
        timeout=120,
        check=False,
    )


def _refusal(report):
    return next(
        entry
        for entry in report["reports"]
        if entry.get("run_id") == RUN_ID and "accepted" in entry
    )


def _merged_gate_refused(report):
    """The refusal, when it is the merged-head gate that failed."""
    refusal = _refusal(report)
    assert refusal["accepted"] is False
    assert refusal["gate_report"]["integrated_verdict"] == "failed"
    return refusal


def test_coordinator_completes_while_the_sweep_checks_unmerged_commits(
    refusing_candidate, monkeypatch
):
    repository, _base, head, _commit = refusing_candidate
    checking, release = threading.Event(), threading.Event()
    original = acceptance._commits_missing_from

    def blocked(*args):
        checking.set()
        assert release.wait(150), "the test never released the commit check"
        return original(*args)

    monkeypatch.setattr(acceptance, "_commits_missing_from", blocked)
    result = {}
    worker = threading.Thread(target=lambda: result.update(_sweep()))
    worker.start()
    try:
        assert checking.wait(60), "the sweep never reached its commit check"
        completed = _coordinator_complete(repository, head)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert not runs.pointer_path(RUN_ID).exists()
        rows = ledger.runs(PROJECT, root=repository)
        assert [row["run_id"] for row in rows] == [RUN_ID]
        assert rows[0]["gate"] == "failed"
    finally:
        release.set()
        worker.join(160)
    assert not worker.is_alive()
    assert result["accepted"] == []


def test_unchanged_refusal_is_skipped_and_a_new_head_is_judged(
    refusing_candidate, monkeypatch
):
    repository, base, _head, _commit = refusing_candidate
    calls = []
    original = acceptance._commits_missing_from

    def counted(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(acceptance, "_commits_missing_from", counted)
    first = _sweep()
    assert _merged_gate_refused(first)["accepted"] is False
    assert "unmerged_commits" not in _refusal(first)
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


def test_a_commit_the_primary_branch_carries_is_promoted_without_a_measure(candidate):
    """A run the branch already carries is re-gated in place, then promoted.

    Acceptance measures a merge candidate only for work the branch does not
    carry. With the run's own commit on the branch the remote carries, the gate
    it recorded is re-run where the repository stands, and the promotion takes
    the cross-host lease and lands on the ledger with
    ``promoted_by: review-acceptance``.
    """
    repository, _base, _commit = candidate

    report = _sweep()

    assert report["accepted"] == [RUN_ID], report
    assert not runs.pointer_path(RUN_ID).exists()
    rows = ledger.runs(PROJECT, root=repository)
    assert [row["run_id"] for row in rows] == [RUN_ID]
    assert rows[0]["promoted_by"] == "review-acceptance"


def test_a_changed_stored_review_rejudges_the_same_head(
    refusing_candidate, monkeypatch
):
    repository, _base, head, _commit = refusing_candidate
    pointer = runs.read_pointer(RUN_ID)
    manifest = Path(pointer["manifest_path"])
    manifest_bytes = manifest.read_bytes()
    remote_head = git(repository, "ls-remote", "origin", "refs/heads/main")
    calls = []
    original = acceptance._commits_missing_from

    def counted(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(acceptance, "_commits_missing_from", counted)
    for _ in range(3):
        report = _sweep()
        assert report["accepted"] == []
        assert _merged_gate_refused(report)
    assert len(calls) == 1

    gate = _refusal(report)["gate_report"]
    candidate = gate["integrated_revision"]
    previous_log = Path(gate["log_path"])
    previous = (previous_log.read_bytes(), previous_log.stat().st_mtime_ns)
    # Identical parents must be rechecked independently of the merge commit's clock.
    monkeypatch.setattr(merge, "build_merge_commit", lambda *args, **options: candidate)

    path = review.review_path(PROJECT, RUN_ID, reviewed_head_sha=head)
    record = json.loads(path.read_text())
    record["scores"]["fit"] -= 1
    record["total"] -= 1
    review.store_review(record)
    assert git(repository, "rev-parse", "HEAD") == head
    assert git(repository, "ls-remote", "origin", "refs/heads/main") == remote_head
    assert manifest.read_bytes() == manifest_bytes

    report = _sweep()

    assert report["accepted"] == []
    assert _merged_gate_refused(report)
    assert len(calls) == 2
    assert git(repository, "rev-parse", "HEAD") == head
    current_gate = _refusal(report)["gate_report"]
    assert current_gate["integrated_revision"] == candidate
    assert Path(current_gate["log_path"]) != previous_log
    assert (previous_log.read_bytes(), previous_log.stat().st_mtime_ns) == previous


def test_missing_commits_reaching_primary_rejudges_the_same_head(
    refusing_candidate, monkeypatch
):
    """The remote moving is an input, so the same head is judged again.

    The run's own head does not move; the commits reach the branch the remote
    carries. That is one of the inputs a refusal is keyed on, so the memo is not
    reused and the run is judged again — and the delivered head still fails the
    gate it recorded, so it is refused again rather than promoted on the earlier
    judgement.
    """
    repository, _base, head, _commit = refusing_candidate
    pointer = runs.read_pointer(RUN_ID)
    manifest = Path(pointer["manifest_path"])
    manifest_bytes = manifest.read_bytes()
    path = review.review_path(PROJECT, RUN_ID, reviewed_head_sha=head)
    review_bytes = path.read_bytes()
    calls = []
    original = acceptance._commits_missing_from

    def counted(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(acceptance, "_commits_missing_from", counted)
    for _ in range(3):
        report = _sweep()
        assert report["accepted"] == []
        assert _merged_gate_refused(report)
    assert len(calls) == 1

    git(repository, "push", "-q", "origin", "main")
    assert git(repository, "rev-parse", "HEAD") == head
    assert manifest.read_bytes() == manifest_bytes
    assert path.read_bytes() == review_bytes

    report = _sweep()

    assert len(calls) == 2
    assert report["accepted"] == [], report
    assert _merged_gate_refused(report)
    assert runs.pointer_path(RUN_ID).exists()
    assert ledger.runs(PROJECT, root=repository) == []


def test_an_unmerged_implementation_is_measured_at_its_merge_candidate(
    refusing_candidate, monkeypatch
):
    """The implementation acceptance lands itself is gated at the merge candidate.

    An implementation whose commits are missing from the primary branch is not
    refused for that: acceptance builds the merge candidate, re-runs the gate the
    run recorded, and only then writes. The delivered head breaks that gate, so
    the run is left un-promoted with its pointer intact and no ledger row, and
    the gate ran as a job on the project's partition.
    """
    repository, _base, _head, _commit = refusing_candidate
    submissions = []
    transport = landing_measure._RUN

    def recording(argv, **options):
        submissions.append(list(argv))
        return transport(argv, **options)

    monkeypatch.setattr(landing_measure, "_RUN", recording)
    report = _sweep()

    assert report["accepted"] == [], report
    refusal = _refusal(report)
    assert "unmerged_commits" not in refusal
    assert refusal["gate_report"]["integrated_verdict"] == "failed"
    assert [
        argv
        for argv in submissions
        if argv[0] == "sbatch" and "--partition=all_debug" in argv
    ]
    assert runs.pointer_path(RUN_ID).exists()
    assert ledger.runs(PROJECT, root=repository) == []


@pytest.mark.parametrize("changed_input", ["review", "manifest", "gate_log"])
def test_changed_refusal_inputs_are_judged_again(
    refusing_candidate, monkeypatch, changed_input
):
    _repository, _base, head, _commit = refusing_candidate
    calls = []
    original = acceptance._commits_missing_from

    def counted(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(acceptance, "_commits_missing_from", counted)
    _sweep()
    pointer = runs.read_pointer(RUN_ID)
    manifest = Path(pointer["manifest_path"])
    if changed_input == "review":
        path = review.review_path(PROJECT, RUN_ID, reviewed_head_sha=head)
        record = json.loads(path.read_text())
        record["scores"]["fit"] -= 1
        record["total"] -= 1
        review.store_review(record)
    elif changed_input == "manifest":
        manifest.write_text(manifest.read_text() + "checkpoint: gate confirmed\n")
    else:
        path = manifest.parent.parent / "gate.log"
        assert path.is_file()
        path.write_text("gate confirmed\nEXIT=0\n")
    _sweep()
    assert len(calls) == 2


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


@pytest.mark.parametrize("changed_input", ["head", "attempt", "review"])
def test_a_changed_run_is_not_promoted_on_an_earlier_judgement(
    candidate, monkeypatch, changed_input
):
    repository, _base, commit = candidate
    git(repository, "push", "-q", "origin", "main")
    original = promotion._promotion_host_lease
    intercepted = []

    @contextmanager
    def change_before_claim(run_id):
        if not intercepted:
            intercepted.append(run_id)
            if changed_input == "head":
                (repository / "candidate.txt").write_text("unreviewed\n")
                git(repository, "add", "candidate.txt")
                git(repository, "commit", "-q", "-m", "fix: revise the deliverable")
            elif changed_input == "attempt":
                pointer = runs.read_pointer(RUN_ID)
                pointer["attempt"] = "resumed-attempt"
                runs._write_json(runs.pointer_path(RUN_ID), pointer)
            else:
                path = review.review_path(PROJECT, RUN_ID, reviewed_head_sha=commit)
                record = json.loads(path.read_text())
                record["findings"] = [{"claim": "hold the run", "severity": "blocking"}]
                review.store_review(record)
        with original(run_id) as lease:
            yield lease

    monkeypatch.setattr(promotion, "_promotion_host_lease", change_before_claim)
    print("DIAG ptr", runs.pointer_path(RUN_ID).exists(), runs.live_dir())
    print("DIAG live", [p.get("run_id") for p in runs.list_live(project=PROJECT)])
    report = _sweep()

    assert intercepted == [RUN_ID]
    assert report["accepted"] == [], report
    assert runs.pointer_path(RUN_ID).exists()
    assert ledger.runs(PROJECT, root=repository) == []
