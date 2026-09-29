"""A finding-bearing review round dispatches exactly one repair through the reflex.

The composer (:mod:`reckon.crew.repair`) turns a stored review record into a
node; this test drives that node *through the reflex* — a real periodic sweep
over a live pointer whose stored review carries findings — and asserts the reflex
dispatches it exactly once. Both failure modes here look like a quiet fleet
rather than an error: a sweep that dispatches nothing is indistinguishable from
a round with no findings, and a sweep that re-fires manufactures a second repair
for a round that already has one. So the negative halves are asserted as
carefully as the positive one.

Idempotence is bounded from both sides. A round whose repair is live is found in
flight and left alone. A round whose repair has been promoted leaves no live
pointer to find, so the round is recognised from the project's ledger rather
than re-composed. A review carrying no finding is not work, so nothing composes
and nothing is dispatched.

The finding ids the brief must name are re-derived here from each finding's own
file, line and text, so the assertion holds against an expectation the module
cannot satisfy by returning a constant.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import recovery, repair, resumption, runs
from reckon.crew import review as review_module
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS

# The reflex is gated by the watch admission, so these tests arm the producer
# the gate reads rather than accepting the suite-wide waiver, which would let
# every dispatch through and prove nothing about the refusal.
pytestmark = pytest.mark.arms_watch_producer

PROJECT = "sample"
RUN_ID = "r-work"
NODE_ID = "a-reviewed-node"

# The three findings the stored review carries. They name paths no other live
# run claims, so the only thing that can refuse the repair dispatch is the
# reflex's own logic rather than a scope collision with a peer.
FINDINGS = [
    {"file": "reckon/crew/thing.py", "line": "10", "text": "off-by-one in the loop"},
    {
        "file": "tests/test_thing.py",
        "line": "3",
        "text": "the test asserts a stale value",
    },
    {
        "file": "reckon/crew/new_module.py",
        "line": "5",
        "text": "a branch no test reaches",
    },
]


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
def isolated_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path, str]:
    """A project whose review store, ledger and repo all live under a temp root."""
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
    (scripts / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s2">A finding-bearing review dispatches its repair</h2>',
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
    return config_home, repo, base_sha


def _completed_pointer(config_home: Path, repo: Path) -> dict:
    """The reviewed run: a completed run whose manifest reports completion.

    The node declares no write path, so the completed run holds no claim the
    repair's scope could collide with. The reviewed run's own fence is the
    composer's concern, exercised where it is composed; here it would only mask
    whether the reflex dispatched, by turning the observation into a scope
    refusal that has nothing to do with the reflex.
    """
    manifest = config_home / "manifests" / (RUN_ID + ".md")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: " + RUN_ID + "\nstatus: complete\ncommits: " + RUN_ID + "\n",
        encoding="utf-8",
    )
    record = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "repo": str(repo),
        "node": {"id": NODE_ID, "plan": "fixture", "section": "s2"},
        "backend": "alpha",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    }
    crew._write_json(crew.pointer_path(RUN_ID), record)
    return record


def _expected_id(finding: dict[str, str]) -> str:
    """The id the documented rule yields for one finding, re-derived here."""
    material = "\x00".join(
        (finding["file"].strip(), finding["line"].strip(), finding["text"].strip())
    )
    return "f" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]


def _store_review(head_sha: str, findings: list[dict[str, str]]) -> None:
    """Write the reviewed run's review record into the isolated store root."""
    record = {
        "project": PROJECT,
        "reviewed_run_id": RUN_ID,
        "reviewed_base_sha": head_sha,
        "reviewed_head_sha": head_sha,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15),
        "total": 75,
        "findings": findings,
    }
    review_module.store_review(record)


def _wait_for_stopped_producer() -> None:
    deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
    while time.monotonic() < deadline:
        if not crew.watch_state(PROJECT)["watcher_live"]:
            return
        time.sleep(0.05)
    pytest.fail("watch producer did not release its seat")


