"""A request on any lane queues at a placement bound and takes the reserve.

The local lane keeps its reserve only while a request on another lane waits.

A bound on the shared placement reservation is a wait for room: the workers
inside the allocation carry on, and one of them ending frees what a held
dispatch needs. The local lane queued at that bound, but a lane a coordinator
named — codex as the fast escape hatch is the case this exists for — was
refused, so the request was lost after its run id had been printed and could
only be retried by hand. Every lane's dispatch path now stores the request as
one queued pointer, and the admission pass launches it when the lane has room.

The reserve the local lane keeps is held back for exactly those requests: with
none waiting the local lane fills every seat, because a standing reserve leaves
local work idle for room nothing asked for.

Every bound here is read through the production guard with a published
reservation and placed occupant.

A dispatch held at the bound is queued as a plain (non-armed) request.

"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew_dispatch_commands
from reckon.crew import dispatch_admission, placement, queue_admission, runs
from reckon.crew.dispatch_admission import RosterFull
from reckon.crew.dispatching import preparation
from tests import test_dispatch_names_its_backend as backend_tests

PROJECT = "proj"
SESSION = "session"
LOCAL = "clive"
NAMED = "codex"

# The roster cap the reservation is read against, and the seats the local lane
# leaves free while a request on another lane waits. Both are production
# constants; the cap is patched down to a size a test can occupy.
LIMIT = 8
RESERVE = placement.LOCAL_LANE_RESERVE

PLACEMENT = {
    "scheduler": "srun",
    "options": ["--partition=all"],
    "state_query": ["squeue", "-h", "-j", "{job}", "-o", "%T"],
    "reason_query": ["squeue", "-h", "-j", "{job}", "-o", "%r"],
}
QUEUED_AT = "2026-11-15T00:00:00+00:00"


@pytest.fixture()
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return backend_tests.dispatch_repo.__wrapped__(tmp_path, monkeypatch)


def _config() -> dict:
    """Both lanes placed inside the one shared reservation."""
    config = deepcopy(backend_tests.CONFIG)
    config["default_backend"] = LOCAL
    config["local_backend"] = LOCAL
    for name in (LOCAL, NAMED):
        config["backends"][name] = {
            "launch": "cli",
            "command": "codex",
            "model": f"{name}-model",
            "effort": "high",
            "sandbox": "cli-only",
            "time_budget": "25m",
            "placement": dict(PLACEMENT),
        }
    return config


def _reserve_the_roster(
    repository: Path, monkeypatch: pytest.MonkeyPatch, *, occupants: int
) -> list[str]:
    """Publish the reservation and place ``occupants`` live workers in it."""
    monkeypatch.setattr(placement, "RESERVATION_ROSTER_LIMIT", LIMIT)
    placement.publish_reservation({"job_id": "1274051"})
    seated = []
    for index in range(occupants):
        run_id = f"r-occupant-{index}"
        _pointer(
            run_id,
            repository,
            backend=LOCAL,
            local=True,
            phase="working",
            pid=os.getpid(),
            placement_record=dict(PLACEMENT),
        )
        seated.append(run_id)
    return seated


def _pointer(
    run_id: str,
    repository: Path,
    *,
    backend: str,
    local: bool,
    phase: str,
    node_id: str = "",
    pid: int | None = None,
    placement_record: dict | None = None,
) -> dict:
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": "another-session",
        "repo": str(repository),
        "node": {
            "id": node_id or run_id,
            "goal": "hold one request at the placement bound",
            "plan": "plan-a",
            "section": "dispatch",
            "done_when": "pytest reports the request passed",
            "write_paths": [f"{run_id}.json"],
            "time_budget": "20m",
            "spec_level": "exact",
            "manifest_path": str(runs.crew_home() / "manifests" / f"{run_id}.md"),
        },
        "backend": backend,
        "launch": "in-harness",
        "local": local,
        "phase": phase,
        "queued_at": QUEUED_AT,
        "created_at": QUEUED_AT,
        "hold": {"held": True},
        "dispatch_options": {
            "project": PROJECT,
            "plan_slug": "plan-a",
            "section": "dispatch",
            "session": SESSION,
            "local": local,
            "no_watch": True,
            "overrides": [],
        },
    }
    if pid is not None:
        record["pid"] = pid
    if placement_record is not None:
        record["placement"] = placement_record
    runs._write_json(runs.pointer_path(run_id), record)
    return record


def _invoke(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    node: str,
    extra: list[str] | None = None,
    config: dict,
) -> tuple[object, dict]:
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    result = CliRunner().invoke(
        cli.main,
        [
            *backend_tests._arguments(repository, node=node, dry_run=False),
            "--no-watch",
            *(extra or ()),
        ],
    )
    return result, json.loads(result.output.splitlines()[0])


def test_a_codex_dispatch_at_the_roster_cap_is_queued_not_refused(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lane a coordinator named waits for a seat instead of being lost."""
    _reserve_the_roster(dispatch_repo, monkeypatch, occupants=LIMIT)
    config = _config()

    result, payload = _invoke(
        dispatch_repo,
        monkeypatch,
        node="codex-at-cap",
        extra=["--backend", NAMED],
        config=config,
    )

    assert result.exit_code == 0, result.output
    assert payload["ok"] is True and payload["queued"] is True, payload
    assert "waits for room" in (payload["reason"] or ""), payload
    pointer = runs.read_pointer(payload["run_id"])
    assert pointer["phase"] == "queued", pointer
    assert pointer["backend"] == NAMED, pointer


