"""Each admission pass records the verdict it reached for every queued pointer.

A request held for its session's allowance, held by the lane's gate, held on
its retry back-off or held behind an earlier request otherwise leaves no trace:
the pass records a decision only when it attempts a launch. So a request that
waited for hours reads afterwards as if nothing had happened. The loop
therefore appends one JSON line per pass whose lane had a queued pointer,
carrying each pointer's allowance read with its source and the pass's verdict
for it, and trims the log to a fixed bound so a long-lived service cannot grow
it without limit.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import queue_admission, runs

PROJECT = "proj"
SESSION = "coordinator-a"
RUN_ID = "r-verdict"

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


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


@pytest.fixture()
def admit_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home and a checkout the shared dispatch path can launch."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repository = tmp_path / "checkout"
    scripts = repository / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills" / "reckon-build" / "scripts"
    (scripts / "worktree_fleet.py").write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plan = repository / "docs" / "plans" / "plan-a.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    quote = '"'
    plan.write_text(
        "\n".join(
            [
                "<!doctype html>",
                f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>",
                f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>",
                f"<meta name={quote}plan-slug{quote} content={quote}plan-a{quote}>",
                f"<h2 id={quote}dispatch{quote}>Dispatch</h2>",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", "docs")
    _git(repository, "add", "--", "skills")
    _git(repository, "commit", "-q", "-m", "chore: seed checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _write_lane(path: Path, payload: dict[str, Any]) -> None:
    """Publish one lane document, as the router would."""
    path.write_text(json.dumps(payload), encoding="utf-8")


def _session_lane(worker_slots: float) -> dict[str, Any]:
    """A lane that publishes this session's own share of the worker slots."""
    return {
        "headroom": 4,
        "admission": {
            "worker_slots": worker_slots,
            "observed_seconds": 900,
            "sessions": {SESSION: {"worker_slots": worker_slots}},
            "verdict": "open",
        },
    }


def _new_session_lane(worker_slots: float) -> dict[str, Any]:
    """A lane whose share for this session comes from another published figure.

    The session is not listed in the admission block's ``sessions`` map, so the
    router's own preference order reads the ``new_session_worker_slots`` figure
    instead — a different field with a different source label. A pass that
    echoed the figure it published last would record the name of the session it
    had never seen, so the two lanes together show the read follows the
    document rather than the test's own input.
    """
    return {
        "headroom": 4,
        "admission": {
            "worker_slots": worker_slots,
            "new_session_worker_slots": worker_slots,
            "observed_seconds": 900,
            "sessions": {"some-other-session": {"worker_slots": 1}},
            "verdict": "open",
        },
    }


def _config_with_lane(lane_document: Path) -> tuple[dict[str, Any], Any]:
    config = json.loads(json.dumps(CONFIG))
    config["backends"]["clive"]["lane_document"] = str(lane_document)

    def resolve(_pointer):
        return config

    return config, resolve