@contextmanager
def _armed_fleet():
    """Hold the follower claim the dispatch is gated on, then release the watch."""
    with runs.follower_claim(PROJECT, "session-orchestrating", delivery="stream"):
        try:
            yield
        finally:
            if crew.watch_state(PROJECT)["watcher_live"]:
                recovery.unwatch(PROJECT)
                _wait_for_stopped_producer()


def _launcher_recording(calls: list[dict]):
    """A launcher that records every call and reports success without spawning."""

    def launcher(plan, *, log_path, stderr_path, prompt_path):
        calls.append(
            {
                "argv": list(getattr(plan, "argv", ())),
                "log_path": str(log_path),
                "prompt_path": str(prompt_path),
            }
        )
        return os.getpid()

    return launcher


def test_a_three_finding_review_dispatches_exactly_one_repair(
    isolated_project: tuple[Path, Path, str],
) -> None:
    """The positive half: the reflex composes and dispatches the repair itself."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, FINDINGS)
    calls: list[dict] = []
    with _armed_fleet():
        report = resumption.sweep(
            PROJECT, config=CONFIG, launcher=_launcher_recording(calls)
        )

    repaired = report["reviews"]["repaired"]
    assert len(repaired) == 1
    assert len(calls) == 1

    pointer = runs.read_pointer(repaired[0])
    node = pointer["node"]
    goal = str(node.get("goal") or "")
    scope = list(node.get("write_paths") or [])
    assert str(node.get("id") or "").startswith(repair.REPAIR_NODE_PREFIX)
    # The brief names every finding by its content-derived id, and the write
    # scope the repair was dispatched with names every path the findings name:
    # one node, all three findings, no coordinator command in between.
    for finding in FINDINGS:
        assert _expected_id(finding) in goal
        assert finding["file"] in scope

    # The reviewed run carries the outcome, so a later reader sees why the run
    # is not yet repaired rather than finding a silent absence.
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "dispatched"
    assert recorded["run_id"] == repaired[0]


def test_a_second_sweep_over_a_live_round_dispatches_nothing(
    isolated_project: tuple[Path, Path, str],
) -> None:
    """Idempotence while the repair is live: the standing round is found."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, FINDINGS)
    calls: list[dict] = []
    launcher = _launcher_recording(calls)
    with _armed_fleet():
        first = resumption.sweep(PROJECT, config=CONFIG, launcher=launcher)
        second = resumption.sweep(PROJECT, config=CONFIG, launcher=launcher)
    assert len(first["reviews"]["repaired"]) == 1
    assert second["reviews"]["repaired"] == []
    assert len(calls) == 1


def test_a_promoted_repair_leaves_the_round_dispatching_nothing(
    isolated_project: tuple[Path, Path, str],
) -> None:
    """Idempotence after promotion: the ledger, not the live pointer, is the record."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, FINDINGS)
    calls: list[dict] = []
    launcher = _launcher_recording(calls)
    with _armed_fleet():
        first = resumption.sweep(PROJECT, config=CONFIG, launcher=launcher)
        repair_run_id = first["reviews"]["repaired"][0]
        # Promote the repair run: it lands a ledger row and its live pointer is
        # reconciled away. The round is now satisfied, so the freed pointer must
        # not read as an unattempted round.
        run_file = ledger.run_path(PROJECT, repair_run_id)
        run_file.parent.mkdir(parents=True, exist_ok=True)
        crew._write_json(run_file, {"run_id": repair_run_id, "status": "promoted"})
        crew.pointer_path(repair_run_id).unlink()
        after = resumption.sweep(PROJECT, config=CONFIG, launcher=launcher)
    assert after["reviews"]["repaired"] == []
    assert len(calls) == 1


def test_a_clean_review_dispatches_no_repair(
    isolated_project: tuple[Path, Path, str],
) -> None:
    """The clean half: a review with no finding is not work, so nothing composes."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, [])
    calls: list[dict] = []
    with _armed_fleet():
        report = resumption.sweep(
            PROJECT, config=CONFIG, launcher=_launcher_recording(calls)
        )
    assert report["reviews"]["repaired"] == []
    assert calls == []
