"""A control log is judged while its writer can still repair it.

A node whose write paths include a test file declares the mutation that check
must fail against, and discharges the declaration by naming the log that
mutation produced. Judging that field only at promotion means a malformed value
— an empty line, a path carrying prose, a log whose run never failed — is read
hours after the worker's process has ended, when the only party who can repair
it is gone. This file asserts the refusal at the write, through the audit entry
point and through the command surface a worker runs itself, and pairs each
refusal with an acceptance so the check is shown to be about the record rather
than about the field being present at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew.node import TaskNode
from reckon.crew.reports import audit_manifest, parse_manifest
from reckon.crew.runs import _write_json, pointer_path

RUN_ID = "r-20260930T213552839178-control-log-at-write"
DECLARATION = "remove the guard so the named case fails"
TEST_PATH = "tests/test_example_check.py"


def _node(**overrides: object) -> TaskNode:
    fields: dict[str, object] = {
        "id": "control-log-check",
        "goal": "judge the control log at the write",
        "plan": "fixture",
        "write_paths": [TEST_PATH],
        "negative_control": DECLARATION,
    }
    fields.update(overrides)
    return TaskNode(**fields)  # type: ignore[arg-type]


def _manifest(
    *log_lines: str, status: str = "complete", changed: str = TEST_PATH
) -> str:
    lines = [
        "node: control-log-check",
        f"status: {status}",
        "commits: 1a2b3c4",
        f"changed_paths: {changed}",
        "tests: pytest tests/test_example_check.py -> 1 passed",
        "test_logs: /tmp/control-log-check.log",
    ]
    lines.extend(log_lines)
    return "\n".join(lines) + "\n"


def _findings(body: str, node: TaskNode | None, manifest_path: Path | None = None):
    return audit_manifest(body, node, manifest_path=manifest_path)["findings"]


def _red_log(tmp_path: Path, body: str = f"{DECLARATION}\n1 failed\nEXIT=1\n") -> Path:
    path = tmp_path / "red.log"
    path.write_text(body, encoding="utf-8")
    return path


def test_an_empty_control_log_is_refused_at_the_write(tmp_path: Path) -> None:
    findings = _findings(_manifest("negative_control_log:"), _node())

    assert len(findings) == 1
    assert "negative_control_log" in findings[0]
    assert "empty" in findings[0]


def test_a_path_carrying_prose_is_refused_rather_than_read_as_a_path(
    tmp_path: Path,
) -> None:
    log = _red_log(tmp_path)
    body = _manifest(f"negative_control_log: {log} (first line: {DECLARATION})")

    findings = _findings(body, _node())

    assert len(findings) == 1
    assert "negative_control_log" in findings[0]
    assert "does not resolve" in findings[0]


def test_a_mapping_of_control_sub_keys_is_refused(tmp_path: Path) -> None:
    log = _red_log(tmp_path)
    body = _manifest(
        "negative_control_log:",
        "  declared: remove the guard",
        f"  log: {log}",
        "  result: 1 failed, exit 1",
    )

    findings = _findings(body, _node())

    assert len(findings) == 1
    assert "negative_control_log" in findings[0]
    assert "dict" in findings[0]


def test_a_heading_above_the_path_composes_to_the_bare_path_and_is_accepted(
    tmp_path: Path,
) -> None:
    """The heading form is a comment to the reader, so the field is the path.

    The value the parser hands every consumer is the path alone, which is the
    shape the field is required to carry, so a heading line above it is a
    presentation of the path rather than a second value — refusing it would
    refuse the bare path the same bytes carry.
    """
    log = _red_log(tmp_path)
    body = _manifest("negative_control_log:", "  ## control", f"  {log}")

    assert parse_manifest(body)["negative_control_log"] == str(log)
    assert _findings(body, _node()) == []


def test_prose_written_before_the_path_is_refused(tmp_path: Path) -> None:
    log = _red_log(tmp_path)
    body = _manifest("negative_control_log:", "  the log for this node", f"  {log}")

    findings = _findings(body, _node())

    assert len(findings) == 1
    assert "negative_control_log" in findings[0]
    assert "does not resolve" in findings[0]


def test_a_bullet_list_of_control_parts_is_refused(tmp_path: Path) -> None:
    log = _red_log(tmp_path)
    body = _manifest(
        "negative_control_log:", "  - declared: remove the guard", f"  - {log}"
    )

    findings = _findings(body, _node())

    assert len(findings) == 1
    assert "negative_control_log" in findings[0]
    assert "list" in findings[0]


def test_a_named_log_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    body = _manifest(f"negative_control_log: {tmp_path / 'absent.log'}")

    findings = _findings(body, _node())

    assert len(findings) == 1
    assert "does not resolve" in findings[0]


def test_a_log_with_no_exit_record_is_refused(tmp_path: Path) -> None:
    log = _red_log(tmp_path, f"{DECLARATION}\n1 failed\n")

    findings = _findings(_manifest(f"negative_control_log: {log}"), _node())

    assert len(findings) == 1
    assert "records no exit status" in findings[0]


def test_a_log_recording_a_zero_exit_is_refused(tmp_path: Path) -> None:
    log = _red_log(tmp_path, f"{DECLARATION}\n1 passed\nEXIT=0\n")

    findings = _findings(_manifest(f"negative_control_log: {log}"), _node())

    assert len(findings) == 1
    assert "EXIT=0" in findings[0]


def test_a_log_ending_in_a_non_zero_exit_is_accepted(tmp_path: Path) -> None:
    log = _red_log(tmp_path, f"{DECLARATION}\n1 failed, 12 passed\nEXIT=1\n\n")

    assert _findings(_manifest(f"negative_control_log: {log}"), _node()) == []


def test_a_relative_path_resolves_beside_the_manifest_it_is_written_in(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.md"
    _red_log(tmp_path)
    body = _manifest("negative_control_log: red.log")
    manifest_path.write_text(body, encoding="utf-8")

    assert _findings(body, _node(), manifest_path) == []


def test_the_nodes_own_manifest_path_resolves_a_relative_log(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.md"
    _red_log(tmp_path)
    body = _manifest("negative_control_log: red.log")
    manifest_path.write_text(body, encoding="utf-8")

    assert _findings(body, _node(manifest_path=str(manifest_path))) == []


def test_a_run_that_declared_no_control_is_not_judged(tmp_path: Path) -> None:
    body = _manifest("negative_control_log:")

    assert _findings(body, _node(negative_control="")) == []
    assert _findings(body, None) == []


def test_a_declared_none_exempts_the_control_log(tmp_path: Path) -> None:
    body = _manifest("negative_control_log:")

    assert _findings(body, _node(negative_control="none: no mutation applies")) == []


def test_a_node_writing_no_check_is_not_judged(tmp_path: Path) -> None:
    body = _manifest("negative_control_log:", changed="reckon/crew/reports.py")

    assert _findings(body, _node(write_paths=["reckon/crew/reports.py"])) == []


def test_a_recorded_waiver_leaves_the_log_to_a_reader(tmp_path: Path) -> None:
    body = _manifest(
        "negative_control_log:",
        "negative_control_waiver: the control ran on a lane whose log was reaped",
    )

    assert _findings(body, _node()) == []


def test_an_empty_waiver_records_nothing_and_the_log_is_still_judged(
    tmp_path: Path,
) -> None:
    body = _manifest("negative_control_log:", "negative_control_waiver:")

    findings = _findings(body, _node())

    assert len(findings) == 1
    assert "empty" in findings[0]


def test_a_run_that_has_not_claimed_completion_is_not_judged(tmp_path: Path) -> None:
    body = _manifest("negative_control_log:", status="blocked")

    assert _findings(body, _node()) == []


# ── The same judgments through the command a worker runs itself ─────────────


def _check(run_id: str = RUN_ID):
    return CliRunner().invoke(cli_main, ["crew", "check-manifest", "--run", run_id])


def _home_tree(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*")}


@pytest.fixture()
def cli_run(isolated_reckon_home: Path, tmp_path: Path) -> dict[str, Path]:
    """A run whose pointer, manifest and log all live inside the temporary home."""
    log = _red_log(tmp_path)
    manifest_path = tmp_path / "manifest.md"
    pointer = pointer_path(RUN_ID)
    assert pointer.is_relative_to(isolated_reckon_home)
    return {"log": log, "manifest": manifest_path, "pointer": pointer}


def _write_cli_run(
    cli_run: dict[str, Path], body: str, **node_overrides: object
) -> None:
    node = _node(manifest_path=str(cli_run["manifest"]), **node_overrides)
    cli_run["manifest"].write_text(body, encoding="utf-8")
    _write_json(
        cli_run["pointer"],
        {
            "run_id": RUN_ID,
            "project": "sample",
            "repo": str(cli_run["manifest"].parent),
            "worktree": str(cli_run["manifest"].parent),
            "base_sha": "0" * 40,
            "launch": "in-harness",
            "role": "implement",
            "node": {
                "id": node.id,
                "plan": node.plan,
                "section": "s9",
                "write_paths": list(node.write_paths),
                "negative_control": node.negative_control,
            },
            "manifest_path": str(cli_run["manifest"]),
        },
    )


def test_the_command_refuses_an_empty_control_log(
    cli_run: dict[str, Path], isolated_reckon_home: Path
) -> None:
    _write_cli_run(cli_run, _manifest("negative_control_log:"))

    result = _check()

    assert result.exit_code != 0, result.output
    findings = json.loads(result.output)["findings"]
    assert len(findings) == 1
    assert "negative_control_log" in findings[0]
    assert "empty" in findings[0]


def test_the_command_accepts_a_log_ending_in_a_non_zero_exit(
    cli_run: dict[str, Path],
) -> None:
    _write_cli_run(cli_run, _manifest(f"negative_control_log: {cli_run['log']}"))

    result = _check()

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["findings"] == []


def test_the_command_accepts_a_run_that_declared_no_control(
    cli_run: dict[str, Path],
) -> None:
    _write_cli_run(cli_run, _manifest("negative_control_log:"), negative_control="")

    result = _check()

    assert result.exit_code == 0, result.output


def test_the_command_accepts_a_recorded_waiver(cli_run: dict[str, Path]) -> None:
    _write_cli_run(
        cli_run,
        _manifest(
            "negative_control_log:",
            "negative_control_waiver: the control ran on a lane whose log was reaped",
        ),
    )

    result = _check()

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["findings"] == []


def test_the_command_refuses_a_path_carrying_prose(cli_run: dict[str, Path]) -> None:
    body = _manifest(
        f"negative_control_log: {cli_run['log']} (first line: {DECLARATION})"
    )
    _write_cli_run(cli_run, body)

    result = _check()

    assert result.exit_code != 0, result.output
    findings = json.loads(result.output)["findings"]
    assert "does not resolve" in findings[0]


def test_the_command_writes_no_state_beside_the_run_it_reads(
    cli_run: dict[str, Path], isolated_reckon_home: Path
) -> None:
    _write_cli_run(cli_run, _manifest(f"negative_control_log: {cli_run['log']}"))
    before = _home_tree(isolated_reckon_home)

    result = _check()

    assert result.exit_code == 0, result.output
    assert _check().exit_code == 0
    assert _home_tree(isolated_reckon_home) == before
