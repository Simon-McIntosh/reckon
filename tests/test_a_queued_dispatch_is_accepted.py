"""A queued dispatch is an acceptance, capped at the session's fair share."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew_dispatch_commands
from reckon.crew import runs
from tests import test_dispatch_names_its_backend as backend_tests


@pytest.fixture()
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return backend_tests.dispatch_repo.__wrapped__(tmp_path, monkeypatch)


def _config(
    repo: Path, monkeypatch: pytest.MonkeyPatch, *, fair_share: object = None
) -> None:
    lane = repo.parent / "lane.json"
    admission = {
        "worker_slots": 4,
        "observed_seconds": 900,
        "sessions": {"session": {"worker_slots": 0, "live_runs": 1}},
        "verdict": "full",
        "headroom": 4,
    }
    if fair_share is not None:
        admission["fair_share"] = fair_share
    lane.write_text(
        json.dumps({"headroom": 4, "admission": admission}), encoding="utf-8"
    )
    config = copy.deepcopy(backend_tests.CONFIG)
    config["local_backend"] = "clive"
    config["backends"]["clive"]["lane_document"] = str(lane)
    config["backends"]["beta"]["lane_document"] = str(lane)
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )


def _dispatch(repo: Path, node: str):
    return CliRunner().invoke(
        cli.main,
        [
            *backend_tests._arguments(repo, node=node, dry_run=False),
            "--local",
            "--no-watch",
        ],
    )


@pytest.mark.parametrize(
    ("fair_share", "expected"),
    [(1, 2), (3, 3), (12, 8), (None, 2), ("unknown", 2)],
)
def test_cap_follows_the_published_share(fair_share: object, expected: int) -> None:
    assert runs.queued_dispatch_cap(fair_share) == expected


def test_queued_dispatch_exits_zero_and_names_its_pointer(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(dispatch_repo, monkeypatch)
    result = _dispatch(dispatch_repo, "accepted-node")
    payload = backend_tests._payload(result)

    assert result.exit_code == 0
    assert payload["ok"] is True
    assert payload["queued"] is True
    assert payload["position"] == 1
    queued = runs.list_live(project="proj")
    assert len(queued) == 1
    assert queued[0]["run_id"] == payload["run_id"]
    assert queued[0]["phase"] == "queued"


def test_a_paused_gate_still_refuses_and_writes_no_pointer(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(dispatch_repo, monkeypatch)
    paused = CliRunner().invoke(
        cli.main, ["crew", "gate", "--pause", "allocation moving"]
    )
    assert paused.exit_code == 0

    result = _dispatch(dispatch_repo, "paused-node")
    payload = backend_tests._payload(result)

    assert result.exit_code == 75
    assert payload["ok"] is False
    assert payload["error"] == "lane-paused"
    assert payload.get("run_id") in (None, "")
    assert runs.list_live(project="proj") == []


def test_a_session_at_its_cap_is_refused_queue_full(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(dispatch_repo, monkeypatch, fair_share=2)
    assert _dispatch(dispatch_repo, "node-one").exit_code == 0
    assert _dispatch(dispatch_repo, "node-two").exit_code == 0

    result = _dispatch(dispatch_repo, "node-three")
    payload = backend_tests._payload(result)

    assert result.exit_code == 75
    assert payload["ok"] is False
    assert payload["error"] == "queue-full"
    assert payload["queued"] == 2
    assert payload["cap"] == 2
    assert len(runs.list_live(project="proj")) == 2


def test_a_repeat_dispatch_at_the_cap_updates_the_pointer(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(dispatch_repo, monkeypatch, fair_share=2)
    first = _dispatch(dispatch_repo, "node-one")
    assert first.exit_code == 0
    assert _dispatch(dispatch_repo, "node-two").exit_code == 0

    repeat = _dispatch(dispatch_repo, "node-one")
    payload = backend_tests._payload(repeat)

    assert repeat.exit_code == 0
    assert payload["queued"] is True
    assert payload["run_id"] == backend_tests._payload(first)["run_id"]
    assert len(runs.list_live(project="proj")) == 2