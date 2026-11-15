"""A deliberate deferral ends on evidence, a deadline, or a new run state."""

from __future__ import annotations

import importlib
import json
import sys
from datetime import UTC, datetime, timedelta

import pytest
from click.testing import CliRunner

from reckon import cli
from reckon.crew import runs
from reckon.hooks import coordinator_obligations as hook

obligations = importlib.import_module("reckon.crew.obligations")
PROJECT = "conditional-ack"
RUN = "conditional-run"


@pytest.fixture()
def duty(tmp_path, monkeypatch):
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    home.mkdir()
    (home / "mounts.json").write_text("{}")
    pointer = {
        "run_id": RUN,
        "project": PROJECT,
        "session": "owner",
        "phase": "blocked",
        "attempt": 1,
        "worktree": str(tmp_path),
    }
    runs._write_json(runs.pointer_path(RUN), pointer)
    row = {
        "run_id": RUN,
        "kind": "blocked",
        "next_command": f"reckon crew resume --run {RUN} --advice continue",
        "age_seconds": 10,
        "reason": "the worker needs advice",
    }
    return {
        "project": PROJECT,
        "session": "owner",
        "obligations": [row],
        "acknowledged": [],
        "summary": {"count": 1},
        "closure": {"unreconciled_runs": 1},
    }


def _ack(condition=None, *, until=None):
    args = [
        "crew",
        "ack",
        "--run",
        RUN,
        "--reason",
        "awaiting re-review",
        "--until",
        until or (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    ]
    if condition is not None:
        args.extend(["--until-condition", json.dumps(condition)])
    result = CliRunner().invoke(cli.main, args)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["acknowledgement"]


def _read(duty):
    return hook.reapply_acknowledgements(duty, project=PROJECT)


def test_a_condition_ends_the_acknowledgement_and_returns_a_snapshotted_duty(
    duty,
    tmp_path,
):
    marker = tmp_path / "review-stored"
    assert _read(duty)["obligations"], "the reader must first see the open duty"
    record = _ack({"condition": "re-review stored", "files": [str(marker)]})
    held = _read(duty)
    assert held["obligations"] == []
    assert held["summary"]["count"] == 0
    assert held["acknowledged"][0]["reason"] == "awaiting re-review"
    assert held["acknowledged"][0]["until_condition"] == record["until_condition"]
    marker.touch()
    released = _read(held)
    assert [row["run_id"] for row in released["obligations"]] == [RUN]
    assert released["acknowledged"] == []
    assert released["summary"]["count"] == 1
    assert released["obligations"] == duty["obligations"]
    marker.unlink()
    assert _read(duty)["obligations"], "an ended deferral cannot become active again"


@pytest.mark.parametrize("field,value", [("phase", "scoring"), ("attempt", 2)])
def test_a_run_state_change_releases_its_acknowledgement(duty, field, value):
    _ack()
    assert _read(duty)["obligations"] == []
    runs._mutate_pointer(RUN, lambda pointer: {**pointer, field: value})
    assert [row["run_id"] for row in _read(duty)["obligations"]] == [RUN]
    assert runs.run_acknowledgement(runs.read_pointer(RUN)) is None


def test_a_state_observed_from_a_direct_pointer_write_releases_the_deferral(duty):
    _ack()
    pointer = runs.read_pointer(RUN)
    pointer["phase"] = "scoring"
    runs._write_json(runs.pointer_path(RUN), pointer)
    assert _read(duty)["obligations"]
    assert runs.run_acknowledgement(runs.read_pointer(RUN)) is None


def test_an_observation_update_preserves_a_deliberate_deferral(duty):
    _ack()
    runs._mutate_pointer(RUN, lambda pointer: {**pointer, "observed_at": "later"})
    assert _read(duty)["obligations"] == []


def test_a_probe_can_end_the_deferral_before_the_deadline(duty, tmp_path):
    marker = tmp_path / "review-state"
    marker.write_text("pending")
    _ack(
        {
            "condition": "re-review accepted",
            "probe": [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text())",
                str(marker),
            ],
            "terminal": ["accepted"],
        }
    )
    assert _read(duty)["obligations"] == []
    marker.write_text("accepted")
    assert _read(duty)["obligations"]


def test_the_deadline_still_ends_a_pending_condition(duty, tmp_path):
    _ack(
        {"condition": "re-review stored", "files": [str(tmp_path / "missing")]},
        until=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    assert _read(duty)["obligations"]


@pytest.mark.parametrize(
    "condition",
    [
        "review complete",
        {},
        {"condition": "review", "files": []},
        {"condition": "review", "probe": "echo done", "terminal": ["done"]},
        {"condition": "review", "probe": ["echo", "done"]},
    ],
)
def test_an_unreadable_condition_is_refused_without_recording_it(duty, condition):
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "ack",
            "--run",
            RUN,
            "--reason",
            "deliberate",
            "--until",
            (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "--until-condition",
            json.dumps(condition),
        ],
    )
    assert result.exit_code != 0
    assert "condition" in result.output
    assert runs.run_acknowledgement(runs.read_pointer(RUN)) is None


def test_a_returned_condition_blocks_the_stop_without_a_new_snapshot(
    duty,
    tmp_path,
    monkeypatch,
    capsys,
):
    marker = tmp_path / "review-stored"
    monkeypatch.setattr(hook, "locate_session", lambda _payload: (PROJECT, "owner"))
    monkeypatch.setattr(hook, "follow_each_local_lane", lambda *_args, **_kwargs: None)
    snapshot = hook.snapshot_module()
    published = {"payload": duty, "stale": False}
    monkeypatch.setattr(snapshot, "read_station_answer", lambda *_args: published)
    monkeypatch.setattr(
        hook, "recheck_worktree_held_rows", lambda payload, **_kwargs: payload
    )
    assert hook._stop({}) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "block"

    _ack({"condition": "re-review stored", "files": [str(marker)]})
    held = _read(duty)
    published["payload"] = held
    assert hook._stop({}) == 0
    assert capsys.readouterr().out == ""
    assert "awaiting re-review" in hook.format_checklist(held)
    marker.touch()
    assert hook._stop({}) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "block"


def test_the_prompt_shows_an_acknowledgement_and_announces_its_release(
    duty,
    tmp_path,
    monkeypatch,
    capsys,
):
    marker = tmp_path / "review-stored"
    monkeypatch.setattr(hook, "pending_model_notices", lambda: 0)
    monkeypatch.setattr(hook, "limit_reset_events", lambda _session: [])
    _ack({"condition": "re-review stored", "files": [str(marker)]})
    held = _read(duty)

    def prompt():
        hook._inject_list(
            {}, project=PROJECT, session="owner", obligations=held, reloading=False
        )
        return capsys.readouterr().out

    first = json.loads(prompt())["hookSpecificOutput"]["additionalContext"]
    assert "[acknowledged]" in first
    assert "awaiting re-review" in first
    assert "re-review stored" in first
    held["acknowledged"][0]["age_seconds"] += 1
    assert prompt() == ""
    marker.touch()
    returned = json.loads(prompt())["hookSpecificOutput"]["additionalContext"]
    assert "1 outstanding" in returned
    assert "[acknowledged]" not in returned
