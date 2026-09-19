"""A wait stanza whose terminal list names a live state, and a lift that repeats.

Measured 2026-09-19 on run ``r-20260919T111944475334-cco-review-repairs``. Its
manifest declared a ``squeue`` probe and, under ``wait_terminal``, the token
``RUNNING`` — the very state that probe prints while the job is still in the
queue. The declaration therefore read "terminal" on every sweep of a job that
had not started, and the recovery watcher resumed the run thirty times in one
afternoon (``resume-1`` through ``resume-30`` in the run directory), most of
them producing a 0-byte stream.

Two halves close it, and both are the reader's:

* A terminal list naming a state the probe reports while the awaited job is
  still live is refused when the manifest is read, with the offending token
  named in the reason the follower already renders. The scheduler spells three
  live states outright; a probe may also invent its own wording for the same
  situation on the branch it takes only while the job is queued, so that branch
  is read out of the probe text.

* A lift is keyed to what the declaration asks for rather than to the manifest
  file's modification time, and a declaration that lifted its run once and came
  back unchanged is marked as a wait-key defect. A worker that re-parks rewrites
  its manifest and so advances the mtime, which made every re-park a brand-new
  condition and re-lifted a wait nothing had ended.

The positive controls matter as much as the refusals: the repaired stanza (the
probe above with ``RUNNING`` removed) still reads, a probe whose live branch
prints nothing is still read, a first lift still happens, and editing the
declaration still lifts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon.crew import recovery, resumption

# The measured probe, verbatim from the run's manifest except that the report
# directory is a fixture path. `$q` is filled from `squeue`, and the branch
# guarded by a non-empty `$q` is the one taken while the job is still queued.
CCO_PROBE = [
    "bash",
    "-lc",
    (
        "q=$(squeue -h -j 1274028); d=/tmp/cco-control-cpu; "
        'if [ -n "$q" ]; then echo RUNNING; '
        'elif [ -f "$d/control-positive-state.npy" ]; then echo TERMINAL-STATE; '
        'elif [ -f "$d/control-positive-checkpoint.json" ]; then echo CHECKPOINTED; '
        "else echo COMPILE-ABORTED; fi"
    ),
]
# The terminal list as the run's manifest carried it, live state included.
CCO_TERMINAL_DEFECTIVE = [
    "TERMINAL-STATE",
    "CHECKPOINTED",
    "COMPILE-ABORTED",
    "RUNNING",
]
# The repaired stanza: identical probe, the live state removed.
CCO_TERMINAL_REPAIRED = ["TERMINAL-STATE", "CHECKPOINTED", "COMPILE-ABORTED"]
CCO_CONDITION = "job 1274028 writes a control state or a checkpoint"
CCO_BRIEF = "read the control-cpu summary, then resubmit the same lane script"

# A probe whose live branch prints a token the fixed scheduler spellings cannot
# know: this is the case the probe-text reading exists for.
RENAMED_LIVE_PROBE = [
    "bash",
    "-lc",
    ('q=$(squeue -h -j 9); if [ -n "$q" ]; then echo STILL-QUEUED; else echo DONE; fi'),
]

# A probe that has no live branch at all: its token means what the declaration
# says it means, so the reader must leave it alone.
UNGUARDED_PROBE = ["bash", "-lc", "if [ -f /tmp/fixture-done ]; then echo DONE; fi"]
UNGUARDED_TERMINAL = ["DONE"]

MANIFEST_BASELINE_NS = 1_788_853_800_000_000_000
NOW_SECONDS = 1_788_853_920.0


def _wait_manifest(
    *,
    condition: str,
    probe: list[str],
    terminal: list[str],
    brief: str = CCO_BRIEF,
) -> str:
    return (
        "status: waiting\n"
        f"wait_condition: {condition}\n"
        f"wait_probe: {json.dumps(probe)}\n"
        f"wait_terminal: {json.dumps(terminal)}\n"
        f"resume_brief: {brief}\n"
    )


def _pointer(
    tmp_path: Path, body: str, *, alive: bool = False, attempt: int = 1
) -> dict:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    manifest = tmp_path / "manifest.md"
    manifest.write_text(body, encoding="utf-8")
    os.utime(manifest, ns=(MANIFEST_BASELINE_NS, MANIFEST_BASELINE_NS))
    return {
        "run_id": "r-wait-stanza-fixture",
        "project": "fixture-project",
        "process_alive": alive,
        "phase": "working",
        "attempt": attempt,
        "created_at": "2026-09-19T00:00:00+00:00",
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
            "id": "r-wait-stanza-fixture",
            "role": "implement",
            "time_budget": "20m",
            "write_paths": [],
        },
    }


def _re_park(pointer: dict) -> None:
    """Advance the manifest's mtime without editing what it declares."""
    manifest = Path(pointer["manifest_path"])
    later = manifest.stat().st_mtime_ns + 5_000_000_000
    os.utime(manifest, ns=(later, later))


