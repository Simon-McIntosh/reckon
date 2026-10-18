"""The admission loop re-executes when the checkout's code moves under it.

A queued request must never be validated or launched by code older than the
checkout the service launches from. The loop therefore compares the code stamp
it started with against the checkout's on every pass boundary, and re-executes
in place when the stamp has moved on a checkout the trusted test accepts. The
admission failure guard fingerprints that same stamp, so a request a pass
refused under the old code is retried by the first pass that runs the new code,
rather than waiting out its back-off.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import queue_admission, runs

PROJECT = "proj"
SESSION = "coordinator-a"

CONFIG = {
    "default_backend": "clive",
    "local_backend": "clive",
    "backends": {
        "clive": {
            "launch": "cli",
            "command": "codex",
            "model": "local-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def admit_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repository = tmp_path / "checkout"
    repository.mkdir()
    return repository


def _config_for(config: dict[str, Any]):
    def resolve(_pointer):
        return config

    return resolve


def _queue(repository: Path) -> dict[str, Any]:
    from reckon.crew.node import TaskNode

    node = TaskNode(
        id="current-code-node",
        goal="admit this queued node",
        plan="plan-a",
        section="dispatch",
        done_when="pytest reports passed",
        write_paths=["current-code-node.txt"],
        time_budget="20m",
        spec_level="exact",
    )
    joined = datetime.now(UTC).isoformat()
    return {
        "run_id": "r-current-code",
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repository),
        "node": node.as_dict(),
        "backend": "clive",
        "launch": "cli",
        "local": True,
        "phase": "queued",
        "queued_at": joined,
        "created_at": joined,
        "hold": {"held": True},
        "dispatch_options": {
            "project": PROJECT,
            "plan_slug": "plan-a",
            "section": "dispatch",
            "session": SESSION,
            "local": True,
            "no_watch": True,
            "overrides": [],
        },
    }


class _RefusingDispatch:
    """A dispatch boundary that refuses every request and records its attempts."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def __call__(self, *, node, admitted_from="", **kwargs):
        self.attempts.append(str(admitted_from))
        raise ValueError("launch refused")


def _drive_loop(
    monkeypatch: pytest.MonkeyPatch,
    *,
    trusted: bool,
    config: dict[str, Any],
    repository: Path,
) -> dict[str, Any]:
    """Run the loop for two passes, moving the code stamp during the wait.

    The checkout commits as the loop waits between passes -- the window a
    running service spends asleep -- so the move is only visible to a stamp
    comparison taken at the head of the next pass. ``admit_once`` is wrapped to
    stop the loop after the second pass, since a stub re-execution returns
    where the real one would have replaced the process.
    """
    holder = {"stamp": "old"}
    events: list[str] = []
    dispatch = _RefusingDispatch()

    def read_stamp() -> str:
        return holder["stamp"]

    def reexec() -> None:
        events.append("reexec")

    def pause(_seconds: float) -> None:
        # The checkout moves while the service waits, after any comparison the
        # previous pass made and before the next pass begins.
        holder["stamp"] = "new"

    monkeypatch.setattr(queue_admission, "_admit_code_stamp", read_stamp)
    monkeypatch.setattr(queue_admission, "_checkout_code_is_trusted", lambda: trusted)
    monkeypatch.setattr(queue_admission, "_reexec_into_current_code", reexec)
    monkeypatch.setattr(queue_admission, "_pause", pause)
    monkeypatch.setattr(
        importlib.import_module("reckon.crew.dispatch"), "dispatch", dispatch
    )

    real_admit_once = queue_admission.admit_once
    passes: list[int] = []
    guards: list[dict[str, Any]] = []

    def admit_once_(*, lane: str = "") -> dict[str, Any]:
        number = len(passes) + 1
        if number > 2:
            raise KeyboardInterrupt
        events.append(f"pass-{number}")
        result = real_admit_once(
            lane=lane, config_for=_config_for(config), launcher=None
        )
        guards.append(
            dict(runs.read_pointer("r-current-code").get("failure_guard") or {})
        )
        passes.append(number)
        return result

    monkeypatch.setattr(queue_admission, "admit_once", admit_once_)

    runs.queue_dispatch(_queue(repository))
    result = queue_admission.admit_until_stopped(interval=0.0)

    return {
        "result": result,
        "events": events,
        "attempts": dispatch.attempts,
        "guards": guards,
        "passes": passes,
    }


def test_the_loop_reexecutes_and_retries_when_the_code_stamp_moves(
    admit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driven = _drive_loop(
        monkeypatch,
        trusted=True,
        config=json.loads(json.dumps(CONFIG)),
        repository=admit_home,
    )

    assert driven["passes"] == [1, 2]
    # The checkout moves during the wait after the first pass, and the
    # re-execution lands before the second pass runs -- never one pass later,
    # on stale code.
    assert driven["events"][:3] == ["pass-1", "reexec", "pass-2"]
    # The request refused in the first pass is attempted again in the second,
    # rather than waiting out the back-off the first failure recorded.
    assert driven["attempts"] == ["r-current-code", "r-current-code"]
    # The failure guard fingerprints the code stamp: the first pass records the
    # stamp it ran under, and that fingerprint moving is what invalidates the
    # back-off for the second pass.
    assert driven["guards"][0]["code"] == "old"
    assert driven["guards"][1]["code"] == "new"


def test_the_loop_keeps_its_current_code_when_the_checkout_is_untrusted(
    admit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driven = _drive_loop(
        monkeypatch,
        trusted=False,
        config=json.loads(json.dumps(CONFIG)),
        repository=admit_home,
    )

    assert driven["passes"] == [1, 2]
    assert "reexec" not in driven["events"]
    assert driven["events"] == ["pass-1", "pass-2"]
