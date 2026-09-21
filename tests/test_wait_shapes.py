"""The shapes a wait declaration may take, and what the reader says to the rest.

A wait stanza today is documented by one example, and that example is a
scheduler query. A worker in this fleet waits for a job's log more often than
for a scheduler to report that the job left the queue, so the ordinary shape
had no first-class form at all, and three workers writing the block in one hour
produced three values the reader discarded for the same reason: each was a
reasonable reading of the field, and none was an argument vector.

* A three-file condition written as a list of ``{kind, path}`` mappings.
* An argument vector followed by ``exit:0`` under ``wait_terminal``, an exit
  status rather than a state the probe prints.
* Whole shell command lines, which pass a string check and cannot exec, because
  the first element is a program name containing spaces.

None of them read as an error. Each reduced, silently, to the absence of a
probe, so the run reached a coordinator as a worker that had declared nothing —
a failure invisible at the moment it could still be repaired. Two halves close
it, both in the reader:

* ``wait_file`` names a file condition, one path or an array of paths, and its
  probe and terminal are derived from those paths. A condition about files
  needs no argument vector, and the one reader that runs a vector answers it.

* A probe that is present and unreadable is refused by naming the shapes the
  reader does accept, and refused at the declaration rather than at the probe
  so the reason reaches the field the follower already renders.

The refusals must not overshoot, and the negative half is asserted for its own
sake rather than inferred from them: no unrecognised shape may read as a
declaration that carried no probe. The no-op probe refusal and the live-state
terminal refusal stand beside these cases and are asserted here too, because a
reader loosened to admit a new shape would admit them as well.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, resumption

# A three-file condition, the ordinary shape: the worker is parked on a job
# whose logs land in its own worktree.
FILE_CONDITION = "the job writes its three logs"
FILE_PATHS = ["logs/run.log", "logs/state.json", "logs/checkpoint.json"]
FILE_BRIEF = "read the three logs and report the outcome"

MANIFEST_BASELINE_NS = 1_788_853_800_000_000_000
NOW_SECONDS = 1_788_853_920.0

# The shapes the reader accepts, in the words its refusals use. Every refusal
# must name both, because a worker that does not know what was expected cannot
# repair the declaration from the row.
ARGUMENT_VECTOR_SHAPE = "argument vector"
FILE_CONDITION_SHAPE = "wait_file"


def _manifest(body: str) -> str:
    return body if body.endswith("\n") else body + "\n"


def _file_manifest(*, paths) -> str:
    return _manifest(
        "status: waiting\n"
        f"wait_condition: {FILE_CONDITION}\n"
        f"wait_file: {json.dumps(paths) if isinstance(paths, list) else paths}\n"
        f"resume_brief: {FILE_BRIEF}\n"
    )


def _probe_manifest(*, probe, terminal, condition="the scheduler job has left"):
    return _manifest(
        "status: waiting\n"
        f"wait_condition: {condition}\n"
        f"wait_probe: {probe}\n"
        f"wait_terminal: {terminal}\n"
        "resume_brief: read the job result and finish\n"
    )


def _pointer(tmp_path: Path, body: str, *, alive: bool = False) -> dict:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    manifest = tmp_path / "manifest.md"
    manifest.write_text(body, encoding="utf-8")
    os.utime(manifest, ns=(MANIFEST_BASELINE_NS, MANIFEST_BASELINE_NS))
    return {
        "run_id": "r-wait-shapes-fixture",
        "project": "fixture-project",
        "process_alive": alive,
        "phase": "working",
        "attempt": 1,
        "created_at": "2026-09-21T00:00:00+00:00",
        "manifest_baseline_mtime_ns": MANIFEST_BASELINE_NS - 1_000_000_000,
        "manifest_path": str(manifest),
        "log_path": str(tmp_path / "stream.jsonl"),
        "stderr_path": str(tmp_path / "stderr.log"),
        "worktree": str(worktree),
        "backend": "fixture-lane",
        "launch": "cli",
        "argv": ["fixture-agent", "exec"],
        "session_id": "fixture-session",
        "node": {
            "id": "r-wait-shapes-fixture",
            "role": "implement",
            "time_budget": "20m",
            "write_paths": [],
        },
    }


def _declaration(pointer: dict):
    return recovery.external_wait(pointer, now_seconds=NOW_SECONDS)


def _row(pointer: dict) -> dict:
    return recovery.classify_pointer(pointer, now_seconds=NOW_SECONDS)


# ── A file condition is a first-class shape ─────────────────────────────


def test_a_file_condition_over_three_paths_is_accepted_and_read_back(
    tmp_path: Path,
) -> None:
    pointer = _pointer(tmp_path, _file_manifest(paths=FILE_PATHS))

    wait = _declaration(pointer)

    assert wait is not None
    assert wait["valid"] is True
    # Read back with the paths the declaration named: the row a reader sees
    # carries the condition itself, not a summary of it.
    assert wait["files"] == FILE_PATHS
    row = _row(pointer)
    assert row["external_wait"]["files"] == FILE_PATHS
    assert row["manifest_error"] is None


def test_a_file_condition_declared_as_one_path_is_accepted(tmp_path: Path) -> None:
    pointer = _pointer(tmp_path, _file_manifest(paths="logs/run.log"))

    wait = _declaration(pointer)

    assert wait is not None
    assert wait["valid"] is True
    assert wait["files"] == ["logs/run.log"]


def test_the_file_condition_is_answered_by_the_paths_existing(tmp_path: Path) -> None:
    """The condition is read by the default probe, over the real filesystem."""
    pointer = _pointer(tmp_path, _file_manifest(paths=FILE_PATHS))
    worktree = Path(pointer["worktree"])

    pending = _row(pointer)
    assert pending["wait_condition_state"] == "pending"
    assert pending["wait_observed"] == "absent"

    for path in FILE_PATHS:
        target = worktree / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("done\n", encoding="utf-8")

    met = _row(pointer)
    assert met["wait_condition_state"] == "met"
    assert met["wait_observed"] == "present"


def test_a_file_condition_declares_no_terminal_of_its_own(tmp_path: Path) -> None:
    """Its end is the paths existing, so the terminal is derived, not written."""
    pointer = _pointer(tmp_path, _file_manifest(paths=FILE_PATHS))

    wait = _declaration(pointer)

    assert wait is not None
    assert wait["valid"] is True
    # Derived from the paths, so the sweep that lifts the run reads it through
    # the same single probe path every other wait uses.
    assert wait["probe"] == [
        "test",
        "-e",
        *FILE_PATHS[:1],
        "-a",
        "-e",
        *FILE_PATHS[1:2],
        "-a",
        "-e",
        *FILE_PATHS[2:],
    ]


def test_a_file_condition_carrying_a_probe_as_well_is_refused(tmp_path: Path) -> None:
    body = _manifest(
        "status: waiting\n"
        f"wait_condition: {FILE_CONDITION}\n"
        f"wait_file: {json.dumps(FILE_PATHS)}\n"
        'wait_probe: ["squeue", "-h", "-j", "42"]\n'
        f"resume_brief: {FILE_BRIEF}\n"
    )
    pointer = _pointer(tmp_path, body)

    wait = _declaration(pointer)

    assert wait is not None
    assert wait["valid"] is False
    assert "one shape" in wait["error"]


# ── The measured wrong shapes are refused by name ───────────────────────


# The three values three workers wrote, verbatim in shape. Each is a reasonable
# reading of the field and none is an argument vector.
WRONG_SHAPES: dict[str, tuple[str, str]] = {
    "a-list-of-mappings": (
        json.dumps(
            [
                {"kind": "file", "path": "logs/run.log"},
                {"kind": "file", "path": "logs/state.json"},
                {"kind": "file", "path": "logs/checkpoint.json"},
            ]
        ),
        '["done"]',
    ),
    "whole-shell-command-lines": (
        json.dumps(
            [
                "q=$(squeue -h -j 1274028); echo $q",
                "test -f logs/run.log && echo done",
            ]
        ),
        '["done"]',
    ),
}


def _exit_sentinel_form() -> tuple[str, str]:
    return ('["squeue", "-h", "-j", "1274028", "-o", "%T"]', "exit:0")


SHAPE_IDS = [*WRONG_SHAPES, "an-exit-code-terminal"]

# What the low-level probe reader makes of each shape, so the negative-half
# case can state the reduction it defeats rather than assume one. Two are
# reduced to nothing; the command lines survive the string check as a vector
# that cannot exec, which is why the shape reads as plausible until it runs.
_LOW_LEVEL_PROBE: dict[str, str] = {
    "a-list-of-mappings": "[]",
    "whole-shell-command-lines": json.dumps(
        [
            "q=$(squeue -h -j 1274028); echo $q",
            "test -f logs/run.log && echo done",
        ]
    ),
    "an-exit-code-terminal": '["squeue", "-h", "-j", "1274028", "-o", "%T"]',
}


def _absent_probe_manifest() -> str:
    """A declaration that genuinely carries no probe, to read the other reason."""
    return _manifest(
        "status: waiting\n"
        "wait_condition: the scheduler job has left\n"
        'wait_terminal: ["done"]\n'
        "resume_brief: read the job result and finish\n"
    )


def _wrong_shape_body(shape_id: str) -> str:
    if shape_id == "an-exit-code-terminal":
        probe, terminal = _exit_sentinel_form()
    else:
        probe, terminal = WRONG_SHAPES[shape_id]
    return _probe_manifest(probe=probe, terminal=terminal)


def _accepted_shapes_are_named(reason: str) -> None:
    assert ARGUMENT_VECTOR_SHAPE in reason, reason
    assert FILE_CONDITION_SHAPE in reason, reason


@pytest.mark.parametrize("shape_id", SHAPE_IDS)
def test_a_wrong_shape_is_refused_naming_the_shapes_the_reader_accepts(
    tmp_path: Path, shape_id: str
) -> None:
    pointer = _pointer(tmp_path, _wrong_shape_body(shape_id))

    wait = _declaration(pointer)

    assert wait is not None
    assert wait["valid"] is False
    _accepted_shapes_are_named(wait["error"])


@pytest.mark.parametrize("shape_id", SHAPE_IDS)
def test_the_refusal_reaches_the_field_the_follower_renders(
    tmp_path: Path, shape_id: str
) -> None:
    """A declared wait with a dead process reads unreadable, and the reason is
    the one the follower already renders — not a second reading of the probe."""
    pointer = _pointer(tmp_path, _wrong_shape_body(shape_id))

    row = _row(pointer)

    assert row["manifest_error"] is not None
    _accepted_shapes_are_named(row["manifest_error"])
    # The same text is what a reader sees as the instruction: the declaration
    # is unreadable at rest, so the repair is stated rather than inferred.
    assert row["classification"] == "unreadable"
    _accepted_shapes_are_named(row["detail"])


@pytest.mark.parametrize("shape_id", SHAPE_IDS)
def test_no_unrecognised_shape_reads_as_a_declaration_without_a_probe(
    tmp_path: Path, shape_id: str
) -> None:
    """The negative half, asserted as the absence of the silent path.

    The low-level reader still answers an unreadable value with no arguments --
    that is its contract, and the terminal reader keeps it too. What must never
    follow is the declaration concluding that no probe was declared, which is
    how an unrecognised shape read before: the reason it produced named the
    field and nothing else, so a worker was told it had declared nothing when
    it had declared something.

    So the reason is compared against the one a manifest with no probe at all
    produces. The two must differ, because the repairs differ.
    """
    pointer = _pointer(tmp_path / "declared", _wrong_shape_body(shape_id))
    raw_probe = pointer_probe_value(pointer)

    # The low-level reader still makes of the raw value exactly what it always
    # did -- that is its contract and is asserted, not assumed.
    assert recovery._wait_probe(raw_probe) == json.loads(_LOW_LEVEL_PROBE[shape_id])

    wait = _declaration(pointer)
    absent = _declaration(_pointer(tmp_path / "absent", _absent_probe_manifest()))

    # A declaration, however, is reported with a reason rather than reduced to
    # a worker that declared no probe.
    assert wait is not None
    assert wait["error"] != ""
    assert wait["valid"] is False
    assert absent is not None
    assert absent["valid"] is False
    assert wait["error"] != absent["error"]


def pointer_probe_value(pointer: dict):
    """The probe value as the manifest carries it."""
    fields = recovery.parse_manifest(
        Path(pointer["manifest_path"]).read_text(encoding="utf-8")
    )
    return fields["wait_probe"] if "wait_probe" in fields else None


# ── The refusals this node must not loosen ──────────────────────────────


def test_the_no_op_probe_refusal_still_holds(tmp_path: Path) -> None:
    """A probe that can report nothing but success is no declaration at all."""
    pointer = _pointer(
        tmp_path,
        _probe_manifest(
            probe='["true"]',
            terminal="exit:0",
            condition="reading the plan and reader now",
        ),
    )

    assert _declaration(pointer) is None


def test_the_live_state_terminal_refusal_still_holds(tmp_path: Path) -> None:
    probe = json.dumps(
        [
            "bash",
            "-lc",
            'q=$(squeue -h -j 1274028); if [ -n "$q" ]; then echo RUNNING; '
            "else echo DONE; fi",
        ]
    )
    pointer = _pointer(
        tmp_path,
        _probe_manifest(probe=probe, terminal=json.dumps(["DONE", "RUNNING"])),
    )

    wait = _declaration(pointer)

    assert wait is not None
    assert wait["valid"] is False
    assert "RUNNING" in wait["error"]


def test_a_genuine_argument_vector_wait_still_reads(tmp_path: Path) -> None:
    pointer = _pointer(
        tmp_path,
        _probe_manifest(
            probe='["squeue", "-h", "-j", "42", "-o", "%T"]',
            terminal=json.dumps(["COMPLETED", "FAILED"]),
        ),
    )

    wait = _declaration(pointer)

    assert wait is not None
    assert wait["valid"] is True


# ── The reader that lifts a park is the sweep's, not the classifier's ────
#
# A declaration reading back correctly says nothing about whether the run it
# parks is ever resumed: the classifier's reader answers a file condition by
# looking for the paths, and the sweep carries its own reader, which is the one
# that decides. A gate that stops at the classifier cannot see the difference,
# so these cases enter where the sweep enters. The lift is a branch on the
# reader's `terminal` field, and the negative half -- the same wait with one
# path absent -- is what distinguishes a lift from a function that always says
# true.


def _sweepable_file_manifest(paths=FILE_PATHS, *, expected: str = "59m") -> str:
    """The file-condition manifest with a horizon, so its age is not the claim."""
    return _manifest(
        "status: waiting\n"
        f"wait_condition: {FILE_CONDITION}\n"
        f"wait_file: {json.dumps(paths)}\n"
        f"wait_expected: {expected}\n"
        f"resume_brief: {FILE_BRIEF}\n"
    )


def _place(pointer: dict, paths) -> None:
    """Create the declared paths inside the run's worktree."""
    root = Path(pointer["worktree"])
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("done\n", encoding="utf-8")


