"""A queued review waits on the lane whose admission launches it.

The reflex composes a review onto the lane a coordinator chose for it. When the
locally served lane grants this session no worker, the review is written as a
queued request that the local admission pass launches once a slot frees. The
request must keep recording the lane it is queued for: the admission pass keeps
only the queued requests whose recorded backend equals the lane it drains, so a
request left recorded against another lane is filtered out on every pass and
nothing launches it, while its hold still tells the reader it waits for a local
slot.

The divergence is a re-dispatch of a node that already has a queued request. A
second dispatch that names no lane resolves its own backend, and the in-place
update records that resolved lane rather than the lane the request is already
queued on. The observation that motivated this test read exactly that: a queued
request carrying the local flag beside an empty recorded backend, which is the
signature of a re-dispatch naming no lane over a request already queued for the
local lane. Both cases below read the queued pointer before any admission pass,
so the assertion is about what the re-queue recorded and not about what an
admission pass happened to do with the lane it was handed.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import queue_admission, recovery, runs
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS

pytestmark = pytest.mark.arms_watch_producer

LOCAL_BACKEND = "clive"
METERED_BACKEND = "codex"
QUEUED_RUN = "r-queued"
ADMITTED_RUN = "r-admitted"


def _backend(command: str) -> dict:
    return {
        "launch": "cli",
        "command": command,
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
        "session_reuse": True,
        "time_budget": "25m",
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
    (scripts / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
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


def _wait_for_stopped_producer() -> None:
    deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
    while time.monotonic() < deadline:
        if not crew.watch_state("sample")["watcher_live"]:
            return
        time.sleep(0.05)
    pytest.fail("watch producer did not release its seat")


def _release_watcher() -> None:
    if crew.watch_state("sample")["watcher_live"]:
        recovery.unwatch("sample")
        _wait_for_stopped_producer()


def _write_lane(tmp_path: Path, *, session_slots: float) -> str:
    path = tmp_path / "lane.json"
    path.write_text(
        json.dumps(
            {
                "headroom": 0,
                "admission": {
                    "worker_slots": session_slots,
                    "observed_seconds": 900,
                    "sessions": {
                        "session-orchestrating": {"worker_slots": session_slots}
                    },
                    "verdict": "full",
                    "headroom": 0,
                },
                "observed_at": "2026-10-16T07:50:00Z",
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def _config(
    tmp_path: Path, *, lane_document: str, review_excluded: tuple[str, ...] = ()
) -> dict:
    config = {
        "default_backend": LOCAL_BACKEND,
        "local_backend": LOCAL_BACKEND,
        "backends": {
            LOCAL_BACKEND: {**_backend("claude"), "lane_document": lane_document},
            METERED_BACKEND: _backend("codex"),
        },
        "roles": {"implement": {}, "review": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }
    if review_excluded:
        config["review_excluded_backends"] = list(review_excluded)
    return config


def _scoring_pointer(config_home: Path, repo: Path, run_id: str, *, lane: str) -> dict:
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
        "node": {
            "id": run_id,
            "plan": "fixture",
            "section": "s2",
            "lane_declaration": {"backend": lane},
        },
        "backend": lane,
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def _queued_pointers() -> list[dict]:
    return [p for p in runs.list_live() if p.get("phase") == "queued"]


def _compose(record: dict, config: dict, *, prefer_local: bool) -> dict:
    fields = recovery._review_dispatch_fields(record)
    with runs.follower_claim("sample", "session-orchestrating", delivery="stream"):
        return recovery._dispatch_composed_review(
            record,
            fields,
            config=config,
            launcher=lambda *a, **k: os.getpid(),
            allow_unreconciled_runs=True,
            prefer_local=prefer_local,
        )


def _queue_for_local_then_redispatch(
    config_home: Path, repo: Path, tmp_path: Path, run_id: str, lane_document: str
) -> tuple[dict, dict]:
    """Queue a review for the local lane, then re-dispatch the node with no lane.

    Returns the config the first dispatch used and the queued pointer that the
    two dispatches together left behind. The second dispatch names no lane, and
    the local lane is removed from review routing in its config, so it resolves
    the metered backend — the case that recorded a second lane over a request
    already waiting for the local one.
    """
    config = _config(tmp_path, lane_document=lane_document)
    record = _scoring_pointer(config_home, repo, run_id, lane=LOCAL_BACKEND)
    _compose(record, config, prefer_local=True)
    queued = _queued_pointers()
    assert len(queued) == 1, "the first composition must queue exactly one request"
    assert queued[0]["backend"] == LOCAL_BACKEND, (
        "the first composition must queue for the local lane, but it recorded "
        f"{queued[0]['backend']!r}"
    )
    assert queued[0]["local"] is True

    record["node"].pop("lane_declaration", None)
    record["backend"] = ""
    crew._write_json(crew.pointer_path(run_id), record)
    rerouted = _config(
        tmp_path, lane_document=lane_document, review_excluded=(LOCAL_BACKEND,)
    )
    _compose(record, rerouted, prefer_local=False)
    return config, rerouted


def test_a_requeued_review_stays_on_the_lane_that_queued_it(
    isolated_project: tuple[Path, Path], tmp_path: Path
) -> None:
    config_home, repo = isolated_project
    lane_document = _write_lane(tmp_path, session_slots=0)
    try:
        _queue_for_local_then_redispatch(
            config_home, repo, tmp_path, QUEUED_RUN, lane_document
        )

        queued = _queued_pointers()
        assert len(queued) == 1, "the re-dispatch must update one request in place"
        assert queued[0]["backend"] == LOCAL_BACKEND, (
            "the queued request records "
            f"{queued[0]['backend']!r}, not the local backend {LOCAL_BACKEND!r} "
            "whose admission launches it"
        )
    finally:
        _release_watcher()


def test_the_local_admission_launches_a_request_queued_for_it(
    isolated_project: tuple[Path, Path], tmp_path: Path
) -> None:
    config_home, repo = isolated_project
    lane_document = _write_lane(tmp_path, session_slots=0)
    try:
        config, _ = _queue_for_local_then_redispatch(
            config_home, repo, tmp_path, ADMITTED_RUN, lane_document
        )
        queued = _queued_pointers()
        assert len(queued) == 1
        assert queued[0]["backend"] == LOCAL_BACKEND, (
            "read before any admission pass, the queued request records "
            f"{queued[0]['backend']!r}"
        )
        queued_run_id = str(queued[0]["run_id"])

        # The lane frees a slot, and the local admission pass is handed it.
        _write_lane(tmp_path, session_slots=1)
        launches: list[str] = []

        def stub_launcher(*args, **kwargs) -> int:
            launches.append(str(args[0] if args else kwargs.get("run_id")))
            return 4321

        result = queue_admission.admit_once(
            lane=LOCAL_BACKEND,
            launcher=stub_launcher,
            config_for=lambda _pointer: config,
            lock=False,
        )
        assert launches, (
            "admit_once for the local lane launched nothing; "
            f"result={json.dumps(result, default=str)}"
        )
        assert result["launched"], f"no run was admitted; result={result}"
        landed = runs.read_pointer(str(result["launched"][0]))
        assert str(landed.get("admitted_from") or "") == queued_run_id, (
            "the run the local lane launched does not name the queued request "
            "it came from"
        )
    finally:
        _release_watcher()
