"""A suite arm the lander cannot read is refused at delivery.

The manifest template asks for one inline JSON observation per suite arm. A
worker that writes the pair as prose — a sentence about the suite, or a bare
command string — states no revision, no exit status and no failure list, so the
landing guard refuses it as no evidence at all. That refusal used to arrive at
landing, hours after the worker's process had ended, when the only party left to
restructure the record was a coordinator: the manifest check declared both arms
as ``text`` or ``mapping`` and passed them.

The check now reads the arm through the same reader the landing guard reads, and
refuses text where the flight declared a suite command. ``crew check
--manifest`` is the surface a worker runs while it still holds the pen, so the
field is repaired before delivery rather than after.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew import queue_landing, reports
from reckon.crew.runs import _write_json, pointer_path, runs_dir

RUN_ID = "r-20261115T000000000000-suite-arm-in-prose"
BASE_REVISION = "b1c2d3e4" * 5
HEAD_REVISION = "a9f8e7d6" * 5
DECLARED = (
    "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q tests/test_alpha.py"
)
# The guard's own words for an arm that states no observation. Pinned here
# because the check gained a refusal: what the landing reader says about a value
# it cannot read must not move with it.
NO_OBSERVATION = (
    "red-suite: the manifest records no after_suite observation of the delivered "
    "head, so the run states no gate result"
)


def _observation(revision: str) -> dict[str, object]:
    """One arm as the manifest template defines it."""
    return {
        "revision": revision,
        "command": DECLARED,
        "exit_status": 0,
        "log_path": "",
        "log_digest": "sha256:0f1e2d3c",
        "completed": True,
        "failure_count": 0,
        "failure_ids": [],
    }


def _manifest(after: str) -> str:
    """A complete delivery whose baseline arm is an observation and head is not."""
    return (
        "node: suite-arm-in-prose\n"
        "status: complete\n"
        "commits: b1c2d3e4\n"
        "tests: pytest -q tests/test_alpha.py -> 3 passed\n"
        f"baseline_suite: {json.dumps(_observation(BASE_REVISION))}\n"
        f"after_suite: {after}\n"
        "artifacts: none\n"
        "evidence_inputs: none\n"
        "outside_scope: none\n"
        "messages_received: none\n"
        "blockers: none\n"
    )


def _delivered(tmp_path: Path, manifest_text: str) -> Path:
    """Write the manifest into its own run directory, and that run's pointer.

    The manifest lives where a delivered one lives — inside its run's directory
    — so the check reaches the run the same way it reaches a real one, and the
    pointer records the same suite command dispatch resolves from a flight's
    declaration. The tree the run claims is a plain directory: this case asks
    about a manifest's shape, not about a commit.
    """
    directory = runs_dir() / RUN_ID
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(manifest_text, encoding="utf-8")
    tree = tmp_path / "run-tree"
    tree.mkdir()
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": "sample",
            "repo": str(tree),
            "worktree": str(tree),
            "base_sha": BASE_REVISION,
            "launch": "in-harness",
            "role": "implement",
            "suite_command": DECLARED,
            "suite_command_resolved": DECLARED,
            "suite_gate": "armed",
            "node": {
                "id": "suite-arm-in-prose",
                "plan": "fixture",
                "section": "s14",
                "write_paths": [],
            },
            "manifest_path": str(manifest),
        },
    )
    return manifest


def _findings(result) -> list[str]:
    return json.loads(result.output)["findings"]


def test_a_prose_after_arm_is_refused_naming_the_field(
    tmp_path: Path, isolated_reckon_home: Path
) -> None:
    """A sentence about the suite carries no observation the lander can read."""
    _delivered(tmp_path, _manifest("the suite ran green at both arms"))

    result = CliRunner().invoke(
        cli_main, ["crew", "check", "--manifest", "--run", RUN_ID]
    )

    assert result.exit_code != 0, result.output
    findings = _findings(result)
    assert findings == ["after_suite must be an inline JSON object"], findings
    _, refusal = queue_landing._after_suite_arm("the suite ran green at both arms")
    assert refusal == NO_OBSERVATION


def test_a_bare_command_after_arm_is_refused_the_same_way(
    tmp_path: Path, isolated_reckon_home: Path
) -> None:
    """A command is not an observation: it names nothing the run measured."""
    _delivered(tmp_path, _manifest(DECLARED))

    result = CliRunner().invoke(
        cli_main, ["crew", "check", "--manifest", "--run", RUN_ID]
    )

    assert result.exit_code != 0, result.output
    findings = _findings(result)
    assert findings == ["after_suite must be an inline JSON object"], findings
    _, refusal = queue_landing._after_suite_arm(DECLARED)
    assert refusal == NO_OBSERVATION


def test_the_json_observation_passes_and_the_lander_reads_it(
    tmp_path: Path, isolated_reckon_home: Path
) -> None:
    """What the check must keep accepting: the pair the template asks for."""
    manifest = _delivered(tmp_path, _manifest(json.dumps(_observation(HEAD_REVISION))))

    result = CliRunner().invoke(
        cli_main, ["crew", "check", "--manifest", "--run", RUN_ID]
    )

    assert result.exit_code == 0, _findings(result)
    assert _findings(result) == []
    audit = reports.audit_manifest(
        manifest.read_text(encoding="utf-8"), manifest_path=manifest
    )
    observation, refusal = queue_landing._after_suite_arm(
        audit["manifest"]["after_suite"]
    )
    assert refusal == ""
    assert observation == _observation(HEAD_REVISION)
