"""A coordinator's own status repair prints no halted row.

``crew repair --status`` rewrites a manifest's verdict because the coordinator
read the run and decided what the record should say. The transition it causes
into a halted word is then a duty the coordinator has just discharged by its own
act — like the ``landed`` or ``dropped`` row its own merge or discard causes —
so it carries nothing new to that coordinator and the stream withholds it.

The producer stamps the event it builds with the repair's ``repaired_at`` and
reason when the run's ``status-repair.json`` is newer than the previous
observation, so a single act stamps the single transition it caused and no
later one. ``ticker.stream_carries`` reads that stamp and withholds the row; it
stays in ``crew view`` and the pane history.

Two checks bind this: a row-policy check that the stamped halted event prints
nothing while the same event without the stamp prints the halted row, and a
producer check that the fold stamps the event the repair caused. The negative
control drops the stamp check from ``stream_carries``, and the stamped event
must then print.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import recovery, runs, ticker

PROJECT = "proj"
RUN = "r-own"

# A moment far past every hold's window, so a row a hold withheld is released.
FAR_FUTURE = 10_000.0


def _event(from_state, to_state, **extra):
    row = {
        "event": "transition",
        "run_id": RUN,
        "node": "own-node",
        "from_state": from_state,
        "to_state": to_state,
    }
    row.update(extra)
    return row


def _printed(events):
    """Drive one policy over a sequence with no clock waiting and collect rows."""
    policy = ticker.RowPolicy()
    printed = []
    now = 1_000.0
    for event in events:
        printed.extend(policy.feed(event, now=now))
        now += 250.0
    printed.extend(policy.flush(now=FAR_FUTURE))
    return printed


def test_a_stamped_halted_row_prints_nothing_and_the_unstamped_one_prints() -> None:
    """The stamp is the whole difference: complete to blocked, with and without it.

    The unstamped row is the duty the coordinator owes and prints as ``halted``.
    The same transition carrying the repair stamp is a duty the coordinator has
    just discharged itself and prints nothing.
    """
    stamp = {"repaired_at": "2027-01-15T00:00:00Z", "reason": "work is blocked"}
    unstamped = _event("complete", "blocked", recovery_classification="blocked")
    stamped = _event(
        "complete",
        "blocked",
        recovery_classification="blocked",
        status_repair=stamp,
    )

    # The rule the row policy applies to every row it would release.
    assert ticker.stream_carries(unstamped) is True
    assert ticker.stream_carries(stamped) is False

    # And the pane it drives: the unstamped row prints as a halt, the stamped
    # row reaches nothing.
    shown = _printed([stamped])
    assert shown == [], shown
    printed = _printed([unstamped])
    assert len(printed) == 1, printed
    assert ticker._pane_word(printed[0]) == "halted"


def test_an_unstamped_transition_is_untouched_by_the_rule() -> None:
    """The stamp alone is read; a row without it is judged exactly as before."""
    for to_state in ("blocked", "completed_unpromoted"):
        assert ticker.stream_carries(_event("working", to_state)), to_state
    for to_state in ("working", "promoted"):
        assert not ticker.stream_carries(_event("blocked", to_state)), to_state


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the crew's transient run home at the test's own directory."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _write_manifest(
    manifest: Path, *, status: str, commits: list[str], blockers: list[str]
) -> None:
    manifest.write_text(
        json.dumps({"status": status, "commits": commits, "blockers": blockers}),
        encoding="utf-8",
    )


def _write_repair(run_id: str, *, reason: str, repaired_at: str) -> Path:
    """Write the record ``repair_manifest_status`` leaves in the run directory."""
    path = runs.run_dir(run_id) / "status-repair.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": "blocked",
                "reason": reason,
                "previous_status": "complete",
                "repaired_at": repaired_at,
            }
        ),
        encoding="utf-8",
    )
    return path


def _pointer(tmp_path: Path, run_id: str, manifest: Path) -> dict:
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": f"session-{run_id}",
        "role": "implement",
        "agent": {"backend": "codex", "model": "gpt-6", "effort": "high"},
        "node": {"id": run_id, "plan": "plan-a", "section": "s4", "role": "implement"},
        "worktree": str(tmp_path / "trees" / run_id),
        "log_path": str(runs.run_dir(run_id) / "stream.jsonl"),
        "stderr_path": str(runs.run_dir(run_id) / "stderr.log"),
        "manifest_path": str(manifest),
        "phase": "working",
        "process_alive": False,
    }


def _snapshot(tmp_path: Path, run_id: str, manifest: Path, *, moment: float) -> dict:
    return recovery._watch_snapshot(
        _pointer(tmp_path, run_id, manifest), moment=moment, stall_seconds=3600
    )


def _emitted(snapshot: dict, previous: str | None, state: str, counts: dict) -> dict:
    return recovery._watch_transition(
        PROJECT,
        kind="transition",
        snapshot=snapshot,
        previous=previous,
        current=state,
        counts=counts,
    )


def test_the_producer_stamps_the_transition_a_status_repair_caused(
    tmp_path: Path, crew_home: Path
) -> None:
    """A repair written between two observations stamps the transition it caused.

    The run reads ``completed_unpromoted`` at one observation and ``blocked`` at
    the next, and the record explaining the move is a coordinator's repair. The
    emitted event carries that repair's instant and reason, and the stream
    withholds it.
    """
    run_id = "r-repaired"
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    _write_manifest(manifest, status="complete", commits=["abc1234"], blockers=[])
    moment = 1_800_000.0
    known = {run_id: _snapshot(tmp_path, run_id, manifest, moment=moment)}
    assert known[run_id]["state"] == "completed_unpromoted", known[run_id]

    _write_manifest(
        manifest, status="blocked", commits=["abc1234"], blockers=["gate unmet"]
    )
    _write_repair(run_id, reason="work is blocked", repaired_at="2027-01-15T00:00:00Z")
    current = {run_id: _snapshot(tmp_path, run_id, manifest, moment=moment + 5)}

    events, next_known = recovery.fleet_transitions(known, current)

    assert len(events) == 1, events
    snapshot, previous, state, counts = events[0]
    assert (previous, state) == ("completed_unpromoted", "blocked")
    emitted = _emitted(snapshot, previous, state, counts)
    assert emitted["status_repair"] == {
        "repaired_at": "2027-01-15T00:00:00Z",
        "reason": "work is blocked",
    }
    assert ticker.stream_carries(emitted) is False
    assert next_known[run_id]["status_repair_at"] == "2027-01-15T00:00:00Z"


def test_the_stamp_rides_only_the_transition_the_repair_caused(
    tmp_path: Path, crew_home: Path
) -> None:
    """A later transition off the repaired word carries no stamp.

    The repair's instant is remembered with the run, so the next state change —
    a resume that rewrote the verdict again — is judged against it and finds
    nothing new. Only the single transition the repair caused is withheld.
    """
    run_id = "r-once"
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    moment = 1_800_000.0
    _write_manifest(manifest, status="complete", commits=["abc1234"], blockers=[])
    known = {run_id: _snapshot(tmp_path, run_id, manifest, moment=moment)}

    _write_manifest(manifest, status="blocked", commits=[], blockers=["gate"])
    _write_repair(run_id, reason="blocked", repaired_at="2027-01-15T00:00:00Z")
    repaired = {run_id: _snapshot(tmp_path, run_id, manifest, moment=moment + 5)}
    events, next_known = recovery.fleet_transitions(known, repaired)
    assert len(events) == 1
    first = _emitted(*events[0])
    assert first["status_repair"]["repaired_at"] == "2027-01-15T00:00:00Z"

    # The run moves again, the repair record unchanged: the stamp must not repeat.
    _write_manifest(manifest, status="complete", commits=["abc1234"], blockers=[])
    after = {run_id: _snapshot(tmp_path, run_id, manifest, moment=moment + 20)}
    later, _ = recovery.fleet_transitions(next_known, after)
    assert len(later) == 1, later
    second = _emitted(*later[0])
    assert "status_repair" not in second, second
