"""A re-attach replays the gap between two armings instead of hiding it.

A session's pane leaves a record, beside its follower registration, of the state
each of its runs was last shown at. A follower attaching for a session that
holds that record opens with one header line — how many live runs it found and
how many changed since the record was written — and then draws the runs whose
state differs from the record, as ordinary transitions from the recorded state.
A run that did not move draws nothing, and a run the record does not name at all
is replayed from ``dispatched``, the state every launch passes through. The
pane withholds observer context, so the replay draws only the rows that ask the
coordinator for something: a run whose replay resolves into a progress state
such as ``working`` is held back, and the record therefore holds only the action
states the pane showed.

The record is written where a row is written to the pane, never where one is
generated: a row this reader never received is not one it saw, and a replay
built from a record that counted it would subtract a run the reader has not been
told about.

The diff answers an attach with no place to resume from — the follower whose
recorded offset is gone — so these cases drop the stored place before the
second arming rather than leaving the arming to resume from its offset. An
arming that still has a place is owed what the stream gained while it was
away, and the fleet replay delivers exactly that; the record's diff would
lose a transition that ends where it started and draw nothing when nothing
changed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew, crew_follow_commands, ledger
from reckon.crew import follow_checkpoint, runs
from reckon.crew import ticker as ticker_module

PROJECT = "replays-the-gap"
SESSION = "s-gap"
RUN_A = "r-gap-a"
RUN_B = "r-gap-b"
RUN_C = "r-gap-c"
RUN_P = "r-gap-p"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep pointers, manifests, streams and records in temporary state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture(autouse=True)
def _a_live_owner(monkeypatch):
    """Isolate every arming from an owner stamped into the ambient environment.

    A follower stamps its own pid into ``RECKON_FOLLOWER_OWNER`` for the
    processes it launches, and one it launched outlives it once the follower
    exits on its lifetime. An arming that read that owner as its own ends at its
    first wait pass, so several cases here measure a *second* arming and a first
    arming ended early would leave the record they replay unset.
    """
    previous = runs._RESOLVED_FOLLOWER_OWNER.resolved
    monkeypatch.delenv(runs._FOLLOWER_OWNER_ENV, raising=False)
    runs._RESOLVED_FOLLOWER_OWNER.resolved = None
    yield
    runs._RESOLVED_FOLLOWER_OWNER.resolved = previous


@pytest.fixture()
def follow_lines(monkeypatch):
    """Capture the follower command's own lines instead of writing them out."""
    lines: list[str] = []

    def capture(line, *, stream=None):
        lines.append(line)

    monkeypatch.setattr(crew_follow_commands, "_echo_follow_line", capture)
    return lines


def _write_pointer(
    home: Path, run_id: str, node: str, *, phase: str, repo: Path | None = None
) -> None:
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n')
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "node": {"id": node, "plan": "plan-a", "time_budget": "20m"},
        "phase": phase,
        "created_at": runs._utc_now(),
        "manifest_path": str(home / "manifests" / f"{run_id}.md"),
        "log_path": str(log),
        "process_alive": None,
    }
    if repo is not None:
        pointer["repo"] = str(repo)
    crew._write_json(crew.pointer_path(run_id), pointer)


def _two_live_runs(home: Path) -> None:
    _write_pointer(home, RUN_A, "node-a", phase="working")
    _write_pointer(home, RUN_B, "node-b", phase="working")


def _deliver(home: Path, run_id: str, status: str) -> None:
    """Write the worker's manifest, moving the run to the state it words."""
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: {status}\ncommits: HEAD\nblockers: none\n"
    )


