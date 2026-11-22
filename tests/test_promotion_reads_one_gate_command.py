"""A manifest names one gate command, and promotion reads that one.

A delivery that states its gate twice gives the reader no way to choose: the
text reader kept the last line, so a review answer written under a second
``tests:`` line was replayed as a shell command and the gate failed against a
sentence. The line is refused at the read, which is before any promotion guard
runs a command, and the refusal quotes every line so the repair is visible.

A gate command given on the landing command line is the operator's own
statement and wins over the manifest's, including for a manifest that states
none; it does not rescue a manifest that states two, because the ambiguity is in
the file rather than in which command was chosen.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import (
    promotion_checks,
    promotion_gate,
    promotion_records,
    reports,
)
from reckon.crew.node import TaskNode
from tests.helpers.git import git

RUN_ID = "r-one-gate-command"
GTEST = "python -m pytest -q tests/test_promotion_reads_one_gate_command.py"
REVIEW_ANSWER = "answered - the new comparator has 22 unit tests and they pass"

# A command a replayed gate would run. It names this file so an accidental
# execution is visible in the record rather than inferred from an exit status.
REPLAYED_COMMAND = "echo one-gate-command"


@pytest.fixture(name="repository")
def repository_fixture(tmp_path: Path) -> Path:
    """A one-commit repository the gate can be replayed against."""
    repository = tmp_path / "repo"
    repository.mkdir()
    git(repository, "init", "-q", "-b", "main")
    git(repository, "config", "user.email", "worker@example.invalid")
    git(repository, "config", "user.name", "Worker")
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(repository, "add", "seed.txt")
    git(repository, "commit", "-q", "-m", "chore: the base")
    return repository


def _manifest_text(
    *, gate_lines: list[str], log: Path) -> str:
    lines = [
        "node: one-gate-command",
        "status: complete",
        f"commits: {'0' * 40}",
        "changed_paths: reckon/crew/reports.py "
        "tests/test_promotion_reads_one_gate_command.py",
    ]
    lines.extend(f"tests: {gate}" for gate in gate_lines)
    lines.append(f"test_logs: {log}")
    return "\n".join(lines) + "\n"


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / "manifests" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _record(tmp_path: Path, path: Path) -> dict[str, Any]:
    return {"run_id": RUN_ID, "manifest_path": str(path), "role": "implement"}


def _node() -> TaskNode:
    return TaskNode(
        id="one-gate-command",
        goal="Promotion reads one gate command",
        plan="delivered-work-lands-without-a-coordinator",
        role="implement",
        write_paths=[
            "reckon/crew/reports.py",
            "tests/test_promotion_reads_one_gate_command.py",
        ],
    )


def test_two_gate_command_lines_are_refused_quoting_both_and_run_nothing(
    repository: Path, tmp_path: Path
) -> None:
    """The second line is refused, and the arm that would run it is shown.

    The second line writes a sentinel when a shell executes it, so the file's
    presence is the proof that a command ran rather than an inference from an
    exit status. A reader that accepts the manifest has the command it chose
    replayed here, exactly as promotion replays it, before the case fails —
    which is what makes this case red on a reader that keeps the last line.
    """
    sentinel = tmp_path / "gate-command-ran"
    second = f"sh -c 'touch {sentinel}'"
    log = tmp_path / "gate.log"
    log.write_text("1 passed\nEXIT=0\n", encoding="utf-8")
    text = _manifest_text(gate_lines=[GTEST, second], log=log)
    path = _write(tmp_path, "two-lines", text)

    try:
        manifest = reports.parse_manifest(text, path=str(path))
    except reports.ManifestParseError as refusal:
        message = str(refusal)
        assert GTEST in message, message
        assert second in message, message
        assert "exactly one" in message, message
    else:
        record = _record(tmp_path, path)
        gate_check, _commits = promotion_records._default_gate_evidence_from_manifest(
            record, verdict="passed", gate_check={}, commits=()
        )
        promotion_gate.rerun_gate_at_integrated_revision(
            repository=repository,
            gate_check=gate_check,
            integrated_revision=git(repository, "rev-parse", "HEAD"),
            replay_log_path=tmp_path / "replay.log",
        )
        raise AssertionError(
            "the read accepted two gate commands and chose "
            f"{manifest['tests']!r}; replayed, it wrote the sentinel: "
            f"{sentinel.exists()}"
        )

    assert not sentinel.exists(), "a line was executed despite the refusal"

    # Both readers of the file refuse with that message: the check a worker
    # runs before delivery, and the guards promotion runs before any gate.
    audit = reports.audit_manifest(
        text, _node(), repository=tmp_path, worktree=tmp_path, manifest_path=path
    )
    assert "cannot read manifest" in " ".join(audit["findings"]), audit["findings"]
    record = _record(tmp_path, path)
    with pytest.raises(reports.ManifestParseError) as promotion_refusal:
        promotion_checks._require_recognised_manifest_status(RUN_ID, record)
    assert GTEST in str(promotion_refusal.value), str(promotion_refusal.value)
    assert second in str(promotion_refusal.value), str(promotion_refusal.value)


def test_one_gate_command_line_is_read_and_replayed(
    repository: Path, tmp_path: Path
) -> None:
    """A manifest naming one command is read as that command and replayed."""
    log = tmp_path / "gate.log"
    log.write_text("1 passed\nEXIT=0\n", encoding="utf-8")
    text = _manifest_text(gate_lines=[REPLAYED_COMMAND], log=log)
    path = _write(tmp_path, "one-line", text)

    manifest = reports.parse_manifest(text)
    assert manifest["tests"] == REPLAYED_COMMAND

    gate_check, _commits = promotion_records._default_gate_evidence_from_manifest(
        _record(tmp_path, path), verdict="passed", gate_check={}, commits=()
    )
    assert gate_check["command"] == REPLAYED_COMMAND
    assert gate_check["exit_status"] == 0

    replay_log = tmp_path / "replay.log"
    report = promotion_gate.rerun_gate_at_integrated_revision(
        repository=repository,
        gate_check=gate_check,
        integrated_revision=git(repository, "rev-parse", "HEAD"),
        replay_log_path=replay_log,
    )
    assert report["ran"] is True, report
    assert report["exit_status"] == 0, report
    replayed = replay_log.read_text(encoding="utf-8")
    assert REPLAYED_COMMAND in replayed, replayed
    assert replayed.rstrip().splitlines()[-1] == "EXIT=0", replayed


def test_a_command_line_gate_command_is_used_over_the_manifest(
    tmp_path: Path,
) -> None:
    """The operator's own command wins for one line and for none."""
    log = tmp_path / "gate.log"
    log.write_text("1 passed\nEXIT=0\n", encoding="utf-8")
    supplied = "python -m pytest -q tests/test_other.py"

    for name, gate_lines in (
        ("one-line", [GTEST]),
        ("no-line", []),
    ):
        path = _write(tmp_path, name, _manifest_text(gate_lines=gate_lines, log=log))
        gate_check, _commits = promotion_records._default_gate_evidence_from_manifest(
            _record(tmp_path, path),
            verdict="passed",
            gate_check={"command": supplied, "log_path": str(log)},
            commits=(),
        )
        assert gate_check["command"] == supplied, name