# ── The measured stanza is refused, the repaired one reads ───────────────


def test_the_measured_cco_stanza_is_refused_with_running_named(tmp_path: Path) -> None:
    pointer = _pointer(
        tmp_path,
        _wait_manifest(
            condition=CCO_CONDITION,
            probe=CCO_PROBE,
            terminal=CCO_TERMINAL_DEFECTIVE,
        ),
    )

    wait = recovery.external_wait(pointer, now_seconds=NOW_SECONDS)

    assert wait is not None
    assert wait["valid"] is False
    assert "RUNNING" in wait["error"]

    # The reason is the one the follower already renders, so the repair is
    # visible without a second reading of the probe.
    row = recovery.classify_pointer(pointer, now_seconds=NOW_SECONDS)
    assert row["manifest_error"] is not None
    assert "RUNNING" in row["manifest_error"]


def test_the_repaired_cco_stanza_reads_unchanged(tmp_path: Path) -> None:
    pointer = _pointer(
        tmp_path,
        _wait_manifest(
            condition=CCO_CONDITION,
            probe=CCO_PROBE,
            terminal=CCO_TERMINAL_REPAIRED,
        ),
    )

    wait = recovery.external_wait(pointer, now_seconds=NOW_SECONDS)

    assert wait is not None
    assert wait["valid"] is True
    assert wait["error"] == ""
    assert wait["terminal"] == CCO_TERMINAL_REPAIRED


@pytest.mark.parametrize("spelling", ["RUNNING", "running", "Pending", "WAITING"])
def test_a_live_state_is_refused_whatever_its_case(
    tmp_path: Path, spelling: str
) -> None:
    pointer = _pointer(
        tmp_path,
        _wait_manifest(
            condition=CCO_CONDITION,
            probe=CCO_PROBE,
            terminal=[*CCO_TERMINAL_REPAIRED, spelling],
        ),
    )

    wait = recovery.external_wait(pointer, now_seconds=NOW_SECONDS)

    assert wait is not None
    assert wait["valid"] is False
    assert spelling in wait["error"] or spelling.upper() in wait["error"]


def test_a_probe_emitted_live_token_is_refused_when_it_is_not_a_known_spelling(
    tmp_path: Path,
) -> None:
    """The probe branch is read, not just the scheduler's own vocabulary."""
    pointer = _pointer(
        tmp_path,
        _wait_manifest(
            condition="job 9 leaves the queue",
            probe=RENAMED_LIVE_PROBE,
            terminal=["DONE", "STILL-QUEUED"],
        ),
    )

    wait = recovery.external_wait(pointer, now_seconds=NOW_SECONDS)

    assert wait is not None
    assert wait["valid"] is False
    assert "STILL-QUEUED" in wait["error"]


def test_a_probe_without_a_live_branch_is_not_refused(tmp_path: Path) -> None:
    pointer = _pointer(
        tmp_path,
        _wait_manifest(
            condition="the fixture-done marker appears",
            probe=UNGUARDED_PROBE,
            terminal=UNGUARDED_TERMINAL,
        ),
    )

    wait = recovery.external_wait(pointer, now_seconds=NOW_SECONDS)

    assert wait is not None
    assert wait["valid"] is True


