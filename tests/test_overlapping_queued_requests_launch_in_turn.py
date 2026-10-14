"""Overlapping queued requests launch in rank order instead of refusing each other.

A queued request holds no worker and no worktree, so it must fence nothing:
``claim_disposition`` reads the ``queued`` phase as non-binding, which keeps the
dispatch scope check and the ``crew scopes`` view in step. On top of that the
admission pass holds a later request behind an earlier-ranked one whose write
paths overlap, for no longer than the queue's starvation age, so the order among
overlapping requests is the queue's order rather than whichever dispatch the
scope check happened to see first.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew_dispatch_commands
from reckon.crew import queue_admission, runs
from reckon.crew.node import TaskNode
from tests import test_dispatch_names_its_backend as backend_tests

PROJECT = "proj"
LANE = "clive"
SESSION_A = "coordinator-a"
SESSION_B = "coordinator-b"
SHARED = "reckon/crew/shared_path.py"
START = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)

CONFIG = {
    "default_backend": LANE,
    "local_backend": LANE,
    "backends": {
        LANE: {
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


def _seed_checkout(tmp_path: Path, project: str) -> Path:
    """A minimal mounted checkout for ``project``, committed and ready to dispatch."""
    repository = tmp_path / f"checkout-{project}"
    scripts = repository / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills" / "reckon-build" / "scripts"
    (scripts / "worktree_fleet.py").write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    state = repository / "docs" / "state" / project
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": project, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plan = repository / "docs" / "plans" / "plan-a.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    quote = '"'
    plan.write_text(
        "\n".join(
            [
                "<!doctype html>",
                f"<meta name={quote}docs-project{quote} content={quote}{project}{quote}>",
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
    return repository


def _mount(config_home: Path, *repositories: Path) -> None:
    """Publish each checkout's docs directory as the mount for its own project."""
    (config_home / "mounts.json").write_text(
        json.dumps(
            {
                path.name.removeprefix("checkout-"): str(path / "docs")
                for path in repositories
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def admit_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repository = _seed_checkout(tmp_path, PROJECT)
    _mount(config_home, repository)
    return repository


@pytest.fixture()
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return backend_tests.dispatch_repo.__wrapped__(tmp_path, monkeypatch)


def _lane_document(tmp_path: Path, *, slots: dict[str, float]) -> Path:
    lane = tmp_path / "lane.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": max(sum(slots.values()), 1.0),
                "admission": {
                    "worker_slots": sum(slots.values()),
                    "observed_seconds": 900,
                    "sessions": {
                        name: {"worker_slots": value} for name, value in slots.items()
                    },
                    "verdict": "full",
                },
            }
        ),
        encoding="utf-8",
    )
    return lane


def _config(lane_document: Path) -> dict:
    config = json.loads(json.dumps(CONFIG))
    config["backends"][LANE]["lane_document"] = str(lane_document)
    return config


