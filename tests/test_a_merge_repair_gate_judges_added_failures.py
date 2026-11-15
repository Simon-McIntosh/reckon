"""A merge repair's gate is red only on a failure the primary branch does not have.

The repair gate used to require absolute green, so a run whose gate files were
already failing on the primary branch could never land through its pending
landing, however clean its own change. It now compares its failing ids with the
same command's arm at the primary revision the conflict record names, and
refuses only on an id the primary does not fail.
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest

from reckon.crew import pending_landing, runs
from tests.helpers.git import git

COMMAND = "pytest tests/test_charged.py"

#: Red on the primary branch before this repair existed.
PRIMARY_RED = "tests/test_charged.py::test_charge"
#: The only id this repair introduces.
ADDED = "tests/test_charged.py::test_repaired"


def _commit(repository, name):
    (repository / name).write_text(f"{name}\n")
    git(repository, "add", name)
    git(repository, "commit", "-q", "-m", "test: record fixture commit")
    return git(repository, "rev-parse", "HEAD")


def _pytest_log(revision, ids, *, command=COMMAND):
    """A gate log the review reader recognises, one ``FAILED`` line per id."""
    lines = [f"# revision: {revision}", f"# command: {command}"]
    lines.extend(f"FAILED {test_id} - AssertionError: red" for test_id in ids)
    lines.append(f"{len(ids)} failed, 1 passed in 0.11s")
    lines.append(f"EXIT={1 if ids else 0}")
    return "\n".join(lines) + "\n"


@pytest.fixture()
def repair(tmp_path):
    """A repository with a base, a primary revision and a repaired head."""
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-q", "-b", "main")
    git(repository, "config", "user.email", "worker@example.invalid")
    git(repository, "config", "user.name", "Worker")
    evidence = SimpleNamespace(repository=repository, tmp=tmp_path)
    evidence.base = _commit(repository, "base.txt")
    evidence.primary = _commit(repository, "primary.txt")
    evidence.head = _commit(repository, "repaired.txt")
    evidence.manifest = tmp_path / "manifest.md"
    return evidence


def _write(repair, *, primary_ids, head_ids, arm_revision=None, arm_command=COMMAND):
    """Write the repair's manifest and both gate logs; return its pointer."""
    primary_log = repair.tmp / "primary-gate.log"
    primary_log.write_text(_pytest_log(repair.primary, primary_ids))
    head_log = repair.tmp / "head-gate.log"
    head_log.write_text(_pytest_log(repair.head, head_ids, command=arm_command))
    arm = {
        "revision": repair.primary if arm_revision is None else arm_revision,
        "command": COMMAND,
        "exit_status": 1 if primary_ids else 0,
        "log_path": str(primary_log),
        "completed": True,
        "failure_count": len(primary_ids),
        "failure_ids": list(primary_ids),
    }
    repair.manifest.write_text(
        "status: complete\n"
        f"tests: {COMMAND}\n"
        f"test_logs: {json.dumps([str(head_log)])}\n"
        f"baseline_suite: {json.dumps(arm)}\n"
    )
    return {
        "manifest_path": str(repair.manifest),
        "repo": str(repair.repository),
    }


def _gate(repair, pointer):
    """Call the gate through the seam the tree under test offers.

    This node's base gate takes no primary revision. The two cases the change
    is measured by fail there on their own assertions, and the ones pinning
    unchanged behaviour read the same refusal through either shape.
    """
    arguments = (pointer, repair.head, repair.repository)
    if "primary" in inspect.signature(
        pending_landing._require_repair_gate
    ).parameters:
        arguments += (repair.primary,)
    pending_landing._require_repair_gate(*arguments)


def test_a_repair_red_only_on_primary_lands(repair):
    """The primary already fails this id, so the repair's gate adds nothing."""
    _gate(repair, _write(repair, primary_ids=[PRIMARY_RED], head_ids=[PRIMARY_RED]))


def test_an_added_failure_is_refused_by_name(repair):
    pointer = _write(repair, primary_ids=[PRIMARY_RED], head_ids=[PRIMARY_RED, ADDED])
    with pytest.raises(runs.CrewError) as refused:
        _gate(repair, pointer)
    message = str(refused.value)
    assert ADDED in message, message
    assert PRIMARY_RED not in message, message
    assert "primary-gate.log" in message and "head-gate.log" in message, message


def test_a_gate_log_naming_another_revision_is_refused(repair):
    """The comparison must not admit a log taken at a different tree.

    Both arms would otherwise agree, so only the head log's own revision
    binding stands between this repair and a landing.
    """
    pointer = _write(repair, primary_ids=[PRIMARY_RED], head_ids=[PRIMARY_RED])
    head_log = repair.tmp / "head-gate.log"
    head_log.write_text(_pytest_log(repair.base, [PRIMARY_RED]))
    with pytest.raises(runs.CrewError):
        _gate(repair, pointer)


def test_a_primary_arm_at_another_revision_is_refused_by_name(repair):
    """An arm measured somewhere else is not the comparison the gate reports."""
    pointer = _write(
        repair,
        primary_ids=[PRIMARY_RED],
        head_ids=[PRIMARY_RED],
        arm_revision=repair.base,
    )
    with pytest.raises(runs.CrewError) as refused:
        _gate(repair, pointer)
    message = str(refused.value)
    assert repair.base[:12] in message or repair.base in message, message
    assert repair.primary in message, message


def test_a_green_gate_needs_no_primary_arm(repair):
    """Absolute green still passes on its own receipt, as it always did."""
    head_log = repair.tmp / "head-gate.log"
    head_log.write_text(_pytest_log(repair.head, [], command="git diff --check"))
    repair.manifest.write_text(
        "status: complete\ntests: git diff --check\n"
        f"test_logs: {json.dumps([str(head_log)])}\n"
    )
    pointer = {"manifest_path": str(repair.manifest), "repo": str(repair.repository)}
    _gate(repair, pointer)
