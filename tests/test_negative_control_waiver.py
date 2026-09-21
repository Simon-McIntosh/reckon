"""A reasoned control-match waiver leaves a filterable, durable ledger record."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import crew, ledger
from reckon.cli import main as cli_main
from reckon.crew.runs import _write_json, pointer_path, read_pointer
from tests.test_crew_promotion_requires_review import (
    PROJECT,
    _row,
    _store_complete_review,
    _write_complete_pointer,
)

pytest_plugins = ("tests.test_crew_promotion_requires_review",)

DECLARATION = "removing the guard turns the fixture red"
PARAPHRASE = "bypassing the guard produced the expected assertion failure"
REASON = "the control ran the declared mutation and described it in other words"
RUN_ID = "r-20260921T060500000000-control"


def _prepare(
    repository: Path,
    tmp_path: Path,
    *,
    log_text: str = PARAPHRASE,
    declaration: str = DECLARATION,
    writes_test: bool = True,
) -> Path:
    _write_complete_pointer(repository, tmp_path, RUN_ID)
    _store_complete_review(RUN_ID)
    red_log = tmp_path / "control.log"
    red_log.write_text(log_text + "\n1 failed\n", encoding="utf-8")
    record = read_pointer(RUN_ID)
    record["node"]["write_paths"] = ["tests/test_guard.py"] if writes_test else []
    record["node"]["negative_control"] = declaration
    manifest = Path(record["manifest_path"])
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + f"negative_control_log: {red_log.name}\n",
        encoding="utf-8",
    )
    delivered_log = manifest.parent / red_log.name
    delivered_log.write_bytes(red_log.read_bytes())
    _write_json(pointer_path(RUN_ID), record)
    return delivered_log


def _complete_cli(repository: Path, tmp_path: Path, *extra: str):
    green_log = tmp_path / "passing.log"
    green_log.write_text("1 passed\n", encoding="utf-8")
    return CliRunner().invoke(
        cli_main,
        [
            "crew",
            "complete",
            "--run",
            RUN_ID,
            "--gate",
            "passed",
            "--checkout-path",
            str(repository),
            "--gate-command",
            "pytest tests/test_guard.py",
            "--gate-exit-status",
            "0",
            "--gate-log-path",
            str(green_log),
            *extra,
        ],
    )


def _assert_not_promoted(repository: Path) -> None:
    assert pointer_path(RUN_ID).exists()
    assert ledger.load(PROJECT, repository)[0]["runs"] == []


def test_complete_help_names_reasoned_control_waiver() -> None:
    result = CliRunner().invoke(cli_main, ["crew", "complete", "--help"])
    assert result.exit_code == 0
    assert "--waive-negative-control REASON" in result.output


def test_mismatch_without_waiver_still_refuses(
    repository: Path, tmp_path: Path
) -> None:
    _prepare(repository, tmp_path)
    result = _complete_cli(repository, tmp_path)
    assert result.exit_code == 1
    assert "does not name it" in result.output
    assert "--waive-negative-control REASON" in result.output
    _assert_not_promoted(repository)


def test_reasoned_waiver_promotes_and_persists_control_evidence(
    repository: Path, tmp_path: Path
) -> None:
    red_log = _prepare(repository, tmp_path)
    original_log = red_log.read_bytes()
    original_manifest = Path(read_pointer(RUN_ID)["manifest_path"]).read_bytes()
    result = _complete_cli(repository, tmp_path, "--waive-negative-control", REASON)
    assert result.exit_code == 0, result.output
    control = _row(repository, RUN_ID)["negative_control"]
    assert control["verdict"] == "waived"
    assert control["reason"] == REASON
    assert control["declaration"] == DECLARATION
    assert control["log"] == red_log.name
    assert control["resolved_log"] == str(red_log.resolve())
    assert red_log.read_bytes() == original_log
    assert (red_log.parent / f"{RUN_ID}.md").read_bytes() == original_manifest
    assert not pointer_path(RUN_ID).exists()


def test_matching_control_records_matched_without_waiver(
    repository: Path, tmp_path: Path
) -> None:
    _prepare(repository, tmp_path, log_text=DECLARATION)
    result = _complete_cli(repository, tmp_path)
    assert result.exit_code == 0, result.output
    control = _row(repository, RUN_ID)["negative_control"]
    assert control["verdict"] == "matched"
    assert "reason" not in control


@pytest.mark.parametrize(
    "case", ["matched", "no-test", "no-declaration", "none", "nonpassing"]
)
def test_waiver_without_control_match_refusal_is_refused(
    repository: Path, tmp_path: Path, case: str
) -> None:
    _prepare(
        repository,
        tmp_path,
        log_text=DECLARATION if case == "matched" else PARAPHRASE,
        declaration={"no-declaration": "", "none": "none: no applicable mutation"}.get(
            case, DECLARATION
        ),
        writes_test=case != "no-test",
    )
    extra = (
        ("--gate", "not-run", "--outcome", "check was not run")
        if case == "nonpassing"
        else ()
    )
    result = _complete_cli(
        repository, tmp_path, "--waive-negative-control", REASON, *extra
    )
    assert result.exit_code == 1
    assert "no negative-control match refusal" in result.output
    assert REASON in result.output
    _assert_not_promoted(repository)


@pytest.mark.parametrize("reason", ["", " \t\n "])
@pytest.mark.parametrize("log_text", [PARAPHRASE, DECLARATION])
def test_empty_waiver_reason_is_refused(
    repository: Path, tmp_path: Path, reason: str, log_text: str
) -> None:
    _prepare(repository, tmp_path, log_text=log_text)
    result = _complete_cli(repository, tmp_path, "--waive-negative-control", reason)
    assert result.exit_code == 1
    assert "requires a non-empty reason" in result.output
    _assert_not_promoted(repository)


def test_waiver_option_requires_an_argument(repository: Path, tmp_path: Path) -> None:
    _prepare(repository, tmp_path)
    result = _complete_cli(repository, tmp_path, "--waive-negative-control")
    assert result.exit_code == 2
    assert "requires an argument" in result.output
    _assert_not_promoted(repository)


@pytest.mark.parametrize("missing", ["path", "file"])
def test_waiver_cannot_substitute_for_missing_control_log(
    repository: Path, tmp_path: Path, missing: str
) -> None:
    red_log = _prepare(repository, tmp_path)
    if missing == "path":
        manifest = Path(read_pointer(RUN_ID)["manifest_path"])
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                f"negative_control_log: {red_log.name}\n", ""
            ),
            encoding="utf-8",
        )
    else:
        red_log.unlink()
    result = _complete_cli(repository, tmp_path, "--waive-negative-control", REASON)
    assert result.exit_code == 1
    assert "negative_control_log" in result.output
    _assert_not_promoted(repository)


def test_python_entry_point_persists_the_same_waiver(
    repository: Path, tmp_path: Path
) -> None:
    _prepare(repository, tmp_path)
    crew.complete(
        RUN_ID,
        gate="passed",
        root=repository,
        negative_control_waiver=REASON,
    )
    assert _row(repository, RUN_ID)["negative_control"]["verdict"] == "waived"
