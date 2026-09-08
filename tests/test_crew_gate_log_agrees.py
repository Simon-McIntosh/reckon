"""A promoted passing gate's cited log must agree with its asserted verdict.

The promotion path asks for a gate command, an exit status and a log and,
until now, checked none of them against the others: promotion recorded
whatever three pieces of evidence a coordinator filed, so the committed ledger
could carry a row whose verdict its own evidence refuted. Two such rows are on
record — one with the wrong log attached to a run, and one asserting exit
status zero beside a log whose entire content was a shell command-not-found
error at exit two, naming a subcommand that does not exist while the real
entry points both do. Both were caught by a person reading afterwards; nothing
in the machinery read the log at all.

These tests pin the refusal: a promotion asserting a passing gate is refused
when the cited log is empty, when the log's own recorded exit status
contradicts the asserted one, or when the log contains no evidence the command
ran; the refusal names which of the three it found and the resolving verb; a
promotion whose log and command agree lands unchanged; and a non-passing
verdict is unaffected, since it already carries its failure.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"

_COMMAND = "uv run pytest tests/test_crew_gate_log_agrees.py"
_EXIT_ZERO = 0


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
        f"<title>{PLAN}</title></head><body></body></html>"
    )
    return root


def _write_pointer(run_id: str, repository: Path) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-09-08T12:00:00Z",
            "manifest_path": "/durable/manifest.md",
            "base_sha": "",
            "node": {
                "id": "gate-log-agreement",
                "plan": PLAN,
                "section": "gate-log-agreement",
                "time_budget": "20m",
                "write_paths": [],
            },
        },
    )


def _complete_arguments(
    run_id: str,
    repository: Path,
    log_path: str,
    *,
    exit_status: int = _EXIT_ZERO,
) -> list[str]:
    return [
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
        _COMMAND,
        "--gate-exit-status",
        str(exit_status),
        "--gate-log-path",
        log_path,
    ]


# ── An empty cited log refuses a passing gate ───────────────────────────────


def test_promotion_refuses_a_passing_gate_whose_cited_log_is_empty(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260908T120000000000-empty"
    _write_pointer(run_id, repository)
    empty_log = tmp_path / "empty-gate.log"
    empty_log.write_text("", encoding="utf-8")

    result = CliRunner().invoke(
        cli_main, _complete_arguments(run_id, repository, str(empty_log))
    )

    assert result.exit_code != 0
    assert "is empty" in result.output
    # The refusal names which of the three shapes it found.
    assert "Found: an empty log" in result.output
    # ... and the resolving verb.
    assert "Re-run the check and cite its full captured output" in result.output
    # A refusal must not have consumed the pointer.
    assert pointer_path(run_id).is_file()


def test_empty_log_of_whitespace_only_refuses_like_no_content(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260908T120100000000-whitespace"
    _write_pointer(run_id, repository)
    blank_log = tmp_path / "blank-gate.log"
    blank_log.write_text("  \n\t\n  \n", encoding="utf-8")

    result = CliRunner().invoke(
        cli_main, _complete_arguments(run_id, repository, str(blank_log))
    )

    assert result.exit_code != 0
    assert "Found: an empty log" in result.output


# ── A recorded exit status that contradicts the asserted one refuses ──────────


def test_promotion_refuses_a_recorded_exit_status_that_contradicts_the_assertion(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260908T120200000000-exit-mismatch"
    _write_pointer(run_id, repository)
    failing_log = tmp_path / "failing-gate.log"
    failing_log.write_text(
        "payload before the shell's own record\nEXIT=2\n", encoding="utf-8"
    )

    result = CliRunner().invoke(
        cli_main,
        _complete_arguments(run_id, repository, str(failing_log), exit_status=0),
    )

    assert result.exit_code != 0
    assert "records EXIT=2" in result.output
    assert (
        "Found: a recorded exit status that contradicts the asserted one"
        in result.output
    )
    assert "Re-run the check and cite its log" in result.output
    assert pointer_path(run_id).is_file()


def test_an_agreeing_recorded_exit_status_does_not_refuse(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260908T120300000000-exit-agree"
    _write_pointer(run_id, repository)
    passing_log = tmp_path / "passing-gate.log"
    passing_log.write_text("36 passed in 1.0s\nEXIT=0\n", encoding="utf-8")

    result = CliRunner().invoke(
        cli_main, _complete_arguments(run_id, repository, str(passing_log))
    )

    assert result.exit_code == 0, result.output
    stored = json.loads(result.output)["record"]["gate_check"]
    assert stored["exit_status"] == 0
    assert stored["log_path"] == str(passing_log)


# ── A log showing the command never ran refuses ─────────────────────────────


def test_promotion_refuses_a_log_showing_the_command_never_ran(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260908T120400000000-not-found"
    _write_pointer(run_id, repository)
    not_found_log = tmp_path / "not-found-gate.log"
    not_found_log.write_text(
        "bash: line 1: rekon crew frobnicate: command not found\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli_main, _complete_arguments(run_id, repository, str(not_found_log))
    )

    assert result.exit_code != 0
    assert "command not found" in result.output
    assert "Found: no evidence the command ran" in result.output
    assert "Re-run the check and cite its log" in result.output
    assert pointer_path(run_id).is_file()


def test_recorded_pair_of_exit_zero_beside_a_command_not_found_log_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    # The measured contradiction that prompted the refusal: a promotion filed
    # as exit status zero whose cited log's entire content is a shell
    # command-not-found error, naming a subcommand that does not exist while
    # the real entry points both do.
    run_id = "r-20260908T120500000000-recorded-pair"
    _write_pointer(run_id, repository)
    contradictory_log = tmp_path / "recorded-pair-gate.log"
    contradictory_log.write_text(
        "bash: line 1: crew complete --gate passed: command not found\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli_main,
        _complete_arguments(run_id, repository, str(contradictory_log), exit_status=0),
    )

    assert result.exit_code != 0
    assert "command not found" in result.output
    assert "Found: no evidence the command ran" in result.output


# ── An agreeing log and command land unchanged ─────────────────────────────


def test_promotion_whose_log_and_command_agree_lands_unchanged(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260908T120600000000-agree"
    _write_pointer(run_id, repository)
    passing_log = tmp_path / "agreeing-gate.log"
    passing_log.write_text(
        "tests/test_crew_gate_log_agrees.py 12 passed in 0.4s\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli_main, _complete_arguments(run_id, repository, str(passing_log))
    )

    assert result.exit_code == 0, result.output
    record = json.loads(result.output)["record"]
    assert record["gate"] == "passed"
    assert record["gate_check"]["command"] == _COMMAND
    assert record["gate_check"]["exit_status"] == 0
    assert record["gate_check"]["log_path"] == str(passing_log)
    # The agreeing promotion consumed the pointer as usual.
    assert not pointer_path(run_id).exists()


# ── A non-passing verdict is unaffected ─────────────────────────────────────


def test_a_non_passing_verdict_beside_a_contradictory_log_is_unaffected(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260908T120700000000-not-passed"
    _write_pointer(run_id, repository)
    contradictory_log = tmp_path / "non-passing-gate.log"
    contradictory_log.write_text(
        "bash: line 1: rekon crew frobnicate: command not found\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "complete",
            "--run",
            run_id,
            "--gate",
            "failed",
            "--failure-classification",
            "work-rejected",
            "--outcome",
            "the check could not run; the gate is failed",
            "--checkout-path",
            str(repository),
            "--gate-command",
            _COMMAND,
            "--gate-exit-status",
            "2",
            "--gate-log-path",
            str(contradictory_log),
        ],
    )

    assert result.exit_code == 0, result.output
    record = json.loads(result.output)["record"]
    assert record["gate"] == "failed"
    # A failing verdict already carries its failure in its outcome, so the
    # contradictory log adds no new information to refuse on.
    assert record["gate_check"]["exit_status"] == 2


# ── A log path that cannot be read is not this check's refusal ─────────────


def test_a_cited_log_path_that_cannot_be_read_is_left_to_the_promotion(
    repository: Path, tmp_path: Path
) -> None:
    # Promotion may run from a machine the worker's log never reached, so a
    # log path that does not resolve here has no text to contradict the
    # verdict and must not turn a passing promotion into a refusal.
    run_id = "r-20260908T120800000000-missing-log"
    _write_pointer(run_id, repository)
    missing_log = tmp_path / "never-written-gate.log"

    result = CliRunner().invoke(
        cli_main, _complete_arguments(run_id, repository, str(missing_log))
    )

    assert result.exit_code == 0, result.output
    record = json.loads(result.output)["record"]
    assert record["gate"] == "passed"
