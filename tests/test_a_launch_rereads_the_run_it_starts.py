"""A launch re-reads the run it starts.

An admission pass ranks the queued pointers it read at its start and then works
through the queue, so a request can move while the pass is busy with others: a
coordinator may promote or discard it, and a supervisor may end an attempt of it
that already ran. Each case below writes that change after the pass has read the
request as queued, and the pass must then leave the request alone rather than
launch it from its opening read.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import queue_admission, runs

PROJECT = "proj"
SESSION = "coordinator-a"
RUN_ID = "r-requeued"

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
def crew_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A crew home of this test's own, and the checkout a launch resolves."""
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


def _config_for():
    config = json.loads(json.dumps(CONFIG))

    def resolve(_pointer):
        return config

    return config, resolve


def _queued_request(repository: Path, *, run_id: str, manifest_path: str = "") -> dict:
    from reckon.crew.node import TaskNode

    node = TaskNode(
        id="reread",
        goal="launch only a request that still waits",
        plan="plan-a",
        section="dispatch",
        done_when="pytest tests/test_a_launch_rereads_the_run_it_starts.py passes",
        write_paths=["reread.txt"],
        time_budget="20m",
        spec_level="exact",
        manifest_path=manifest_path,
    )
    joined = datetime.now(UTC).isoformat()
    return {
        "run_id": run_id,
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


def _launcher(recorded: list, manifest: Path | None = None):
    """Stand in for the worker process a launch starts.

    A launched worker writes its manifest at the run's own path, so the stub
    writes there too: that write is what the delivered manifest loses when a
    second attempt is launched over it.
    """

    def launch(_plan, *, log_path, stderr_path, prompt_path):
        recorded.append({"prompt_path": prompt_path})
        if manifest is not None:
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("status: in-progress\n", encoding="utf-8")
        return os.getpid()

    return launch


def _stubbed_launch(recorded: list):
    """Stand in for the launch itself, for a request whose pointer is gone.

    Dispatch refuses a run it has no pointer for before it spawns anything, so
    the pass's own decision needs this seam to be observed: a launch that
    re-publishes the request is exactly what a stale read produces, and it is
    what the re-read exists to stop.
    """

    def launch(pointer, **_kwargs):
        recorded.append(str(pointer["run_id"]))
        record = dict(pointer)
        record["phase"] = "starting"
        runs._write_json(runs.pointer_path(str(pointer["run_id"])), record)
        return record

    return launch


def _after_the_pass_reads_the_queue(
    monkeypatch: pytest.MonkeyPatch, change: Callable[[], None]
) -> list[int]:
    """Write ``change`` once the pass reaches its launch decision.

    The pass ranks what its first read returned and decides each request's
    launch later, so this is the last point at which a request can still have
    moved. A change written here is visible only to a read taken here; a pass
    that read the request's attempts when it started would already be past
    them, which is what makes this the window the re-read exists for.
    """
    reread = queue_admission._launch_reread_refusal
    fired: list[int] = []

    def re_read(pointer):
        if not fired:
            fired.append(1)
            change()
        return reread(pointer)

    monkeypatch.setattr(queue_admission, "_launch_reread_refusal", re_read)
    return fired


def _delivered_manifest(run_id: str) -> Path:
    """Leave on a run the record an attempt that ran and delivered leaves."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "attempt-1-exit.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "attempt": 1,
                "recorded_by": "supervisor",
                "exited_at": "2026-11-05T12:00:46+00:00",
                "stream_records_seen": 412,
                "last_record_type": "assistant",
                "ended_during": "working",
                "exit_code": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = directory / "manifest.md"
    manifest.write_text(
        "status: complete\n\nwhat the attempt landed\n", encoding="utf-8"
    )
    return manifest


def _backdate_queued(run_id: str, seconds: float) -> None:
    """Age a queued request past the stall threshold, as a slow pass sees it."""
    path = runs.pointer_path(run_id)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["queued_at"] = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    runs._write_json(path, record)


def _stall_rows_in_log(run_id: str) -> int:
    """How many stall rows the verdict log records for this request in total."""
    path = runs.crew_home() / queue_admission.ADMISSION_VERDICT_LOG
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    count = 0
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        for row in record.get("pointers") or ():
            if (
                str(row.get("run_id") or "") == run_id
                and str(row.get("verdict") or "") == "stalled"
            ):
                count += 1
    return count


def _rows(result: dict, run_id: str) -> list[dict]:
    return [row for row in result["verdicts"] if row.get("run_id") == run_id]


def _verdict(result: dict, run_id: str) -> dict:
    for row in result["verdicts"]:
        if row.get("run_id") == run_id:
            return row
    raise AssertionError(f"no verdict for {run_id}: {json.dumps(result, default=str)}")


def test_a_request_whose_pointer_is_removed_is_not_launched(
    crew_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, resolve = _config_for()
    runs.queue_dispatch(_queued_request(crew_checkout, run_id=RUN_ID))
    recorded: list = []
    monkeypatch.setattr(queue_admission, "_launch_queued", _stubbed_launch(recorded))
    fired = _after_the_pass_reads_the_queue(
        monkeypatch, lambda: runs.pointer_path(RUN_ID).unlink()
    )

    result = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))

    assert fired, "the change was never written after the pass read the queue"
    assert recorded == []
    assert result["launched"] == []
    detail = _verdict(result, RUN_ID)["detail"]
    assert "pointer was removed" in detail
    assert not runs.pointer_path(RUN_ID).exists()


def test_a_request_whose_attempt_already_resulted_is_not_launched(
    crew_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, resolve = _config_for()
    manifest = runs.run_dir(RUN_ID) / "manifest.md"
    runs.queue_dispatch(
        _queued_request(crew_checkout, run_id=RUN_ID, manifest_path=str(manifest))
    )
    delivered: list[str] = []
    recorded: list = []

    def deliver() -> None:
        _delivered_manifest(RUN_ID)
        delivered.append(manifest.read_text(encoding="utf-8"))

    fired = _after_the_pass_reads_the_queue(monkeypatch, deliver)

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(recorded, manifest=manifest)
    )

    assert fired, "the change was never written after the pass read the queue"
    assert delivered, "the attempt's result was never written after the read"
    assert manifest.read_text(encoding="utf-8") == delivered[0], (
        f"a launch overwrote the delivered manifest; launcher ran {recorded!r}"
    )
    assert recorded == []
    assert result["launched"] == []
    detail = _verdict(result, RUN_ID)["detail"]
    assert "already exited with a recorded result" in detail
    assert "attempt-1-exit.json" in detail


def test_an_unchanged_queued_request_is_launched(
    crew_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, resolve = _config_for()
    runs.queue_dispatch(_queued_request(crew_checkout, run_id=RUN_ID))
    recorded: list = []
    fired = _after_the_pass_reads_the_queue(monkeypatch, lambda: None)

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(recorded)
    )

    assert fired, "the change was never written after the pass read the queue"
    assert len(recorded) == 1
    assert result["launched"] == [RUN_ID]
    assert _verdict(result, RUN_ID)["verdict"] == "launched"


def test_an_exit_record_written_at_the_launch_decision_stops_the_launch(
    crew_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attempt record is read where the launch decision is made.

    The request is still queued and unchanged when the pass reads it, and its
    attempt only records a result at the launch decision. A pass that read the
    run's attempts when it started would already be past them and would launch,
    so this case tells the two readings apart.
    """
    _, resolve = _config_for()
    runs.queue_dispatch(_queued_request(crew_checkout, run_id=RUN_ID))
    recorded: list = []

    def record_the_result() -> None:
        directory = runs.run_dir(RUN_ID)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "attempt-1-exit.json").write_text(
            json.dumps(
                {
                    "run_id": RUN_ID,
                    "attempt": 1,
                    "recorded_by": "supervisor",
                    "exited_at": "2026-11-05T12:00:46+00:00",
                    "stream_records_seen": 412,
                    "last_record_type": "assistant",
                    "ended_during": "working",
                    "exit_code": 0,
                }
            )
            + "\n",
            encoding="utf-8",
        )

    fired = _after_the_pass_reads_the_queue(monkeypatch, record_the_result)

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(recorded)
    )

    assert fired, "the exit record was never written at the launch decision"
    assert recorded == [], "the launch started over an attempt that had resulted"
    assert result["launched"] == []
    detail = _verdict(result, RUN_ID)["detail"]
    assert "already exited with a recorded result" in detail
    assert "attempt-1-exit.json" in detail


