"""A named admission decides one request; ordered passes leave readable places."""

from __future__ import annotations

import fcntl
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from reckon import cli
from reckon.crew import queue_admission, runs
from tests import test_a_queued_request_waits_on_its_dependencies as waits
from tests import test_a_queued_run_is_admitted as queued

admit_repo = queued.admit_repo
chain_repo = waits.chain_repo


def _drive(monkeypatch, repository, tmp_path, *, slots):
    config, resolve = queued._config_for()
    config["backends"]["clive"]["lane_document"] = str(
        queued._lane_document(tmp_path, worker_slots=slots)
    )
    launched = []

    def launch(pointer, **_options):
        # A second drainer must be refused for the entire decision and launch.
        with (
            queue_admission._admit_lock_path().open("a+b") as handle,
            pytest.raises(BlockingIOError),
        ):
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        launched.append(pointer["run_id"])
        return runs._mutate_pointer(
            pointer["run_id"], lambda current: {**current, "phase": "starting"}
        )

    monkeypatch.setattr(queue_admission, "_launch_queued", launch)
    monkeypatch.setattr(
        queue_admission, "_resolve_lane_config", lambda _pointer, **_options: config
    )
    real_admit = queue_admission.admit_once

    def admit(**options):
        return real_admit(config_for=resolve, **options)

    monkeypatch.setattr(queue_admission, "admit_once", admit)
    return SimpleNamespace(
        repository=repository, launched=launched, resolve=resolve, admit=admit
    )


def _two_requests(repository):
    for run_id, priority in (("r-first", 100), ("r-chosen", 0)):
        pointer = queued._queue(repository, node_id=run_id, run_id=run_id)
        pointer["priority_override"] = priority
        runs.queue_dispatch(pointer)
    order = queue_admission.fair_share_admission_order(
        runs.list_live(),
        runs.list_live(),
        now=datetime.now(UTC),
        config_for=lambda _pointer: queued.CONFIG,
    )
    assert [pointer["run_id"] for pointer in order] == ["r-first", "r-chosen"]


def _invoke(run_id):
    result = CliRunner().invoke(cli.main, ["crew", "admit", "--run", run_id])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_named_admission_launches_the_lower_ranked_request_only(
    admit_repo, tmp_path, monkeypatch
):
    driver = _drive(monkeypatch, admit_repo, tmp_path, slots=2)
    _two_requests(admit_repo)
    untouched = runs.read_pointer("r-first")

    result = _invoke("r-chosen")

    assert driver.launched == ["r-chosen"]
    assert result["launched"] == ["r-chosen"]
    assert {row["run_id"] for row in result["verdicts"]} == {"r-chosen"}
    assert runs.read_pointer("r-first") == untouched
    assert runs.read_pointer("r-chosen")["phase"] == "starting"


def test_named_admission_prints_a_refusal_and_keeps_the_request_queued(
    admit_repo, tmp_path, monkeypatch
):
    driver = _drive(monkeypatch, admit_repo, tmp_path, slots=0)
    _two_requests(admit_repo)
    untouched = runs.read_pointer("r-first")

    result = _invoke("r-chosen")

    assert driver.launched == []
    assert result["held"] == ["r-chosen"]
    assert result["verdicts"][0]["verdict"] == "held-for-allowance"
    assert result["verdicts"][0]["allowance"] == 0
    pointer = runs.read_pointer("r-chosen")
    assert pointer["phase"] == "queued"
    assert pointer["last_admission"]["verdict"] == "held-for-allowance"
    assert runs.read_pointer("r-first") == untouched


def test_named_admission_runs_no_release_or_landing_and_leaves_a_ready_waiter(
    chain_repo, tmp_path, monkeypatch
):
    _drive(monkeypatch, chain_repo, tmp_path, slots=0)
    for run_id, section, refs in (
        ("r-n1", "s1", []),
        ("r-n2", "s2", [f"{waits.PLAN}#s1"]),
    ):
        pointer = queued._queue(chain_repo, node_id=run_id, run_id=run_id)
        pointer["node"]["section"] = section
        pointer["waits"] = refs
        pointer["phase"] = "awaiting-upstream" if refs else "queued"
        runs._write_json(runs.pointer_path(run_id), pointer)
    waits._declare(chain_repo, "s1")
    waiter = runs.read_pointer("r-n2")
    assert waiter["phase"] == "awaiting-upstream"
    release = Mock(wraps=queue_admission._release_tick)
    landing = Mock()
    landing.return_value.settle.return_value = {}
    advance = Mock(wraps=queue_admission.advance_waiting_requests)
    monkeypatch.setattr(queue_admission, "_release_tick", release)
    monkeypatch.setattr(queue_admission, "_LandingWorker", landing)
    monkeypatch.setattr(queue_admission, "advance_waiting_requests", advance)

    _invoke("r-n1")

    assert release.call_count == 0
    assert landing.call_count == 0
    assert advance.call_count == 0
    assert runs.read_pointer("r-n2") == waiter
    # Positive control: the same waiter really is releasable by a full pass.
    released = advance(runs.list_live())
    assert "r-n2" in released["promoted"]
    assert runs.read_pointer("r-n2")["phase"] == "queued"