def test_a_command_line_gate_command_does_not_rescue_two_manifest_lines(
    tmp_path: Path,
) -> None:
    """Two lines are still refused when the operator names a command."""
    log = tmp_path / "gate.log"
    log.write_text("1 passed\nEXIT=0\n", encoding="utf-8")
    text = _manifest_text(gate_lines=[GTEST, REVIEW_ANSWER], log=log)
    path = _write(tmp_path, "two-lines-with-a-flag", text)
    record = _record(tmp_path, path)

    with pytest.raises(reports.ManifestParseError) as refusal:
        promotion_checks._require_recognised_manifest_status(RUN_ID, record)

    message = str(refusal.value)
    assert GTEST in message and REVIEW_ANSWER in message, message


def test_a_single_gate_command_line_still_reads_as_before(
    tmp_path: Path,
) -> None:
    """A one-line manifest earns no fault from the duplicate-lines rule."""
    log = tmp_path / "gate.log"
    log.write_text("1 passed\nEXIT=0\n", encoding="utf-8")
    path = _write(tmp_path, "clean", _manifest_text(gate_lines=[GTEST], log=log))

    manifest = reports.parse_manifest(path.read_text(encoding="utf-8"))
    assert manifest["tests"] == GTEST
    assert not [
        finding
        for finding in reports.audit_manifest(
            path.read_text(encoding="utf-8"),
            _node(),
            repository=tmp_path,
            worktree=tmp_path,
            manifest_path=path,
        )["findings"]
        if "tests: lines" in finding
    ]


def test_a_gate_command_inside_a_body_is_not_a_second_gate_command() -> None:
    """A ``tests:`` word nested in another field's body is that field's text."""
    text = (
        "node: one-gate-command\n"
        "status: complete\n"
        f"tests: {GTEST}\n"
        "outcome: |\n"
        "  tests: not a gate\n"
    )
    manifest = reports.parse_manifest(text)
    assert manifest["tests"] == GTEST
    assert manifest["outcome"] == "tests: not a gate"


def test_a_json_manifest_repeating_the_key_is_refused() -> None:
    """A repeated JSON key is refused rather than silently collapsed."""
    text = (
        '{"node": "one-gate-command", "status": "complete", '
        f'"tests": "{GTEST}", "tests": "{REVIEW_ANSWER}"}}'
    )
    with pytest.raises(reports.ManifestParseError) as refusal:
        reports.parse_manifest(text)
    message = str(refusal.value)
    assert GTEST in message and REVIEW_ANSWER in message, message