def test_a_refused_request_records_one_verdict_that_does_not_become_a_stall(
    crew_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal is the pass's verdict for that request, not a wait it repeats.

    The request has waited past the stall threshold on a lane with room, so a
    pass that did not recognise the refusal would record a stall beside it in
    the same pass, and read that refusal on the next pass as a request still
    waiting and append another. Its stall count would then grow with every pass
    while nothing about it changed.
    """
    _, resolve = _config_for()
    runs.queue_dispatch(_queued_request(crew_checkout, run_id=RUN_ID))
    _backdate_queued(RUN_ID, 1200.0)
    _delivered_manifest(RUN_ID)
    _after_the_pass_reads_the_queue(monkeypatch, lambda: None)

    first = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))
    first_rows = [row["verdict"] for row in _rows(first, RUN_ID)]
    queue_admission._record_pass_verdicts(first, lane="local", code_stamp="test")
    stalls_after_first = _stall_rows_in_log(RUN_ID)

    second = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))
    second_rows = [row["verdict"] for row in _rows(second, RUN_ID)]
    queue_admission._record_pass_verdicts(second, lane="local", code_stamp="test")
    stalls_after_second = _stall_rows_in_log(RUN_ID)

    assert (first_rows, second_rows, stalls_after_first, stalls_after_second) == (
        ["refused-before-launch"],
        ["refused-before-launch"],
        0,
        0,
    ), (
        "one refusal per pass and never a stall row beside it: "
        f"{first_rows} then {second_rows}, "
        f"stall rows recorded {stalls_after_first} then {stalls_after_second}"
    )
