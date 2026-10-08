"""Dispatch counts workers against the router's worker slots, not headroom.

The local lane's router publishes an admission block whose figures are its own
arithmetic: a per-session fair share, a share offered to a session it has not
admitted before, and a global pool of extra workers the gate can carry. A
worker holds a request only while it generates and holds nothing while it runs
tools, so holding dispatch on request headroom under-fills the lane by the
inverse of that duty cycle.

Each case below builds a lane document the way the router publishes it — the
document is resolved through the lane-document reader's own path, so the rule
is driven by the same reading production takes — with and without a
``sessions`` block, one case per rung of the rule's order. The hold the rule
computes is driven through a real dispatch, and a lane change is shown to
resolve its destination under the coordinator session its run records.

The declared negative control removes the ``LaneHeld`` raise from
``reckon/crew/dispatch.py``: the non-dry-run case asserts the hold a real
dispatch raises, and without the raise that dispatch settles into a launch
instead and the case fails.
"""

from __future__ import annotations

import copy
import importlib
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import reckon.crew.dispatch_sessions as dispatch_sessions_module
from reckon import cli as cli_module
from reckon import crew_dispatch_commands
from reckon.crew import lane_document, runs
from reckon.crew.dispatch import change_lane
from reckon.crew.runs import _write_json, pointer_path
from tests import test_a_live_run_never_reads_dead as liveness
from tests import test_dispatch_names_its_backend as backend_tests

dispatch_module = importlib.import_module("reckon.crew.dispatch")

SESSION = "s22-coord"

# A window the router reports, and the young window a measured router published
# at 320 s of history. Either is a stated window: the router's figure is used
# as published once the block says where it was averaged, and the allowance
# falls back to headroom only for a block that states no window at all.
FULL_WINDOW_SECONDS = 900
MEASURED_YOUNG_WINDOW_SECONDS = 320

# The router's own shares, as it publishes them.
PER_SESSION_SLOTS = 4
NEW_SESSION_SLOTS = 5
GLOBAL_SLOTS = 8

# An engine headroom that would hold the dispatch on its own, so a case that
# proceeds proves the slot figure overrode it rather than agreeing with it.
HEADROOM_THAT_WOULD_HOLD = -3


def _admission(fields: dict[str, object]) -> dict[str, object]:
    """One admission block whose keys are the reader's own."""
    return dict(fields)


def _lane_document(
    *,
    admission: dict[str, object] | None = None,
    headroom: float = 20.0,
) -> dict[str, object]:
    """A lane document shaped as the router publishes one."""
    document: dict[str, object] = {"headroom": headroom}
    if admission is not None:
        document[lane_document.ADMISSION_KEY] = admission
    return document


def _allowance(document: dict[str, object]) -> dict[str, object]:
    """Drive the dispatch rule over a document through the reader's path."""
    return dispatch_module._lane_worker_allowance(document, session=SESSION)


def test_a_positive_per_session_share_proceeds_over_negative_headroom() -> None:
    """Rung one: the session's own share, even beside a headroom that would hold.

    A worker holds a request only while it generates, so a headroom of -3 on
    the request count is not a worker count of none. The router's own share for
    this session is the figure dispatch is to count against.
    """
    document = _lane_document(
        headroom=HEADROOM_THAT_WOULD_HOLD,
        admission=_admission(
            {
                lane_document.ADMISSION_WORKER_SLOTS_KEY: GLOBAL_SLOTS,
                lane_document.ADMISSION_NEW_SESSION_WORKER_SLOTS_KEY: NEW_SESSION_SLOTS,
                lane_document.ADMISSION_OBSERVED_SECONDS_KEY: FULL_WINDOW_SECONDS,
                lane_document.ADMISSION_SESSIONS_KEY: {
                    SESSION: {
                        lane_document.SESSION_LIVE_RUNS_KEY: 5,
                        lane_document.SESSION_WORKER_SLOTS_KEY: PER_SESSION_SLOTS,
                    }
                },
                "headroom": HEADROOM_THAT_WOULD_HOLD,
                "verdict": "congested",
            }
        ),
    )

    decision = _allowance(document)

    assert decision["allowance"] == PER_SESSION_SLOTS
    assert "session's own" in decision["source"]
    assert decision["held"] is False


