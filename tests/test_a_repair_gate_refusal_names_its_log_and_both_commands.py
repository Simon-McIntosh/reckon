"""A repair-gate refusal names the log it read and both commands it compared."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from reckon.crew import pending_landing, runs
from tests.helpers.git import git

DECLARED = "pytest tests/test_this_work.py"
OTHER = "pytest tests/test_other_work.py"


@pytest.fixture()
def repair(tmp_path):
    """A repository with one commit, and writers for its gate evidence."""
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-q", "-b", "main")
    git(repository, "config", "user.email", "worker@example.invalid")
    manifest = tmp_path / "manifest.md"
    evidence = SimpleNamespace(repository=repository, manifest=manifest)

    def manifest_text(body):
        manifest.write_text(body)
        return {"manifest_path": str(manifest)}

    def write_log(name, *, command=None):
        head = git(repository, "rev-parse", "HEAD")
        stated = "" if command is None else f"# command: {command}\n"
        log = tmp_path / name
        log.write_text(f"# revision: {head}\n{stated}EXIT=0\n")
        return log

    def refuse(pointer):
        with pytest.raises(runs.CrewError) as caught:
            pending_landing._require_repair_gate(
                pointer, evidence.head, evidence.repository
            )
        return str(caught.value)

    def accept(pointer):
        pending_landing._require_repair_gate(
            pointer, evidence.head, evidence.repository
        )

    (repository / "functions.py").write_text("def first():\n    return 1\n")
    git(repository, "add", "functions.py")
    git(repository, "commit", "-q", "-m", "test: define one function")
    evidence.head = git(repository, "rev-parse", "HEAD")
    evidence.write_manifest = manifest_text
    evidence.write_log = write_log
    evidence.refuse = refuse
    evidence.accept = accept
    return evidence


def _declaring(repair, log, *, tests=DECLARED, extra=""):
    return repair.write_manifest(
        f"status: complete\ntests: {tests}\n"
        f"test_logs: {json.dumps([str(log)])}\n{extra}"
    )


def test_a_mismatched_command_names_the_log_and_both_commands(repair):
    log = repair.write_log("gate.log", command=OTHER)
    message = repair.refuse(_declaring(repair, log))
    assert str(log) in message, message
    assert DECLARED in message, message
    assert OTHER in message, message


def test_a_log_with_no_command_line_says_the_log_records_none(repair):
    log = repair.write_log("gate.log")
    message = repair.refuse(_declaring(repair, log))
    assert str(log) in message, message
    assert DECLARED in message, message
    assert "records no command line" in message, message


def test_the_after_suite_log_is_the_one_the_refusal_names(repair):
    after = repair.write_log("after-suite.log", command=OTHER)
    cited = repair.write_log("cited.log", command=DECLARED)
    pointer = _declaring(repair, cited, extra=f"after_suite:\n  log_path: {after}\n")
    message = repair.refuse(pointer)
    assert str(after) in message, message
    assert str(cited) not in message, message
    assert DECLARED in message, message
    assert OTHER in message, message


def test_a_matching_gate_lands(repair):
    log = repair.write_log("gate.log", command=DECLARED)
    repair.accept(_declaring(repair, log))
