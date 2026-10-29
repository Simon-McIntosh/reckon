"""An admitted launch publishes the launched run's record, not the queued one.

A request refused once and launched later by the admission pass must end up
with the same pointer a direct dispatch writes: its argv, its worktree, its
lane and every other launched field. The fields it carried while it waited —
the failure, its time, its attempt count and its retry instant — belong under
``admission_history``, where a reader of the launched run does not mistake them
for a launch that failed.

A failed or stripped launch must also leave nothing binding on the reviewed
run's review-records paths, so a later review of that run can launch; and a
failed admitted launch must keep the waits a direct dispatch recorded, so a
request whose upstreams have landed moves to ``queued`` rather than reading
``awaiting-upstream`` forever.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import queue_admission, runs
from reckon.crew.node import TaskNode

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
            "lane": "local-lane",
            "model_key": "local-model",
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
    (state / "index.json").parent.mkdir(parents=True)
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


def _lane_document(tmp_path: Path, *, worker_slots: float) -> Path:
    lane = tmp_path / "lane.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": 4,
                "admission": {
                    "worker_slots": worker_slots,
                    "observed_seconds": 900,
                    "sessions": {SESSION: {"worker_slots": worker_slots}},
                    "verdict": "full",
                },
            }
        ),
        encoding="utf-8",
    )
    return lane


def _config_for(worker_slots: float = 1.0):
    config = json.loads(json.dumps(CONFIG))

    def resolve(pointer):
        return config

    return config, resolve


def _queued_request(
    repository: Path, *, node_id: str, run_id: str, session: str = SESSION
) -> dict:
    node = TaskNode(
        id=node_id,
        goal="admit this queued node",
        plan="plan-a",
        section="dispatch",
        done_when="pytest tests/test_a_queued_launch_keeps_the_run_record.py reports passed",
        write_paths=[f"{node_id}.txt"],
        time_budget="20m",
        spec_level="exact",
    )
    joined = datetime.now(UTC).isoformat()
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": session,
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
            "session": session,
            "local": True,
            "no_watch": True,
            "overrides": [],
        },
    }


def _launcher(recorded: list):
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(plan)
        Path(log_path).write_text('{"type":"turn.started"}\n', encoding="utf-8")
        return os.getpid()

    return launch


def _failing_launcher(plan, *, log_path, stderr_path, prompt_path):
    raise FileNotFoundError("the harness executable is absent")


def _prime_lane(tmp_path: Path, config: dict, *, worker_slots: float = 1.0) -> None:
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=worker_slots)
    )


def _refuse_once_then_launch(repository: Path, tmp_path: Path) -> tuple[dict, dict]:
    """Queue one request, fail its first attempt, then launch it.

    Returns the launched pointer and the pass result that launched it. The
    second pass runs after the failed attempt's retry instant, so it is a
    retry of the same request and not a fresh one.
    """
    config, resolve = _config_for()
    _prime_lane(tmp_path, config)
    run_id = "r-refused-twice"
    runs.queue_dispatch(_queued_request(repository, node_id="node-a", run_id=run_id))

    failed = queue_admission.admit_once(config_for=resolve, launcher=_failing_launcher)
    assert failed["failed"] == [run_id], failed
    refused = runs.read_pointer(run_id)
    assert refused["phase"] == "queued"
    assert refused["failure_attempts"] == 1

    later = datetime.now(UTC) + timedelta(minutes=5)
    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher([]), now=later
    )
    assert result["launched"] == [run_id], result
    return runs.read_pointer(run_id), result


def test_a_refused_then_launched_request_carries_the_launch_record(
    admit_repo: Path, tmp_path: Path
) -> None:
    launched, _ = _refuse_once_then_launch(admit_repo, tmp_path)

    assert launched["phase"] != "queued"
    assert isinstance(launched.get("argv"), list) and launched["argv"], launched.keys()
    assert launched.get("worktree"), launched.keys()
    assert launched.get("lane"), launched.keys()
    for key in ("failure", "failure_at", "failure_attempts", "retry_at"):
        assert key not in launched, f"{key} left at the top level of a launched run"


def test_the_queued_life_is_kept_under_admission_history(
    admit_repo: Path, tmp_path: Path
) -> None:
    launched, _ = _refuse_once_then_launch(admit_repo, tmp_path)

    history = launched.get("admission_history")
    assert isinstance(history, list) and history, launched.keys()
    record = history[-1]
    assert str(record.get("failure") or "")
    assert record.get("failure_at")
    assert record.get("failure_attempts") == 1
    assert record.get("retry_at")


def test_the_launched_record_carries_every_key_a_direct_dispatch_writes(
    admit_repo: Path, tmp_path: Path
) -> None:
    launched, _ = _refuse_once_then_launch(admit_repo, tmp_path)
    direct = _direct_dispatch_pointer(admit_repo, tmp_path)

    missing = sorted(set(direct) - set(launched))
    assert not missing, f"a queued launch is missing direct-dispatch keys: {missing}"


def _direct_dispatch_pointer(repository: Path, tmp_path: Path) -> dict:
    """A node dispatched straight through the coordinator's own path."""
    from reckon.crew.dispatch import dispatch

    config, _ = _config_for()
    _prime_lane(tmp_path, config)
    node = TaskNode(
        id="direct-node",
        goal="dispatch straight",
        plan="plan-a",
        section="dispatch",
        done_when="a direct dispatch records its argv: assert 1 run",
        write_paths=["direct-node.txt"],
        time_budget="20m",
        spec_level="exact",
    )
    return dispatch(
        node=node,
        project=PROJECT,
        repo=repository,
        config=config,
        session=SESSION,
        launcher=_launcher([]),
        local=True,
        watch_override=True,
        run_id="r-direct",
    )