def test_an_unlisted_session_uses_the_new_session_share() -> None:
    """Rung two: a session the router has not admitted before takes its share.

    ``sessions`` is present and does not list this session, so the
    new-session figure. The global pool would be the wrong rung.
    """
    document = _lane_document(
        admission=_admission(
            {
                lane_document.ADMISSION_WORKER_SLOTS_KEY: GLOBAL_SLOTS,
                lane_document.ADMISSION_NEW_SESSION_WORKER_SLOTS_KEY: NEW_SESSION_SLOTS,
                lane_document.ADMISSION_OBSERVED_SECONDS_KEY: FULL_WINDOW_SECONDS,
                lane_document.ADMISSION_SESSIONS_KEY: {
                    "s22-nova": {
                        lane_document.SESSION_LIVE_RUNS_KEY: 2,
                        lane_document.SESSION_WORKER_SLOTS_KEY: 3,
                    }
                },
                "headroom": 6,
                "verdict": "open",
            }
        )
    )

    decision = _allowance(document)

    assert decision["allowance"] == NEW_SESSION_SLOTS
    assert "new-session" in decision["source"]
    assert decision["held"] is False


def test_no_sessions_block_and_a_positive_global_share_proceeds() -> None:
    """Rung three: with no per-session map, the global worker slots are used."""
    document = _lane_document(
        admission=_admission(
            {
                lane_document.ADMISSION_WORKER_SLOTS_KEY: GLOBAL_SLOTS,
                lane_document.ADMISSION_OBSERVED_SECONDS_KEY: FULL_WINDOW_SECONDS,
                "headroom": 1,
                "verdict": "open",
            }
        )
    )

    decision = _allowance(document)

    assert decision["allowance"] == GLOBAL_SLOTS
    assert "global" in decision["source"]
    assert decision["held"] is False


def test_an_allowance_of_zero_holds_and_names_the_router_verdict() -> None:
    """Rung four: no room granted holds the node, naming the router's verdict."""
    document = _lane_document(
        admission=_admission(
            {
                lane_document.ADMISSION_WORKER_SLOTS_KEY: 0,
                lane_document.ADMISSION_OBSERVED_SECONDS_KEY: FULL_WINDOW_SECONDS,
                "headroom": 1,
                "verdict": "full",
            }
        )
    )

    decision = _allowance(document)

    assert decision["allowance"] == 0
    assert decision["held"] is True
    assert "full" in decision["reason"]
    assert "the gate is full" in decision["reason"]


def test_a_null_worker_slots_falls_back_to_headroom() -> None:
    """Rung five: no slot figure published, so headroom is the allowance."""
    document = _lane_document(
        admission=_admission(
            {
                lane_document.ADMISSION_WORKER_SLOTS_KEY: None,
                lane_document.ADMISSION_OBSERVED_SECONDS_KEY: FULL_WINDOW_SECONDS,
                "headroom": 3,
                "verdict": "open",
            }
        ),
        headroom=20,
    )

    decision = _allowance(document)

    assert decision["allowance"] == 3
    assert "headroom" in decision["source"]
    assert decision["held"] is False


def test_a_young_router_without_a_window_falls_back_to_headroom() -> None:
    """Rung six: a router that states no observation window is not counted.

    It states no window, so nothing shows its slot arithmetic to rest on any
    history, and the request headroom is the allowance.
    """
    document = _lane_document(
        admission=_admission(
            {
                lane_document.ADMISSION_WORKER_SLOTS_KEY: GLOBAL_SLOTS,
                lane_document.ADMISSION_SESSIONS_KEY: {
                    SESSION: {
                        lane_document.SESSION_LIVE_RUNS_KEY: 5,
                        lane_document.SESSION_WORKER_SLOTS_KEY: PER_SESSION_SLOTS,
                    }
                },
                "headroom": 2,
                "verdict": "open",
            }
        )
    )

    decision = _allowance(document)

    assert decision["allowance"] == 2
    assert "headroom" in decision["source"]
    assert decision["held"] is False


