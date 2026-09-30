"""A repair held with no lane names each rule that removed one.

The repair reflex composes onto the same lane list a review does, so the same
two rules can leave it with nowhere to run: a backend the flight configuration
excludes from review routing, and a backend whose declared launch is the calling
harness itself, which no coordinator starts on the reflex's behalf. Both are
deliberate, and the run they hold is left for a reader to act on — so a hold
that names only the fact of having no lane sends that reader looking for a lane
to add, when what the configuration says is that the lane is withheld. The
reason therefore composes the same clauses the review's hold composes, naming
each rule that emptied the candidate list.

Each hold is read against a control where a spawnable lane remains and the same
run dispatches, so a hold is attributed to the lane drop rather than to a
fixture that never composed a repair at all.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery, repair, runs
from reckon.crew import review as review_module
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS

# The reflex is gated by the watch admission, so these tests arm the producer
# the gate reads rather than accepting the suite-wide waiver, which would let
# every dispatch through and prove nothing about the refusal.
pytestmark = pytest.mark.arms_watch_producer

PROJECT = "sample"
RUN_ID = "r-reviewed"
NODE_ID = "a-reviewed-node"

# The calling harness itself, and two lanes the configuration withholds.
HARNESS_BACKEND = "native"
EXCLUDED_BACKENDS = ("beta", "gamma")

# Findings naming repository source and test paths, so the composed scope is
# non-empty and the only thing that can hold the repair is the lane list.
FINDINGS = [
    {"file": "reckon/crew/thing.py", "line": "10", "text": "off-by-one in the loop"},
    {
        "file": "tests/test_thing.py",
        "line": "3",
        "text": "the test asserts a stale value",
    },
]


def _cli_backend(command: str) -> dict:
    return {
        "launch": "cli",
        "command": command,
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
        "session_reuse": True,
        "time_budget": "25m",
    }


# Only lanes the calling harness owns, which is the shape that leaves a repair
# with no lane rather than with a lane nothing starts.
CONFIG_ONLY_IN_HARNESS = {
    "default_backend": HARNESS_BACKEND,
    "local_backend": HARNESS_BACKEND,
    "backends": {
        HARNESS_BACKEND: {
            "launch": "in-harness",
            "sandbox": "worktree-full",
            "session_reuse": False,
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

# A fleet whose every spawnable lane is withheld by the routing key, so the
# exclusion is the only rule that can have emptied the candidate list.
CONFIG_ALL_EXCLUDED = {
    "default_backend": EXCLUDED_BACKENDS[0],
    "local_backend": EXCLUDED_BACKENDS[0],
    "backends": {name: _cli_backend("codex") for name in EXCLUDED_BACKENDS},
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    recovery.REVIEW_EXCLUDED_BACKENDS_KEY: list(EXCLUDED_BACKENDS),
}

# The control: one spawnable lane, no rule removing it.
CONFIG_WITH_A_LANE = {
    "default_backend": "alpha",
    "local_backend": "alpha",
    "backends": {"alpha": _cli_backend("codex")},
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
        '<h2 id="s2">A repair held with no lane names its cause</h2>',
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
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return config_home, repo, head_sha


def _reviewed_pointer(config_home: Path, repo: Path, *, backend: str) -> dict:
    """The reviewed run: a completed implement run whose manifest reports completion."""
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
        "role": "implement",
        "node": {"id": NODE_ID, "plan": "fixture", "section": "s2"},
        "backend": backend,
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    }
    crew._write_json(crew.pointer_path(RUN_ID), record)
    return record


def _store_review(head_sha: str, findings: list[dict[str, str]]) -> None:
    """Write the reviewed run's review record into the isolated store root."""
    review_module.store_review(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN_ID,
            "reviewed_base_sha": head_sha,
            "reviewed_head_sha": head_sha,
            "status": "parsed",
            "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15),
            "total": 75,
            "findings": findings,
        }
    )


def _wait_for_stopped_producer() -> None:
    deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
    while time.monotonic() < deadline:
        if not crew.watch_state(PROJECT)["watcher_live"]:
            return
        time.sleep(0.05)
    pytest.fail("watch producer did not release its seat")