def test_an_idle_ordered_pass_persists_every_queued_place_and_verdict(
    admit_repo, tmp_path, monkeypatch
):
    driver = _drive(monkeypatch, admit_repo, tmp_path, slots=0)
    _two_requests(admit_repo)

    result = driver.admit()

    assert result["launched"] == []
    assert driver.launched == []
    for position, run_id in enumerate(("r-first", "r-chosen"), 1):
        pointer = runs.read_pointer(run_id)
        assert pointer["phase"] == "queued"
        assert pointer["queue_position"] == position
        assert pointer["rank_key"] == [position, pointer["queued_at"], run_id]
        assert pointer["last_admission"]["verdict"] == "held-for-allowance"
        assert pointer["last_admission"]["run_id"] == run_id
        assert datetime.fromisoformat(pointer["last_admission"]["recorded_at"])


def test_a_waiting_fleet_gate_writes_no_queue_order(admit_repo, tmp_path, monkeypatch):
    driver = _drive(monkeypatch, admit_repo, tmp_path, slots=2)
    _two_requests(admit_repo)
    original = {run_id: runs.read_pointer(run_id) for run_id in ("r-first", "r-chosen")}
    monkeypatch.setattr(
        queue_admission, "_dispatch_fleet_gate", lambda: {"state": "paused"}
    )

    named = _invoke("r-chosen")
    ordered = driver.admit()

    assert named["held"] == ["r-chosen"]
    assert named["verdicts"][0]["detail"] == "paused"
    assert ordered["action"] == "gated"
    assert driver.launched == []
    for run_id, pointer in original.items():
        assert runs.read_pointer(run_id) == pointer


@pytest.mark.parametrize("phase", ["awaiting-upstream", "running", "held"])
def test_named_admission_refuses_a_request_outside_the_queue(
    admit_repo, tmp_path, monkeypatch, phase
):
    driver = _drive(monkeypatch, admit_repo, tmp_path, slots=2)
    _two_requests(admit_repo)
    runs._mutate_pointer("r-chosen", lambda pointer: {**pointer, "phase": phase})

    result = CliRunner().invoke(cli.main, ["crew", "admit", "--run", "r-chosen"])

    assert result.exit_code != 0
    assert "named admission requires a queued request" in result.output
    assert driver.launched == []
    assert runs.read_pointer("r-chosen")["phase"] == phase


def test_the_help_names_the_single_request_boundary():
    result = CliRunner().invoke(cli.main, ["crew", "admit", "--help"])

    assert result.exit_code == 0
    text = " ".join(result.output.split())
    assert "--run" in text
    assert "only this queued request" in text
    assert "without release or landing" in text
    assert "under the admit lock" in text


@pytest.mark.parametrize("narrowed", [False, True])
def test_live_view_shows_the_stored_queue_position_rank_and_verdict(
    admit_repo, tmp_path, monkeypatch, narrowed
):
    driver = _drive(monkeypatch, admit_repo, tmp_path, slots=0)
    _two_requests(admit_repo)
    driver.admit()
    expected = {run_id: runs.read_pointer(run_id) for run_id in ("r-first", "r-chosen")}
    fields = ("queue_position", "rank_key", "last_admission")
    assert all(set(fields) <= set(pointer) for pointer in expected.values())
    arguments = [
        "crew",
        "view",
        "live",
        "--project",
        queued.PROJECT,
        "--checkout-path",
        str(admit_repo),
    ]
    if narrowed:
        arguments.extend(["--fields", ",".join(fields)])

    result = CliRunner().invoke(cli.main, arguments)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"], payload
    rows = {row["run_id"]: row for row in payload["runs"]}
    assert set(rows) == set(expected)
    for run_id, pointer in expected.items():
        for field in fields:
            assert rows[run_id][field] == pointer[field]
        if narrowed:
            assert set(rows[run_id]) == {"run_id", *fields}


def test_live_view_accepts_admission_fields_when_no_request_is_queued(admit_repo):
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "view",
            "live",
            "--project",
            queued.PROJECT,
            "--checkout-path",
            str(admit_repo),
            "--fields",
            "queue_position,rank_key,last_admission",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"], payload
    assert payload["runs"] == []


def test_live_view_omits_stored_queue_fields_after_launch(
    admit_repo, tmp_path, monkeypatch
):
    driver = _drive(monkeypatch, admit_repo, tmp_path, slots=0)
    _two_requests(admit_repo)
    driver.admit()
    pointer = runs.read_pointer("r-chosen")
    assert pointer["queue_position"] == 2
    runs._mutate_pointer("r-chosen", lambda current: {**current, "phase": "starting"})

    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "view",
            "live",
            "--project",
            queued.PROJECT,
            "--checkout-path",
            str(admit_repo),
        ],
    )

    assert result.exit_code == 0, result.output
    rows = {row["run_id"]: row for row in json.loads(result.output)["runs"]}
    assert set(rows) == {"r-first", "r-chosen"}
    assert not {"queue_position", "rank_key", "last_admission"} & set(rows["r-chosen"])
