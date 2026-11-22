"""A run that died during launch is told to discard, never to resume.

A launch refused by the scheduler reaches no model and writes no stream record,
so the run has no session to resume and nothing to continue. The obligation it
raises must name a command that is accepted for it, and must carry the first
line of the launch error so the reader sees the cause without opening the file.

Two arms are measured through the obligations view a coordinator reads. A
launch-failed pointer carries the launch error's first line and a discard
obligation, and that obligation's own command is then run against the same
temporary crew home and must succeed. A run that ended after its first turn is
a different reading — it reached its model and may be continued, so its duty
still names the resume remedy.

The declared negative control is the base revision, where the first case fails
with the resume command named.
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime
from pathlib import Path

from click.testing import CliRunner

from reckon import cli
from reckon.crew import obligations, recovery_classification
from reckon.crew.runs import _write_json, pointer_path, runs_dir

PROJECT = "launch-failed-discard-fixture"
SESSION = "launch-failed-discard-session"
LAUNCH_RUN_ID = "r-launch-failed-told-to-discard"
TURN_RUN_ID = "r-first-turn-ended-told-to-resume"

# The scheduler's refusal, written on two lines so a reader of the obligation
# text can tell the first line from the last.
LAUNCH_ERROR_FIRST = "srun: error: Unable to allocate resources: Connection refused"
LAUNCH_ERROR_SECOND = "retrying the allocation for 10 seconds"


def _pid_the_kernel_cannot_issue() -> int:
    """A pid no process can hold, so the liveness probe answers the same anywhere."""
    try:
        ceiling = int(Path("/proc/sys/kernel/pid_max").read_text().strip())
    except (OSError, ValueError):
        ceiling = 2**22
    return ceiling + 1024


def _build_run(
    run_id: str,
    *,
    phase: str,
    stream_events: list[dict],
    launch_failures: list[dict] | None = None,
) -> dict:
    """One run the crew home holds, launched here, whose pid cannot be alive.

    The stream and stderr are written beside the pointer because the classifier
    reads them: a launch failure is quoted from stderr, and a first-turn end is
    read from the stream's own shape.
    """
    directory = runs_dir() / run_id
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / "stream.jsonl"
    stream.write_text(
        "".join(json.dumps(event) + "\n" for event in stream_events),
        encoding="utf-8",
    )
    stderr = directory / "stderr.log"
    stderr.write_text(
        f"{LAUNCH_ERROR_FIRST}\n{LAUNCH_ERROR_SECOND}\n", encoding="utf-8"
    )
    started = datetime.now(UTC)
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(directory),
        "worktree": str(directory / f"{run_id}-tree"),
        "launch": "cli",
        "argv": ["srun", "--partition=all_debug", "claude", "--", "carry the node"],
        "backend": "srun",
        "role": "implement",
        "session": SESSION,
        "session_id": SESSION,
        "created_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attempt_started_at": started.isoformat(),
        "attempt": 1,
        "phase": phase,
        "pid": _pid_the_kernel_cannot_issue(),
        "launcher_host": socket.gethostname(),
        "log_path": str(stream),
        "stderr_path": str(stderr),
        "manifest_path": str(directory / "manifest.md"),
        "node": {
            "id": run_id,
            "plan": "a-run-told-discard",
            "section": "name-a-remedy-that-works",
            "time_budget": "60m",
            "write_paths": [],
        },
    }
    if launch_failures:
        record["launch_failures"] = launch_failures
    _write_json(pointer_path(run_id), record)
    return record


def _launch_failure() -> dict:
    """The failure record the launcher's own recorder writes for a refused exec."""
    return {
        "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": "launch-failed",
        "backend": "srun",
        "exit_status": 97,
        "argv": ["srun", "--partition=all_debug", "claude", "--", "carry the node"],
        "stderr_tail": f"{LAUNCH_ERROR_FIRST}\n{LAUNCH_ERROR_SECOND}\n",
    }


