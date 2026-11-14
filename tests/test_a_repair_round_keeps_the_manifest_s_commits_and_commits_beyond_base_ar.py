"""A rewritten repair manifest cannot erase the run's committed history."""

import shlex

import pytest

from reckon.crew import promotion, recovery_classification, recovery_watch, reports
from tests.helpers.git import git


@pytest.fixture
def repaired_run(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    tree = tmp_path / "repository"
    tree.mkdir()
    git(tree, "init", "-q")
    git(tree, "config", "user.name", "Test User")
    git(tree, "config", "user.email", "test@example.invalid")
    target = tree / "result.txt"
    target.write_text("base\n")
    git(tree, "add", "result.txt")
    git(tree, "commit", "-qm", "test: seed history", "-m", "Establish the run base.")
    base = git(tree, "rev-parse", "HEAD")
    manifest = tmp_path / "manifest.md"
    commits = []
    for text in ("implementation", "repair"):
        target.write_text(text + "\n")
        git(tree, "add", "result.txt")
        git(tree, "commit", "-qm", f"test: {text}", "-m", "Record the result.")
        commits.append(git(tree, "rev-parse", "HEAD"))
        manifest.write_text(
            "node: repaired-run\nstatus: complete\n"
            f"commits: {commits[0] if len(commits) == 1 else ''}\n"
        )
    record = {
        "run_id": "r-repaired-history",
        "project": "repair-history",
        "node": {"id": "repaired-run", "time_budget": "20m"},
        "phase": "complete",
        "worktree": str(tree),
        "base_sha": base,
        "manifest_path": str(manifest),
        "process_alive": False,
    }
    assert reports.parse_manifest(manifest.read_text())["commits"] == []
    assert git(tree, "rev-list", "--count", f"{base}..HEAD") == "2"
    return record, commits


def test_promotion_offers_every_commit_after_a_repair(repaired_run):
    record, commits = repaired_run
    command = recovery_classification._promotion_command(record["run_id"], record, [])
    words = shlex.split(command)
    presented = [
        words[index + 1] for index, word in enumerate(words) if word == "--commit"
    ]
    assert presented == commits


def test_promotion_defaults_to_history_after_a_repair(repaired_run):
    record, commits = repaired_run
    _, presented = promotion._default_gate_evidence_from_manifest(
        record, verdict="passed", gate_check=None, commits=()
    )
    assert list(presented) == commits


def test_completed_repair_reports_its_commits_beyond_base(repaired_run):
    record, commits = repaired_run
    assert recovery_watch._commits_beyond_base(record) == len(commits)
    row = recovery_classification.classify_pointer(record)
    assert row["commits_beyond_base"] == len(commits)
