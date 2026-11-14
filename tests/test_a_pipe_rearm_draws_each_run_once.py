"""A pipe re-arm draws each live run of its session once.

A session with its host down runs its follower as a Monitor, and a Monitor is a
re-pipe, so the follower is re-armed every half hour. Each re-arm asks which of
the session's live runs the pane has not been shown at its current word, and the
answer is read from the delivered-state store the *live* rows advance. On a node
whose station is running, that store is the station subscriber's checkpoint and
the file record is not written at all, so a re-arm reading the file compares
against a record that stopped advancing the day the station took over: every
live run then looks undrawn and every arming reprints the session's whole fleet.

The cases below drive the wired ``crew follow`` command against a temporary crew
home, so the store selection under test is the one the command itself makes
rather than one a case hands it. Each case re-arms twice, which is the pair the
report named: the first draws what the pane lacks, and the second must find the
word already recorded and draw nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew_follow_commands as follow
from reckon.crew import runs, station_store
from reckon.crew.runs import crew_home
from tests.helpers.git import git as _git

PROJECT = "rearm-proj"
SESSION = "s-rearm"
RUN = "r-rearm-one"

# The words the pane draws, written here as literals rather than derived, so a
# case cannot pass by agreeing with the code it measures.
EARLIER = "working"
LATER = "waiting"
MOVED = "queued"

# The pointer phases that classify to those words. A request held on a section
# it waits for carries ``awaiting-upstream``, whose pane word is ``waiting``.
PHASE_OF = {EARLIER: "running", LATER: "awaiting-upstream", MOVED: "queued"}


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A crew home and a mounted project the follower can read."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the re-arm fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _write_pointer(home: Path, word: str, *, session: str = SESSION) -> None:
    """A live run of the session, standing at the word named."""
    log = home / "logs" / f"{RUN}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n', encoding="utf-8")
    runs._write_json(
        runs.pointer_path(RUN),
        {
            "run_id": RUN,
            "project": PROJECT,
            "session": session,
            "node": {"id": RUN, "plan": "rearm-plan", "section": "rearm-section"},
            "phase": PHASE_OF[word],
            "created_at": runs._utc_now(),
            "manifest_path": str(home / "manifests" / f"{RUN}.md"),
            "log_path": str(log),
            "process_alive": True,
        },
    )


def _rewrite_word(home: Path, word: str) -> None:
    """Move the run to another word, as the producer's next classification would."""
    _write_pointer(home, word)


def _station(runtime: Path, *, beating: bool) -> station_store.StationStore:
    """A station store this crew home owns, optionally one that has beaten."""
    store = station_store.StationStore(runtime)
    store.set_station_value(station_store.CREW_HOME_KEY, str(crew_home()))
    if beating:
        store.heartbeat()
    return store


def _seed_station_checkpoint(store: station_store.StationStore, word: str) -> None:
    store.write_cursor(PROJECT, SESSION, checkpoint={"reported": {RUN: word}})


@pytest.fixture()
def station(
    tmp_path: Path, repository: Path, monkeypatch: pytest.MonkeyPatch
) -> station_store.StationStore:
    """A beaten station, so the follower delivers from the station's own row."""
    runtime = tmp_path / "runtime"
    store = _station(runtime, beating=True)
    monkeypatch.setattr(
        station_store.StationStore,
        "at_runtime_root",
        classmethod(lambda cls, **_kw: station_store.StationStore(runtime)),
    )
    return store


def _rearm(monkeypatch: pytest.MonkeyPatch) -> str:
    """One pipe re-arm of the wired command: its pane output, and nothing else.

    The watch loop is replaced by the one event a re-arm emits, so what is
    measured is the resume branch's derivation against the store the command
    selected, with no producer, no station tick and no fresh row in the way.
    """
    events = [
        {
            "event": follow.FOLLOWER_RESUME_EVENT,
            "project": PROJECT,
            "session": SESSION,
            "reported": {},
        }
    ]

    def arming(*_args, **_kwargs):
        yield from (dict(event) for event in events)

    monkeypatch.setattr(follow, "_follow_watch_lines", arming)
    monkeypatch.setattr(follow, "_hand_off_from_follower", lambda *_a, **_kw: False)
    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "follow",
            "--project",
            PROJECT,
            "--session",
            SESSION,
            "--lifetime",
            "3s",
        ],
    )
    assert result.exit_code == 0, result.output
    return result.output


def _drawn(output: str) -> list[str]:
    """The lines on the pane that name the run."""
    return [line for line in output.splitlines() if RUN in line]


def _live_word() -> str:
    """The word the resume derivation reads for the run, as an instrument check."""
    rows = follow._follow_live_state_rows(PROJECT, SESSION)
    assert [str(row["run_id"]) for row in rows] == [RUN], rows
    return str(rows[0]["to_state"])


def test_a_pipe_rearm_with_a_subscriber_draws_the_run_once(
    station: station_store.StationStore,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The station's delivered states are what the re-arm reads and records."""
    home = repository.parent / "config"
    _write_pointer(home, LATER)
    # Both stores hold the run at an earlier word, exactly as the reported 9
    # November record did: the subscriber's checkpoint advances with every live
    # row, the file record has not moved since the station took delivery.
    _seed_station_checkpoint(station, EARLIER)
    runs.write_delivered(PROJECT, SESSION, {RUN: EARLIER})
    assert _live_word() == LATER, "the fixture did not produce the word under test"

    first = _rearm(monkeypatch)
    assert len(_drawn(first)) == 1, first
    assert f"  {RUN}  {LATER}" in first, first

    second = _rearm(monkeypatch)
    assert _drawn(second) == [], second


def test_a_run_that_moved_after_the_rearm_is_drawn_once_at_its_new_word(
    station: station_store.StationStore,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = repository.parent / "config"
    _write_pointer(home, LATER)
    _seed_station_checkpoint(station, EARLIER)
    runs.write_delivered(PROJECT, SESSION, {RUN: EARLIER})

    assert len(_drawn(_rearm(monkeypatch))) == 1

    _rewrite_word(home, MOVED)
    assert _live_word() == MOVED

    moved = _rearm(monkeypatch)
    assert len(_drawn(moved)) == 1, moved
    assert f"  {RUN}  {MOVED}" in moved, moved
    assert _drawn(_rearm(monkeypatch)) == []


def test_a_pipe_rearm_with_no_subscriber_draws_the_run_once(
    tmp_path: Path,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no station delivering, the file record is the store the rows use."""
    runtime = tmp_path / "runtime"
    _station(runtime, beating=False)
    monkeypatch.setattr(
        station_store.StationStore,
        "at_runtime_root",
        classmethod(lambda cls, **_kw: station_store.StationStore(runtime)),
    )
    home = repository.parent / "config"
    _write_pointer(home, LATER)
    runs.write_delivered(PROJECT, SESSION, {RUN: EARLIER})
    assert _live_word() == LATER, "the fixture did not produce the word under test"

    first = _rearm(monkeypatch)
    assert len(_drawn(first)) == 1, first

    second = _rearm(monkeypatch)
    assert _drawn(second) == [], second
