"""A landing-measured arm is read by its receipt, not by the log's last line.

A partitioned arm is submitted as a job whose script prints ``EXIT=<status>``
from its own trap. The scheduler writes lines of its own after that receipt —
``srun: error: … Exited with exit code 1`` — so a reader that requires the
receipt to be the log's last line judges a perfectly good red base arm
unreadable. That discarded the measured pair for every node that adds a test,
because such a node's base arm is red by construction: its new test fails where
the change is absent.

The fixtures are the tails of two real base logs, from the landings of
``worktree-leaves-shared-config`` and ``split-queue-admission``. Both end with
``EXIT=1`` on one line and an ``srun: error`` line after it.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import landing_measure, promotion_records, queue_landing, runs

SUITE = "python -m pytest -q tests/test_sample.py"

NODE_TEST = "tests/test_a_worktree_leaves_the_shared_config_alone.py"

RED_IDS = (
    f"{NODE_TEST}::test_existing_setting_leaves_the_shared_config_alone",
    f"{NODE_TEST}::test_absent_setting_is_written_once",
)

#: Two failure ids and the receipt the arm's trap prints, above the scheduler's
#: own line — the tail of the `worktree-leaves-shared-config` base log.
RED_BASE_TAIL = "\n".join(
    [
        f"FAILED {RED_IDS[0]}",
        f"FAILED {RED_IDS[1]}",
        "2 failed, 51 passed in 12.34s",
        "EXIT=1",
        "srun: error: 98dci4-clu-3141: task 0: Exited with exit code 1",
        "",
    ]
)

#: A green head arm, as the `worktree-leaves-shared-config` after log ends.
GREEN_HEAD_TAIL = "55 passed in 9.87s\nEXIT=0\n"

#: A log that ran the suite and reported its failures, but carries no receipt:
#: the job ended before its trap could print one.
NO_RECEIPT_TAIL = "\n".join(
    [f"FAILED {RED_IDS[0]}", f"FAILED {RED_IDS[1]}", "2 failed, 51 passed in 12.3s", ""]
)


def _git(repo: Path, *arguments: str) -> str:
    finished = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return finished.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    """A repository with a base commit and one commit beyond it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "worker")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_sample.py").write_text(
        "def test_existing():\n    assert True\n", encoding="utf-8"
    )
    _git(repo, "add", "tests/test_sample.py")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "head_only.py").write_text("# delivered change\n", encoding="utf-8")
    _git(repo, "add", "head_only.py")
    _git(repo, "commit", "-q", "-m", "head")
    head_sha = _git(repo, "rev-parse", "HEAD")
    return repo, base_sha, head_sha


def _record(
    tmp_path: Path, repo: Path, base_sha: str, after_revision: str
) -> tuple[str, dict[str, Any]]:
    """A run whose after arm was recorded behind its head.

    The after arm sits at an earlier revision, so the pair the run recorded is
    unusable and the landing measures both arms itself. That is the path a
    partitioned measurement takes, and the path a receipt is read on.
    """
    run_id = "r-test-measured-arm-receipt"
    arm = {
        "revision": after_revision,
        "command": SUITE,
        "exit_status": 0,
        "completed": True,
        "failure_count": 0,
        "failure_ids": [],
        "log_path": "/scratch/after.log",
    }
    baseline = dict(arm, revision=base_sha)
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "\n".join(
            [
                "node: node",
                "status: complete",
                f"tests: {SUITE}",
                f"baseline_suite: {json.dumps(baseline)}",
                f"after_suite: {json.dumps(arm)}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    record = {
        "run_id": run_id,
        "project": "demo",
        "role": "implement",
        "repo": str(repo),
        "worktree": str(repo),
        "base_sha": base_sha,
        "head_sha": base_sha,
        "suite_command": SUITE,
        "manifest_path": str(manifest),
        "baseline_suite": baseline,
        "after_suite": arm,
    }
    pointer = runs.pointer_path(run_id)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps(record), encoding="utf-8")
    return run_id, record


@pytest.fixture(name="crew_home")
def crew_home_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "crew-home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setattr(
        landing_measure, "worker_scratch_root", lambda: tmp_path / "scratch"
    )
    monkeypatch.setattr(
        queue_landing,
        "_measure_config",
        lambda _record: {"landing": {"measure_partition": "all_debug"}},
    )
    monkeypatch.setattr(queue_landing, "_measure_timeout", lambda _record: 120.0)
    return home


