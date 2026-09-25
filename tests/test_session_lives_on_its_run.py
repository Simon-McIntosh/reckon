"""Captured sessions belong to run records and follow their owning harness."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew.dispatch import change_lane, resume_plan

FIRST_SESSION = "266f04b2-75c1-43f0-aa27-0d72a67b340f"
SECOND_SESSION = "366f04b2-75c1-43f0-aa27-0d72a67b340f"
CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "first-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "session_reuse": True,
        },
        "beta": {
            "launch": "cli",
            "command": "claude",
            "model": "second-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "session_reuse": True,
        },
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def repo(tmp_path, monkeypatch, isolated_reckon_home):
    outside = sorted(isolated_reckon_home.rglob("*"))
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    root = tmp_path / "repo"
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills/reckon-build/scripts/worktree_fleet.py"
    (scripts / "worktree_fleet.py").write_text(source.read_text())
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "session-routing.html").write_text(
        '<!doctype html><html><head><meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="session-routing">'
        '</head><body><h2 id="capture">Session capture</h2></body></html>'
    )
    (root / "target.py").write_text("value = 1\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "target.py", "skills", "docs/plans/session-routing.html"],
        [
            "commit",
            "-q",
            "-m",
            "chore: seed",
            "-m",
            "Create a temporary test repository.",
        ],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"proj": str(root / "docs")}))
    ledger.register_member("proj", "worker", harness="alpha", root=root)
    yield root
    assert sorted(isolated_reckon_home.rglob("*")) == outside


def _launcher(session_id):
    def launch(plan, *, log_path, stderr_path, prompt_path):
        if plan.dialect == "codex":
            events = [
                {"type": "thread.started", "thread_id": session_id},
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                },
            ]
        else:
            events = [
                {"type": "system", "subtype": "init", "session_id": session_id},
                {
                    "type": "result",
                    "subtype": "success",
                    "result": "done",
                    "is_error": False,
                },
            ]
        Path(log_path).write_text("".join(json.dumps(event) + "\n" for event in events))
        return 999999999

    return launch


def _dispatch(repo, *, node_id="capture", backend="alpha"):
    return crew.dispatch(
        node=crew.TaskNode(
            id=node_id,
            goal="record a worker session on its run",
            plan="session-routing",
            section="capture",
            spec_level="guided",
            done_when="pytest tests/test_session_lives_on_its_run.py passes",
            write_paths=["target.py"],
            time_budget="20m",
        ),
        project="proj",
        repo=repo,
        config={**CONFIG, "default_backend": backend},
        session=f"coordinator-{len(ledger.runs('proj', root=repo)) + len(crew.list_live())}",
        member="worker",
        launcher=_launcher(FIRST_SESSION),
    )


def _members(repo):
    roster = json.loads((repo / "docs/state/proj/crew.json").read_bytes())
    return json.dumps(roster["data"]["members"], separators=(",", ":")).encode()


@pytest.mark.parametrize("legacy_session", [False, True])
def test_dispatch_and_observe_leave_roster_members_byte_identical(repo, legacy_session):
    if legacy_session:
        data, version = ledger.load("proj", root=repo)
        data["members"][0].update(
            session_id="unrelated-session",
            session_model="unrelated-model",
            sessions={"unrelated-configuration": "unrelated-session"},
            session_owners={"unrelated-configuration": "unrelated-coordinator"},
        )
        ledger.write("proj", data, version, root=repo)
    before = _members(repo)
    raw_before = (repo / "docs/state/proj/crew.json").read_bytes()
    dispatched = _dispatch(repo)
    assert _members(repo) == before
    observed = crew.observe(dispatched["run_id"], config=CONFIG)
    assert observed["session_id"] == FIRST_SESSION
    assert observed["session_harness"] == "codex"
    assert crew.read_pointer(dispatched["run_id"])["session_id"] == FIRST_SESSION
    assert _members(repo) == before
    assert (repo / "docs/state/proj/crew.json").read_bytes() == raw_before
    crew.observe(dispatched["run_id"], config=CONFIG)
    assert _members(repo) == before


@pytest.mark.parametrize("observe_first", [False, True])
def test_promoted_session_resumes_same_task_without_roster_write(repo, observe_first):
    before = _members(repo)
    dispatched = _dispatch(repo)
    observed = (
        crew.observe(dispatched["run_id"], config=CONFIG)
        if observe_first
        else dispatched
    )
    Path(observed["manifest_path"]).write_text("node: capture\nstatus: complete\n")
    promoted = crew.complete(
        observed["run_id"],
        gate="passed",
        commits=[observed["base_sha"]],
        review_waiver="synthetic session-persistence fixture with no product edits",
    )
    assert promoted["pointer_removed"] is True
    row = ledger.runs("proj", root=repo)[0]
    assert row["session_id"] == FIRST_SESSION
    assert row["session_harness"] == "codex"
    assert row["session_model"] == "first-model"
    again = _dispatch(repo)
    assert again["argv"][again["argv"].index("resume") + 1] == FIRST_SESSION
    assert _members(repo) == before


def test_redispatch_resume_uses_the_new_harness_session(repo):
    dispatched = _dispatch(repo)
    crew.observe(dispatched["run_id"], config=CONFIG)
    moved = change_lane(
        dispatched["run_id"],
        "beta",
        "continue on another harness",
        config=CONFIG,
        launcher=_launcher(SECOND_SESSION),
    )
    assert moved["lane_change"]["session"] == "fresh"
    assert FIRST_SESSION not in moved["argv"]
    plan = resume_plan(dispatched["run_id"], "continue the same task", config=CONFIG)
    assert plan.dialect == "claude"
    assert plan.argv[plan.argv.index("--resume") + 1] == SECOND_SESSION
    assert FIRST_SESSION not in plan.argv
    observed = crew.observe(dispatched["run_id"], config=CONFIG)
    assert observed["session_id"] == SECOND_SESSION
    returned = change_lane(
        dispatched["run_id"],
        "alpha",
        "return to the first harness",
        config=CONFIG,
        launcher=_launcher(FIRST_SESSION),
    )
    assert returned["backend"] == "alpha"
    assert returned["lane_change"]["session"] == "fresh"
    assert SECOND_SESSION not in returned["argv"]


def test_resume_starts_fresh_when_the_current_harness_has_no_session(repo):
    dispatched = _dispatch(repo)
    crew.observe(dispatched["run_id"], config=CONFIG)
    moved = change_lane(
        dispatched["run_id"],
        "beta",
        "continue on another harness",
        config=CONFIG,
        launcher=lambda *args, **kwargs: 999999999,
    )
    plan = resume_plan(dispatched["run_id"], "continue the same task", config=CONFIG)
    assert plan.dialect == "claude"
    assert FIRST_SESSION not in plan.argv
    assert "--resume" not in plan.argv
    recorded = crew.read_pointer(moved["run_id"])
    assert recorded["session_withheld"]["reason"]
    assert recorded["session_id"] is None
    resumed = crew.record_resumption(
        moved["run_id"],
        pid=999999999,
        turn=1,
        log_path=str(Path(moved["log_path"]).parent / "resume-1.jsonl"),
        stderr_path=str(Path(moved["log_path"]).parent / "resume-1.stderr.log"),
    )
    assert resumed["session_resumed"] is False


def test_a_session_owned_by_another_harness_is_withheld_from_same_task_dispatch(repo):
    first = _dispatch(repo)
    crew.observe(first["run_id"], config=CONFIG)
    second = _dispatch(repo, backend="beta")
    assert second["dialect"] == "claude"
    assert FIRST_SESSION not in second["argv"]
    assert "--resume" not in second["argv"]
    assert second["session_withheld"]["reason"]