def _run_follow(*, json_output: bool = False) -> None:
    """Arm the real follower command once, to its own short lifetime."""
    result = CliRunner().invoke(
        cli.crew,
        [
            "follow",
            "--project",
            PROJECT,
            "--session",
            SESSION,
            "--lifetime",
            "1s",
            "--no-color",
            "--width",
            "200",
            *(["--json"] if json_output else []),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output


def _the_place() -> Path:
    """Where this session's follower records the place it may resume from."""
    return follow_checkpoint.checkpoint_path(PROJECT, SESSION)


def _drop_the_place() -> None:
    """Take the place away, so the next arming has none to resume from.

    An arming that still has a place resumes from its offset and is owed the
    transitions the stream gained while it was away, which is the fleet
    replay's job. These cases measure the attach that has no such place.
    """
    _the_place().unlink(missing_ok=True)
    assert not _the_place().exists(), f"{_the_place()} still names a place"


def _promote(home: Path, run_id: str, repo: Path) -> None:
    """Word a run promoted in the ledger, with its live pointer left behind."""
    _deliver(home, run_id, "complete")
    row = ledger.run_path(PROJECT, run_id, str(repo))
    row.parent.mkdir(parents=True, exist_ok=True)
    row.write_text(
        json.dumps({"run_id": run_id, "project": PROJECT, "commits": ["HEAD"]})
    )


def _fleet_rows(lines) -> list[str]:
    """The drawn fleet rows, without the pane's framing or the follower's end."""
    return [
        line
        for line in lines
        if "re-attached" not in line and "follower end" not in line
    ]


def _naming(lines, node: str) -> list[str]:
    """The drawn rows that name one run's node."""
    return [line for line in _fleet_rows(lines) if node in line]


def test_a_reattach_replays_only_the_runs_that_moved(home, follow_lines) -> None:
    """The gap is one action row per moved run, under one header, and nothing else.

    The first arming shows both failed runs and leaves the record behind. One run
    is then delivered — it stops failing and waits to be promoted — while the
    other stays exactly where it was. The re-attach opens with the header, draws
    the moved run once as ``failed → unpromoted``, and draws nothing at all for
    the run that did not move: a run still at the state the record names carries
    no news.
    """
    _two_live_runs(home)
    _deliver(home, RUN_A, "failed")
    _deliver(home, RUN_B, "failed")
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)
        _run_follow()
        record = runs.read_delivered(PROJECT, SESSION)
        assert record.get("states") == {RUN_A: "failed", RUN_B: "failed"}, (
            f"the first arming records the action rows it showed; got {record!r}"
        )
        shown_at = str(record.get("recorded_at") or "")
        assert shown_at, record
        assert _the_place().exists(), (
            "the first arming leaves a place in the stream behind it"
        )

        # One owned run moves while nothing is attached; the other does not.
        _deliver(home, RUN_A, "complete")
        crew.list_live(project=PROJECT)

        _drop_the_place()
        follow_lines.clear()
        _run_follow()

    header = follow_lines[0]
    assert "re-attached" in header, follow_lines
    assert "2 live runs" in header, header
    assert "1 changed" in header, header
    assert ticker_module.local_clock(shown_at) in header, header

    moved = _naming(follow_lines, "node-a")
    assert len(moved) == 1, f"the moved run is drawn exactly once; got {follow_lines!r}"
    row = moved[0]
    assert "failed" in row and "unpromoted" in row, row
    assert ticker_module.ARROW in row, row

    assert not _naming(follow_lines, "node-b"), (
        f"a run whose state is unchanged draws no row; got {follow_lines!r}"
    )


def test_a_run_the_record_does_not_name_is_replayed_from_dispatched(
    home, follow_lines
) -> None:
    """A run dispatched while nothing was attached is counted, and held back.

    The record names the two runs the pane was last shown. A third pointer is
    written afterwards, so the re-attach finds a live run the record does not
    name; it is replayed from ``dispatched``, which is the state its launch
    passed through, but the replay resolves into ``working``, an observer state,
    so the pane withholds it. The two runs the record does name and that did not
    move draw nothing either, while the header still counts the new run among
    the changed.
    """
    _two_live_runs(home)
    _deliver(home, RUN_A, "failed")
    _deliver(home, RUN_B, "failed")
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)
        _run_follow()
        assert runs.read_delivered(PROJECT, SESSION).get("states"), (
            "the first arming records what it showed"
        )

        _write_pointer(home, RUN_C, "node-c", phase="working")
        crew.list_live(project=PROJECT)
        _drop_the_place()
        follow_lines.clear()
        _run_follow()

    header = follow_lines[0]
    assert "3 live runs" in header, header
    assert "1 changed" in header, header
    arrived = _naming(follow_lines, "node-c")
    assert arrived == [], (
        f"a run replayed from dispatched into working is observer context the "
        f"pane withholds; got {follow_lines!r}"
    )
    assert not _naming(follow_lines, "node-a"), follow_lines
    assert not _naming(follow_lines, "node-b"), follow_lines


def test_the_record_follows_every_row_the_pane_receives(home, follow_lines) -> None:
    """The replay row itself is recorded, so a second re-attach is quiet.

    The record is the pane's own memory: after the moved run is drawn, the
    record holds its new state, so a further attach with nothing else moved
    opens with the header and draws no run at all.
    """
    _two_live_runs(home)
    _deliver(home, RUN_A, "failed")
    _deliver(home, RUN_B, "failed")
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)
        _run_follow()

        _deliver(home, RUN_A, "complete")
        crew.list_live(project=PROJECT)
        _drop_the_place()
        _run_follow()
        record = runs.read_delivered(PROJECT, SESSION)
        assert record.get("states", {}).get(RUN_A) == "completed_unpromoted", record

        _drop_the_place()
        follow_lines.clear()
        _run_follow()

    assert "re-attached" in follow_lines[0], follow_lines
    assert "0 changed" in follow_lines[0], follow_lines[0]
    assert not _naming(follow_lines, "node-a"), follow_lines
    assert not _naming(follow_lines, "node-b"), follow_lines


def test_a_json_arming_records_only_what_a_pane_would_draw(
    home, follow_lines
) -> None:
    """The record is the pane's memory, not the JSON consumer's.

    A session can be armed as a JSON reader and later as a pane in front of a
    person. JSON output carries the settled inventory a pane withholds — a
    promoted run asks a reader for nothing — and recording one would leave the
    record holding a state only the JSON consumer received. The pane's next
    attach would diff against a state it never showed, and read the run's
    re-dispatch as a promotion the reader was never told about. The run shown as
    failed is the pane's own row, so it is recorded; the re-dispatched run's
    replay resolves into working, an observer state, and is withheld.
    """
    repo = home / "repo"
    repo.mkdir()
    _write_pointer(home, RUN_A, "node-a", phase="working")
    _write_pointer(home, RUN_P, "node-p", phase="working", repo=repo)
    _deliver(home, RUN_A, "failed")
    _promote(home, RUN_P, repo)

    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        crew.list_live(project=PROJECT)
        _run_follow(json_output=True)
        record = runs.read_delivered(PROJECT, SESSION)
        assert record.get("states", {}).get(RUN_A) == "failed", record
        assert RUN_P not in record.get("states", {}), (
            f"a row only the JSON consumer received is not one the pane drew; got {record!r}"
        )

        # The run is dispatched again while nothing is attached, so the pane's
        # next attach has something to say about it.
        ledger.run_path(PROJECT, RUN_P, str(repo)).unlink()
        _deliver(home, RUN_P, "in-progress")
        crew.list_live(project=PROJECT)
        _drop_the_place()
        follow_lines.clear()
        _run_follow()

    assert "re-attached" in follow_lines[0], follow_lines
    rows = _naming(follow_lines, "node-p")
    assert rows == [], (
        f"a run replayed from dispatched into working is observer context the "
        f"pane withholds; got {follow_lines!r}"
    )
