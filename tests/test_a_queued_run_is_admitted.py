"""The drainer admits queued dispatches: one launcher, fair order, nothing lost.

A queued local dispatch is launched by one loop, through the same dispatch path
a coordinator uses, whenever the lane's router grants its session room. A
launch that fails leaves the request queued rather than destroying it, two
drainers racing launch each run once, and a run the reaper later marks
``launch-failed`` is re-queued by the next pass with the failure recorded.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import dispatch_sessions, queue_admission, runs
from reckon.crew.dispatch_admission import LaneHeld, LanePaused
from reckon.crew.fleet_supervisor import declared_services, services_config_path

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


class LaunchStepError(RuntimeError):
    """Stands in for a launch step that fails after the pointer was published."""


def _run(repository: Path, *arguments: str) -> None:
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
    _run(repository, "init", "-q", "-b", "main")
    _run(repository, "config", "user.email", "worker@example.invalid")
    _run(repository, "config", "user.name", "Worker")
    _run(repository, "add", "--", "docs")
    _run(repository, "add", "--", "skills")
    _run(repository, "commit", "-q", "-m", "chore: seed checkout")
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


def _queue(
    repository: Path,
    *,
    node_id: str,
    run_id: str,
    lane_document: Path | None = None,
    session: str = SESSION,
) -> dict:
    from reckon.crew.node import TaskNode

    node = TaskNode(
        id=node_id,
        goal="admit this queued node",
        plan="plan-a",
        section="dispatch",
        done_when="pytest tests/test_a_queued_run_is_admitted.py reports passed",
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
        return os.getpid()

    return launch


def _failing_launcher(plan, *, log_path, stderr_path, prompt_path):
    raise FileNotFoundError("the harness executable is absent")


def test_one_slot_launches_the_highest_ranked_queued_run_only(
    admit_repo: Path, tmp_path: Path
) -> None:
    config, resolve = _config_for()
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=1)
    )
    runs.queue_dispatch(_queue(admit_repo, node_id="first-node", run_id="r-first"))
    runs.queue_dispatch(_queue(admit_repo, node_id="second-node", run_id="r-second"))
    recorded: list = []

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(recorded)
    )

    assert len(result["launched"]) == 1
    assert "r-second" in result["held"]
    assert len(recorded) == 1
    # The launch continues the request it was admitted from, so the queued
    # id is the one the launched run answers to and its pointer never leaves.
    assert result["launched"][0] == "r-first"
    assert runs.read_pointer(result["launched"][0])["phase"] != "queued"
    assert runs.read_pointer("r-second")["phase"] == "queued"


def test_two_admission_passes_launch_each_run_once(
    admit_repo: Path, tmp_path: Path
) -> None:
    config, resolve = _config_for()
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=2)
    )
    runs.queue_dispatch(_queue(admit_repo, node_id="node-a", run_id="r-a"))
    runs.queue_dispatch(
        _queue(admit_repo, node_id="node-b", run_id="r-b", session="coordinator-b")
    )
    recorded: list = []
    launcher = _launcher(recorded)

    def pass_once(_):
        return queue_admission.admit_once(config_for=resolve, launcher=launcher)

    with ThreadPoolExecutor(max_workers=2) as pool:
        passes = list(pool.map(pass_once, range(2)))

    launched = [run_id for one in passes for run_id in one["launched"]]
    assert len(launched) == 2
    assert len(set(launched)) == 2
    assert len(recorded) == 2


def test_a_paused_fleet_gate_launches_nothing(admit_repo: Path, tmp_path: Path) -> None:
    from click.testing import CliRunner

    from reckon import cli

    _, resolve = _config_for()
    runs.queue_dispatch(_queue(admit_repo, node_id="held", run_id="r-held"))
    paused = CliRunner().invoke(cli.main, ["crew", "gate", "--pause", "moving"])
    assert paused.exit_code == 0
    recorded: list = []

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(recorded)
    )

    assert result["action"] == "gated"
    assert result["launched"] == []
    assert recorded == []
    assert runs.read_pointer("r-held")["phase"] == "queued"


def test_a_failed_spawn_leaves_the_request_queued(
    admit_repo: Path, tmp_path: Path
) -> None:
    _, resolve = _config_for()
    runs.queue_dispatch(_queue(admit_repo, node_id="failed", run_id="r-failed"))

    result = queue_admission.admit_once(config_for=resolve, launcher=_failing_launcher)

    assert result["failed"] == ["r-failed"]
    pointer = runs.read_pointer("r-failed")
    assert pointer["phase"] == "queued"
    assert "the worker launch could not start" in pointer["failure"]
    assert pointer["failure_at"]


@pytest.mark.parametrize(
    ("error_type", "gate_state", "local"),
    [
        (LanePaused, "paused", True),
        (LanePaused, "unreadable", True),
        (LaneHeld, "full", False),
        (ValueError, "unexpected value", True),
        (KeyError, "missing key", True),
    ],
)
def test_a_refused_launch_keeps_its_queued_request(
    admit_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
    gate_state: str,
    local: bool,
) -> None:
    _, resolve = _config_for()
    request = _queue(admit_repo, node_id="refused", run_id="r-refused")
    request["local"] = local
    runs.queue_dispatch(request)

    def refuse(**kwargs):
        if issubclass(error_type, LanePaused):
            raise error_type({"state": gate_state, "detail": gate_state})
        raise error_type(gate_state)

    monkeypatch.setattr(
        importlib.import_module("reckon.crew.dispatch"), "dispatch", refuse
    )

    result = queue_admission.admit_once(config_for=resolve)

    pointer = runs.read_pointer("r-refused")
    assert pointer["phase"] == "queued"
    assert gate_state in pointer["failure"]
    assert pointer["failure_at"]
    assert "r-refused" in result["held"] + result["failed"]
    if not issubclass(error_type, LanePaused):
        assert gate_state in result["failure_detail"]["r-refused"]


def test_an_annotation_write_error_restores_the_request(
    admit_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, resolve = _config_for()
    runs.queue_dispatch(_queue(admit_repo, node_id="write-failed", run_id="r-write"))

    def refuse(**kwargs):
        raise ValueError("launch refused")

    def fail_write(*args, **kwargs):
        raise OSError("annotation storage unavailable")

    monkeypatch.setattr(
        importlib.import_module("reckon.crew.dispatch"), "dispatch", refuse
    )
    monkeypatch.setattr(runs, "_mutate_pointer", fail_write)

    result = queue_admission.admit_once(config_for=resolve)

    pointer = runs.read_pointer("r-write")
    assert pointer["phase"] == "queued"
    assert "launch refused" in pointer["failure"]
    assert result["failed"] == ["r-write"]


def test_a_restore_write_error_is_reported_without_ending_the_pass(
    admit_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, resolve = _config_for()
    runs.queue_dispatch(_queue(admit_repo, node_id="write-failed", run_id="r-write"))

    def refuse(**kwargs):
        raise ValueError("launch refused")

    def fail_write(*args, **kwargs):
        raise OSError("storage unavailable")

    monkeypatch.setattr(
        importlib.import_module("reckon.crew.dispatch"), "dispatch", refuse
    )
    monkeypatch.setattr(runs, "_mutate_pointer", fail_write)
    monkeypatch.setattr(queue_admission, "_restore_queued", fail_write)

    result = queue_admission.admit_once(config_for=resolve)

    assert result["failed"] == ["r-write"]
    assert "storage unavailable" in result["restore_failure_detail"]["r-write"]


@pytest.mark.parametrize("returns_queued", [False, True])
def test_a_requeued_hold_keeps_the_newer_pointer(
    admit_repo: Path, monkeypatch: pytest.MonkeyPatch, returns_queued: bool
) -> None:
    config, resolve = _config_for()
    lane = _lane_document(admit_repo.parent, worker_slots=1)
    config["backends"]["clive"]["lane_document"] = str(lane)
    request = _queue(admit_repo, node_id="held", run_id="r-held")
    request["hold"]["reason"] = "older allowance"
    runs.queue_dispatch(request)
    launch = queue_admission._launch_queued
    newer_reasons: list[str] = []

    def requeue(pointer, **kwargs):
        document = json.loads(lane.read_text(encoding="utf-8"))
        document["admission"]["worker_slots"] = 0
        document["admission"]["sessions"][SESSION]["worker_slots"] = 0
        lane.write_text(json.dumps(document), encoding="utf-8")
        try:
            return launch(pointer, **kwargs)
        except LaneHeld as held:
            newer_reasons.append(str(held.gate["reason"]))
            # The real dispatch has re-queued the original request. A stale
            # reported id must not move failure accounting to a new pointer.
            if returns_queued:
                return {**runs.read_pointer("r-held"), "run_id": "r-requeued"}
            held.queued["run_id"] = "r-requeued"
            raise

    monkeypatch.setattr(queue_admission, "_launch_queued", requeue)

    result = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))

    pointer = runs.read_pointer("r-held")
    assert pointer["phase"] == "queued"
    assert newer_reasons
    assert pointer["hold"]["reason"] == newer_reasons[0]
    assert pointer["hold"]["reason"] != "older allowance"
    failure = "did not return a launched record" if returns_queued else newer_reasons[0]
    assert failure in pointer["failure"]
    assert pointer["failure_at"]
    assert not runs.pointer_path("r-requeued").exists()
    assert [row["run_id"] for row in runs.list_live(project=PROJECT)] == ["r-held"]
    assert result["held" if not returns_queued else "failed"] == ["r-held"]
    assert not result.get("superseded")


@pytest.mark.parametrize("returned", [None, {"run_id": "r-held", "phase": "queued"}])
def test_a_dispatch_without_a_launch_keeps_the_request(
    admit_repo: Path, monkeypatch: pytest.MonkeyPatch, returned: dict | None
) -> None:
    _, resolve = _config_for()
    runs.queue_dispatch(_queue(admit_repo, node_id="held", run_id="r-held"))
    monkeypatch.setattr(
        importlib.import_module("reckon.crew.dispatch"),
        "dispatch",
        lambda **kwargs: returned,
    )

    result = queue_admission.admit_once(config_for=resolve)

    pointer = runs.read_pointer("r-held")
    assert pointer["phase"] == "queued"
    assert "did not return a launched record" in pointer["failure"]
    assert pointer["failure_at"]
    assert result["failed"] == ["r-held"]


def test_a_run_marked_launch_failed_is_requeued(
    admit_repo: Path, tmp_path: Path
) -> None:
    config, resolve = _config_for()
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=0)
    )
    pointer = _queue(admit_repo, node_id="reaped", run_id="r-reaped")
    pointer["phase"] = "launch-failed"
    pointer["admit_request"] = _queue(admit_repo, node_id="reaped", run_id="r-reaped")
    pointer["launch_failures"] = [
        {"kind": "launch-failed", "recorded_at": "2026-10-12T05:00:00+00:00"}
    ]
    runs._write_json(runs.pointer_path("r-reaped"), pointer)
    recorded: list = []

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(recorded), lock=True
    )

    assert "r-reaped" in result["requeued"]
    restored = runs.read_pointer("r-reaped")
    assert restored["phase"] == "queued"
    assert restored["failure"]["kind"] == "launch-failed"
    assert restored["failure_at"]


def test_the_services_configuration_declares_the_admit_verb(
    admit_repo: Path,
) -> None:
    services = services_config_path()
    services.parent.mkdir(parents=True, exist_ok=True)
    services.write_text(
        json.dumps({"admit": ["reckon", "crew", "admit", "--lane", "clive"]}),
        encoding="utf-8",
    )

    declared = declared_services()

    assert declared is not None
    assert declared["admit"][:3] == ["reckon", "crew", "admit"]


def _working_launcher(plan, *, log_path, stderr_path, prompt_path):
    """Stand in for the worker: write the stream a working fold reads."""
    Path(log_path).write_text('{"type":"turn.started"}\n', encoding="utf-8")
    return os.getpid()


def _queued_request(
    repository: Path,
    *,
    node_id: str,
    run_id: str,
    manifest_path: str | None = None,
    write_paths: list[str] | None = None,
    brief_path: str | None = None,
    brief: str = "",
    role: str = "implement",
) -> dict:
    """A queued pointer whose node carries the defaults a dispatch stored.

    The queued pointer records the node exactly as ``dispatch_plan`` resolved
    it for the queued run, so a default it derived there — a manifest path or a
    write path inside the queued run's own directory — is the input the
    admission pass reads.
    """
    request = _queue(repository, node_id=node_id, run_id=run_id)
    node = request["node"]
    node["role"] = role
    if manifest_path is not None:
        node["manifest_path"] = manifest_path
    if write_paths is not None:
        node["write_paths"] = list(write_paths)
    if brief_path is not None:
        node["brief_path"] = brief_path
    node["brief"] = brief
    return request


def _launched_pointer(result: dict) -> dict:
    assert result["launched"], json.dumps(result, default=str)
    return runs.read_pointer(result["launched"][0])


def test_a_queued_launch_takes_its_manifest_from_its_launched_run(
    admit_repo: Path,
) -> None:
    _, resolve = _config_for()
    queued_dir = runs.run_dir("r-queued-manifest")
    request = _queued_request(
        admit_repo,
        node_id="queued-manifest",
        run_id="r-queued-manifest",
        manifest_path=str(queued_dir / "manifest.md"),
    )
    runs.queue_dispatch(request)

    result = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))

    launched = _launched_pointer(result)
    launched_id = str(launched["run_id"])
    launched_dir = runs.run_dir(launched_id)
    assert launched["manifest_path"] == str(launched_dir / "manifest.md")
    prompt = Path(launched["prompt_path"]).read_text(encoding="utf-8")
    assert str(launched_dir) in prompt
    # One run id means one run directory: the request and its launch share it,
    # so the manifest the launch derives is its own.
    assert launched_dir == queued_dir


def test_a_queued_launch_keeps_a_manifest_named_outside_a_run_directory(
    admit_repo: Path, tmp_path: Path
) -> None:
    _, resolve = _config_for()
    named = tmp_path / "named" / "manifest.md"
    request = _queued_request(
        admit_repo,
        node_id="named-manifest",
        run_id="r-named-manifest",
        manifest_path=str(named),
    )
    runs.queue_dispatch(request)

    result = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))

    assert _launched_pointer(result)["manifest_path"] == str(named)


def test_a_queued_review_keeps_its_brief_path(
    admit_repo: Path,
) -> None:
    config, resolve = _config_for()
    config["roles"]["review"] = {}
    queued_dir = runs.run_dir("r-queued-brief")
    brief = queued_dir / "brief.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text("review this section\n", encoding="utf-8")
    request = _queued_request(
        admit_repo,
        node_id="queued-brief",
        run_id="r-queued-brief",
        brief_path=str(brief),
        brief=str(brief),
        role="review",
    )
    runs.queue_dispatch(request)

    result = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))

    assert _launched_pointer(result)["node"]["brief"] == str(brief)


def test_a_queued_launch_derives_write_paths_under_its_launched_run(
    admit_repo: Path,
) -> None:
    config, resolve = _config_for()
    config["backends"]["clive"]["write_paths"] = ["scratch"]
    queued_dir = runs.run_dir("r-queued-writes")
    request = _queued_request(
        admit_repo,
        node_id="queued-writes",
        run_id="r-queued-writes",
        write_paths=[str((queued_dir / "scratch").resolve())],
    )
    runs.queue_dispatch(request)

    result = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))

    launched = _launched_pointer(result)
    launched_dir = runs.run_dir(str(launched["run_id"])).resolve()
    paths = [str(path) for path in launched["node"]["write_paths"]]
    # The default write path the plan derived inside the run directory is the
    # launched run's own, which is the directory the request was queued in.
    assert str((launched_dir / "scratch").resolve()) in paths
    # One run id means one run directory, so the derived write path is under
    # the queued request directory because that is the launched run's own.
    assert launched_dir == queued_dir.resolve()


def test_a_launched_run_leaves_queued_for_starting_then_working(
    admit_repo: Path, tmp_path: Path
) -> None:
    config, resolve = _config_for()
    runs.queue_dispatch(_queue(admit_repo, node_id="phased", run_id="r-phased"))
    observed = ["queued"]

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_working_launcher, lane="clive"
    )
    run_id = result["launched"][0]
    observed.append(runs.read_pointer(run_id)["phase"])

    # The follower folds the worker's own stream back into the pointer.
    folded = dispatch_sessions.observe(run_id, config=config)
    observed.append(folded["phase"])

    assert observed == ["queued", "starting", "working"]
    assert run_id == "r-phased"