def _queue(
    repository: Path,
    *,
    node_id: str,
    run_id: str,
    session: str,
    write_paths: list[str],
    queued_at: str,
    project: str = PROJECT,
) -> dict:
    node = TaskNode(
        id=node_id,
        goal="admit an overlapping queued node",
        plan="plan-a",
        section="dispatch",
        done_when="pytest tests/test_overlapping_queued_requests_launch_in_turn.py passes",
        write_paths=list(write_paths),
        time_budget="20m",
        spec_level="exact",
    )
    return {
        "run_id": run_id,
        "project": project,
        "session": session,
        "repo": str(repository),
        "node": node.as_dict(),
        "backend": LANE,
        "launch": "cli",
        "local": True,
        "phase": "queued",
        "queued_at": queued_at,
        "created_at": queued_at,
        "hold": {"held": True},
        "dispatch_options": {
            "project": project,
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


def _payload(result) -> dict:
    return json.loads(result.output.splitlines()[0])


# --- Case 1: the earlier-ranked request launches, the later stays queued -------


def test_overlapping_queued_requests_launch_the_earlier_and_keep_the_later(
    admit_repo: Path, tmp_path: Path
) -> None:
    lane = _lane_document(tmp_path, slots={SESSION_A: 1, SESSION_B: 1})
    config = _config(lane)
    runs.queue_dispatch(
        _queue(
            admit_repo,
            node_id="earlier",
            run_id="r-earlier",
            session=SESSION_A,
            write_paths=[SHARED],
            queued_at="2026-10-14T11:58:00+00:00",
        )
    )
    runs.queue_dispatch(
        _queue(
            admit_repo,
            node_id="later",
            run_id="r-later",
            session=SESSION_B,
            write_paths=[SHARED],
            queued_at="2026-10-14T11:59:00+00:00",
        )
    )
    recorded: list = []

    result = queue_admission.admit_once(
        now=START, config_for=lambda _pointer: config, launcher=_launcher(recorded)
    )

    assert len(result["launched"]) == 1
    launched = runs.read_pointer(result["launched"][0])
    assert launched["node"]["id"] == "earlier"
    assert "r-later" in result["held"]
    later = runs.read_pointer("r-later")
    assert later["phase"] == "queued"
    assert later["run_id"] == "r-later"
    assert not later.get("failure")


# --- Case 2: the earlier's session has no slot, the pre-check keeps the order --


def test_a_later_request_waits_behind_an_earlier_whose_session_has_no_slot(
    admit_repo: Path, tmp_path: Path
) -> None:
    lane = _lane_document(tmp_path, slots={SESSION_A: 0, SESSION_B: 1})
    config = _config(lane)
    runs.queue_dispatch(
        _queue(
            admit_repo,
            node_id="earlier",
            run_id="r-earlier",
            session=SESSION_A,
            write_paths=[SHARED],
            queued_at="2026-10-14T11:58:00+00:00",
        )
    )
    runs.queue_dispatch(
        _queue(
            admit_repo,
            node_id="later",
            run_id="r-later",
            session=SESSION_B,
            write_paths=[SHARED],
            queued_at="2026-10-14T11:59:00+00:00",
        )
    )
    recorded: list = []

    result = queue_admission.admit_once(
        now=START, config_for=lambda _pointer: config, launcher=_launcher(recorded)
    )

    assert result["launched"] == []
    assert "r-earlier" in result["held"]
    assert "r-later" in result["held"]
    assert recorded == []
    later = runs.read_pointer("r-later")
    assert later["phase"] == "queued"
    assert not later.get("failure")
    assert runs.read_pointer("r-earlier")["phase"] == "queued"


# --- Case 3: once the earlier is promoted the later launches ------------------


def test_the_later_request_launches_once_the_earlier_is_promoted(
    admit_repo: Path, tmp_path: Path
) -> None:
    lane = _lane_document(tmp_path, slots={SESSION_A: 1, SESSION_B: 1})
    config = _config(lane)

    def resolve(_pointer):
        return config

    runs.queue_dispatch(
        _queue(
            admit_repo,
            node_id="earlier",
            run_id="r-earlier",
            session=SESSION_A,
            write_paths=[SHARED],
            queued_at="2026-10-14T11:58:00+00:00",
        )
    )
    runs.queue_dispatch(
        _queue(
            admit_repo,
            node_id="later",
            run_id="r-later",
            session=SESSION_B,
            write_paths=[SHARED],
            queued_at="2026-10-14T11:59:00+00:00",
        )
    )
    first = queue_admission.admit_once(
        now=START, config_for=resolve, launcher=_launcher([])
    )
    earlier_id = first["launched"][0]

    # The earlier run now holds the path as a working claim, so the later
    # request is refused on it and put back with the conflict recorded.
    queue_admission.admit_once(
        now=START + timedelta(minutes=1), config_for=resolve, launcher=_launcher([])
    )
    later = runs.read_pointer("r-later")
    assert later["phase"] == "queued"
    assert later.get("failure")
    assert later.get("retry_at")

    # The earlier run is promoted (its pointer leaves), and the later request
    # launches on the first pass after its admission back-off expires.
    runs.pointer_path(earlier_id).unlink()
    released = queue_admission.admit_once(
        now=START + timedelta(minutes=15), config_for=resolve, launcher=_launcher([])
    )

    assert len(released["launched"]) == 1
    assert runs.read_pointer(released["launched"][0])["node"]["id"] == "later"


# --- Case 4: the starvation age releases a request held without end -----------


def test_the_pre_check_releases_the_later_request_after_the_starvation_age(
    admit_repo: Path, tmp_path: Path
) -> None:
    lane = _lane_document(tmp_path, slots={SESSION_A: 0, SESSION_B: 1})
    config = _config(lane)
    runs.queue_dispatch(
        _queue(
            admit_repo,
            node_id="earlier",
            run_id="r-earlier",
            session=SESSION_A,
            write_paths=[SHARED],
            queued_at="2026-10-14T11:28:00+00:00",
        )
    )
    runs.queue_dispatch(
        _queue(
            admit_repo,
            node_id="later",
            run_id="r-later",
            session=SESSION_B,
            write_paths=[SHARED],
            queued_at="2026-10-14T11:29:30+00:00",
        )
    )

    result = queue_admission.admit_once(
        now=START, config_for=lambda _pointer: config, launcher=_launcher([])
    )

    assert len(result["launched"]) == 1
    assert runs.read_pointer(result["launched"][0])["node"]["id"] == "later"
    assert runs.read_pointer("r-earlier")["phase"] == "queued"


# --- Case 5: a new dispatch over only a queued claim, and the session cap -----


def _configure(
    repo: Path, monkeypatch: pytest.MonkeyPatch, *, session_slots: float
) -> dict:
    lane = repo.parent / "lane.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": 4,
                "admission": {
                    "worker_slots": session_slots,
                    "observed_seconds": 900,
                    "sessions": {"session": {"worker_slots": session_slots}},
                },
            }
        ),
        encoding="utf-8",
    )
    config = json.loads(json.dumps(backend_tests.CONFIG))
    config["local_backend"] = LANE
    config["backends"][LANE]["lane_document"] = str(lane)
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    return config


