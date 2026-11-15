"""A delivered suite arm is re-pointed through a verb, not a hand edit.

An arm whose revision is prose or an abbreviation cannot be compared with the
base it must match, and the worker that recorded it has already ended its turn —
so the coordinator has to be able to correct it without losing what the worker
delivered. The verb rewrites the arm's revision in place, keeps the file as
delivered beside it, carries the arm's other fields over untouched, and records
the reason in the run directory. A revision that is still not an object id, an
arm the manifest does not carry, and a missing reason are all refused rather
than half-repaired.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import crew
from reckon.cli import main as cli_main
from reckon.crew import manifest_faults, reports, runs

RUN_ID = "r-arm-repair"
BASE = "0a524ce6f8b1d3a4e5f60718293a4b5c6d7e8f90"
HEAD = "1b635df7a9c2e4b6d8f0a1c3e5b7d9f0a2c4e6b8"
COMMAND = "python -m pytest -q tests/test_new.py"


def _arm(revision: str, *, exit_status: Any = 0) -> dict[str, Any]:
    return {
        "revision": revision,
        "command": COMMAND,
        "exit_status": exit_status,
        "completed": True,
        "failure_count": 0,
        "failure_ids": [],
        "log_path": "/scratch/base.log",
    }


def _manifest_text(baseline: str, after: str) -> str:
    return (
        "node: arm-repair\n"
        "status: complete\n"
        f"commits: {HEAD}\n"
        "changed_paths: tests/test_new.py\n"
        f"tests: {COMMAND}\n"
        f"baseline_suite: {baseline}\n"
        f"after_suite: {after}\n"
    )


@pytest.fixture(name="run")
def run_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A live run whose manifest records its baseline arm as prose."""
    home = tmp_path / "crew-home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    manifest = tmp_path / "manifest.md"
    text = _manifest_text(
        json.dumps(_arm(f"{BASE} (the base)")),
        json.dumps(_arm(HEAD)),
    )
    manifest.write_text(text, encoding="utf-8")
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
    return {"manifest": manifest, "text": text, "tmp": tmp_path}


def _parsed(manifest: Path) -> dict[str, Any]:
    return reports.parse_manifest(manifest.read_text(encoding="utf-8"))


def test_the_arm_is_repointed_and_the_delivered_file_is_kept(
    run: dict[str, Any],
) -> None:
    manifest = run["manifest"]

    record = runs.repair_manifest_arm(
        RUN_ID,
        "baseline_suite",
        revision=BASE,
        reason="the arm recorded the base as prose",
    )

    as_delivered = manifest.with_name(manifest.name + ".asdelivered")
    assert as_delivered.read_text(encoding="utf-8") == run["text"]
    assert record["previous_manifest"] == str(as_delivered)
    assert record["previous_arm"] == json.dumps(_arm(f"{BASE} (the base)"))
    assert record["revision"] == BASE
    assert record["reason"] == "the arm recorded the base as prose"
    repaired = _parsed(manifest)["baseline_suite"]
    assert repaired["revision"] == BASE
    assert repaired["command"] == COMMAND
    assert repaired["exit_status"] == 0
    assert repaired["log_path"] == "/scratch/base.log"
    assert (
        json.loads((runs.run_dir(RUN_ID) / runs.ARM_REPAIR_RECORD_NAME).read_text())[
            "arm"
        ]
        == "baseline_suite"
    )


def test_the_repointed_arm_is_no_longer_a_fault(run: dict[str, Any]) -> None:
    """The repair answers the finding the delivery check raised, by name."""
    before = manifest_faults.manifest_faults(_parsed(run["manifest"]))
    assert [fault.field for fault in before] == ["baseline_suite"]

    runs.repair_manifest_arm(
        RUN_ID,
        "baseline_suite",
        revision=BASE,
        reason="the arm recorded the base as prose",
    )

    assert manifest_faults.manifest_faults(_parsed(run["manifest"])) == []


def test_a_second_repair_keeps_the_first_as_delivered_file(run: dict[str, Any]) -> None:
    """The as-delivered copy is the worker's own file, never a repaired one."""
    manifest = run["manifest"]
    runs.repair_manifest_arm(
        RUN_ID,
        "baseline_suite",
        revision=BASE,
        reason="the arm recorded the base as prose",
    )
    runs.repair_manifest_arm(
        RUN_ID,
        "after_suite",
        revision=BASE,
        reason="both arms were recorded at the base",
    )

    as_delivered = manifest.with_name(manifest.name + ".asdelivered")
    assert as_delivered.read_text(encoding="utf-8") == run["text"]
    assert _parsed(manifest)["after_suite"]["revision"] == BASE


@pytest.mark.parametrize(
    "revision",
    [BASE[:9], f"{BASE} (the base)", ""],
    ids=["abbreviated", "prose", "empty"],
)
def test_a_revision_that_is_not_an_object_id_is_refused(
    run: dict[str, Any], revision: str
) -> None:
    with pytest.raises(crew.CrewError):
        runs.repair_manifest_arm(
            RUN_ID, "baseline_suite", revision=revision, reason="re-pointing"
        )

    assert run["manifest"].read_text(encoding="utf-8") == run["text"]


def test_an_arm_the_manifest_does_not_carry_is_refused(run: dict[str, Any]) -> None:
    run["manifest"].write_text(
        "node: arm-repair\nstatus: complete\nhead_suite: none\n", encoding="utf-8"
    )

    with pytest.raises(crew.CrewError) as refusal:
        runs.repair_manifest_arm(
            RUN_ID, "baseline_suite", revision=BASE, reason="re-pointing"
        )

    assert "no top-level baseline_suite line" in str(refusal.value)


def test_an_unknown_arm_name_is_refused(run: dict[str, Any]) -> None:
    with pytest.raises(crew.CrewError):
        runs.repair_manifest_arm(
            RUN_ID, "head_suite", revision=BASE, reason="re-pointing"
        )


def test_a_missing_reason_is_refused(run: dict[str, Any]) -> None:
    with pytest.raises(crew.CrewError):
        runs.repair_manifest_arm(RUN_ID, "baseline_suite", revision=BASE, reason="  ")


def test_the_command_repoints_an_arm_from_the_shell(run: dict[str, Any]) -> None:
    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "repair",
            "--run",
            RUN_ID,
            "--arm",
            "baseline_suite",
            "--revision",
            BASE,
            "--reason",
            "the arm recorded the base as prose",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["arm"] == "baseline_suite"
    assert payload["revision"] == BASE
    assert _parsed(run["manifest"])["baseline_suite"]["revision"] == BASE
