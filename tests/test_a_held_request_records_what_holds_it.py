"""A held request records what actually holds it, and says so to a person.

A request sat first in a queue for twenty hours carrying one stored reason:
"router grants 0 extra workers ... router's own verdict is full". The lane
published 46 worker slots with one running. What actually held it was a scope
conflict with a run of another session that had completed, been reviewed, and
never been promoted — and that session had been idle for nineteen hours. Nobody
was told, because the stored reason was written once and never revisited.

These cases pin the four facts that fix it: a launch refused on a scope conflict
records the refusal on the request, naming the holder's run, session and phase,
and a later pass with the same holder leaves that record exactly as it stands; a
request whose stored reason said the router was full records the conflict the
moment a pass finds one; the pass's record raises one obligation on the holder's
session; and a holder session idle past the bound raises it on the waiting
request's session too, so a session that is never coming back cannot fence paths
silently.

The fleet is the queue fixture's own: a real checkout, a real project mount and
real queued pointers, so what these cases read is what an admission pass writes.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckon.crew import obligations as obligations_module
from reckon.crew import queue_admission, runs
from reckon.crew.node import ScopeConflict
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures

PROJECT = fixtures.PROJECT
chain_repo = fixtures.chain_repo
WAITING_SESSION = fixtures.SESSION
HOLDER_SESSION = "coordinator-b"
RUN_ID = "r-waiting"
HOLDER_RUN_ID = "r-holder"
HELD_PATH = "s1.txt"
#: Six hours and one minute: past the bound at which a holder's session is
#: plainly not coming back on its own.
IDLE_SECONDS = 6 * 3600 + 60


def _lane_document(tmp_path: Path, *, worker_slots: float) -> Path:
    lane = tmp_path / "lane.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": 4,
                "admission": {
                    "worker_slots": worker_slots,
                    "observed_seconds": 900,
                    "sessions": {
                        WAITING_SESSION: {"worker_slots": worker_slots},
                        HOLDER_SESSION: {"worker_slots": worker_slots},
                    },
                    "verdict": "full",
                },
            }
        ),
        encoding="utf-8",
    )
    return lane


def _config_for(tmp_path: Path, *, worker_slots: float):
    config = json.loads(json.dumps(fixtures.CONFIG))
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=worker_slots)
    )

    def resolve(_pointer):
        return config

    return resolve


def _queue_waiting(repository: Path) -> dict:
    """One queued request on the fixture's dependency-free section."""
    stored = queue_admission.queue_requests(
        [fixtures._node("n1", "s1")],
        project=PROJECT,
        session=WAITING_SESSION,
        repo_root=repository,
        config=fixtures.CONFIG,
        run_ids={"n1": RUN_ID},
    )
    assert stored[RUN_ID]["phase"] == queue_admission.QUEUED_PHASE, stored[RUN_ID]
    return stored[RUN_ID]


def _write_holder(
    repository: Path,
    *,
    run_id: str = HOLDER_RUN_ID,
    phase: str = "complete",
    status: str = "complete",
    write_paths: list[str] | None = None,
    idle_seconds: int = 0,
) -> dict:
    """One live pointer for a run that has ended, holding the queued path.

    Its manifest carries the status it ended with, and the pointer's own file
    time dates its last activity: the fixture ages both so a case can say how
    long the holding session has been unattended.
    """
    home = runs.crew_home()
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: holder\nstatus: {status}\n", encoding="utf-8")
    stamp = datetime.now(UTC) - timedelta(seconds=idle_seconds)
    os.utime(manifest, (stamp.timestamp(), stamp.timestamp()))
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": HOLDER_SESSION,
        "repo": str(repository),
        "node": {
            "id": "holder",
            "plan": fixtures.PLAN,
            "write_paths": write_paths or [],
            "time_budget": "20m",
        },
        "phase": phase,
        "created_at": (stamp - timedelta(days=1)).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(home / "absent-stream.jsonl"),
    }
    runs._write_json(runs.pointer_path(run_id), record)
    if idle_seconds:
        os.utime(
            runs.pointer_path(run_id),
            (stamp.timestamp(), stamp.timestamp()),
        )
    return record


def _refusing_launcher(*, run_id: str = HOLDER_RUN_ID):
    """A launcher whose launch the repository's own claim check refuses."""

    def launch(plan, *, log_path, stderr_path, prompt_path):
        raise ScopeConflict(
            run_id=run_id,
            node_id="holder",
            candidate_path=HELD_PATH,
            claimed_path=HELD_PATH,
        )

    return launch


def _admit(config_for, *, launcher=None) -> dict:
    return queue_admission.admit_once(config_for=config_for, launcher=launcher)


def _hold(run_id: str) -> dict:
    return queue_admission.recorded_pass_hold(runs.read_pointer(run_id))


def _duties(payload: dict) -> list[dict]:
    return [
        row
        for row in payload.get("obligations") or ()
        if row.get("kind") == obligations_module.HELD_BY_FINISHED_RUN_DUTY_KIND
    ]