def _dispatch(repo: Path, node: str, *extra: str, paths=("result.json",)):
    arguments = backend_tests._arguments(repo, node=node, dry_run=False)
    index = arguments.index("--write-path")
    arguments[index + 1 : index + 2] = list(paths)
    return CliRunner().invoke(cli.main, [*arguments, "--local", "--no-watch", *extra])


def test_a_new_dispatch_over_a_queued_claim_queues_then_launches(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(dispatch_repo, monkeypatch, session_slots=0)
    first = _dispatch(dispatch_repo, "node-one", paths=(SHARED,))
    assert first.exit_code == 0, first.output
    assert _payload(first)["queued"] is True

    # A second node over the queued request's path queues too: the queued claim
    # fences nothing, so neither request refuses the other.
    second = _dispatch(dispatch_repo, "node-two", paths=(SHARED,))
    assert second.exit_code == 0, second.output
    assert _payload(second)["queued"] is True
    assert _payload(second).get("error") != "scope-conflict"

    # With room, a third node over the same path launches like any dispatch.
    _configure(dispatch_repo, monkeypatch, session_slots=4)
    third = _dispatch(dispatch_repo, "node-three", paths=(SHARED,))
    assert third.exit_code == 0, third.output
    assert _payload(third)["phase"] == "starting"
    assert {pointer["node"]["id"] for pointer in runs.list_live(project=PROJECT)} >= {
        "node-one",
        "node-two",
    }


def test_a_new_dispatch_over_a_queued_claim_at_the_session_cap_is_refused(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The lane document publishes no fair share, so the session's cap is the
    # floor of two queued requests.
    _configure(dispatch_repo, monkeypatch, session_slots=0)
    first = _dispatch(dispatch_repo, "node-one", paths=(SHARED,))
    second = _dispatch(dispatch_repo, "node-two", paths=(SHARED,))
    assert first.exit_code == 0 and second.exit_code == 0

    third = _dispatch(dispatch_repo, "node-three", paths=(SHARED,))

    assert third.exit_code == 75, third.output
    assert _payload(third)["error"] == "queue-full"
    assert {pointer["node"]["id"] for pointer in runs.list_live(project=PROJECT)} == {
        "node-one",
        "node-two",
    }


# --- Case 6: a working or starting claim still refuses a dispatch -------------


def _live(repository: Path, run_id: str, *, phase: str, write_paths: list[str]) -> None:
    node = TaskNode(
        id="holder",
        goal="hold the path",
        plan="plan-a",
        section="dispatch",
        done_when="pytest reports the case passed",
        write_paths=list(write_paths),
        time_budget="20m",
        spec_level="exact",
    )
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": "session",
        "repo": str(repository),
        "phase": phase,
        "node": node.as_dict(),
    }
    runs._write_json(runs.pointer_path(run_id), record)


@pytest.mark.parametrize("phase", ["working", "starting"])
def test_a_dispatch_over_a_working_or_starting_claim_is_refused(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    _configure(dispatch_repo, monkeypatch, session_slots=4)
    _live(dispatch_repo, "r-holder", phase=phase, write_paths=[SHARED])

    refused = _dispatch(dispatch_repo, "node-three", paths=(SHARED,))

    assert refused.exit_code == 7, refused.output
    assert _payload(refused)["error"] == "scope-conflict"


# --- Case 7: the scopes view agrees with the dispatch scope check -------------


def test_the_scopes_view_reports_a_queued_claim_as_non_binding(
    admit_repo: Path,
) -> None:
    runs.queue_dispatch(
        _queue(
            admit_repo,
            node_id="queued-node",
            run_id="r-queued",
            session=SESSION_A,
            write_paths=[SHARED],
            queued_at="2026-10-14T11:58:00+00:00",
        )
    )
    _live(admit_repo, "r-starting", phase="starting", write_paths=[SHARED])

    claims = {
        claim["run_id"]: claim for claim in runs.scope_claims(PROJECT, admit_repo)
    }

    assert claims["r-queued"]["binding"] is False
    assert claims["r-starting"]["binding"] is True


# --- Case 8: one relative path in two repositories is not an overlap ----------


PROJECT_B = "proj-b"


def test_requests_in_different_repositories_over_one_relative_path_do_not_hold(
    admit_repo: Path, tmp_path: Path
) -> None:
    """A shared spelling is not a shared file: the pre-check compares repositories.

    The pass drains every project's queued requests on the lane, so a comparison
    over bare repository-relative paths would hold a request in one repository
    behind an unrelated one in another whose declared path merely reads the same.
    """
    config_home = Path(os.environ["RECKON_HOME"])
    second = _seed_checkout(tmp_path, PROJECT_B)
    _mount(config_home, admit_repo, second)
    lane = _lane_document(tmp_path, slots={SESSION_A: 1, SESSION_B: 1})
    config = _config(lane)
    runs.queue_dispatch(
        _queue(
            admit_repo,
            project=PROJECT,
            node_id="first-repo",
            run_id="r-first-repo",
            session=SESSION_A,
            write_paths=[SHARED],
            queued_at="2026-10-14T11:58:00+00:00",
        )
    )
    runs.queue_dispatch(
        _queue(
            second,
            project=PROJECT_B,
            node_id="second-repo",
            run_id="r-second-repo",
            session=SESSION_B,
            write_paths=[SHARED],
            queued_at="2026-10-14T11:59:00+00:00",
        )
    )

    result = queue_admission.admit_once(
        now=START, config_for=lambda _pointer: config, launcher=_launcher([])
    )

    launched = {
        runs.read_pointer(run_id)["node"]["id"] for run_id in result["launched"]
    }
    assert launched == {"first-repo", "second-repo"}
    assert not result["held"]