def _sweep_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pointer: dict) -> dict:
    """Run the recovery sweep over one live pointer, entering where it enters."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(resumption, "list_live", lambda **_kwargs: [pointer])
    monkeypatch.setattr(resumption, "_claimed_write_paths", lambda _pointer: [])
    return resumption.sweep(
        "fixture-project",
        dry_run=True,
        now=datetime.fromtimestamp(NOW_SECONDS, tz=UTC),
    )


def test_the_sweep_lifts_a_wait_whose_declared_paths_all_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lift is the sweep's decision, so the case is made at the sweep.

    Nothing here is asserted about the classifier's answer: a run is lifted
    when the sweep offers it to the resume loop, and a file condition whose
    paths all exist must reach that loop. The declared vector is a `test -e`
    chain that prints nothing and exits 0, so this is also the case that fails
    if the sweep stops reading `exit:<code>` for a condition it did not write.
    """
    pointer = _pointer(tmp_path, _sweepable_file_manifest())
    _place(pointer, FILE_PATHS)

    report = _sweep_one(monkeypatch, tmp_path, pointer)

    assert report["checked"] == 1
    assert [row["run_id"] for row in report["resumed"]] == [pointer["run_id"]]
    assert report["skipped"] == []


def test_the_sweep_does_not_lift_the_same_wait_when_one_path_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative half: a lift that fires with a path missing is no lift.

    One declared path is withheld, so the vector exits 1. The run must be
    reported as still waiting rather than offered to the resume loop, which is
    what stops the case above from passing against a sweep that resumes
    everything it is shown.
    """
    pointer = _pointer(tmp_path, _sweepable_file_manifest())
    _place(pointer, FILE_PATHS[:-1])

    report = _sweep_one(monkeypatch, tmp_path, pointer)

    assert report["checked"] == 1
    assert [row["run_id"] for row in report["resumed"]] == []
    # The run is reported as still waiting, with the reason a reader acts on.
    assert [row["reason"] for row in report["skipped"]] == ["condition-pending"]


def test_the_sweep_records_the_derived_terminal_true_when_every_path_exists(
    tmp_path: Path,
) -> None:
    """The field that decides the lift, named rather than inferred from it.

    `terminal` is the value the sweep tests before it offers a run to the
    resume loop, and for a file condition whose paths all exist it must be
    true. Asserting it by name is what makes this case fail where the lift
    stops being derived from the paths rather than from the run's presence in
    a report, which a case that read only `resumed` could not tell apart.
    """
    pointer = _pointer(tmp_path, _sweepable_file_manifest())
    _place(pointer, FILE_PATHS)

    wait = _declaration(pointer)
    assert wait is not None
    # Derived from the paths rather than declared by the worker: the exit
    # status of the vector the declaration carries.
    assert wait["terminal"] == ["exit:0"]

    recorded = resumption._run_condition_probe(pointer, wait)

    assert recorded["terminal"] is True
    assert recorded["observed"] == "exit:0"
    assert wait["error"] == ""
