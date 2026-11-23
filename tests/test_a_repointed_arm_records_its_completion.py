"""A re-pointed suite arm records the completion its new log shows.

A gate that runs several test processes into one log closes each process with
its own ``# ---- process exit N rc=<n>`` header and the log with a terminal
``EXIT=<n>`` record. A coordinator who re-points an arm at such a log corrects
the arm's citation, but the red-base rule reads the manifest's literal
``completed`` key and the arm's failure ids, so an arm left carrying the result
of the run it was pointed at before can never admit the merge it now describes.
``crew repair --arm`` therefore re-derives the gate's exit status, failure ids
and completion from the log it is given, and names a process that stopped
before its own summary rather than trusting the processes that did finish.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import crew
from reckon.cli import main as cli_main
from reckon.crew import promotion_evidence, reports, runs

RUN_ID = "r-repointed-arm-completion"
BASE = "0a524ce6f8b1d3a4e5f60718293a4b5c6d7e8f90"
HEAD = "1b635df7a9c2e4b6d8f0a1c3e5b7d9f0a2c4e6b8"

PROCESS_A = ["tests/test_charge.py::test_charge", "tests/test_charge.py::test_hold"]
PROCESS_B = ["tests/test_discharge.py::test_discharge"]
ALL_IDS = PROCESS_A + PROCESS_B


def _process_output(failed_ids: list[str]) -> str:
    """One pytest process's summary, as its own run writes it."""
    failed = len(failed_ids)
    lines = [f"FAILED {test_id} - AssertionError: no" for test_id in failed_ids]
    lines.append(f"===== {failed} failed, 3 passed in 1.00s =====")
    return "\n".join(lines) + "\n"


def _two_process_log() -> str:
    return (
        "# file: tests/test_charge.py\n"
        + _process_output(PROCESS_A)
        + "# ---- process exit 1 rc=1\n"
        + "# file: tests/test_discharge.py\n"
        + _process_output(PROCESS_B)
        + "# ---- process exit 2 rc=1\n"
        + "EXIT=1\n"
    )


def _arm(
    revision: str,
    failure_ids: list[str],
    *,
    exit_status: Any,
    completed: bool = True,
) -> dict[str, Any]:
    return {
        "revision": revision,
        "command": "python -m pytest -q tests/test_charge.py tests/test_discharge.py",
        "exit_status": exit_status,
        "completed": completed,
        "failure_count": len(failure_ids),
        "failure_ids": list(failure_ids),
        "log_path": "",
    }


