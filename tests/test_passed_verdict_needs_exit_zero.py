"""A passing verdict is refused beside a nonzero exit status, or a command no
shell could start.

The verdict and the exit status are two statements about one run. The gate-log
comparison reads one of them only from the log's terminal ``EXIT=<n>`` record,
so a log whose command wrote no such line leaves the pair unreported: a
promotion could record ``gate: passed`` beside a nonzero exit status and
nothing weighed the two. The cases below drive ``crew complete`` through its
CLI in a fixture crew home and a fixture repository, and assert the refusal
from the two values alone, whether or not the log carries its own exit record.

One pair is admitted: the repository judges a gate by its delta against its
base, so a run whose manifest records both suite arms as complete, a red
baseline among them, and a cited head log that adds no id to that base measures
zero added against it, and its passing verdict is that delta. The head ids are
read from the cited log's own ``FAILED``/``ERROR`` lines rather than the
manifest's ``after_suite`` list, so a manifest that leaves out a head failure
cannot pass a nonzero exit as zero added. A run whose head adds an id to that
base is refused, and so is one whose baseline arm stopped before its suite
finished — a short list that happens to cover every id the head records is not
a measurement of what the head added. A recorded gate command whose first
token names no executable is also refused.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew.runs import _write_json, pointer_path, run_dir

PROJECT = "proj"
PLAN = "plan-a"
PROGRAM = sys.executable
COMMAND = f"{PROGRAM} -m pytest tests/test_passed_verdict_needs_exit_zero.py"
ASSIGNED_COMMAND = f"PYTHONPATH=/tmp/verdict-fixture {COMMAND}"
PROSE_COMMAND = "one all_debug job over three files"
BASELINE_FAILURE = "tests/test_verdict_fixture.py::test_the_guard_refuses"
ADDED_FAILURE = "tests/test_verdict_fixture.py::test_the_guard_reports"
BASE_SHA = "ca630c06e894d95111a8627b11d02a023aa270dd"
HEAD_SHA = "9b0eac9cf4c1e0a1b5c2f0e6d3a7c1e9f2b4d6a8"


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "plans" / f"{PLAN}.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{PLAN}">'
        '<meta name="plan-effort-hours" content="4">'
        f"<title>{PLAN}</title></head><body></body></html>",
        encoding="utf-8",
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(root, "add", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    return root


def _write_pointer(run_id: str, repository: Path, manifest: str | None = None) -> None:
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.md"
    if manifest is not None:
        manifest_path.write_text(manifest, encoding="utf-8")
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-10-02T11:00:00Z",
            "manifest_path": str(manifest_path),
            "node": {
                "id": "verdict-exit-fixture",
                "plan": PLAN,
                "section": "verdict-exit",
                "time_budget": "20m",
                "write_paths": [],
            },
        },
    )


def _red_baseline_manifest(
    base_log: str,
    *,
    baseline_completed: bool = True,
) -> str:
    """A manifest recording both arms: a red baseline and the head's own run.

    The baseline's failure ids are the set the head is compared against. The
    head ids themselves are read from the cited gate log, so the ``completed``
    check is asked of the manifest while the ids are asked of the log — and
    this manifest's empty ``after_suite`` list is what a record that simply
    never listed any head failure would carry.
    """
    baseline = {
        "revision": BASE_SHA,
        "command": COMMAND,
        "exit_status": 3,
        "log_path": base_log,
        "log_digest": "",
        "completed": baseline_completed,
        "failure_count": 1,
        "failure_ids": [BASELINE_FAILURE],
    }
    after = {
        "revision": HEAD_SHA,
        "command": COMMAND,
        "exit_status": 1,
        "log_path": "",
        "log_digest": "sha256:head-suite",
        "completed": True,
        "failure_count": 0,
        "failure_ids": [],
    }
    return (
        "node: verdict-exit-fixture\n"
        "status: complete\n"
        f"baseline_suite: {json.dumps(baseline)}\n"
        f"after_suite: {json.dumps(after)}\n"
    )


def _complete_arguments(
    run_id: str,
    repository: Path,
    log_path: Path,
    *,
    command: str = COMMAND,
    exit_status: int = 0,
    waive_review: bool = False,
) -> list[str]:
    arguments = [
        "crew",
        "complete",
        "--run",
        run_id,
        "--gate",
        "passed",
        "--checkout-path",
        str(repository),
        "--no-commit",
        "report-only fixture",
        "--gate-command",
        command,
        "--gate-exit-status",
        str(exit_status),
        "--gate-log-path",
        str(log_path),
    ]
    if waive_review:
        # A run whose fresh manifest makes the row scoreable owes a stored
        # review; this fixture measures the exit-status pair, not the review.
        arguments += [
            "--waive-unreviewed-promotion",
            "a report-only fixture with no review store",
        ]
    return arguments


def _invoke(run_id: str, repository: Path, log_path: Path, **extra):
    return CliRunner().invoke(
        cli_main, _complete_arguments(run_id, repository, log_path, **extra)
    )


def test_a_passing_verdict_beside_a_nonzero_status_without_an_exit_record_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """The measured pair: the log carries no EXIT= line, so the comparison
    that reads only a recorded status sees nothing to refuse."""
    run_id = "r-20261002T120000000000-no-record"
    _write_pointer(run_id, repository)
    log = tmp_path / "no-record-gate.log"
    log.write_text("3 failed, 30 passed in 223.55s\n", encoding="utf-8")

    result = _invoke(run_id, repository, log, exit_status=1)

    assert result.exit_code != 0, result.output
    assert "gate 'passed'" in result.output
    assert "exit status 1" in result.output
    assert pointer_path(run_id).is_file()


def test_a_passing_verdict_beside_a_nonzero_status_with_an_agreeing_record_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """The log's own record agreeing with the status does not make the pair
    coherent: the verdict still contradicts both."""
    run_id = "r-20261002T120100000000-recorded"
    _write_pointer(run_id, repository)
    log = tmp_path / "recorded-gate.log"
    log.write_text("3 failed, 30 passed in 223.55s\nEXIT=2\n", encoding="utf-8")

    result = _invoke(run_id, repository, log, exit_status=2)

    assert result.exit_code != 0, result.output
    assert "gate 'passed'" in result.output
    assert "exit status 2" in result.output


def test_a_passing_verdict_with_a_zero_status_and_a_runnable_command_promotes(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20261002T120200000000-promotes"
    _write_pointer(run_id, repository)
    log = tmp_path / "green-gate.log"
    log.write_text("12 passed in 0.4s\nEXIT=0\n", encoding="utf-8")

    result = _invoke(run_id, repository, log)

    assert result.exit_code == 0, result.output
    record = json.loads(result.output)["record"]
    assert record["gate"] == "passed"
    assert record["gate_check"]["exit_status"] == 0


def test_a_leading_assignment_before_the_program_still_promotes(
    repository: Path, tmp_path: Path
) -> None:
    """The first token after the leading NAME=value assignment is the program."""
    run_id = "r-20261002T120300000000-assigned"
    _write_pointer(run_id, repository)
    log = tmp_path / "assigned-gate.log"
    log.write_text("12 passed in 0.4s\nEXIT=0\n", encoding="utf-8")

    result = _invoke(run_id, repository, log, command=ASSIGNED_COMMAND)

    assert result.exit_code == 0, result.output
    record = json.loads(result.output)["record"]
    assert record["gate_check"]["command"] == ASSIGNED_COMMAND


def test_a_prose_gate_command_that_names_no_program_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20261002T120400000000-prose"
    _write_pointer(run_id, repository)
    log = tmp_path / "prose-gate.log"
    log.write_text("30 passed, 3 failed in 223.55s\n", encoding="utf-8")

    result = _invoke(run_id, repository, log, command=PROSE_COMMAND)

    assert result.exit_code != 0, result.output
    assert PROSE_COMMAND in result.output
    assert "'one'" in result.output


def test_a_gate_command_that_does_not_parse_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20261002T120500000000-unparseable"
    _write_pointer(run_id, repository)
    log = tmp_path / "unparseable-gate.log"
    log.write_text("12 passed in 0.4s\nEXIT=0\n", encoding="utf-8")

    result = _invoke(run_id, repository, log, command=f'{PROGRAM} -m pytest "unclosed')

    assert result.exit_code != 0, result.output
    assert "does not parse" in result.output


def test_an_interrupted_baseline_that_covers_every_head_id_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """The baseline stopped before its suite finished, so its short list of
    failures covers every id the head log records without stating anything
    about it: the delta it appears to measure was never taken."""
    run_id = "r-20261002T120800000000-interrupted-base"
    base_log = tmp_path / "base-suite.log"
    _write_pointer(
        run_id,
        repository,
        _red_baseline_manifest(
            str(base_log),
            baseline_completed=False,
        ),
    )
    head_log = tmp_path / "head-suite.log"
    head_log.write_text(
        f"FAILED {BASELINE_FAILURE} - AssertionError: boom\n"
        "1 failed, 11 passed in 0.4s\n"
        "EXIT=1\n",
        encoding="utf-8",
    )

    result = _invoke(run_id, repository, head_log, exit_status=1, waive_review=True)

    assert result.exit_code != 0, result.output
    assert "gate 'passed'" in result.output
    assert "exit status 1" in result.output
    assert "baseline_suite" in result.output
    assert pointer_path(run_id).is_file()


def test_a_passing_verdict_on_a_red_base_that_adds_no_failure_promotes(
    repository: Path, tmp_path: Path
) -> None:
    """The delta case: the base is red, the head arm records only ids the
    baseline already fails, so zero was added and the passing verdict is that
    delta."""
    run_id = "r-20261002T120600000000-red-base"
    base_log = tmp_path / "base-suite.log"
    _write_pointer(
        run_id,
        repository,
        _red_baseline_manifest(str(base_log)),
    )
    head_log = tmp_path / "head-suite.log"
    head_log.write_text(
        f"FAILED {BASELINE_FAILURE} - AssertionError: boom\n"
        "1 failed, 11 passed in 0.4s\n"
        "EXIT=1\n",
        encoding="utf-8",
    )

    result = _invoke(run_id, repository, head_log, exit_status=1, waive_review=True)
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)["record"]
    assert record["gate"] == "passed"
    assert record["gate_check"]["exit_status"] == 1


def test_a_head_that_adds_a_failure_beside_a_red_base_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """The base is red, but the head log names an id the baseline does not
    fail: the delta is not zero, so the passing verdict is not the delta."""
    run_id = "r-20261002T120700000000-added-failure"
    base_log = tmp_path / "base-suite.log"
    _write_pointer(
        run_id,
        repository,
        _red_baseline_manifest(str(base_log)),
    )
    head_log = tmp_path / "head-suite.log"
    head_log.write_text(
        f"FAILED {BASELINE_FAILURE} - AssertionError: boom\n"
        f"FAILED {ADDED_FAILURE} - AssertionError: new boom\n"
        "2 failed, 10 passed in 0.4s\n"
        "EXIT=1\n",
        encoding="utf-8",
    )
    result = _invoke(run_id, repository, head_log, exit_status=1)

    assert result.exit_code != 0, result.output
    assert "gate 'passed'" in result.output
    assert "exit status 1" in result.output


def test_an_empty_after_suite_failure_list_cannot_hide_an_id_the_head_log_names(
    repository: Path, tmp_path: Path
) -> None:
    """The manifest's own head list is empty while the cited head log fails an
    id the baseline lacks. Read from the manifest the delta would be zero and a
    nonzero exit would pass as the measured delta; the log the promotion cites
    names the added id, so the pair is refused."""
    run_id = "r-20261002T120900000000-empty-head-list"
    base_log = tmp_path / "base-suite.log"
    _write_pointer(
        run_id,
        repository,
        _red_baseline_manifest(str(base_log)),
    )
    head_log = tmp_path / "head-suite.log"
    head_log.write_text(
        f"FAILED {ADDED_FAILURE} - AssertionError: new boom\n"
        "1 failed, 11 passed in 0.4s\n"
        "EXIT=1\n",
        encoding="utf-8",
    )

    result = _invoke(run_id, repository, head_log, exit_status=1)

    assert result.exit_code != 0, result.output
    assert "gate 'passed'" in result.output
    assert "exit status 1" in result.output
    assert pointer_path(run_id).is_file()
