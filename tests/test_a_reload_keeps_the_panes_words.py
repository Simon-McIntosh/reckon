"""A follower that reloads or re-arms keeps the words its pane already shows.

A pane fed by the station subscriber lost two things every time its follower
started a new process. Its row painter remembered no run, so the first row for
each run after the start was drawn as arriving from ``new``, although the
subscriber's checkpoint held the word the pane last drew. And a reload onto new
code, which keeps the pane, drew the re-arm snapshot of the session's live runs
as if the pane had been cleared, once for every landing on the primary branch.

The first two cases drive the follower's own attach on a fake subscriber, so
what is measured is the event the attach yields. The last two drive the wired
``crew follow`` command over that event, so what is measured is the pane.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew_follow_commands as follow
from reckon.crew import runs
from tests import test_a_pipe_rearm_draws_each_run_once as rearm

PROJECT = "reload-proj"
SESSION = "s-reload"
RUN = "r-reload-one"


@pytest.fixture()
def rearm_repository(tmp_path, monkeypatch):
    return rearm.repository.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture()
def rearm_station(tmp_path, rearm_repository, monkeypatch):
    return rearm.station.__wrapped__(tmp_path, rearm_repository, monkeypatch)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


class _Subscriber:
    """The attach reads the stored history and the checkpoint's words only."""

    def __init__(self, *, history, reported) -> None:
        self.history = list(history)
        self.path = SimpleNamespace(reported=dict(reported))

    def wake(self, **_kwargs):
        return iter(())


def _attach_events(*, reloaded_in_place: bool) -> list[dict]:
    subscriber = _Subscriber(
        history=[{"run_id": RUN, "to_state": "working"}],
        reported={RUN: "working"},
    )
    events = follow._follow_watch_lines(
        PROJECT,
        session=SESSION,
        subscriber=subscriber,
        reloaded_in_place=reloaded_in_place,
        sleeper=lambda _seconds: None,
        lifetime=1.0,
    )
    return [
        event for event in events if event.get("event") == follow.FOLLOWER_RESUME_EVENT
    ]


def test_a_reload_seeds_the_painter_and_owes_no_snapshot(repository):
    events = _attach_events(reloaded_in_place=True)
    assert len(events) == 1, events
    assert events[0]["reported"] == {RUN: "working"}
    assert events[0]["resume_snapshot"] is False


def test_a_rearm_seeds_the_painter_and_owes_a_snapshot(repository):
    events = _attach_events(reloaded_in_place=False)
    assert len(events) == 1, events
    assert events[0]["reported"] == {RUN: "working"}
    assert events[0]["resume_snapshot"] is True


def _follow_output(monkeypatch: pytest.MonkeyPatch, events: list[dict]) -> str:
    def arming(*_args, **_kwargs):
        yield from (dict(event) for event in events)

    monkeypatch.setattr(follow, "_follow_watch_lines", arming)
    monkeypatch.setattr(follow, "_hand_off_from_follower", lambda *_a, **_kw: False)
    result = CliRunner().invoke(
        cli_module.main,
        ["crew", "follow", "--project", PROJECT, "--session", SESSION],
    )
    assert result.exit_code == 0, result.output
    return result.output


def _snapshot_fixture(station, repository) -> None:
    """A live run the pane last saw at an earlier word, which a re-arm draws."""
    home = repository.parent / "config"
    rearm._write_pointer(home, rearm.LATER)
    rearm._seed_station_checkpoint(station, rearm.EARLIER)
    runs.write_delivered(rearm.PROJECT, rearm.SESSION, {rearm.RUN: rearm.EARLIER})


def _snapshot_output(monkeypatch, *, owed: bool) -> str:
    def arming(*_args, **_kwargs):
        yield {
            "event": follow.FOLLOWER_RESUME_EVENT,
            "project": rearm.PROJECT,
            "session": rearm.SESSION,
            "reported": {},
            "resume_snapshot": owed,
        }

    monkeypatch.setattr(follow, "_follow_watch_lines", arming)
    monkeypatch.setattr(follow, "_hand_off_from_follower", lambda *_a, **_kw: False)
    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "follow",
            "--project",
            rearm.PROJECT,
            "--session",
            rearm.SESSION,
            "--lifetime",
            "3s",
        ],
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_a_rearm_draws_the_snapshot_line(rearm_station, rearm_repository, monkeypatch):
    """The positive control: the fixture draws the run when a snapshot is owed."""
    _snapshot_fixture(rearm_station, rearm_repository)
    output = _snapshot_output(monkeypatch, owed=True)
    assert f"  {rearm.RUN}  {rearm.LATER}" in output, output


def test_a_reload_draws_no_snapshot_line(rearm_station, rearm_repository, monkeypatch):
    _snapshot_fixture(rearm_station, rearm_repository)
    output = _snapshot_output(monkeypatch, owed=False)
    assert f"  {rearm.RUN}  " not in output, output


def test_a_remembered_run_continues_from_its_word(repository, monkeypatch):
    row = {
        "event": "transition",
        "run_id": RUN,
        "node": RUN,
        "project": PROJECT,
        "session": SESSION,
        "role": "implement",
        "from_state": "running",
        "to_state": "complete",
        "classification": "complete",
        "observed_at": "2026-11-14T08:00:00Z",
    }
    output = _follow_output(
        monkeypatch,
        [
            {
                "event": follow.FOLLOWER_RESUME_EVENT,
                "project": PROJECT,
                "session": SESSION,
                "reported": {RUN: "working"},
                "resume_snapshot": False,
            },
            row,
        ],
    )
    drawn = [line for line in output.splitlines() if "→" in line]
    assert drawn, output
    assert all(" new " not in line for line in drawn), output
    assert any("working" in line for line in drawn), output