def _duties() -> list[dict]:
    return obligations.obligations(PROJECT, SESSION)["obligations"]


def _duty_for(run_id: str) -> dict | None:
    return next((item for item in _duties() if item.get("run_id") == run_id), None)


def test_a_launch_failed_run_is_told_to_discard_and_names_its_cause() -> None:
    """The duty names a discard, never a resume, and carries the launch error's cause."""
    record = _build_run(
        LAUNCH_RUN_ID,
        phase="launch-failed",
        # A launch that reached no model wrote no turn.
        stream_events=[],
        launch_failures=[_launch_failure()],
    )

    # The reading the duty rests on, asserted first so a case that reports for
    # some other reason cannot pass as this one.
    row = recovery_classification.classify_pointer(record)
    assert row["classification"] == "launch-failed", row["detail"]

    duty = _duty_for(LAUNCH_RUN_ID)
    assert duty is not None, "the launch failure raised no obligation"
    assert duty["kind"] == "launch-failed"
    assert duty["next_command"] == f"reckon crew discard --run {LAUNCH_RUN_ID}"
    assert "resume" not in duty["next_command"].lower(), duty["next_command"]
    assert LAUNCH_ERROR_FIRST in duty["reason"], duty["reason"]
    assert LAUNCH_ERROR_SECOND not in duty["reason"], (
        "the obligation quoted more than the launch error's first line: "
        f"{duty['reason']!r}"
    )


def test_the_launch_failed_obligation_names_a_command_that_is_accepted() -> None:
    """The command the duty names runs against the run and succeeds."""
    _build_run(
        LAUNCH_RUN_ID,
        phase="launch-failed",
        stream_events=[],
        launch_failures=[_launch_failure()],
    )

    duty = _duty_for(LAUNCH_RUN_ID)
    assert duty is not None, "the launch failure raised no obligation"
    command = str(duty["next_command"])
    arguments = command.split()
    assert arguments[:2] == ["reckon", "crew"], command

    result = CliRunner().invoke(cli.main, arguments[1:])

    assert result.exit_code == 0, (
        f"the named command {command!r} was refused: {result.output}"
    )
    assert not pointer_path(LAUNCH_RUN_ID).exists(), (
        "the named command did not settle the run it named"
    )


def test_a_run_that_ended_after_its_first_turn_is_still_told_to_resume() -> None:
    """A run that reached its model keeps the resume remedy it has today."""
    record = _build_run(
        TURN_RUN_ID,
        phase="working",
        stream_events=[
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "I will carry the node."},
                    ]
                },
            }
        ],
    )

    row = recovery_classification.classify_pointer(record)
    assert row["classification"] == "interrupted", row["detail"]

    duty = _duty_for(TURN_RUN_ID)
    assert duty is not None, "the ended turn raised no obligation"
    assert duty["next_command"] == (
        f"reckon crew resume --run {TURN_RUN_ID} --advice continue"
    )


def test_the_checklist_line_carries_the_launch_cause() -> None:
    """A coordinator reading the prompt sees why the run is offered a discard."""
    from reckon.hooks import coordinator_obligations

    _build_run(
        LAUNCH_RUN_ID,
        phase="launch-failed",
        stream_events=[],
        launch_failures=[_launch_failure()],
    )

    duty = _duty_for(LAUNCH_RUN_ID)
    assert duty is not None, "the launch failure raised no obligation"
    rendered = coordinator_obligations.format_checklist(
        {
            "project": PROJECT,
            "session": SESSION,
            "obligations": [duty],
            "summary": {"oldest_age_seconds": duty["age_seconds"]},
        }
    )

    line = next((text for text in rendered.splitlines() if LAUNCH_RUN_ID in text), "")
    assert line, f"the checklist did not name the run: {rendered!r}"
    assert f"reckon crew discard --run {LAUNCH_RUN_ID}" in line, line
    assert LAUNCH_ERROR_FIRST in line, line