def test_a_bound_met_at_the_later_check_queues_with_no_created_run_printed(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The later read is after gates that take seconds, and before any claim.

    A seat can be taken between the two reads, so the bound is read again. That
    second read used to sit after the run id had been printed and its pointer
    published, and the refusal took the pointer back — leaving a printed id
    that named nothing.
    """
    reads: list[dict] = []

    def taken_between_the_reads(*_args, **kwargs):
        reads.append(dict(kwargs))
        if len(reads) < 2:
            return
        raise RosterFull(
            "the placement reservation is full (8/8)",
            occupancy=LIMIT,
            limit=LIMIT,
            occupying=["r-occupant-0"],
        )

    monkeypatch.setattr(
        preparation, "_refuse_over_concurrency_ceiling", taken_between_the_reads
    )

    result, payload = _invoke(
        dispatch_repo,
        monkeypatch,
        node="bound-later",
        extra=["--local"],
        config=_config(),
    )

    assert len(reads) == 2, "the bound must be read twice on one dispatch"
    assert result.exit_code == 0, result.output
    assert payload["queued"] is True, payload
    assert "created run" not in (result.stderr or ""), result.stderr
    pointer = runs.read_pointer(payload["run_id"])
    assert pointer["phase"] == "queued", pointer
    assert pointer["backend"] == LOCAL, pointer
    assert "waits for room" in (pointer["hold"]["reason"] or ""), pointer["hold"]


def test_the_local_lane_fills_the_cap_with_no_named_request_waiting(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A standing reserve leaves local work idle for room nothing asked for."""
    _reserve_the_roster(dispatch_repo, monkeypatch, occupants=LIMIT - 1)
    config = _config()
    backend = config["backends"][LOCAL]

    assert preparation._local_lane_reserve(True, config=config, project=PROJECT) == 0
    # The cap is one seat away and nothing is waiting for it: the local lane
    # takes it.
    dispatch_admission._refuse_over_concurrency_ceiling(
        LOCAL, backend, PROJECT, reserve=0
    )


def test_the_local_lane_stops_at_the_reserve_when_a_named_request_waits(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seats held back go to the request another lane named."""
    _reserve_the_roster(dispatch_repo, monkeypatch, occupants=LIMIT - RESERVE)
    config = _config()
    backend = config["backends"][LOCAL]
    _pointer(
        "r-codex-waiting", dispatch_repo, backend=NAMED, local=False, phase="queued"
    )

    reserve = preparation._local_lane_reserve(True, config=config, project=PROJECT)

    assert reserve == RESERVE
    with pytest.raises(RosterFull):
        dispatch_admission._refuse_over_concurrency_ceiling(
            LOCAL, backend, PROJECT, reserve=reserve
        )

    # One seat frees: the local lane is admitted again, below the reserve.
    runs.pointer_path(f"r-occupant-{LIMIT - RESERVE - 1}").unlink()
    dispatch_admission._refuse_over_concurrency_ceiling(
        LOCAL, backend, PROJECT, reserve=reserve
    )
    # A dispatch naming another lane is measured against the cap itself.
    dispatch_admission._refuse_over_concurrency_ceiling(
        NAMED, config["backends"][NAMED], PROJECT, reserve=0
    )


def test_a_local_dispatch_rerouted_on_context_fit_is_queued_naming_the_shortfall(
    dispatch_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request that left its first lane on size says so when it queues.

    The bound it meets belongs to the lane it moved to, so an answer that named
    only a full reservation would read as a node too large for the lane that was
    asked for — the opposite remedy.
    """
    _reserve_the_roster(dispatch_repo, monkeypatch, occupants=LIMIT)
    config = _config()
    config["backends"][LOCAL]["usable_input_window"] = 1
    monkeypatch.setattr(preparation, "_router_free_resolution", lambda **_kwargs: True)

    def rerouted(**kwargs):
        """Resolve onto the wider lane, as the router's own fit check does."""
        node = kwargs["node"]
        node.time_budget = kwargs["caller_time_budget"]
        node.write_paths = list(kwargs["caller_write_paths"])
        wider = preparation.plan_dispatch(
            node=node,
            config=kwargs["config"],
            project=kwargs["project"],
            repo=kwargs["repo"],
            authority=kwargs["authority"],
            session=kwargs["session"],
            local=False,
            backend_override=NAMED,
            route="deterministic",
            picker_selection=None,
            **dict(kwargs["reroute_kwargs"]),
        )
        return wider, {
            "from_backend": LOCAL,
            "to_backend": NAMED,
            "reason": "context-fit",
        }

    monkeypatch.setattr(preparation, "_reroute_oversized_request", rerouted)

    result, payload = _invoke(
        dispatch_repo, monkeypatch, node="context-fit", extra=["--local"], config=config
    )

    assert result.exit_code == 0, result.output
    assert payload["queued"] is True, payload
    reason = str(payload["reason"] or "")
    assert "context fit" in reason, reason
    assert LOCAL in reason, reason
    assert "exceeds by" in reason and "tokens" in reason, reason
    assert runs.read_pointer(payload["run_id"])["phase"] == "queued"


def test_a_named_lane_the_routed_document_holds_is_queued_naming_the_hold(
    dispatch_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane's own budget reading holds its requests, and they wait queued."""
    lane = tmp_path / "codex-lane.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": 4,
                "admission": {
                    "worker_slots": 0,
                    "observed_seconds": 900,
                    "sessions": {SESSION: {"worker_slots": 0}},
                    "verdict": "full",
                },
            }
        ),
        encoding="utf-8",
    )
    config = _config()
    config["backends"][NAMED]["lane_document"] = str(lane)

    result, payload = _invoke(
        dispatch_repo,
        monkeypatch,
        node="codex-held",
        extra=["--backend", NAMED],
        config=config,
    )

    assert result.exit_code == 0, result.output
    assert payload["queued"] is True, payload
    reason = str(payload["reason"] or "")
    assert "grants 0 extra workers" in reason, reason
    pointer = runs.read_pointer(payload["run_id"])
    assert pointer["phase"] == "queued", pointer
    assert pointer["backend"] == NAMED, pointer


