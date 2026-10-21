"""A follower stands a line naming the runs that need the coordinator, and repeats it.

The pane a coordinator reads is quiet for long stretches, so a follower that has
died and one watching a healthy idle fleet look the same. The standing line
answers both: it names every owned run whose state needs action, and it re-emits
on a cadence whether or not the set is empty, so a silent pane is itself a fact
about the follower rather than about the fleet.

The line is one derivation, in ``reckon/crew/needs_you.py``, because the follower
prints it while it lives and again as the run it names is delivered; two
derivations would let the pane and its closing row disagree about which runs
needed attention. So the last case here asserts the two agree over one fixture
rather than asserting each alone.

The list reads the owning session's runs only. A peer's blocked run in the same
fixture must never appear: the reader can do nothing about it, and naming it
would send a coordinator to a run another session already owns.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest

from reckon import crew
from reckon import crew_follow_commands as follow
from reckon.crew import needs_you as need_module
from reckon.crew import runs

PROJECT = "needs-proj"
SESSION = "mine"
PEER = "peers"

# One second, so a case finishes in the time it takes to observe a handful of
# lines rather than the production ten minutes.
REMIND_SECONDS = 1.0
POLL_SECONDS = 0.01
OBSERVE_SECONDS = 3.2

# The age the fixture's unpromoted run has held its state, measured from a
# transition stamped this far before the fixture is built, so the case asserts a
# real span rather than a literal that would have to be edited as time passes.
HELD_SECONDS = 2 * 60 * 60

# The age token the pane prints for a span in hours; minutes alone below one.
_AGE_TOKEN = re.compile(r"\b\d+h\d{2}m\b|\b\d+m\b")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep pointers, manifests, streams and the review store in temporary state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _write_pointer(
    home: Path,
    run_id: str,  # noqa: ARG001 - kept for call-site symmetry with the fixtures
    node: str,
    *,
    session: str,
    manifest_status: str | None = None,
    transitions: list[tuple[str, str]] | None = None,
) -> None:
    """Write one live pointer, its stream and its manifest the way a dispatch does."""
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "turn.started"})]
    for state, stamp in transitions or ():
        lines.append(
            json.dumps(
                {
                    "run_id": run_id,
                    "to_state": state,
                    "observed_at": stamp,
                    "legacy": False,
                }
            )
        )
    log.write_text("\n".join(lines) + "\n")
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": session,
        "node": {"id": node},
        "phase": "working",
        "created_at": runs._utc_now(),
        "log_path": str(log),
    }
    if manifest_status is not None:
        manifest = home / "manifests" / f"{run_id}.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            f"node: {node}\nstatus: {manifest_status}\nblockers: none\n"
        )
        pointer["manifest_path"] = str(manifest)
    crew._write_json(crew.pointer_path(run_id), pointer)


def _stamp_seconds_ago(seconds: float) -> str:
    """A UTC stamp this many seconds before now, in the stream's own spelling."""
    from datetime import UTC, datetime

    moment = datetime.fromtimestamp(time.time() - seconds, tz=UTC)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_stored_review(project: str, run_id: str) -> None:
    """File a staging review record against a reviewed run, as a reviewer does."""
    from reckon.crew import review as review_module

    store = review_module.review_store_root() / project
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{run_id}.json").write_text(json.dumps({"verdict": "pass"}))


