"""A gate command that names no program is refused while its writer can repair it.

A worker records the command its gate actually ran on the manifest's ``tests``
line. A run that recorded ``-m pytest tests/test_a.py`` — the interpreter left
off — states a command no shell can start: the lander replays the text through
``sh -c``, the shell reads ``-m`` as an option with no program to receive it, and
the landing stalls until a coordinator rewrites the command by hand. The refusal
belongs at delivery, while the worker still holds the turn that can record the
literal command, rather than at the landing hours later.

The delivery check and promotion read one rule, so both refuse in the same
words. A run whose gate is declared through ``crew declare-gate`` states the
command its arms actually ran, so the declaration is the command the replay
executes and the recorded line is judged in its place.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import crew
from reckon.cli import main as cli_main
from reckon.crew import promotion_checks, promotion_records, runs
from reckon.crew.runs import _write_json, pointer_path, runs_dir

RUN_ID = "r-20261123T000000000000-gate-names-its-program"
BASE_REVISION = "b1c2d3e4" * 5
RECORDED_WITHOUT_A_PROGRAM = "-m pytest tests/test_a.py"
DECLARED = ".venv/bin/python -m pytest tests/test_a.py"


def _manifest(tests: str) -> str:
    """A complete delivery whose only question is the command it recorded."""
    return (
        "node: gate-names-its-program\n"
        "status: complete\n"
        "commits: b1c2d3e4\n"
        "changed_paths: none\n"
        f"tests: {tests}\n"
        "artifacts: none\n"
        "evidence_inputs: none\n"
        "outside_scope: none\n"
        "messages_received: none\n"
        "blockers: none\n"
    )


def _delivered(tmp_path: Path, tests: str, *, declared: str = "") -> Path:
    """Write the manifest into its own run directory, and that run's pointer."""
    directory = runs_dir() / RUN_ID
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(_manifest(tests), encoding="utf-8")
    tree = tmp_path / "run-tree"
    tree.mkdir(exist_ok=True)
    record = {
        "run_id": RUN_ID,
        "project": "sample",
        "repo": str(tree),
        "worktree": str(tree),
        "base_sha": BASE_REVISION,
        "launch": "in-harness",
        "role": "implement",
        "node": {
            "id": "gate-names-its-program",
            "plan": "fixture",
            "section": "s53",
            "write_paths": [],
        },
        "manifest_path": str(manifest),
    }
    if declared:
        record["gate_declaration"] = {"command": declared}
    _write_json(pointer_path(RUN_ID), record)
    return manifest


def _check() -> tuple[int, list[str]]:
    """Run the delivery check on the fixture run and read its findings."""
    result = CliRunner().invoke(
        cli_main, ["crew", "check", "--manifest", "--run", RUN_ID]
    )
    try:
        return result.exit_code, json.loads(result.output)["findings"]
    except (ValueError, KeyError):
        return result.exit_code, []


def test_a_recorded_command_that_begins_with_an_option_is_refused(
    tmp_path: Path, isolated_reckon_home: Path
) -> None:
    """The recorded line is replayed through a shell, which cannot start it."""
    _delivered(tmp_path, RECORDED_WITHOUT_A_PROGRAM)

    exit_code, findings = _check()

    assert exit_code != 0, findings
    refusal = " ".join(findings)
    assert RECORDED_WITHOUT_A_PROGRAM in refusal, refusal
    assert "declare-gate" in refusal, refusal
    assert DECLARED in refusal, refusal

    # Promotion reads the same rule from the same file, so a delivery refused
    # here is refused there in the same words rather than re-described.
    with pytest.raises(crew.CrewError) as promoted:
        promotion_checks._require_no_manifest_fault(RUN_ID, runs.read_pointer(RUN_ID))
    assert str(promoted.value).count("-m pytest tests/test_a.py") >= 1


def test_a_command_that_names_its_program_is_accepted(
    tmp_path: Path, isolated_reckon_home: Path
) -> None:
    """What must keep passing: the literal command, interpreter and all."""
    _delivered(tmp_path, DECLARED)

    exit_code, findings = _check()

    assert exit_code == 0, findings
    assert findings == [], findings


def test_a_declared_gate_supplies_the_program_and_is_replayed(
    tmp_path: Path, isolated_reckon_home: Path
) -> None:
    """A run whose gate is declared replays the declared command."""
    _delivered(tmp_path, RECORDED_WITHOUT_A_PROGRAM, declared=DECLARED)

    exit_code, findings = _check()

    assert exit_code == 0, findings
    assert findings == [], findings
    record = runs.read_pointer(RUN_ID)
    gate, _commits = promotion_records._default_gate_evidence_from_manifest(
        record,
        verdict="passed",
        gate_check={
            "command": "",
            "exit_status": None,
            "log_path": "",
            "log_digest": "",
        },
        commits=(),
    )
    assert gate["command"] == DECLARED, gate