def _launcher(recorded: list):
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(plan)
        return os.getpid()

    return launch


def _admit(config: dict, launcher) -> dict:
    return queue_admission.admit_once(
        config_for=lambda _pointer: config, launcher=launcher
    )


def test_a_full_roster_keeps_every_lane_queued_and_the_named_lane_takes_the_next_seat(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass at the cap loses nothing, and the freed seat goes to codex."""
    _reserve_the_roster(dispatch_repo, monkeypatch, occupants=LIMIT)
    config = _config()
    for run_id, backend, local in (
        ("r-codex", NAMED, False),
        ("r-local-a", LOCAL, True),
        ("r-local-b", LOCAL, True),
    ):
        _pointer(run_id, dispatch_repo, backend=backend, local=local, phase="queued")
    recorded: list = []
    launcher = _launcher(recorded)

    at_the_cap = _admit(config, launcher)

    assert at_the_cap["launched"] == [], at_the_cap
    assert recorded == [], "nothing may launch into a full roster"
    assert at_the_cap["failed"] == [], at_the_cap
    for run_id in ("r-codex", "r-local-a", "r-local-b"):
        assert run_id in at_the_cap["held"], run_id
        assert runs.read_pointer(run_id)["phase"] == "queued", run_id

    # One seat frees: the named request takes it, and the local queue stays
    # short of the seats the reserve keeps for it.
    runs.pointer_path(f"r-occupant-{LIMIT - 1}").unlink()
    next_pass = _admit(config, launcher)

    assert next_pass["launched"] == ["r-codex"], next_pass
    for run_id in ("r-local-a", "r-local-b"):
        assert runs.read_pointer(run_id)["phase"] == "queued", run_id


def test_a_queued_review_edited_to_codex_keeps_its_reviewed_run(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving a waiting review changes the lane, not the run it reviews."""
    _reserve_the_roster(dispatch_repo, monkeypatch, occupants=LIMIT)
    config = _config()
    _pointer(
        "r-delivered",
        dispatch_repo,
        backend=LOCAL,
        local=True,
        phase="working",
        node_id="delivered-node",
        pid=os.getpid(),
    )
    _pointer(
        "r-review",
        dispatch_repo,
        backend=LOCAL,
        local=True,
        phase="queued",
        node_id="review-of-r-delivered",
    )
    _pointer(
        "r-local-waiting",
        dispatch_repo,
        backend=LOCAL,
        local=True,
        phase="queued",
        node_id="local-waiting",
    )

    from reckon.crew import queue_edit, recovery_review_delivery

    # The edit resolves the lane's own flight config; this test states it.
    monkeypatch.setattr(queue_admission, "_resolve_lane_config", lambda _record: config)
    queue_edit.edit("r-review", backend=NAMED)
    moved = runs.read_pointer("r-review")
    assert moved["backend"] == NAMED, moved

    recorded: list = []
    # A full roster holds it: the moved review waits rather than being lost.
    at_the_cap = _admit(config, _launcher([]))
    assert at_the_cap["launched"] == [], at_the_cap
    assert at_the_cap["failed"] == [], at_the_cap
    assert runs.read_pointer("r-review")["phase"] == "queued"

    # One seat frees, and the named request takes it from the local queue.
    runs.pointer_path(f"r-occupant-{LIMIT - 1}").unlink()
    admitted = _admit(config, _launcher(recorded))

    assert admitted["launched"] == ["r-review"], admitted
    launched = runs.read_pointer("r-review")
    assert launched["backend"] == NAMED, launched
    assert (
        recovery_review_delivery._resolved_reviewed_run_id(launched, PROJECT)
        == "r-delivered"
    ), launched