@pytest.fixture(name="scheduler")
def scheduler_fixture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the scheduler below the receipt reader, so the reader still runs.

    ``partition_submission`` is stubbed only because it would otherwise inspect
    this host's SLURM configuration; the submission and the log it names are
    what a real arm leaves behind, and the receipt parsing that the test is
    about runs unchanged.
    """
    state: dict[str, Any] = {"logs": [], "arms": [], "calls": 0}
    real_run = subprocess.run

    def fake_submission(*, log_path, **_options):
        state["logs"].append(Path(log_path))
        return "all_debug", ["sbatch", "--wait"], {}

    def fake_run(argv, **options):
        # The module's own git calls go through the same name the arm does, so
        # everything that is not the submitted job is left to the real runner.
        if not argv or argv[0] != "sbatch":
            return real_run(argv, **options)
        log = state["logs"].pop(0)
        tail, returncode = state["arms"].pop(0)
        state["calls"] += 1
        log.write_text(tail, encoding="utf-8")
        return subprocess.CompletedProcess(argv, returncode, tail, "")

    monkeypatch.setattr(landing_measure, "partition_submission", fake_submission)
    monkeypatch.setattr(landing_measure, "_RUN", fake_run)
    return state


def _measure(
    repo: Path, base_sha: str, head_sha: str, logs_dir: Path
) -> dict[str, Any]:
    return landing_measure.armed_observations(
        command=SUITE,
        tree=repo,
        base_tree=repo,
        base_sha=base_sha,
        head_sha=head_sha,
        logs_dir=logs_dir,
        timeout=120.0,
        config={"landing": {"measure_partition": "all_debug"}},
    )


def test_a_red_base_arm_is_read_past_the_scheduler_line(tmp_path, crew_home, scheduler):
    """The receipt yields its status and its ids although the job printed more."""
    repo, base_sha, head_sha = _repository(tmp_path)
    scheduler["arms"][:] = [(RED_BASE_TAIL, 1), (GREEN_HEAD_TAIL, 0)]

    measured = _measure(repo, base_sha, head_sha, tmp_path / "armed")

    assert "unreadable" not in measured
    baseline = measured["baseline_suite"]
    assert baseline["exit_status"] == 1
    assert tuple(baseline["failure_ids"]) == RED_IDS
    assert baseline["failure_count"] == 2
    assert measured["after_suite"]["exit_status"] == 0
    assert measured["after_suite"]["failure_ids"] == []


def test_a_measured_red_base_and_green_head_land_with_no_added_failure(
    tmp_path, crew_home, scheduler, monkeypatch
):
    """The pair lands, with zero added failures, and is written on the run."""
    repo, base_sha, head_sha = _repository(tmp_path)
    superseded = _git(repo, "rev-parse", "HEAD~1")
    run_id, record = _record(tmp_path, repo, base_sha, superseded)
    scheduler["arms"][:] = [(RED_BASE_TAIL, 1), (GREEN_HEAD_TAIL, 0)]
    monkeypatch.setattr(promotion_records, "_run_head", lambda _record: head_sha)

    delta = promotion_records._evaluate_suite_delta(run_id, record, waiver_reason="")

    assert delta["status"] == "clean"
    assert delta["measured_by"] == "landing"
    assert delta["added_failure_ids"] == []
    assert delta["baseline_suite"]["exit_status"] == 1
    assert set(delta["baseline_suite"]["failure_ids"]) == set(RED_IDS)
    stored = json.loads(
        (runs.run_dir(run_id) / "landing-measure" / "measured-arms.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["head"] == head_sha
    assert stored["baseline_suite"]["measured_by"] == "landing"
    assert set(stored["baseline_suite"]["failure_ids"]) == set(RED_IDS)
    pointer = json.loads(runs.pointer_path(run_id).read_text(encoding="utf-8"))
    assert pointer["suite_measured_arms"]["after_suite"]["revision"] == head_sha


def test_a_log_with_no_receipt_names_the_arm_and_its_missing_receipt(
    tmp_path, crew_home, scheduler
):
    """Test output with no EXIT line yields no pair, and says why."""
    repo, base_sha, head_sha = _repository(tmp_path)
    scheduler["arms"][:] = [(NO_RECEIPT_TAIL, 1), (GREEN_HEAD_TAIL, 0)]

    measured = _measure(repo, base_sha, head_sha, tmp_path / "armed")

    assert "baseline_suite" not in measured
    refusal = measured["unreadable"]
    assert "baseline" in refusal
    assert "carries no exit receipt" in refusal
    assert "did not run" not in refusal


def test_a_receipt_disagreeing_with_the_job_names_both_statuses(
    tmp_path, crew_home, scheduler
):
    """A receipt the scheduler contradicts yields no pair and names both."""
    repo, base_sha, head_sha = _repository(tmp_path)
    scheduler["arms"][:] = [(RED_BASE_TAIL, 0), (GREEN_HEAD_TAIL, 0)]

    measured = _measure(repo, base_sha, head_sha, tmp_path / "armed")

    assert "baseline_suite" not in measured
    refusal = measured["unreadable"]
    assert "baseline" in refusal
    assert "reported exit 1" in refusal
    assert "returned 0" in refusal
    assert "did not run" not in refusal


def test_a_receipt_read_only_as_the_last_line_loses_the_red_base(
    tmp_path, crew_home, scheduler, monkeypatch
):
    """The declared mutation, asserted here so its effect stays visible.

    Reading the receipt only as the log's last line makes the red base arm
    unreadable, which is the defect this node repairs; the assertion is the
    one the repair must keep true.
    """
    monkeypatch.setattr(
        landing_measure, "_EXIT_RECEIPT", re.compile(r"^EXIT=(\d+)\n?\Z", re.MULTILINE)
    )
    repo, base_sha, head_sha = _repository(tmp_path)
    scheduler["arms"][:] = [(RED_BASE_TAIL, 1), (GREEN_HEAD_TAIL, 0)]

    measured = _measure(repo, base_sha, head_sha, tmp_path / "armed")

    assert "baseline_suite" not in measured
    assert "baseline" in measured["unreadable"]