def _queue(repository: Path) -> dict[str, Any]:
    from reckon.crew.node import TaskNode

    node = TaskNode(
        id="verdict-node",
        goal="admit this queued node",
        plan="plan-a",
        section="dispatch",
        done_when="pytest tests/test_each_admission_pass_records_its_verdict.py passes",
        write_paths=["verdict-node.txt"],
        time_budget="20m",
        spec_level="exact",
    )
    joined = datetime.now(UTC).isoformat()
    return {
        "run_id": RUN_ID,
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


def _launcher(recorded: list) -> Any:
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(plan)
        return os.getpid()

    return launch


def _failing_launcher(plan, *, log_path, stderr_path, prompt_path):
    raise FileNotFoundError("the harness executable is absent")


def _verdict_log() -> Path:
    return runs.crew_home() / queue_admission.ADMISSION_VERDICT_LOG


def _read_verdict_lines(_admit_repo: Path) -> list[dict[str, Any]]:
    path = _verdict_log()
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    *,
    lane_document: Path,
    resolve: Any,
    launcher: Any,
    lanes: list[dict[str, Any]],
) -> list[int]:
    """Drive the loop for ``len(lanes)`` passes, publishing each pass's lane.

    ``admit_once`` is wrapped so the loop is driven with a stub launch and a
    fixed config, and stops after the last pass the case asks for. The append
    under test stays inside ``admit_until_stopped``.
    """
    calls: list[int] = []
    real_admit_once = queue_admission.admit_once

    def wrapper(*, lane: str = "") -> dict[str, Any]:
        number = len(calls) + 1
        if number > len(lanes):
            raise KeyboardInterrupt
        calls.append(number)
        _write_lane(lane_document, lanes[number - 1])
        return real_admit_once(lane=lane, config_for=resolve, launcher=launcher)

    monkeypatch.setattr(queue_admission, "admit_once", wrapper)
    monkeypatch.setattr(queue_admission, "_pause", lambda _seconds: None)
    queue_admission.admit_until_stopped(interval=0.0)
    return calls


