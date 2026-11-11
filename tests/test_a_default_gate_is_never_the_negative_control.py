"""A merge's default gate log is never the negative control.

A run's manifest names its deliberately failing negative control both as
``negative_control_log`` and as the first entry of ``test_logs``. A promotion
given no gate log took ``test_logs[0]`` as its head gate, read the negative
control's expected failure as a failure the baseline did not cover, and refused
a sound run on the evidence that exists to prove the check can fail.

The cases below hold the default to the manifest's own statement of what it
measured: the ``after_suite`` arm's log wins, the negative control is skipped
when no arm names a log, and a manifest offering nothing but its negative
control has no default at all and says which flag supplies one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew.node import CrewError
from reckon.crew.promotion_records import _default_gate_evidence_from_manifest


def _log(tmp_path: Path, name: str, exit_status: int) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"1 passed in 0.10s\nEXIT={exit_status}\n", encoding="utf-8")
    return path


def _write(tmp_path: Path, body: str) -> dict:
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(body, encoding="utf-8")
    return {"manifest_path": str(manifest_path)}


HEADER = "node: a-node\nstatus: complete\ntests: python -m pytest -q tests/x.py\n"


def _after_arm(tmp_path: Path, log: Path) -> str:
    return (
        "after_suite: {"
        '"revision": "abc123", '
        '"command": "python -m pytest -q tests/x.py", '
        '"exit_status": 0, "completed": true, "failure_count": 0, '
        f'"failure_ids": [], "log_path": "{log.relative_to(tmp_path)}"'
        "}\n"
    )


def test_the_after_arm_log_wins_over_the_negative_control(tmp_path: Path) -> None:
    """The arm's own log is the default gate, even beside a leading negative control."""
    negative = _log(tmp_path, "negative-final.log", 1)
    after = _log(tmp_path, "after/unit.log", 0)
    record = _write(
        tmp_path,
        HEADER
        + f"test_logs: {negative.name} candidate/unit.log\n"
        + f"negative_control_log: {negative.name}\n"
        + _after_arm(tmp_path, after),
    )

    gate, _ = _default_gate_evidence_from_manifest(
        record, verdict="passed", gate_check=None, commits=()
    )

    assert gate["log_path"] == str(after)
    assert gate["exit_status"] == 0


def test_the_default_skips_the_negative_control_when_no_arm_names_a_log(
    tmp_path: Path,
) -> None:
    """With no after-suite log the next test log stands in, never the negative control."""
    negative = _log(tmp_path, "negative-final.log", 1)
    candidate = _log(tmp_path, "candidate/unit.log", 0)
    record = _write(
        tmp_path,
        HEADER
        + f"test_logs: {negative.name} candidate/unit.log\n"
        + f"negative_control_log: {negative.name}\n",
    )

    gate, _ = _default_gate_evidence_from_manifest(
        record, verdict="passed", gate_check=None, commits=()
    )

    assert gate["log_path"] == str(candidate)
    assert gate["exit_status"] == 0


def test_a_manifest_whose_only_test_log_is_the_negative_control_is_refused(
    tmp_path: Path,
) -> None:
    """No default exists, so the refusal names the flag that supplies one."""
    negative = _log(tmp_path, "negative-final.log", 1)
    record = _write(
        tmp_path,
        HEADER
        + f"test_logs: {negative.name}\n"
        + f"negative_control_log: {negative.name}\n",
    )

    with pytest.raises(CrewError) as refusal:
        _default_gate_evidence_from_manifest(
            record, verdict="passed", gate_check=None, commits=()
        )

    assert "--gate-log-path" in str(refusal.value)
