"""A context-fit refusal at admission is routed to a lane that fits, or reported.

Admission refuses a queued request whose context estimate exceeds its lane's
window. Judged against the lane alone, that refusal is a fact about the lane
that was asked and not about the request: another lane may carry the window for
it. The pass therefore asks the picker for its ranked candidates and moves the
request to the first lane whose competence verdict passes, or, when no lane
fits, holds it with one obligation naming the shortfall in tokens. The same two
refusals an edit could cause — a window no lane carries, a claim a live run
holds — are raised by ``crew queue edit`` while the coordinator is still there
to read them, and an accepted edit re-records the request's review contract.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import queue_admission, queue_edit, runs
from reckon.crew.node import CompetenceLimit, ScopeConflict
from reckon.crew.picker import snapshot as snapshot_module
from reckon.crew.picker.types import Candidate

PROJECT = "routed-fixture"
SESSION = "coordinator-routed"
LOCAL = "narrow"
WIDER = "wider"


def _git(root: Path, *arguments: str) -> None:
    import subprocess

    subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
    )


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "state" / PROJECT / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plan = root / "docs" / "plans" / "plan-a.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        "\n".join(
            [
                "<!doctype html>",
                '<meta name="docs-project" content="' + PROJECT + '">',
                '<meta name="reckon-type" content="plan">',
                '<meta name="plan-slug" content="plan-a">',
                '<h2 id="dispatch">Dispatch</h2>',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(root, "add", "--", "docs", "seed.txt")
    _git(root, "commit", "-q", "-m", "chore: seed fixture")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _config(window: int = 262_144) -> dict[str, Any]:
    return {
        "default_backend": LOCAL,
        "local_backend": LOCAL,
        "backends": {
            LOCAL: {
                "launch": "cli",
                "command": "codex",
                "model": "local-model",
                "effort": "medium",
                "sandbox": "worktree-full",
                "time_budget": "25m",
                "usable_input_window": window,
            },
            WIDER: {
                "launch": "cli",
                "command": "codex",
                "model": "wider-model",
                "effort": "medium",
                "sandbox": "worktree-full",
                "time_budget": "25m",
                "usable_input_window": window * 4,
            },
        },
        "roles": {"implement": {}},
        "fences": {"time_budget": "25m", "needs_after_failures": 2},
    }


def _config_for(config: dict[str, Any]):
    return lambda _pointer: config


def _queue(
    repository: Path,
    *,
    run_id: str = "r-request",
    backend: str = LOCAL,
    write_paths: list[str] | None = None,
) -> str:
    from reckon.crew.node import TaskNode

    node = TaskNode(
        id=run_id,
        goal="admit this queued node",
        plan="plan-a",
        section="dispatch",
        done_when="pytest reports passed",
        write_paths=write_paths or [f"{run_id}.txt"],
        time_budget="20m",
        spec_level="exact",
        manifest_path=str(repository.parent / f"{run_id}-manifest.md"),
    )
    joined = datetime.now(UTC).isoformat()
    runs.queue_dispatch(
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(repository),
            "node": node.as_dict(),
            "backend": backend,
            "launch": "cli",
            "local": backend == LOCAL,
            "phase": "queued",
            "queued_at": joined,
            "created_at": joined,
            "hold": {"held": True},
            "dispatch_options": {
                "project": PROJECT,
                "plan_slug": "plan-a",
                "section": "dispatch",
                "session": SESSION,
                "no_watch": True,
                "overrides": [],
            },
        }
    )
    return run_id


def _candidate(backend: str, *, reasons: list[str], window: int) -> Candidate:
    return Candidate(
        backend=backend,
        family=backend,
        model=f"{backend}-model",
        effort="medium",
        local=backend == LOCAL,
        availability="available" if not reasons else "not-probed",
        utilisation_pct=0.0,
        burn_multiple=None,
        pace_allowance=None,
        resets_at=None,
        worker_slots=4,
        congestion=None,
        outcomes={},
        reasons=list(reasons),
        context={"window_tokens": window},
    )


def _refusals(monkeypatch: pytest.MonkeyPatch, rows: list[Candidate]) -> None:
    monkeypatch.setattr(
        snapshot_module, "candidates", lambda *args, **kwargs: list(rows)
    )


def _refusing_launcher(monkeypatch: pytest.MonkeyPatch, *, window: int = 262_144):
    """A launch path that refuses the queued lane and launches any other."""

    def launch(pointer, *, backend_override=None, **kwargs):
        destination = str(backend_override or pointer.get("backend") or "")
        if destination == LOCAL:
            raise CompetenceLimit(
                {
                    "allowed": False,
                    "reason": "context-window-exceeded",
                    "estimated_tokens": window + 637_856,
                    "window_tokens": window,
                    "shortfall_tokens": 637_856,
                }
            )
        record = dict(pointer)
        record.update({"phase": "running", "backend": destination, "pid": os.getpid()})
        runs._write_json(runs.pointer_path(str(record["run_id"])), record)
        return record

    monkeypatch.setattr(queue_admission, "_launch_queued", launch)


def test_a_request_refused_by_its_lane_is_moved_to_one_that_fits(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _queue(repository)
    config = _config()
    _refusing_launcher(monkeypatch)
    _refusals(
        monkeypatch,
        [
            _candidate(LOCAL, reasons=["context-fit: exceeds the window"], window=10),
            _candidate(WIDER, reasons=[], window=90_000),
        ],
    )

    result = queue_admission.admit_once(config_for=_config_for(config))

    assert result["routed"] == ["r-request"]
    assert result["obligations"] == []
    moved = runs.read_pointer("r-request")
    assert moved["backend"] == WIDER
    assert moved["phase"] == "running"
    assert moved["lane_move"]["to_backend"] == WIDER


def test_a_request_no_lane_fits_is_held_naming_the_shortfall(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _queue(repository)
    config = _config()
    _refusing_launcher(monkeypatch)
    _refusals(
        monkeypatch,
        [
            _candidate(LOCAL, reasons=["context-fit: exceeds the window"], window=10),
            _candidate(WIDER, reasons=["context-fit: exceeds the window"], window=20),
        ],
    )

    result = queue_admission.admit_once(config_for=_config_for(config))

    assert result["held"] == ["r-request"]
    assert result["routed"] == []
    assert len(result["obligations"]) == 1
    obligation = result["obligations"][0]
    assert obligation["run_id"] == "r-request"
    assert "shortfall of 637856 tokens" in obligation["detail"]
    verdict = result["verdicts"][0]
    assert verdict["verdict"] == "held-until-changed"
    # The stored reason is the refusal itself, not the lane's last allowance.
    held = runs.read_pointer("r-request")
    assert held["phase"] == "queued"
    assert "CompetenceLimit" in held["failure"]
    assert "context estimate" in held["failure"]


def test_a_request_routed_to_a_lane_this_service_cannot_launch_is_queued_there(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _queue(repository)
    config = _config()
    _refusing_launcher(monkeypatch)
    _refusals(
        monkeypatch,
        [_candidate(WIDER, reasons=[], window=90_000)],
    )

    result = queue_admission.admit_once(config_for=_config_for(config), lane=LOCAL)

    assert result["moved"] == ["r-request"]
    assert result["routed"] == []
    assert "wider" in result["obligations"][0]["detail"]
    queued = runs.read_pointer("r-request")
    assert queued["phase"] == "queued"
    assert queued["backend"] == WIDER


def _edit_config(monkeypatch: pytest.MonkeyPatch, window: int) -> dict[str, Any]:
    config = _config(window=window)
    monkeypatch.setattr(
        queue_admission, "_resolve_lane_config", lambda pointer, **kwargs: config
    )
    return config


def test_an_edit_past_every_lanes_window_is_refused_naming_the_shortfall(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _queue(repository)
    _edit_config(monkeypatch, window=5_000)
    # An estimate that grows with the request's declared scope, so the same
    # measurement accepts a small edit and refuses a large one.
    monkeypatch.setattr(
        snapshot_module,
        "estimated_context_tokens",
        lambda node, repo, **kwargs: 1_000 * len(node.write_paths),
    )

    accepted = queue_edit.edit("r-request", write_paths=["one.txt", "two.txt"])
    assert accepted["write_paths"] == ["one.txt", "two.txt"]

    before = runs.read_pointer("r-request")
    with pytest.raises(CompetenceLimit) as refused:
        queue_edit.edit(
            "r-request", write_paths=[f"grown-{index}.txt" for index in range(30)]
        )

    assert "exceeds the 20000 token window by 10000 tokens" in str(refused.value)
    after = runs.read_pointer("r-request")
    assert after["node"]["write_paths"] == before["node"]["write_paths"]
    assert after["edited_at"] == before["edited_at"]


def test_an_edit_onto_a_path_a_live_claim_holds_is_refused_naming_the_holder(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _queue(repository)
    _edit_config(monkeypatch, window=10_000_000)
    runs._write_json(
        runs.pointer_path("r-live"),
        {
            "run_id": "r-live",
            "project": PROJECT,
            "session": "other-session",
            "repo": str(repository),
            "phase": "running",
            "node": {
                "id": "live-node",
                "plan": "plan-a",
                "section": "dispatch",
                "write_paths": ["shared/file.txt"],
            },
        },
    )

    before = runs.read_pointer("r-request")
    with pytest.raises(ScopeConflict) as refused:
        queue_edit.edit("r-request", write_paths=["shared/file.txt"])

    assert refused.value.run_id == "r-live"
    assert "r-live" in str(refused.value)
    assert (
        runs.read_pointer("r-request")["node"]["write_paths"]
        == before["node"]["write_paths"]
    )


def test_an_accepted_edit_re_records_the_review_contract(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _queue(repository)
    _edit_config(monkeypatch, window=10_000_000)
    recorded: list[str] = []

    def record(pointer):
        recorded.append(str(pointer.get("run_id") or ""))
        return True

    monkeypatch.setattr(queue_admission, "record_review_contract", record)

    result = queue_edit.edit("r-request", done_when="pytest reports passed twice")

    assert result["review_contract_recorded"] is True
    assert recorded == ["r-request"]