def test_a_launch_refused_on_a_scope_conflict_records_the_holder(
    chain_repo, tmp_path: Path
) -> None:
    """The refusal that actually holds a request is written on the request.

    A request refused at launch carries the holder's run, its session and its
    phase, because those three are what a coordinator needs to settle it and the
    refusal's own text carries only the run and the node. The second pass, which
    finds the same holder still live, leaves that record exactly as it stands
    rather than replacing it with its retry back-off.
    """
    _queue_waiting(chain_repo)
    _write_holder(chain_repo)
    config_for = _config_for(tmp_path, worker_slots=4)

    _admit(config_for, launcher=_refusing_launcher())

    recorded = _hold(RUN_ID)
    assert recorded["verdict"] == "refused-scope-conflict", recorded
    assert recorded["holder"]["run_id"] == HOLDER_RUN_ID, recorded
    assert recorded["holder"]["session"] == HOLDER_SESSION, recorded
    assert recorded["holder"]["phase"] == "complete", recorded
    assert HELD_PATH in recorded["reason"], recorded
    assert HOLDER_RUN_ID in recorded["reason"], recorded
    # The pointer's own reason is the pass's reading, not a stale one.
    assert runs.read_pointer(RUN_ID)["reason"] == recorded["reason"]

    # The pass after it is held by the back-off the refusal recorded, and the
    # holder has not moved: the reading stands unchanged, instant included.
    _admit(config_for, launcher=_refusing_launcher())

    assert _hold(RUN_ID) == recorded


def test_a_scope_conflict_replaces_a_stored_router_full_reason(
    chain_repo, tmp_path: Path
) -> None:
    """The reason a request carries is the one that holds it now, not the first.

    The specimen was a request whose reason still said the router was full
    months after a live run's claim had started blocking it. A pass that finds
    the claim replaces the stored reason, so the wait reads for what it is.
    """
    _queue_waiting(chain_repo)
    pointer = dict(runs.read_pointer(RUN_ID))
    router_full = (
        "the lane's router grants 0 extra workers to session "
        f"{WAITING_SESSION!r}; the router's own verdict is full"
    )
    pointer["reason"] = router_full
    pointer["pass_hold"] = {"verdict": "held-for-allowance", "reason": router_full}
    runs._write_json(runs.pointer_path(RUN_ID), pointer)

    _write_holder(chain_repo, write_paths=[HELD_PATH])

    _admit(_config_for(tmp_path, worker_slots=4))

    recorded = _hold(RUN_ID)
    assert recorded["verdict"] == "held-on-claim", recorded
    assert recorded["reason"] != router_full
    assert router_full not in runs.read_pointer(RUN_ID)["reason"]
    assert recorded["holder"]["run_id"] == HOLDER_RUN_ID, recorded


def test_a_finished_holder_owes_its_session_one_obligation(
    chain_repo, tmp_path: Path
) -> None:
    """A request held by a complete run is the holder session's duty, with its name.

    Nothing a timer does will launch the waiting request: the claim releases
    when the holder is settled, and only the holder's session can settle it. So
    the duty is raised there, naming the request the holder keeps waiting, and
    one request raises exactly one duty.
    """
    _queue_waiting(chain_repo)
    _write_holder(chain_repo)
    config_for = _config_for(tmp_path, worker_slots=4)
    _admit(config_for, launcher=_refusing_launcher())

    holder_view = obligations_module.obligations(PROJECT, HOLDER_SESSION)
    duties = _duties(holder_view)

    assert len(duties) == 1, duties
    duty = duties[0]
    assert duty["run_id"] == RUN_ID, duty
    assert duty["holder_run_id"] == HOLDER_RUN_ID, duty
    assert duty["holder_session"] == HOLDER_SESSION, duty
    assert duty["holder_phase"] == "complete", duty
    assert duty["held_path"] == HELD_PATH, duty
    assert "complete" in duty["reason"], duty
    assert RUN_ID not in duty["next_command"], duty


def test_an_idle_holder_reaches_the_waiting_requests_session(
    chain_repo, tmp_path: Path
) -> None:
    """A holder whose session has gone quiet is also the waiting session's duty.

    A session idle past the bound is not coming back on its own, so the request
    it fences would wait for ever. The waiting request's session is told as
    well, and it can settle the holder or ask the lead. Below the bound nothing
    is raised there: the holder's session is still the one that should act.
    """
    _queue_waiting(chain_repo)
    _write_holder(chain_repo, idle_seconds=IDLE_SECONDS)
    config_for = _config_for(tmp_path, worker_slots=4)
    _admit(config_for, launcher=_refusing_launcher())

    waiting_view = obligations_module.obligations(PROJECT, WAITING_SESSION)
    duties = _duties(waiting_view)

    assert len(duties) == 1, duties
    assert duties[0]["run_id"] == RUN_ID, duties[0]
    assert duties[0]["holder_session"] == HOLDER_SESSION, duties[0]

    # The holder's own session still owes it: the escalation adds a reader, it
    # does not move the duty.
    holder_duties = _duties(obligations_module.obligations(PROJECT, HOLDER_SESSION))
    assert len(holder_duties) == 1, holder_duties


def test_an_attended_holder_keeps_the_duty_on_its_own_session(
    chain_repo, tmp_path: Path
) -> None:
    """Below the idle bound the waiting session is told nothing yet.

    The negative arm of the escalation: the holder's session was seen this hour,
    so it is the one that should settle the holder, and raising the duty on the
    waiting session as well would send a second coordinator at work the first is
    about to do.
    """
    _queue_waiting(chain_repo)
    _write_holder(chain_repo)
    config_for = _config_for(tmp_path, worker_slots=4)
    _admit(config_for, launcher=_refusing_launcher())

    assert _duties(obligations_module.obligations(PROJECT, WAITING_SESSION)) == []
    assert len(_duties(obligations_module.obligations(PROJECT, HOLDER_SESSION))) == 1
