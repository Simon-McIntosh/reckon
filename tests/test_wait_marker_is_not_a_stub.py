"""A manifest with nothing to wait on yet declares no wait.

The delivery contract asks every worker to write its manifest before starting
any long output, so the first write happens at orientation — before there is
anything to wait for. Offered a ``status: waiting`` line and four wait fields
at that moment, healthy workers filled them with a note about where they were:
``initial orientation; reading source``, ``reading the plan and reader now``,
``exploring``-style notes such as ``exploring; not yet set``. The classifier
read the presence of those fields as a declared wait, so a working run sat in
the waiting column, aged into wait-aged, and reached the resume sweep as a
candidate. Measured 2026-09-17: a sweep evaluated three live healthy workers
for automatic resume on exactly these markers and was stopped only by
unrelated guards.

Two halves close it: the orientation stub stops offering the wait fields
before there is a wait, and the classifier reads what the fields say rather
than that they exist — a prose that declares no wait, or a probe that cannot
report anything but success, is not a held wait.

The negative half is mandatory: a genuine wait, with a real condition, a real
probe and a real terminal, still classifies as waiting and still ages into
wait-aged when overdue.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import prompts, recovery, resumption
from reckon.crew.node import TaskNode

# The five field sets, verbatim as healthy live workers wrote them. Four were
# read out of their own manifests under the crew runs directory (the two
# `["true"]`/`exit:0` pairs, the `exploring; not yet set` note and one whose
# condition declares no external resource is awaited); the `none — …`
# condition is the one a resume sweep recorded against a live run.
RECORDED_STUB_FIELDS: dict[str, str] = {
    "an-interim-checkpoint-declares-none": (
        "status: waiting\n"
        "wait_condition: none - this is an interim checkpoint, not a held wait\n"
    ),
    "an-exploration-has-not-set-a-condition": (
        "status: waiting\n"
        "wait_condition: exploring; not yet set\n"
        "checkpoint: orientation read; next = inspect the wall-polygon contract\n"
    ),
    "a-deferred-write-block-points-at-a-checkpoint": (
        "status: waiting\n"
        "wait_condition: deferred write block - see checkpoint\n"
        'wait_probe: ["true"]\n'
        "wait_terminal: exit:0\n"
        "resume_brief: continue from the checkpoint recorded at orientation\n"
    ),
    "reading-the-plan-and-reader-now": (
        "status: waiting\n"
        "wait_condition: reading the plan and reader now\n"
        'wait_probe: ["true"]\n'
        "wait_terminal: exit:0\n"
        "resume_brief: continue implementing the manifest reader\n"
    ),
    "initial-orientation-reading-source": (
        "status: waiting\n"
        "wait_condition: initial orientation; reading source\n"
        'wait_probe: ["true"]\n'
        "wait_terminal: exit:0\n"
        "resume_brief: continue implementation\n"
    ),
}

STUB_IDS = sorted(RECORDED_STUB_FIELDS)

# The recorded `none — …` condition, with the em dash the live run carried.
RECORDED_DASHED_NONE = (
    "status: waiting\n"
    "wait_condition: none — this is an interim checkpoint, not a held wait\n"
)

# One real condition, one real probe and one real terminal: the shape a worker
# writes when it is genuinely parked on an external job.
GENUINE_WAIT_CONDITION = "scheduler job 42 has left the queue"
GENUINE_WAIT_PROBE = '["squeue","-h","-j","42","-o","%T"]'
GENUINE_WAIT_TERMINAL = '["COMPLETED","FAILED"]'

NOW_SECONDS = 1_788_853_920.0


def _manifest_text(
    body: str, *, expected: str = "", started: str | None = None
) -> str:
    lines = [body.rstrip("\n")]
    if expected:
        lines.append(f"wait_expected: {expected}")
    if started:
        lines.append(f"wait_started_at: {started}")
    return "\n".join(lines) + "\n"


def _pointer(tmp_path: Path, body: str, *, alive: bool = True) -> dict:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    manifest = tmp_path / "manifest.md"
    manifest.write_text(body, encoding="utf-8")
    return {
        "run_id": "r-orientation-stub",
        "project": "fixture-project",
        "process_alive": alive,
        "phase": "working",
        "manifest_path": str(manifest),
        "log_path": str(tmp_path / "stream.jsonl"),
        "stderr_path": str(tmp_path / "stderr.log"),
        "worktree": str(worktree),
        "launch": "cli",
        "argv": ["fixture-agent", "exec"],
        "backend": "fixture-lane",
        "session_id": "fixture-session",
        "node": {
            "id": "r-orientation-stub",
            "role": "implement",
            "time_budget": "20m",
            "write_paths": [],
        },
    }


def _genuine_wait_body() -> str:
    return (
        "status: waiting\n"
        f"wait_condition: {GENUINE_WAIT_CONDITION}\n"
        f"wait_probe: {GENUINE_WAIT_PROBE}\n"
        f"wait_terminal: {GENUINE_WAIT_TERMINAL}\n"
        "resume_brief: collect the scheduler result\n"
    )


# ── The recorded stubs declare no wait at all ────────────────────────────


@pytest.mark.parametrize("stub_id", [*STUB_IDS, "the-dashed-none"])
def test_a_recorded_stub_declares_no_wait(tmp_path: Path, stub_id: str) -> None:
    body = (
        RECORDED_DASHED_NONE
        if stub_id == "the-dashed-none"
        else RECORDED_STUB_FIELDS[stub_id]
    )
    pointer = _pointer(tmp_path, _manifest_text(body))

    # No declaration reaches any reader: the sweep, the pane and the resume
    # ladder all read the run's wait through this one call.
    assert recovery.external_wait(pointer, now_seconds=NOW_SECONDS) is None


@pytest.mark.parametrize("stub_id", STUB_IDS)
def test_a_stub_manifest_on_a_healthy_worker_is_not_waiting(
    tmp_path: Path, stub_id: str
) -> None:
    pointer = _pointer(tmp_path, _manifest_text(RECORDED_STUB_FIELDS[stub_id]))

    row = recovery.classify_pointer(pointer, now_seconds=NOW_SECONDS)

    # The run is working, and it is none of the three readings the fence
    # forbids — not waiting, not aged past a wait it never held, not
    # unreadable for a declaration it never made.
    assert row["classification"] == "running"
    assert row["recovery_classification"] == "running"
    assert row["recovery_classification"] not in {"waiting", "wait-aged", "unreadable"}
    assert row["classification"] not in {"waiting", "wait-aged", "unreadable"}
    assert row["external_wait"] is None
    assert row["wait_overdue"] is None
    assert row["lifting_condition"] is None


@pytest.mark.parametrize("stub_id", STUB_IDS)
def test_a_stub_run_is_not_offered_to_the_resume_sweep(
    tmp_path: Path, stub_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pointer = _pointer(
        tmp_path,
        _manifest_text(RECORDED_STUB_FIELDS[stub_id]),
        # A stub written before the worker has anything to wait on is a live
        # worker's interim note; the sweep must not evaluate it at all.
        alive=False,
    )
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(resumption, "list_live", lambda **_kwargs: [pointer])
    monkeypatch.setattr(resumption, "_claimed_write_paths", lambda _pointer: [])

    report = resumption.sweep("fixture-project", dry_run=True)

    # Not considered, not skipped, and never resumed. This is the injury the
    # markers caused: with `["true"]`/`exit:0` the sweep's own probe reports
    # `exit:0`, matches `exit:0`, and resumes a live healthy worker.
    assert report["checked"] == 0
    assert [row["run_id"] for row in report["resumed"]] == []
    assert report["skipped"] == []


# ── The negative half: a genuine wait is still a wait ─────────────────────


def test_a_genuine_wait_still_classifies_as_waiting(tmp_path: Path) -> None:
    pointer = _pointer(
        tmp_path,
        _manifest_text(_genuine_wait_body(), expected="59m"),
    )
    wait = recovery.external_wait(pointer, now_seconds=NOW_SECONDS)

    assert wait is not None
    assert wait["valid"] is True
    assert wait["condition"] == GENUINE_WAIT_CONDITION
    assert wait["terminal"] == ["COMPLETED", "FAILED"]

    row = recovery.classify_pointer(
        pointer,
        condition_test=lambda _p, _w: {
            "state": "pending",
            "observed": "RUNNING",
            "detail": "the job is still queued",
        },
        now_seconds=NOW_SECONDS,
    )

    assert row["classification"] == "waiting"
    assert row["recovery_classification"] == "waiting"
    assert row["wait_condition_state"] == "pending"
    assert row["external_wait"]["probe"] == ["squeue", "-h", "-j", "42", "-o", "%T"]
    assert row["lifting_condition"] == (
        "scheduler job 42 has left the queue reports one of COMPLETED, FAILED"
    )


def test_a_genuine_wait_still_ages_into_wait_aged(tmp_path: Path) -> None:
    pointer = _pointer(
        tmp_path,
        _manifest_text(
            _genuine_wait_body(),
            expected="1m",
            started="2026-09-08T07:42:00+00:00",
        ),
    )

    row = recovery.classify_pointer(
        pointer,
        condition_test=lambda _p, _w: {
            "state": "pending",
            "observed": "RUNNING",
            "detail": "the job is still queued",
        },
        now_seconds=NOW_SECONDS,
    )

    assert row["wait_overdue"] is True
    assert row["classification"] == "waiting"
    assert row["recovery_classification"] == "wait-aged"
    assert row["recovery"] == "investigate"


def test_a_genuine_wait_is_still_a_resume_candidate_when_its_probe_terminates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pointer = _pointer(
        tmp_path,
        _manifest_text(_genuine_wait_body(), expected="59m"),
        alive=False,
    )
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(resumption, "list_live", lambda **_kwargs: [pointer])
    monkeypatch.setattr(resumption, "_claimed_write_paths", lambda _pointer: [])

    report = resumption.sweep(
        "fixture-project",
        dry_run=True,
        condition_test=lambda _pointer, _wait: {
            "terminal": True,
            "observed": "COMPLETED",
            "detail": "the job finished",
        },
    )

    assert report["checked"] == 1
    assert [row["run_id"] for row in report["resumed"]] == [pointer["run_id"]]


# ── Controls: the prose and probe rules must not overshoot ────────────────


def test_a_real_condition_that_merely_mentions_none_is_kept(tmp_path: Path) -> None:
    body = (
        "status: waiting\n"
        "wait_condition: waiting for scheduler job 42 to leave the queue; none of "
        "the arms have reported yet\n"
        f"wait_probe: {GENUINE_WAIT_PROBE}\n"
        f"wait_terminal: {GENUINE_WAIT_TERMINAL}\n"
        "resume_brief: collect the scheduler result\n"
    )
    pointer = _pointer(tmp_path, _manifest_text(body, expected="59m"))

    wait = recovery.external_wait(pointer, now_seconds=NOW_SECONDS)

    assert wait is not None and wait["valid"] is True


def test_a_real_probe_that_is_not_a_no_op_is_kept(tmp_path: Path) -> None:
    pointer = _pointer(
        tmp_path, _manifest_text(_genuine_wait_body(), expected="59m")
    )

    wait = recovery.external_wait(pointer, now_seconds=NOW_SECONDS)

    assert wait is not None and wait["valid"] is True


# ── The other half: the orientation write does not offer the wait fields ──


MANIFEST_HEADER_MARKER = "MANIFEST (write exactly these keys"
WORKTREE_RULES_MARKER = "WORKTREE AND PARALLEL-SAFETY RULES"


def _manifest_block(prompt: str) -> list[str]:
    _, rest = prompt.split(MANIFEST_HEADER_MARKER, 1)
    body, _ = rest.split(WORKTREE_RULES_MARKER, 1)
    return body.splitlines()


def _manifest_line(prompt: str, key: str) -> str:
    for line in _manifest_block(prompt):
        if line.strip().startswith(key + ":"):
            return line.strip()
    raise AssertionError(f"the manifest block lacks a `{key}:` line")


def _prompt() -> str:
    node = TaskNode(
        id="orientation-write-node",
        goal="the orientation write offers a checkpoint rather than a wait block",
        plan="plan-a",
        section="",
        role="implement",
        done_when="the wait fields are not offered before there is a wait",
        write_paths=["reckon/crew/prompts.py"],
        time_budget="20m",
    )
    return prompts.compose_prompt(
        node=node,
        project="proj",
        worktree="/repo/worktrees/orientation-write-run",
        working_directory="/repo/worktrees/orientation-write-run",
        manifest_path="/state/runs/orientation-write-run/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )


def test_the_orientation_instruction_does_not_offer_a_wait() -> None:
    prompt = _prompt()
    header = next(
        line
        for line in prompt.splitlines()
        if line.startswith(MANIFEST_HEADER_MARKER)
    )
    flat = " ".join(header.split())

    # The write that happens before there is anything to wait on is named as
    # an orientation write carrying a checkpoint, and it is told to leave the
    # wait fields alone — which is what removes the invitation.
    assert "first three lines your first write" in flat
    assert "orientation write" in flat
    assert "checkpoint" in flat
    assert "leave the wait fields" in flat


@pytest.mark.parametrize(
    "key", ["wait_condition", "wait_probe", "wait_terminal", "resume_brief"]
)
def test_each_wait_field_is_scoped_to_a_wait_actually_held(key: str) -> None:
    line = _manifest_line(_prompt(), key)

    assert line.startswith(f"{key}:")
    assert "only when an external condition is actually awaited" in line


def test_the_orientation_status_is_not_waiting() -> None:
    status_line = _manifest_line(_prompt(), "status")
    offered = {word.strip() for word in status_line.split(":", 1)[1].split("|")}

    # A worker writing its first lines has a status word for it that is not
    # the wait status; the wait status stays offered for a real wait.
    assert "in-progress" in offered
    assert "waiting" in offered
    for state in ("complete", "blocked", "failed"):
        assert state in offered


def test_the_checkpoint_scope_line_survives() -> None:
    line = _manifest_line(_prompt(), "checkpoint")

    assert "not setting status to waiting" in line
    assert "checkpoint rather than a wait block" in line
    assert "any worker recording progress at any point" in line