"""The snapshot census tallies, its positive controls, and its single-command bound."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
CHECK_PATH = SCRIPTS_DIR / "check_snapshot_assertions.py"


def _load_check() -> object:
    spec = importlib.util.spec_from_file_location(
        "snapshot_assertion_check", CHECK_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_check = _load_check()
_run_census = _check.run_census
_report = _check.report


def _kind(results, kind: str) -> list:
    return [result for result in results if result.kind == kind]


def test_every_specimen_has_a_passing_positive_control() -> None:
    for result in _run_census():
        assert result.baseline_pass, result.name


def test_recorded_snapshot_instances_flag_three_of_four() -> None:
    recorded = _kind(_run_census(), "recorded-snapshot")

    assert len(recorded) == 4
    flagged = [result for result in recorded if result.flagged]
    assert len(flagged) == 3
    kept = [result.name for result in recorded if not result.flagged]
    assert kept == ["nine literal implementation strings across two components"]


def test_object_substitution_instance_flags() -> None:
    substituted = _kind(_run_census(), "object-substitution")

    assert len(substituted) == 1
    assert substituted[0].flagged


def test_carve_outs_survive_both_before_and_after() -> None:
    carve_outs = _kind(_run_census(), "carve-out")

    assert len(carve_outs) == 4
    assert not any(result.flagged for result in carve_outs)
    assert all(result.perturbed_pass for result in carve_outs)


def test_report_states_both_tallies_as_numbers() -> None:
    text = _report(_run_census())

    assert "snapshot instances flagged: 4 of 5" in text
    assert "carve-outs flagged: 0 of 4" in text


def test_census_runs_as_a_single_command_naming_its_moment() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHECK_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Bound to the moment" in completed.stdout
    assert "snapshot instances flagged: 4 of 5" in completed.stdout
    assert "carve-outs flagged: 0 of 4" in completed.stdout