def test_a_stated_window_is_trusted_as_the_router_publishes_it() -> None:
    """A stated window licenses the figure, however young the router is.

    Measured on 2026-10-01, a router at 320 s of history published 57 slots
    against about 11 true. The block also stated the window its ratio was
    averaged over, and that statement is the router's own account of what its
    figure rests on, so a dispatch uses the figure as published. Only a block
    stating no window at all is distrusted, because nothing there can be shown
    to rest on any history.
    """
    document = _lane_document(
        admission=_admission(
            {
                lane_document.ADMISSION_WORKER_SLOTS_KEY: 57,
                lane_document.ADMISSION_OBSERVED_SECONDS_KEY: MEASURED_YOUNG_WINDOW_SECONDS,
                "headroom": 9,
                "verdict": "open",
            }
        )
    )

    decision = _allowance(document)

    assert decision["allowance"] == 57
    assert "global" in decision["source"]
    assert decision["held"] is False


def test_a_nonpositive_window_is_read_as_no_history() -> None:
    """A window of zero or below is no history: the slot figures are not counted.

    The router reports its window from its first reading, so an
    ``observed_seconds`` of zero states that no window has been averaged rather
    than an empty history that licenses the figure, and a negative figure is
    not a window at all. Both are read exactly as a missing window is: the slot
    figures are not counted and the allowance falls back to headroom.
    """
    no_window = _lane_document(
        admission=_admission(
            {
                lane_document.ADMISSION_WORKER_SLOTS_KEY: GLOBAL_SLOTS,
                lane_document.ADMISSION_NEW_SESSION_WORKER_SLOTS_KEY: NEW_SESSION_SLOTS,
                lane_document.ADMISSION_SESSIONS_KEY: {
                    SESSION: {
                        lane_document.SESSION_LIVE_RUNS_KEY: 5,
                        lane_document.SESSION_WORKER_SLOTS_KEY: PER_SESSION_SLOTS,
                    }
                },
                "headroom": 2,
                "verdict": "open",
            }
        )
    )

    for observed in (0, 0.0, -1):
        document = copy.deepcopy(no_window)
        document["admission"][lane_document.ADMISSION_OBSERVED_SECONDS_KEY] = observed

        decision = _allowance(document)

        assert decision == _allowance(no_window), observed
        assert decision["allowance"] == 2, observed
        assert decision["held"] is False, observed


def test_an_unreadable_allowance_never_holds_a_dispatch() -> None:
    """Absence of a signal is not exhaustion: no figure holds nothing."""
    document = _lane_document(headroom=12)

    decision = _allowance(document)

    assert decision["allowance"] == 12
    assert decision["held"] is False

    empty = dispatch_module._lane_worker_allowance({}, session=SESSION)
    assert empty["allowance"] is None
    assert empty["held"] is False
    assert empty["state"] == "unknown"


# --- The hold where it acts, and the session a lane change resolves under ----