def _release_watcher() -> None:
    if crew.watch_state(PROJECT)["watcher_live"]:
        recovery.unwatch(PROJECT)
        _wait_for_stopped_producer()


@contextmanager
def _armed_fleet():
    with runs.follower_claim(PROJECT, "session-orchestrating", delivery="stream"):
        try:
            yield
        finally:
            _release_watcher()


def _dispatch_repair(record: dict, config: dict) -> dict:
    with _armed_fleet():
        return recovery.dispatch_repair_for_run(
            record, config=config, launcher=lambda *a, **k: os.getpid()
        )


def _repair_pointers() -> list[dict]:
    """Every live pointer whose node is a composed repair."""
    return [
        row
        for row in runs.list_live(project=PROJECT)
        if str((row.get("node") or {}).get("id") or "").startswith(
            repair.REPAIR_NODE_PREFIX
        )
    ]


def test_a_repair_whose_only_lane_is_in_harness_is_held_with_a_reason(
    isolated_project: tuple[Path, Path, str],
) -> None:
    """The hold names the in-harness drop, and creates no repair run.

    On this configuration the routing key is silent, so a reason that named only
    exclusions would read as a hold with no cause at all; the lane list is empty
    because the one backend's declared launch is the calling harness.
    """
    config_home, repo, head_sha = isolated_project
    record = _reviewed_pointer(config_home, repo, backend=HARNESS_BACKEND)
    _store_review(head_sha, FINDINGS)

    report = _dispatch_repair(record, CONFIG_ONLY_IN_HARNESS)

    assert report["dispatched"] is False
    assert report["awaiting_lane"] is True
    assert RUN_ID in report["reason"]
    assert f"launch as {recovery.IN_HARNESS_LAUNCH}" in report["reason"]
    assert HARNESS_BACKEND in report["reason"]
    assert _repair_pointers() == []
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "awaiting-lane"
    assert recorded["reason"] == report["reason"]


def test_a_repair_held_by_an_exclusion_names_the_exclusion(
    isolated_project: tuple[Path, Path, str],
) -> None:
    """The hold names the routing key and every lane it withheld.

    Every spawnable lane is excluded here, so the exclusion is the one rule that
    can have emptied the candidate list and the reason has to be the rule rather
    than the empty list it produced.
    """
    config_home, repo, head_sha = isolated_project
    record = _reviewed_pointer(config_home, repo, backend=EXCLUDED_BACKENDS[0])
    _store_review(head_sha, FINDINGS)

    report = _dispatch_repair(record, CONFIG_ALL_EXCLUDED)

    assert report["dispatched"] is False
    assert report["awaiting_lane"] is True
    assert recovery.REVIEW_EXCLUDED_BACKENDS_KEY in report["reason"]
    for name in EXCLUDED_BACKENDS:
        assert name in report["reason"]
    assert _repair_pointers() == []
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "awaiting-lane"
    assert recovery.REVIEW_EXCLUDED_BACKENDS_KEY in recorded["reason"]


def test_a_spawnable_lane_still_dispatches_the_composition(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """The drop is a filter, not a refusal: a lane left still repair-dispatches.

    Without this arm the two holds would pass on a fixture that never composed a
    repair at all, which is a failure that looks exactly like a lane hold.
    """
    config_home, repo, head_sha = isolated_project
    _reviewed_pointer(config_home, repo, backend="alpha")
    _store_review(head_sha, FINDINGS)
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    calls: list[dict] = []

    def fake_dispatch(**kwargs):
        calls.append(kwargs)
        return {"run_id": "r-repair-stub"}

    monkeypatch.setattr(dispatch_module, "dispatch", fake_dispatch)
    report = _dispatch_repair(runs.read_pointer(RUN_ID), CONFIG_WITH_A_LANE)

    assert report["dispatched"] is True
    assert len(calls) == 1
    assert str(calls[0]["node"].id).startswith(repair.REPAIR_NODE_PREFIX)
