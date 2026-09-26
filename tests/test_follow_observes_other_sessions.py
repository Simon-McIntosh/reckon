"""One follower can deliver another session's runs for oversight.

A ``crew follow --session mine --observe-session old-a`` reader sees the
owning session's transitions and the observed session's transitions on one
pane. The two halves are deliberately not the same: the owning session is
registered as attached so a dispatch guard may trust it, and an observed
session is admitted for oversight only — nothing is recorded for it, no
attachment lock exists, and the observed session's own dispatch still has to
arm its own follower. A guard satisfied by someone else's delivery is the
failure this surface exists to prevent, so the split between delivering and
vouching is asserted by reading the records the guard reads, never by reading
the follower's own claims.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from reckon import cli, crew
from reckon.crew import recovery, runs
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep registrations, pointers, and streams in temporary state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _write_pointer(home: Path, run_id: str, node: str, *, session: str, phase: str):
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n')
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "session": session,
            "node": {"id": node, "plan": "plan-a", "time_budget": "20m"},
            "phase": phase,
            "created_at": runs._utc_now(),
            "manifest_path": str(home / "manifests" / f"{run_id}.md"),
            "log_path": str(log),
        },
    )


def _set_phase(run_id: str, phase: str) -> None:
    pointer = crew.read_pointer(run_id)
    pointer["phase"] = phase
    crew._write_json(crew.pointer_path(run_id), pointer)


def _deliver(home: Path, run_id: str, status: str) -> None:
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: n\nstatus: {status}\ncommits: HEAD\nblockers: none\n")


def _follow(project: str, *, settle: float = 0.15, **kwargs) -> list[dict]:
    """Collect what a follower delivers, then stop it as a session would."""
    received: list[dict] = []
    stop = threading.Event()

    def reader() -> None:
        for event in cli._follow_watch_lines(
            project, poll_interval=0.001, stop=stop, **kwargs
        ):
            if event.get("event") in {"attached", "reattached"}:
                continue
            received.append(event)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(settle)
    stop.set()
    thread.join(timeout=2)
    assert not thread.is_alive(), "a stopped follower must return"
    return received


def _wait_for(predicate, *, timeout: float = WATCHER_LOAD_BOUND_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    pytest.fail("the follower did not receive the expected transition in time")


def _real_home_artifacts(
    *,
    run_ids: tuple[str, ...] = ("r-mine", "r-old"),
    sessions: tuple[str, ...] = ("mine", "old-a", "old-b", "other", "ghost"),
) -> list[Path]:
    """The artifacts this test writes, resolved against the real config home.

    Every write in this file is redirected to the temporary ``RECKON_HOME``.
    Resolving the same artifact paths with the override popped yields where
    they would have landed without it; asserting those are absent proves the
    redirect was complete rather than merely believed.
    """
    saved = os.environ.pop("RECKON_HOME", None)
    try:
        return [
            *[crew.pointer_path(run_id) for run_id in run_ids],
            *[runs.follower_lock_path("proj", session) for session in sessions],
            runs.watch_lock_path("proj"),
            runs.watch_stream_path("proj"),
            runs.follower_dir("proj"),
        ]
    finally:
        if saved is not None:
            os.environ["RECKON_HOME"] = saved


# ── Delivery ────────────────────────────────────────────────────────────────


def test_a_follower_with_an_owning_and_two_observed_sessions_delivers_all_three(
    home,
) -> None:
    _write_pointer(home, "r-mine", "my-node", session="mine", phase="starting")
    _write_pointer(home, "r-old-a", "old-a-node", session="old-a", phase="starting")
    _write_pointer(home, "r-old-b", "old-b-node", session="old-b", phase="starting")

    with runs._project_watch_claim("proj", "1h") as (acquired, _seat):
        assert acquired
        _deliver(home, "r-mine", "complete")
        _deliver(home, "r-old-a", "complete")
        _deliver(home, "r-old-b", "blocked")
        crew.list_live(project="proj")

        events = _follow("proj", session="mine", observed=("old-a", "old-b"))

    nodes = {event["node"] for event in events}
    assert nodes == {"my-node", "old-a-node", "old-b-node"}, (
        "the owning session and every observed session reach this follower"
    )


def test_an_observed_session_row_achieves_the_pane_without_falling_out(home) -> None:
    """Deliveries for observed sessions arrive before the follower is stopped."""
    _write_pointer(home, "r-late", "late-node", session="old-a", phase="working")

    received: list[dict] = []
    stop = threading.Event()

    def reader() -> None:
        for event in cli._follow_watch_lines(
            "proj", session="mine", observed=("old-a",), poll_interval=0.001, stop=stop
        ):
            if event.get("event") in {"attached", "reattached"}:
                continue
            received.append(event)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        time.sleep(0.05)
        _write_pointer(home, "r-owned", "owned-node", session="mine", phase="working")
        with runs._project_watch_claim("proj", "1h"):
            crew.list_live(project="proj")
            _wait_for(
                lambda: (
                    any(e["node"] == "owned-node" for e in received)
                    and any(e["node"] == "late-node" for e in received)
                )
            )
    finally:
        stop.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert all(e["node"] in {"owned-node", "late-node"} for e in received)


# ── Registration: the owning session only ───────────────────────────────────


def test_observing_an_old_session_registers_only_the_owning_session(home) -> None:
    """The attachment record names exactly the owning session.

    The guard that refuses a session whose runs would finish unheard is
    decided on this record, so it is read directly rather than inferred from
    anything the follower printed.
    """
    with runs.follower_claim("proj", "mine", delivery="stream") as (held, record):
        assert held is True
        assert record["session"] == "mine"
        assert runs.follower_state("proj", "mine")["live"] is True
        assert runs.follower_state("proj", "old-a")["live"] is False
        assert not runs.follower_lock_path("proj", "old-a").exists()
        assert not runs.follower_lock_path("proj", "old-b").exists()


def test_a_dispatch_guard_consulted_for_an_observed_session_reports_no_delivery(
    home,
) -> None:
    """Reading the guard's own record: observed sessions are not attached.

    The dispatch guard reads ``session_attached`` for the session being
    dispatched. An observed session has no registration, so the guard it
    consults sees no delivery — the follower's own output never enters the
    verdict.
    """
    with runs.follower_claim("proj", "mine", delivery="stream"):
        for observed in ("old-a", "old-b"):
            state = runs.watch_state("proj", session=observed)
            assert state["session_attached"] is False, observed
        own = runs.watch_state("proj", session="mine")
        assert own["session_attached"] is True


def test_naming_an_observed_session_leaves_a_third_session_unaffected(home) -> None:
    """A third session — neither owned nor observed — stays exactly as before."""
    with runs.follower_claim("proj", "mine", delivery="stream"):
        third = runs.watch_state("proj", session="other")
        assert third["session_attached"] is False
        assert not runs.follower_lock_path("proj", "other").exists()


# ── No observed sessions: unchanged ─────────────────────────────────────────


def test_a_follower_without_observed_sessions_behaves_exactly_as_today(home) -> None:
    """The owned-only path is the pre-existing single-session follower."""
    _write_pointer(home, "r-mine", "my-node", session="mine", phase="starting")
    _write_pointer(home, "r-peer", "peer-node", session="peers", phase="starting")

    with runs._project_watch_claim("proj", "1h") as (acquired, _seat):
        assert acquired
        _deliver(home, "r-mine", "complete")
        _deliver(home, "r-peer", "blocked")
        crew.list_live(project="proj")
        events = _follow("proj", session="mine")

    nodes = {event["node"] for event in events}
    assert nodes == {"my-node"}, "a peer session's run must not reach this follower"


def test_an_empty_observed_set_render_is_byte_identical_to_today(home) -> None:
    """With ``observed=()`` the render path rewrites nothing and adds no column.

    Ownership is no longer a row column: the ticker's owner glyph was retired
    when the row became one aligned transition, so asking for the owner mark
    changes nothing about the line.
    """
    event = {
        "project": "proj",
        "event": "transition",
        "run_id": "r-mine",
        "node": "my-node",
        "session": "mine",
        "to_state": "working",
        "working": 1,
        "blocked": 0,
        "unpromoted": 0,
    }
    render = cli._follow_render_event(event, session="mine", observed=())
    assert render is event, "an empty observed set must not rewrite the row"
    assert recovery.format_watch_transition(event, with_session=True) == (
        recovery.format_watch_transition(event, with_session=False)
    ), "ownership is not a row column: no owner mark is drawn"


# ── Marking ─────────────────────────────────────────────────────────────────


def test_rows_from_an_observed_session_carry_the_foreign_marker_and_own_rows_do_not(
    home,
) -> None:
    """A row observed from another session keeps its name where an own row's is blanked.

    The ticker's foreign-owner glyph was retired when the row became one
    aligned transition and ownership stopped being a column, so the marker is
    now the retained session on the event rather than a drawn glyph. The
    owning session's row is blanked and an observed one keeps its name; the
    render treats the two identically, which is exactly what "no owner column"
    means.
    """
    _write_pointer(home, "r-mine", "my-node", session="mine", phase="working")
    _write_pointer(home, "r-old", "old-node", session="old-a", phase="working")

    with runs._project_watch_claim("proj", "1h"):
        crew.list_live(project="proj")
        events = _follow("proj", session="mine", observed=("old-a",))

    by_node = {event["node"]: event for event in events}
    own_event = cli._follow_render_event(
        by_node["my-node"], session="mine", observed=("old-a",)
    )
    observed_event = cli._follow_render_event(
        by_node["old-node"], session="mine", observed=("old-a",)
    )
    assert own_event["session"] == "", "the owning session's row is blanked"
    assert observed_event["session"] == "old-a", "an observed row keeps its name"
    own_rendered = recovery.format_watch_transition(own_event, with_session=True)
    observed_rendered = recovery.format_watch_transition(
        observed_event, with_session=True
    )
    assert "old-a" not in observed_rendered, (
        "the marker names no session: ownership is not a row column"
    )
    assert "mine" not in own_rendered.split("my-node", 1)[0], (
        "the owning session's row carries no session either"
    )
    assert observed_rendered == recovery.format_watch_transition(
        {**observed_event, "session": ""}, with_session=True
    ), "ownership draws no column: blanking the session changes nothing"


def test_an_observed_session_named_identically_to_the_owning_session_adds_no_duplicate(
    home,
) -> None:
    """Observing your own session again is harmless: one transition, one row.

    The row is the owning session's, so it is blanked like any own row and no
    second copy is delivered for the same name. Ownership draws no column, so
    there is no marker to be added or withheld.
    """
    _write_pointer(home, "r-mine", "my-node", session="mine", phase="working")

    with runs._project_watch_claim("proj", "1h"):
        crew.list_live(project="proj")
        received = _follow("proj", session="mine", observed=("mine",))

    rows = [event for event in received if event["node"] == "my-node"]
    assert len(rows) == 1, "the owning session's row is delivered exactly once"
    rendered_event = cli._follow_render_event(
        rows[0], session="mine", observed=("mine",)
    )
    assert rendered_event["session"] == "", "an own row stays blank"
    rendered = recovery.format_watch_transition(rendered_event, with_session=True)
    assert "mine" not in rendered.split("my-node", 1)[0], (
        "an own row stays own even when it is also named as observed"
    )


def test_an_observed_session_that_dispatched_nothing_yields_no_rows_and_no_error(
    home,
) -> None:
    """Observing a mute session is quiet, not an error."""
    _write_pointer(home, "r-mine", "my-node", session="mine", phase="working")

    with runs._project_watch_claim("proj", "1h"):
        crew.list_live(project="proj")
        events = _follow("proj", session="mine", observed=("ghost",))

    assert {event["node"] for event in events} == {"my-node"}
    assert not runs.follower_lock_path("proj", "ghost").exists()


# ── Isolation ───────────────────────────────────────────────────────────────


def test_the_real_configuration_home_gains_no_file(home) -> None:
    """Every write in this file lands in the temporary home, never the real one.

    The artifact paths are re-resolved against the real config home (the env
    override popped), and must be absent there both before and after the full
    observe flow. The names are unique to this test, so a live session writing
    its own state elsewhere in the shared home cannot race the check.
    """
    artifacts = _real_home_artifacts()
    for artifact in artifacts:
        assert not artifact.exists(), f"a real-home artifact pre-exists: {artifact}"

    _write_pointer(home, "r-mine", "my-node", session="mine", phase="working")
    _write_pointer(home, "r-old", "old-node", session="old-a", phase="working")
    with runs._project_watch_claim("proj", "1h"):
        crew.list_live(project="proj")
        with runs.follower_claim("proj", "mine", delivery="stream") as (held, _r):
            assert held is True
            events = _follow("proj", session="mine", observed=("old-a",))

    assert {event["node"] for event in events} == {"my-node", "old-node"}

    for artifact in artifacts:
        assert not artifact.exists(), f"the real home gained a file: {artifact}"