def test_a_held_then_launched_pass_writes_one_line_each(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = tmp_path / "lane.json"
    _config, resolve = _config_with_lane(lane)
    runs.queue_dispatch(_queue(admit_repo))
    recorded: list = []

    _drive(
        monkeypatch,
        lane_document=lane,
        resolve=resolve,
        launcher=_launcher(recorded),
        lanes=[_session_lane(0.0), _new_session_lane(3.0)],
    )

    lines = _read_verdict_lines(admit_repo)
    assert len(lines) == 2, lines
    held, launched = lines
    assert held["lane"] == ""
    assert held["code_stamp"], "the line records the code stamp the service runs"
    held_pointer = held["pointers"][0]
    assert held_pointer["run_id"] == RUN_ID
    assert held_pointer["session"] == SESSION
    assert held_pointer["verdict"] == "held-for-allowance"
    assert held_pointer["allowance"] == 0
    assert held_pointer["allowance_source"] == "the session's own worker slots"
    launched_pointer = launched["pointers"][0]
    assert launched_pointer["run_id"] == RUN_ID
    assert launched_pointer["verdict"] == "launched"
    # The second pass publishes its figure from another field, so the allowance
    # and its source both change: a pass that echoed what it read last, or the
    # figure the test wrote, would report neither of these.
    assert launched_pointer["allowance"] == 3
    assert launched_pointer["allowance_source"] == "the new-session worker slots"
    assert recorded, "the stub launch was reached"
    still_queued = [
        str(pointer.get("run_id"))
        for pointer in runs.list_live()
        if pointer.get("phase") == "queued"
    ]
    assert still_queued == [], "the launched request left the queue"


def test_a_refused_launch_records_the_backoff_hold_with_its_retry_at(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = tmp_path / "lane.json"
    _config, resolve = _config_with_lane(lane)
    runs.queue_dispatch(_queue(admit_repo))

    _drive(
        monkeypatch,
        lane_document=lane,
        resolve=resolve,
        launcher=_failing_launcher,
        lanes=[_session_lane(2.0), _session_lane(2.0)],
    )

    lines = _read_verdict_lines(admit_repo)
    assert len(lines) == 2, lines
    refused = lines[0]["pointers"][0]
    assert refused["verdict"] == "refused"
    assert "the worker launch could not start" in refused["failure"]
    retry_at = str(runs.read_pointer(RUN_ID).get("retry_at") or "")
    assert retry_at, "the refused launch stamped a retry time"
    backoff = lines[1]["pointers"][0]
    assert backoff["verdict"] == "held-on-retry-backoff"
    assert backoff["retry_at"] == retry_at


def test_a_pass_with_no_queued_pointer_writes_no_line(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = tmp_path / "lane.json"
    _config, resolve = _config_with_lane(lane)

    _drive(
        monkeypatch,
        lane_document=lane,
        resolve=resolve,
        launcher=_launcher([]),
        lanes=[_session_lane(2.0)],
    )

    assert _read_verdict_lines(admit_repo) == []


def test_a_pass_under_a_waiting_gate_records_the_pointer_as_gate_held(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gated pass reads the queue before it returns, so the hold is recorded.

    The pass returns as soon as the fleet gate is waiting, before it would
    otherwise read the queued pointers. A request held there leaves the same
    empty trace as one held for its allowance, so the gated pass must name the
    request and the gate itself.
    """
    from click.testing import CliRunner

    from reckon import cli

    lane = tmp_path / "lane.json"
    _config, resolve = _config_with_lane(lane)
    runs.queue_dispatch(_queue(admit_repo))
    paused = CliRunner().invoke(cli.main, ["crew", "gate", "--pause", "moving"])
    assert paused.exit_code == 0, paused.output

    _drive(
        monkeypatch,
        lane_document=lane,
        resolve=resolve,
        launcher=_launcher([]),
        lanes=[_session_lane(2.0)],
    )

    lines = _read_verdict_lines(admit_repo)
    assert len(lines) == 1, lines
    assert lines[0]["gate"] == "paused"
    pointer = lines[0]["pointers"][0]
    assert pointer["run_id"] == RUN_ID
    assert pointer["session"] == SESSION
    assert pointer["verdict"] == "held-by-gate"
    assert pointer["allowance"] == 2
    assert pointer["allowance_source"] == "the session's own worker slots"


def test_a_trim_failure_leaves_the_pass_and_its_decision_intact(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A storage error while bounding the log never ends the admission service.

    The log is best effort: the pass may not fail because its record could not
    be written, or because bounding that record could not be done. A trim that
    raises is swallowed, and the decision the pass had already reached stands
    unchanged.
    """
    from reckon.crew import fleet_supervisor

    def failing_trim(path: Path, *, limit: int, keep: int) -> bool:
        raise OSError(28, "No space left on device", str(path))

    monkeypatch.setattr(fleet_supervisor, "trim_service_log", failing_trim)
    lane = tmp_path / "lane.json"
    _config, resolve = _config_with_lane(lane)
    runs.queue_dispatch(_queue(admit_repo))
    recorded: list = []

    calls = _drive(
        monkeypatch,
        lane_document=lane,
        resolve=resolve,
        launcher=_launcher(recorded),
        lanes=[_session_lane(0.0)],
    )

    assert calls == [1], "the pass ran to its end despite the failing trim"
    lines = _read_verdict_lines(admit_repo)
    assert len(lines) == 1, lines
    pointer = lines[0]["pointers"][0]
    assert pointer["verdict"] == "held-for-allowance"
    assert pointer["allowance"] == 0
    assert recorded == [], "a held request launched nothing"
    still_queued = [
        str(entry.get("run_id"))
        for entry in runs.list_live()
        if entry.get("phase") == "queued"
    ]
    assert still_queued == [RUN_ID], "the held request stayed queued"


def test_the_log_is_trimmed_within_its_size_bound(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = tmp_path / "lane.json"
    _config, resolve = _config_with_lane(lane)
    runs.queue_dispatch(_queue(admit_repo))
    bound = 700
    monkeypatch.setattr(queue_admission, "ADMISSION_VERDICT_LOG_MAX_BYTES", bound)

    passes = 6
    _drive(
        monkeypatch,
        lane_document=lane,
        resolve=resolve,
        launcher=_launcher([]),
        lanes=[_session_lane(0.0)] * passes,
    )

    path = _verdict_log()
    assert path.exists()
    assert path.stat().st_size <= bound, path.stat().st_size
    lines = _read_verdict_lines(admit_repo)
    assert lines, "the newest line survives the trim"
    assert len(lines) < passes, "the oldest lines were dropped"
    assert lines[-1]["pointers"][0]["verdict"] == "held-for-allowance"