def _drive(events: list[dict], stop: threading.Event, *, session: str) -> threading.Thread:
    """Consume the follower's watch generator on a thread, ended by ``stop``."""

    def run() -> None:
        events.extend(
            follow._follow_watch_lines(
                PROJECT,
                session=session,
                poll_interval=POLL_SECONDS,
                stop=stop,
                sweep=None,
                remind=REMIND_SECONDS,
                lifetime=OBSERVE_SECONDS * 2,
            )
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _needs_you_lines(events: list[dict]) -> list[str]:
    return [
        str(event.get("line") or "")
        for event in events
        if event.get("event") == follow.FOLLOWER_NEEDS_YOU_EVENT
    ]


def _run_needs_you(home: Path, *, session: str = SESSION) -> tuple[list[str], list[dict]]:
    """Drive one follower for the observation window and return its lines and rows.

    A live producer is never started: the wait pass composes the standing line
    on its own, and the run does not settle, so the follower is left running
    until the stop event ends it.
    """
    events: list[dict] = []
    stop = threading.Event()
    thread = _drive(events, stop, session=session)
    try:
        time.sleep(OBSERVE_SECONDS)
    finally:
        stop.set()
        thread.join(timeout=5.0)
    rows = [e for e in events if e.get("event") == follow.FOLLOWER_NEEDS_YOU_EVENT]
    return _needs_you_lines(events), rows


def test_the_line_names_your_unpromoted_run_and_repeats(home):
    """The first case: a standing line, repeated, naming only your own needs."""
    _write_pointer(
        home,
        "r-own-unpromoted",
        "own-unpromoted",
        session=SESSION,
        manifest_status="complete",
        transitions=[
            ("working", _stamp_seconds_ago(HELD_SECONDS + 600)),
            ("completed_unpromoted", _stamp_seconds_ago(HELD_SECONDS)),
        ],
    )
    _write_pointer(home, "r-own-work", "own-node", session=SESSION, manifest_status="running")
    _write_pointer(
        home,
        "r-peer-block",
        "peer-block-node",
        session=PEER,
        manifest_status="blocked",
    )

    lines, _rows = _run_needs_you(home)

    assert len(lines) >= 2, f"expected the cadence to repeat the line: {lines!r}"
    first = lines[0]
    assert first.startswith("needs you:"), first
    assert "own-unpromoted" in first, first
    assert "unpromoted" in first, first
    assert _AGE_TOKEN.search(first), f"no age token in: {first!r}"
    assert not any("peer-block-node" in line for line in lines), lines
    assert not any("blocked" in line for line in lines), lines


def test_a_quiet_pane_still_beats(home):
    """With nothing needing action the cadence line still appears and says so."""
    _write_pointer(home, "r-own-work", "own-node", session=SESSION, manifest_status="running")

    lines, _rows = _run_needs_you(home)

    assert len(lines) >= 2, f"expected the heartbeat to repeat: {lines!r}"
    assert all(line == "yours: 1 working · nothing needs you" for line in lines), lines


def test_a_stored_review_marks_its_run(home):
    """An unpromoted run whose review already landed is marked ``reviewed``.

    Promotion unlinks the pointer and the session it captured, so a review read
    after the fact can no longer be answered by resuming its author; the mark is
    the live window's only warning that the chance to resume is going.
    """
    _write_pointer(
        home,
        "r-reviewed",
        "reviewed-node",
        session=SESSION,
        manifest_status="complete",
    )
    _write_stored_review(PROJECT, "r-reviewed")

    lines, _rows = _run_needs_you(home)

    assert lines, "no standing line was emitted"
    assert "reviewed-node" in lines[0], lines[0]
    assert "reviewed" in lines[0], lines[0]


def test_the_follower_and_the_derivation_name_the_same_runs(home):
    """One fixture, two readers: the pane's line and the module name the same set."""
    _write_pointer(
        home,
        "r-own-unpromoted",
        "own-unpromoted",
        session=SESSION,
        manifest_status="complete",
    )
    _write_pointer(home, "r-own-work", "own-node", session=SESSION, manifest_status="running")
    _write_pointer(home, "r-peer-block", "peer-block-node", session=PEER, manifest_status="blocked")

    lines, _rows = _run_needs_you(home)

    derived = need_module.needs_you_runs(PROJECT, session=SESSION)
    derived_nodes = {str(row["node"]) for row in derived}
    assert derived_nodes == {"own-unpromoted"}, derived
    for node in derived_nodes:
        assert all(node in line for line in lines), (node, lines)
    # And the closing row the follower composes uses the same derivation.
    end_needs = follow._needs_you_runs(PROJECT, session=SESSION)
    assert {str(row["node"]) for row in end_needs} == {str(row["node"]) for row in derived}