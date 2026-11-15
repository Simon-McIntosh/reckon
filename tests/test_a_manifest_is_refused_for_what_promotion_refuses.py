"""A manifest the delivery check refuses is a manifest promotion refuses.

The check a worker runs before delivery reads the same file promotion reads
hours later, so the two cannot disagree: a fault promotion refuses and the check
passes costs the run its turn, and the repair then has to be made for the worker
once it can no longer make it. Each fault row here is driven through both
readers, and both are read for the same reason — the reason the two share rather
than either reader's own copy of it.

A delivery whose only test file is new at the head is the case where an absent
base arm is a *complete* measurement rather than missing evidence: the base tree
holds none of the node's tests, so there was nothing to run, nothing could fail,
and the pair reads as a clean base. The same absent arm beside a base tree that
does hold the tests is still missing evidence, and is still refused.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from reckon import crew, ledger
from reckon.crew import (
    manifest_faults,
    promotion_checks,
    promotion_records,
    reports,
    runs,
)
from reckon.crew.node import TaskNode
from tests.helpers.git import git as _git

RUN_ID = "r-manifest-faults"

SUITE = "python -m pytest -q {node_test_files}"
TEST_FILE = "tests/test_new.py"


def _repository(tmp_path: Path, *, test_at_base: bool) -> dict[str, Any]:
    """A repository one commit past its base, whose head holds the node's test."""
    repository = tmp_path / "repo"
    repository.mkdir(exist_ok=True)
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    (repository / "tests").mkdir(exist_ok=True)
    seeded = TEST_FILE if test_at_base else "seed.txt"
    (repository / seeded).write_text("seed\n", encoding="utf-8")
    _git(repository, "add", seeded)
    _git(repository, "commit", "-q", "-m", "chore: the base")
    base = _git(repository, "rev-parse", "HEAD")
    (repository / TEST_FILE).write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    _git(repository, "add", TEST_FILE)
    _git(repository, "commit", "-q", "-m", "test: add the node's test")
    head = _git(repository, "rev-parse", "HEAD")
    log = tmp_path / "manifests" / "after.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    command = SUITE.replace("{node_test_files}", TEST_FILE)
    log.write_text(
        f"# rev={head}\ncommand: {command}\n1 passed\nEXIT=0\n",
        encoding="utf-8",
    )
    return {
        "repository": repository,
        "base": base,
        "head": head,
        "log": log,
        "tmp": tmp_path,
    }


@pytest.fixture(name="scratch")
def scratch_fixture(tmp_path: Path) -> dict[str, Any]:
    """A base holding no test: the node's own test is new at its head."""
    return _repository(tmp_path, test_at_base=False)


def _arm(scratch: Mapping[str, Any], revision: str) -> dict[str, Any]:
    return {
        "revision": revision,
        "command": SUITE.replace("{node_test_files}", TEST_FILE),
        "exit_status": 0,
        "completed": True,
        "failure_count": 0,
        "failure_ids": [],
        "log_path": str(scratch["log"]),
    }


def _manifest_text(
    scratch: Mapping[str, Any], *, status: str = "complete", **overrides: Any
) -> str:
    gate = str(overrides.pop("tests", SUITE.replace("{node_test_files}", TEST_FILE)))
    commits = str(overrides.pop("commits", scratch["head"]))
    lines = [
        "node: manifest-faults",
        f"status: {status}",
        f"commits: {commits}",
        f"changed_paths: {TEST_FILE}",
        f"tests: {gate}",
        f"test_logs: {scratch['log']}",
    ]
    for name in manifest_faults.SUITE_ARMS:
        if name not in overrides:
            continue
        value = overrides.pop(name)
        rendered = value if isinstance(value, str) else json.dumps(value)
        lines.append(f"{name}: {rendered}")
    return "\n".join(lines) + "\n"


def _write(scratch: Mapping[str, Any], name: str, text: str) -> Path:
    path = Path(str(scratch["tmp"])) / "manifests" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _node() -> TaskNode:
    return TaskNode(
        id="manifest-faults",
        goal="Refuse what promotion refuses",
        plan="manifest-faults",
        role="implement",
        write_paths=[TEST_FILE],
    )


def _record(scratch: Mapping[str, Any], path: Path) -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "project": "demo",
        "role": "implement",
        "repo": str(scratch["repository"]),
        "worktree": str(scratch["repository"]),
        "base_sha": scratch["base"],
        "head_sha": scratch["head"],
        "suite_command": SUITE,
        "manifest_path": str(path),
        "node": {
            "id": "manifest-faults",
            "plan": "manifest-faults",
            "role": "implement",
            "write_paths": [TEST_FILE],
        },
    }


ROWS: dict[str, Callable[[Mapping[str, Any]], str]] = {
    "status-word-outside-the-vocabulary": lambda scratch: _manifest_text(
        scratch, status="ready-for-review"
    ),
    "commit-entry-carrying-a-subject": lambda scratch: _manifest_text(
        scratch, commits=f"{scratch['head']} test: add the node's test"
    ),
    "prose-arm-revision": lambda scratch: _manifest_text(
        scratch,
        baseline_suite=_arm(scratch, f"{scratch['base']} (the base)"),
        after_suite=_arm(scratch, str(scratch["head"])),
    ),
    "abbreviated-arm-revision": lambda scratch: _manifest_text(
        scratch,
        baseline_suite=_arm(scratch, str(scratch["base"])),
        after_suite=_arm(scratch, str(scratch["head"])[:9]),
    ),
    "gate-command-naming-a-temporary-tree": lambda scratch: _manifest_text(
        scratch,
        tests=f"python -m pytest -B {scratch['tmp']}/repo/{TEST_FILE}",
    ),
}