def test_the_launched_record_reads_as_a_promotable_run(
    admit_repo: Path, tmp_path: Path
) -> None:
    """What promotion reads from a pointer is present on an admitted launch.

    Promotion reads the run's stream through its recorded argv and audits the
    worktree the record names. A queued-shaped record supplies neither, which
    is the refusal the recorded defect produced ("records no argv and its
    backend is not in the supplied config").
    """
    from reckon.crew import dispatch_sessions, promotion

    launched, _ = _refuse_once_then_launch(admit_repo, tmp_path)
    config, _ = _config_for()
    run_id = str(launched["run_id"])

    settings = dispatch_sessions._backend_settings(launched, config)
    assert settings.get("command") and settings.get("launch") == "cli"
    assert Path(str(launched["worktree"])).is_dir()

    report = promotion.complete_dry_run(run_id, root=admit_repo, gate="passed")
    refusals = "\n".join(str(item) for item in report["refusals"])
    assert "records no argv" not in refusals, refusals
    assert "worktree" not in refusals.lower() or "no worktree" not in refusals.lower()


def test_the_queued_record_it_replaced_could_not_be_read_as_a_launch(
    admit_repo: Path, tmp_path: Path
) -> None:
    """The shape the defect produced is refused, so the arms above can fail."""
    from reckon.crew import dispatch_sessions

    config, _ = _config_for()
    _prime_lane(tmp_path, config)
    run_id = "r-queued-only"
    request = _queued_request(admit_repo, node_id="node-b", run_id=run_id)
    runs.queue_dispatch(request)
    queued = runs.read_pointer(run_id)
    queued["backend"] = "retired-lane"

    with pytest.raises(Exception, match="records no argv"):
        dispatch_sessions._backend_settings(queued, config)


def test_a_failed_admitted_launch_keeps_the_waits_it_recorded(
    admit_repo: Path,
) -> None:
    """Every restore a failed launch makes keeps the waits dispatch recorded.

    A request whose upstreams have landed is launched, and the launch fails.
    Both restore paths the admission pass can take — annotating the live
    pointer, and writing the snapshot back as a queued request — must keep the
    waits and the wait projects the launch read, and a later re-derivation of
    the refs from the plan must not drop them either.
    """
    run_id = "r-waiting-request"
    request = _queued_request(admit_repo, node_id="node-c", run_id=run_id)
    request["waits"] = ["run:r-upstream"]
    request["wait_projects"] = {"r-upstream": PROJECT}
    runs._write_json(runs.pointer_path(run_id), dict(request))

    queue_admission._record_queued_failure(
        request, "ScopeConflict: a peer holds the record path"
    )
    annotated = runs.read_pointer(run_id)
    assert annotated["wait_projects"] == {"r-upstream": PROJECT}
    assert annotated["waits"] == ["run:r-upstream"]

    queue_admission._restore_queued(
        request, "ScopeConflict: a peer holds the record path"
    )
    restored = runs.read_pointer(run_id)
    assert restored["phase"] == "queued"
    assert restored["wait_projects"] == {"r-upstream": PROJECT}
    assert restored["waits"] == ["run:r-upstream"]
    assert restored["failure_attempts"] >= 1

    queue_admission._rederive_waits(dict(restored))
    assert runs.read_pointer(run_id)["wait_projects"] == {"r-upstream": PROJECT}


def test_a_wait_naming_no_project_records_an_issue_and_an_obligation(
    admit_repo: Path,
) -> None:
    """A wait that can never resolve says why instead of stalling silently.

    The run it waits on has delivered, but the request recorded no project for
    the ref, so the landing that would satisfy it can never be read. The
    request keeps its waiting phase and gains an issue, and that issue is
    raised as an obligation rather than leaving the request PENDING forever.
    """
    run_id = "r-waitless-project"
    request = _queued_request(admit_repo, node_id="node-d", run_id=run_id)
    request["waits"] = ["run:r-delivered"]
    request["wait_projects"] = {}
    request["phase"] = queue_admission.AWAITING_UPSTREAM_PHASE
    runs._write_json(runs.pointer_path(run_id), request)
    delivered = runs.run_dir("r-delivered") / runs.DEFAULT_MANIFEST_NAME
    delivered.parent.mkdir(parents=True, exist_ok=True)
    delivered.write_text("status: complete\n", encoding="utf-8")

    outcome = queue_admission.advance_waiting_requests([runs.read_pointer(run_id)])

    pointer = runs.read_pointer(run_id)
    issues = list(pointer.get("upstream_obligations") or ())
    assert issues, pointer
    assert "names no project" in issues[0]
    assert outcome["obligations"], outcome
    assert pointer["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
