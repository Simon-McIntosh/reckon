"""A run reaching scoring runs the review dispatch it has already composed.

A silent skip and a duplicate dispatch are both wrong, and not equally. A
contained duplicate is visible, two reviews for one source run; a skip leaves
the run in ``scoring`` looking identical to a review that ran and wrote
nothing, which is the ambiguity this reflex exists to remove. So each assertion
here is argued inside out, with the negative half asserted first: a run whose
review already exists dispatches nothing, a run with no review and a serving
local lane has a review in flight with no coordinator issuing a command, a run
whose lane is unavailable records the lane's own state as its reason and
dispatches once the negative half stays asserted, and an admission refusal that
would refuse a manual dispatch still refuses the automatic one.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import time
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery, runs
from reckon.crew import review as review_module
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS

# The reflex is gated by the watch admission, so these tests arm the producer
# the gate reads rather than accepting the suite-wide waiver, which would let
# every dispatch through and prove nothing about the refusal.
pytestmark = pytest.mark.arms_watch_producer


VALID_REVIEW = (
    "SCORE goal_fidelity: 18\n"
    "SCORE evidence: 15\n"
    "SCORE scope_discipline: 17\n"
    "SCORE durability: 19\n"
    "SCORE fit: 16\n"
)


CONFIG = {
    "default_backend": "alpha",
    "local_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def isolated_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(source.read_text(encoding="utf-8"))
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s2">A finished run dispatches its own review</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        '{"sample": "' + str(repo / "docs") + '"}', encoding="utf-8"
    )

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def prepare_worktree(_repo: Path, session: str, node: str, base: str) -> dict:
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": base, "base_sha": base_sha}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    return config_home, repo


def _node(config_home: Path, name: str) -> crew.TaskNode:
    return crew.TaskNode(
        id=f"node-{name}",
        goal="record one producer-backed dispatch",
        plan="fixture",
        section="arming",
        spec_level="guided",
        done_when="pytest reports one live watcher for every dispatch",
        write_paths=[f"src/{name}.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{name}.md"),
    )


def _dispatch(config_home: Path, repo: Path, name: str, **kwargs) -> dict:
    session = kwargs.pop("session", f"session-{name}")
    return crew.dispatch(
        node=_node(config_home, name),
        project="sample",
        repo=repo,
        config=CONFIG,
        session=session,
        launcher=lambda *args, **kwargs: os.getpid(),
        watch_required=True,
        **kwargs,
    )


def _attached(config_home: Path, repo: Path, name: str, **kwargs) -> dict:
    """Dispatch with this session's delivery registered, as a coordinator does."""
    session = kwargs.pop("session", f"session-{name}")
    with runs.follower_claim("sample", session, delivery="stream"):
        return _dispatch(config_home, repo, name, session=session, **kwargs)


def _wait_for_stopped_producer() -> None:
    deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
    while time.monotonic() < deadline:
        if not crew.watch_state("sample")["watcher_live"]:
            return
        time.sleep(0.05)
    pytest.fail("watch producer did not release its seat")


def _scoring_pointer(config_home: Path, repo: Path, run_id: str) -> dict:
    manifest = config_home / "manifests" / (run_id + ".md")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: " + run_id + "\nstatus: complete\ncommits: " + run_id + "\n",
        encoding="utf-8",
    )
    record = {
        "run_id": run_id,
        "project": "sample",
        "repo": str(repo),
        "node": {"id": run_id, "plan": "fixture", "section": "s2"},
        "backend": "alpha",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def test_a_run_whose_review_already_parses_dispatches_nothing(
    isolated_project: tuple[Path, Path],
) -> None:
    """The quiet half: a second review is a manufactured run, not a review."""
    config_home, repo = isolated_project
    record = _scoring_pointer(config_home, repo, "r-reviewed")
    parsed = review_module.parse_review(VALID_REVIEW)
    review_module.store_review(
        {"project": "sample", "reviewed_run_id": "r-reviewed", **parsed}
    )
    before = [row["run_id"] for row in runs.list_live()]
    report = recovery.dispatch_review_for_run(record, config=CONFIG)
    assert report["dispatched"] is False
    assert report["reason"]
    assert [row["run_id"] for row in runs.list_live()] == before


def test_a_scoring_run_dispatches_its_own_review_unasked(
    isolated_project: tuple[Path, Path],
) -> None:
    """The positive half: no coordinator issues the command; the run reaches it."""
    config_home, repo = isolated_project
    record = _scoring_pointer(config_home, repo, "r-unreviewed")
    try:
        with runs.follower_claim(
            "sample", "session-orchestrating", delivery="stream"
        ):
            report = recovery.dispatch_review_for_run(
                record, config=CONFIG, launcher=lambda *a, **k: os.getpid()
            )
        assert report["dispatched"] is True
        assert report["review_run_id"]
        live = [row["run_id"] for row in runs.list_live()]
        assert report["review_run_id"] in live
        stored = runs.read_pointer("r-unreviewed")
        assert stored["review_dispatch"]["status"] == "dispatched"
        assert stored["review_dispatch"]["run_id"] == report["review_run_id"]
    finally:
        if crew.watch_state("sample")["watcher_live"]:
            recovery.unwatch("sample")
            _wait_for_stopped_producer()


def test_an_unavailable_lane_is_recorded_and_a_returning_lane_dispatches(
    isolated_project: tuple[Path, Path],
) -> None:
    """A skip must name the lane; the run says why it is still awaiting review."""
    config_home, repo = isolated_project
    record = _scoring_pointer(config_home, repo, "r-lane")
    no_lane = {key: value for key, value in CONFIG.items() if key != "local_backend"}
    held = recovery.dispatch_review_for_run(record, config=no_lane)
    assert held["dispatched"] is False
    assert held["awaiting_lane"] is True
    assert held["reason"]
    recorded = runs.read_pointer("r-lane")["review_dispatch"]
    assert recorded["status"] == "awaiting-lane"
    assert "local_backend" in recorded["reason"]
    assert held["reason"] == recorded["reason"]
    try:
        with runs.follower_claim(
            "sample", "session-orchestrating", delivery="stream"
        ):
            returned = recovery.dispatch_review_for_run(
                record, config=CONFIG, launcher=lambda *a, **k: os.getpid()
            )
        assert returned["dispatched"] is True
    finally:
        if crew.watch_state("sample")["watcher_live"]:
            recovery.unwatch("sample")
            _wait_for_stopped_producer()


def test_an_unattached_session_refuses_the_automatic_dispatch_too(
    isolated_project: tuple[Path, Path],
) -> None:
    """An automatic dispatch is refused exactly where a manual one is."""
    config_home, repo = isolated_project
    record = _scoring_pointer(config_home, repo, "r-unheard")
    try:
        report = recovery.dispatch_review_for_run(
            record, config=CONFIG, launcher=lambda *a, **k: os.getpid()
        )
        assert report["dispatched"] is False
        assert report["refused"] is True
        assert runs._watch_attach_line(
            "sample", session="session-orchestrating"
        ) in report["reason"]
        assert not any(
            row["node"].get("id", "").startswith(recovery.REVIEW_NODE_PREFIX)
            for row in runs.list_live(project="sample")
        )
    finally:
        if crew.watch_state("sample")["watcher_live"]:
            recovery.unwatch("sample")
            _wait_for_stopped_producer()