@pytest.fixture()
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The backend-routing fixture repository, as a real dispatch needs it."""
    return backend_tests.dispatch_repo.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home, so a case writes nowhere the live fleet reads."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def test_a_zero_allowance_holds_a_real_dispatch(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The allowance hold stops a real dispatch, not only a dry run that reports it.

    A dry run reports the allowance without acting on it, so a hold that is
    only ever exercised through a preview could be deleted with every case
    still green. Here the same lane document reaches a real dispatch, which
    must stop on the router's own verdict.

    A real dispatch is what the check names: the dry-run answer carries
    ``dry_run``, so its absence is the hold the launch path raised rather than
    a preview reproducing it. Nothing was created either -- the hold is raised
    before a pointer or a worktree exists -- so the live pointer directory is
    still empty when it returns.
    """
    lane_path = dispatch_repo.parent / "lane-zero-allowance.json"
    lane_path.write_text(
        json.dumps(
            _lane_document(
                headroom=3.0,
                admission=_admission(
                    {
                        lane_document.ADMISSION_WORKER_SLOTS_KEY: 0,
                        lane_document.ADMISSION_OBSERVED_SECONDS_KEY: FULL_WINDOW_SECONDS,
                        "headroom": 3.0,
                        "verdict": "full",
                    }
                ),
            )
        ),
        encoding="utf-8",
    )
    config = copy.deepcopy(backend_tests.CONFIG)
    config["backends"]["beta"]["lane_document"] = str(lane_path)
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_args, **_kwargs: config
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_args, **_kwargs: None
    )

    result = CliRunner().invoke(
        cli_module.main,
        [
            *backend_tests._arguments(
                dispatch_repo, node="held-zero-allowance", dry_run=False
            ),
            "--backend",
            "beta",
            "--no-watch",
        ],
    )

    payload = backend_tests._payload(result)
    assert result.exit_code == 75
    assert payload["error"] == "lane-paused"
    assert "dry_run" not in payload
    assert payload["lane_gate"]["state"] == "held"
    assert payload["lane_gate"]["allowance"] == 0
    assert "grants 0" in payload["detail"]
    assert "the gate is full" in payload["detail"]
    assert list((runs.crew_home() / "live").glob("*.json")) == []


def _stopped_run(tmp_path: Path, run_id: str) -> dict:
    """A stopped run as a lane change reads it, carrying its coordinator session."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / f"{run_id}-tree"
    worktree.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(f"node: {run_id}\nstatus: waiting\n", encoding="utf-8")
    prompt = directory / "prompt.txt"
    prompt.write_text("the original dispatch prompt\n", encoding="utf-8")
    record = {
        "run_id": run_id,
        "project": "proj",
        "repo": str(tmp_path),
        "worktree": str(worktree),
        "launch": "cli",
        "argv": ["codex", "exec"],
        "dialect": "codex",
        "session_harness": "codex",
        "backend": "alpha",
        "role": "implement",
        "attempt": 1,
        "base_sha": "HEAD",
        "pid": liveness._absent_pid(),
        "pid_start_time": None,
        "process_alive": None,
        "session_id": "sess-on-the-pointer",
        "session": SESSION,
        "created_at": "2026-10-02T09:59:00+00:00",
        "log_path": str(directory / "stream.jsonl"),
        "manifest_path": str(manifest),
        "prompt_path": str(prompt),
        "phase": "working",
        "launcher_host": socket.gethostname(),
        "node": {
            "id": run_id,
            "plan": "plan-a",
            "section": "",
            "time_budget": "30m",
            "write_paths": [],
        },
    }
    _write_json(pointer_path(run_id), record)
    return record


def test_a_lane_change_resolves_under_the_runs_coordinator_session(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane change spends the coordinator's own share of the destination lane.

    The allowance follows the session the caller names, so a resolution that
    names none is charged the share offered to a session the router has not
    admitted before rather than the coordinator's own share. The session the
    run records is the coordinator's, and it is what the destination
    resolution must carry.
    """
    captured: dict = {}
    resolution = SimpleNamespace(
        backend="beta",
        launch="in-harness",
        backend_settings={},
        lane_gate=dispatch_module._dispatch_lane_gate({}),
        validation=SimpleNamespace(ok=True, findings=[]),
        competence={"allowed": True},
        authority="a-ledger-authority",
        sandbox_write_roots=None,
    )

    def destination(**kwargs):
        captured.update(kwargs)
        return resolution

    monkeypatch.setattr(dispatch_sessions_module, "plan_dispatch", destination)
    monkeypatch.setattr(
        dispatch_sessions_module, "_budget_verdict", lambda **kwargs: {"held": False}
    )
    monkeypatch.setattr(
        dispatch_sessions_module, "resolve_dispatch_ledger_root", lambda authority: authority
    )
    record = _stopped_run(tmp_path, "r-lane-change-session")

    change_lane(
        "r-lane-change-session",
        "beta",
        "the local lane is the destination",
        config=backend_tests.CONFIG,
        launch=False,
    )

    assert record["session"] == SESSION
    assert captured["session"] == SESSION