def _check_findings(scratch: Mapping[str, Any], text: str, path: Path) -> list[str]:
    audit = reports.audit_manifest(
        text,
        _node(),
        worktree=scratch["repository"],
        repository=scratch["repository"],
        manifest_path=path,
    )
    return list(audit["findings"])


def _promotion_refusals(scratch: Mapping[str, Any], path: Path) -> str:
    """Every reason the promotion's own guards give for this delivery."""
    record = _record(scratch, path)
    refusals = []
    for guard in (
        promotion_checks._require_recognised_manifest_status,
        promotion_checks._require_no_manifest_fault,
    ):
        try:
            guard(RUN_ID, record)
        except crew.CrewError as refusal:
            refusals.append(str(refusal))
    return "\n".join(refusals)


def _shared_reasons(text: str) -> list[str]:
    """The reasons the rules state, read without consulting either reader."""
    try:
        manifest = reports.parse_manifest(text)
    except reports.ManifestParseError as refusal:
        return [str(refusal)]
    return [fault.reason for fault in manifest_faults.manifest_faults(manifest)]


@pytest.mark.parametrize("row", sorted(ROWS))
def test_the_check_and_the_promotion_refuse_with_one_reason(
    scratch: dict[str, Any], row: str
) -> None:
    text = ROWS[row](scratch)
    path = _write(scratch, row, text)
    reasons = _shared_reasons(text)

    assert reasons, f"the rules state no reason for {row}"
    findings = _check_findings(scratch, text, path)
    refusal = _promotion_refusals(scratch, path)

    assert findings, f"the delivery check passed a manifest promotion refuses: {row}"
    assert refusal, f"the promotion accepted a manifest the check refuses: {row}"
    for reason in reasons:
        assert reason in " ".join(findings), row
        assert reason in refusal, row


def test_a_delivery_with_no_other_fault_is_clean_once_the_rule_is_withdrawn(
    scratch: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prose row's finding is the rule's, and nothing else about it fails.

    A check that refused the row for some unrelated reason would satisfy the
    test above while the rule it is meant to exercise did nothing, so the rule
    is withdrawn here and the same delivery is read again. What remains is what
    the check read before the rule existed.
    """
    text = ROWS["prose-arm-revision"](scratch)
    path = _write(scratch, "prose-rule-withdrawn", text)

    assert _check_findings(scratch, text, path)

    monkeypatch.setattr(manifest_faults, "manifest_faults", lambda _manifest: [])
    assert _check_findings(scratch, text, path) == []


@pytest.fixture(name="crew_home")
def crew_home_fixture(scratch: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Path:
    home = Path(str(scratch["tmp"])) / "crew-home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _landing_cannot_measure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave the pair unmeasured, so the arm rules alone decide the delta."""
    monkeypatch.setattr(
        promotion_records, "_measure_missing_arms", lambda *_args, **_kw: {}
    )


def _record_the_run(scratch: dict[str, Any], record: dict[str, Any]) -> None:
    pointer = runs.pointer_path(RUN_ID)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    runs._write_json(pointer, record)


def test_an_absent_base_arm_is_complete_when_the_base_holds_no_test(
    scratch: dict[str, Any], crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The change creates the only test, so a base arm measuring nothing is right.

    The landing cannot measure an arm here either, which is the state a run
    whose base tree holds none of its test files is in: there is nothing to run.
    The pair then reads as a clean base with zero failures rather than as a
    delivery missing half its evidence.
    """
    _landing_cannot_measure(monkeypatch)
    text = _manifest_text(
        scratch,
        baseline_suite="none",
        after_suite=_arm(scratch, str(scratch["head"])),
    )
    path = _write(scratch, "base-arm-none", text)
    record = _record(scratch, path)
    _record_the_run(scratch, record)

    assert _check_findings(scratch, text, path) == []
    delta = promotion_records._evaluate_suite_delta(RUN_ID, record, waiver_reason="")

    assert delta["status"] == "clean"
    assert delta["baseline_suite"]["revision"] == scratch["base"]
    assert delta["baseline_suite"]["failure_count"] == 0
    assert delta["baseline_suite"]["failure_ids"] == []
    assert delta["baseline_measured_nothing"] is True
    assert delta["added_failure_ids"] == []


def test_an_absent_base_arm_is_a_refusal_when_the_base_tree_holds_the_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same declaration beside a base tree that holds the tests is a gap."""
    scratch = _repository(tmp_path, test_at_base=True)
    home = tmp_path / "crew-home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    _landing_cannot_measure(monkeypatch)
    text = _manifest_text(
        scratch,
        baseline_suite="none",
        after_suite=_arm(scratch, str(scratch["head"])),
    )
    path = _write(scratch, "base-arm-none-refused", text)
    record = _record(scratch, path)
    _record_the_run(scratch, record)

    with pytest.raises(ledger.SuiteDeltaError) as refusal:
        promotion_records._evaluate_suite_delta(RUN_ID, record, waiver_reason="")

    assert "baseline_suite" in str(refusal.value)