# ── A lift that re-parks on an unchanged declaration is a wait-key defect ──


def _sweep(pointer: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(resumption, "list_live", lambda **_kwargs: [pointer])
    monkeypatch.setattr(resumption, "_claimed_write_paths", lambda _pointer: [])
    return resumption.sweep(
        "fixture-project",
        dry_run=True,
        condition_test=lambda _pointer, _wait: {
            "terminal": True,
            "observed": "DONE",
            "detail": "the probe reports a terminal state",
        },
    )


def test_a_re_park_on_an_unchanged_declaration_is_detected_after_one_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pointer = _pointer(
        tmp_path,
        _wait_manifest(
            condition="the fixture-done marker appears",
            probe=UNGUARDED_PROBE,
            terminal=UNGUARDED_TERMINAL,
        ),
    )

    # First cycle: nothing has lifted this declaration, so it is a candidate.
    first = _sweep(pointer, monkeypatch, tmp_path)
    assert first["checked"] == 1
    assert [row["run_id"] for row in first["resumed"]] == [pointer["run_id"]]
    assert first["resumed"][0]["would_resume"] is True

    # The lift the sweep just judged, recorded exactly as the launcher records
    # it: the declaration's own identity, not the file's mtime.
    declaration = recovery.external_wait(pointer, now_seconds=NOW_SECONDS)
    assert declaration is not None
    pointer["auto_resume"] = {"trigger": declaration["signature"], "at": "now"}

    # The worker re-parks: the manifest is rewritten (mtime advances) carrying
    # the same declaration, so the same condition reports terminal again.
    _re_park(pointer)

    defect = recovery.external_wait(pointer, now_seconds=NOW_SECONDS)
    assert defect is not None
    assert defect["wait_key_defect"]
    assert "wait-key defect" in defect["wait_key_defect"]

    row = recovery.classify_pointer(
        pointer,
        condition_test=lambda _p, _w: {
            "state": "met",
            "observed": "DONE",
            "detail": "the probe reports a terminal state",
        },
        now_seconds=NOW_SECONDS,
    )
    assert row["wait_key_defect"] == defect["wait_key_defect"]
    assert "wait-key defect" in row["detail"]

    # Second cycle: the lift loop is stopped rather than repeating.
    second = _sweep(pointer, monkeypatch, tmp_path)
    assert second["checked"] == 1
    assert second["resumed"] == []
    assert [row["reason"] for row in second["skipped"]] == [
        "already-resumed-for-this-condition"
    ]


def test_editing_the_declaration_clears_the_wait_key_defect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pointer = _pointer(
        tmp_path,
        _wait_manifest(
            condition="the fixture-done marker appears",
            probe=UNGUARDED_PROBE,
            terminal=UNGUARDED_TERMINAL,
        ),
    )
    declaration = recovery.external_wait(pointer, now_seconds=NOW_SECONDS)
    assert declaration is not None
    pointer["auto_resume"] = {"trigger": declaration["signature"], "at": "now"}
    _re_park(pointer)
    assert recovery.external_wait(pointer, now_seconds=NOW_SECONDS)["wait_key_defect"]

    # The worker edits the declaration for the next stage of its work: a new
    # terminal state, so a new condition, so the loop is free to lift again.
    Path(pointer["manifest_path"]).write_text(
        _wait_manifest(
            condition="the fixture-second marker appears",
            probe=UNGUARDED_PROBE,
            terminal=["READY"],
        ),
        encoding="utf-8",
    )

    cleared = recovery.external_wait(pointer, now_seconds=NOW_SECONDS)
    assert cleared is not None
    assert cleared["wait_key_defect"] == ""

    report = _sweep(pointer, monkeypatch, tmp_path)
    assert [row["run_id"] for row in report["resumed"]] == [pointer["run_id"]]