@pytest.fixture(name="run")
def run_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A run whose head arm recorded an unfinished run of another log."""
    home = tmp_path / "crew-home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "node: repointed-arm-completion\n"
        "status: complete\n"
        f"commits: {HEAD}\n"
        "changed_paths: reckon/crew/review.py\n"
        "tests: python -m pytest -q tests/test_charge.py tests/test_discharge.py\n"
        f"baseline_suite: {json.dumps(_arm(BASE, ALL_IDS, exit_status=1))}\n"
        f"after_suite: {json.dumps(_arm(HEAD, [], exit_status=0, completed=False))}\n",
        encoding="utf-8",
    )
    runs._write_json(
        runs.pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": "demo",
            "repo": str(tmp_path / "repo"),
            "worktree": str(tmp_path / "repo"),
            "base_sha": BASE,
            "manifest_path": str(manifest),
        },
    )
    return {"manifest": manifest, "tmp": tmp_path}


def _parsed(manifest: Path) -> dict[str, Any]:
    return reports.parse_manifest(manifest.read_text(encoding="utf-8"))


def _record(manifest: Path) -> dict[str, Any]:
    return {"manifest_path": str(manifest)}


def _gate(log: Path) -> dict[str, Any]:
    return {"log_path": str(log), "exit_status": 1}


def test_the_union_of_the_processes_is_recorded_and_the_merge_lands(
    run: dict[str, Any],
) -> None:
    log = run["tmp"] / "head.log"
    log.write_text(_two_process_log(), encoding="utf-8")
    gate = _gate(log)

    # The negative control: before the re-point the re-pointed arm still
    # declares the result of the run it was pointed at before, so the red-base
    # rule cannot measure a delta and the merge is refused.
    assert (
        promotion_evidence._zero_added_against_a_red_base(
            _record(run["manifest"]), gate
        )
        is False
    )
    with pytest.raises(crew.CrewError):
        promotion_evidence._require_verdict_matches_exit_status(
            RUN_ID, _record(run["manifest"]), gate, verdict="passed"
        )

    runs.repair_manifest_arm(
        RUN_ID,
        "after_suite",
        revision=HEAD,
        reason="re-pointed at the H200 log the merge is on",
        log_path=str(log),
    )

    arm = _parsed(run["manifest"])["after_suite"]
    assert arm["completed"] is True
    assert arm["exit_status"] == 1
    assert sorted(arm["failure_ids"]) == sorted(ALL_IDS)

    record = _record(run["manifest"])
    manifest = _parsed(run["manifest"])
    assert promotion_evidence._arm_without_completion(manifest, gate) is None
    assert promotion_evidence._zero_added_against_a_red_base(record, gate) is True
    promotion_evidence._require_verdict_matches_exit_status(
        RUN_ID, record, gate, verdict="passed"
    )


def test_a_process_that_stopped_before_its_summary_leaves_the_arm_incomplete(
    run: dict[str, Any],
) -> None:
    log = run["tmp"] / "stopped.log"
    log.write_text(
        "# file: tests/test_charge.py\n"
        + _process_output(PROCESS_A)
        + "# ---- process exit 1 rc=1\n"
        + "# file: tests/test_discharge.py\n"
        + "Traceback (most recent call last):\n"
        + "KeyboardInterrupt\n"
        + "# ---- process exit 2 rc=1\n"
        + "EXIT=1\n",
        encoding="utf-8",
    )

    runs.repair_manifest_arm(
        RUN_ID,
        "after_suite",
        revision=HEAD,
        reason="re-pointed at the H200 log the merge is on",
        log_path=str(log),
    )

    arm = _parsed(run["manifest"])["after_suite"]
    assert arm["completed"] is False
    assert arm["incomplete_processes"] == ["tests/test_discharge.py"]
    assert (
        promotion_evidence._arm_without_completion(_parsed(run["manifest"]), _gate(log))
        == "after_suite"
    )


def test_a_single_process_log_records_its_own_exit_status_and_ids(
    run: dict[str, Any],
) -> None:
    """The arm takes the log's own status, not the log it was pointed at before."""
    log = run["tmp"] / "single.log"
    log.write_text(
        "FAILED tests/test_charge.py::test_charge - AssertionError: no\n"
        "===== 1 failed, 7 passed in 2.00s =====\n"
        "EXIT=2\n",
        encoding="utf-8",
    )

    record = runs.repair_manifest_arm(
        RUN_ID,
        "after_suite",
        revision=HEAD,
        reason="re-pointed at a single-process gate log",
        log_path=str(log),
    )

    arm = _parsed(run["manifest"])["after_suite"]
    assert arm["exit_status"] == 2
    assert arm["completed"] is True
    assert arm["failure_ids"] == ["tests/test_charge.py::test_charge"]
    assert record["observation"]["exit_status"] == 2


def test_a_log_named_by_the_repair_that_cannot_be_read_is_refused(
    run: dict[str, Any],
) -> None:
    with pytest.raises(crew.CrewError) as refusal:
        runs.repair_manifest_arm(
            RUN_ID,
            "after_suite",
            revision=HEAD,
            reason="re-pointed at a log that is not there",
            log_path=str(run["tmp"] / "absent.log"),
        )

    assert "cannot be read" in str(refusal.value)
    assert _parsed(run["manifest"])["after_suite"]["completed"] is False


def test_the_command_records_the_completion_from_the_shell(
    run: dict[str, Any],
) -> None:
    log = run["tmp"] / "cli.log"
    log.write_text(_two_process_log(), encoding="utf-8")

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "repair",
            "--run",
            RUN_ID,
            "--arm",
            "after_suite",
            "--revision",
            HEAD,
            "--log-path",
            str(log),
            "--reason",
            "re-pointed at the H200 log the merge is on",
        ],
    )

    assert result.exit_code == 0, result.output
    arm = _parsed(run["manifest"])["after_suite"]
    assert arm["completed"] is True
    assert sorted(arm["failure_ids"]) == sorted(ALL_IDS)
