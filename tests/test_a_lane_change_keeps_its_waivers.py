"""Moving a run to another lane carries the waivers its dispatch had answered.

A dispatch may answer a gate ahead of the run: it waives a missing plan review
(``--allow-unreviewed-plan``) or accepts a directory claim. When that dispatch
is held at the lane it records those answers on the queued request, and when
the request is later moved to another backend they must ride the new launch —
otherwise the run is refused by a gate the original dispatch had already
answered. The move carries the recorded options unchanged; it never adds a
waiver the original request did not carry.
"""

from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew_dispatch_commands, crew_run_commands
from reckon.crew import runs
from tests import test_dispatch_names_its_backend as backend_tests


@pytest.fixture()
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return backend_tests.dispatch_repo.__wrapped__(tmp_path, monkeypatch)


def _config(repo: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    held_lane = repo.parent / "lane-held.json"
    held_lane.write_text(
        json.dumps(
            {
                "headroom": 4,
                "admission": {
                    "worker_slots": 4,
                    "observed_seconds": 900,
                    "sessions": {"session": {"worker_slots": 0, "live_runs": 1}},
                    "verdict": "full",
                    "headroom": 4,
                },
            }
        ),
        encoding="utf-8",
    )
    serving_lane = repo.parent / "lane-serving.json"
    serving_lane.write_text(
        json.dumps(
            {
                "headroom": 4,
                "admission": {
                    "worker_slots": 4,
                    "observed_seconds": 900,
                    "sessions": {"session": {"worker_slots": 4, "live_runs": 0}},
                    "verdict": "ok",
                    "headroom": 4,
                },
            }
        ),
        encoding="utf-8",
    )
    config = deepcopy(backend_tests.CONFIG)
    config["local_backend"] = "clive"
    # Enforced so a build of an uncovered plan is refused rather than warned.
    config["plan_review_gate"] = "enforce"
    config["backends"]["clive"]["lane_document"] = str(held_lane)
    config["backends"]["beta"]["lane_document"] = str(serving_lane)
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config
    )
    monkeypatch.setattr(crew_run_commands, "_resolved_flight", lambda *_a, **_k: config)
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    return config


def test_a_lane_change_keeps_the_waivers_its_dispatch_recorded(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(dispatch_repo, monkeypatch)

    # A build of an uncovered plan on the held local lane: the waiver lets it
    # past the plan-review gate, the held lane queues it.
    first = CliRunner().invoke(
        cli.main,
        [
            *backend_tests._arguments(dispatch_repo, node="waiver-node", dry_run=False),
            "--local",
            "--no-watch",
            "--allow-unreviewed-plan",
        ],
    )
    first_payload = backend_tests._payload(first)
    assert first.exit_code == 0, first.output
    assert first_payload["queued"] is True
    queued_id = first_payload["run_id"]

    queued = runs.read_pointer(queued_id)
    assert queued["phase"] == "queued"
    assert queued["dispatch_options"]["allow_unreviewed_plan"] is True

    # Moved to a serving backend, the request is launched — not refused by the
    # plan-review gate it already answered — and the waiver is recorded as
    # carried.
    moved = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "resume",
            "--run",
            queued_id,
            "--backend",
            "beta",
            "--reason",
            "another lane has room now",
        ],
    )
    assert moved.exit_code == 0, moved.output
    moved_payload = backend_tests._payload(moved)
    # A queued request has never launched, so the move re-composes it under the
    # id the coordinator recorded rather than minting a second one.
    assert moved_payload["run_id"] == queued_id

    launched = runs.read_pointer(queued_id)
    assert launched["backend"] == "beta"
    assert launched["phase"] != "queued"
    assert launched["unreviewed_plan_override"] == {"requested": True, "plan": "plan-a"}
    assert launched["carried_options"]["allow_unreviewed_plan"] is True
    # The move is a continuation, not a withdrawal: the launched run names the
    # queued request it was admitted from and is the live row for that id.
    assert launched["admitted_from"] == queued_id
    assert queued_id in [row["run_id"] for row in runs.list_live(project="proj")]


def test_a_node_dispatched_without_the_waiver_is_still_refused_on_redispatch(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(dispatch_repo, monkeypatch)
    joined = "2026-10-13T00:00:00+00:00"
    runs.queue_dispatch(
        {
            "run_id": "queued-without-waiver",
            "project": "proj",
            "plan": "plan-a",
            "section": "dispatch",
            "repo": str(dispatch_repo),
            "session": "session",
            "node": {
                "id": "no-waiver-node",
                "goal": "record one resolved backend",
                "plan": "plan-a",
                "section": "dispatch",
                "role": "implement",
                "spec_level": "exact",
                "done_when": "pytest reports nine backend-routing cases passed",
                "write_paths": ["result.json"],
            },
            "backend": "clive",
            "launch": "in-harness",
            "local": True,
            "phase": "queued",
            "dispatch_options": {"allow_unreviewed_plan": False},
            "queued_at": joined,
            "created_at": joined,
        }
    )

    moved = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "resume",
            "--run",
            "queued-without-waiver",
            "--backend",
            "beta",
            "--reason",
            "another lane has room now",
        ],
    )
    assert moved.exit_code != 0
    assert "review" in moved.output.lower()
    # The request the move could not launch is left exactly where it was.
    assert runs.read_pointer("queued-without-waiver")["phase"] == "queued"
    assert (
        subprocess.run(
            ["git", "-C", str(dispatch_repo), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.count("worktree ")
        == 1
    )
