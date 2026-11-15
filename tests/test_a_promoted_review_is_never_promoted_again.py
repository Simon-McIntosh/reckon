"""A review whose promotion is recorded is never promoted again.

A review run's whole deliverable is the record it writes for the run it reads,
so a coordinator may promote it while its worker is still running and its
manifest already reads complete. Promotion records the ledger row and commits
the review, but it cannot retire the live pointer under a worker that still
holds the promotion's own attempt. The pointer then survives into the
acceptance sweep, which reads an unpromoted review with a stored record and
files a ``review-self`` promotion request for a run the ledger already holds --
a request that fails on each pass and raises a standing obligation.

A promotion that cannot retire the pointer marks it promoted instead. The first
sweep after the worker exits removes that marked pointer and files nothing, so
the recorded promotion is the run's only promotion.

The fixture runs a temporary crew home and repository and asserts afterwards
that the workstation's real crew pointer directory is untouched, because an
isolated read does not prove an isolated write.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew import recovery
from reckon.crew import review as review_module
from reckon.crew.promotion_requests import read_promotion
from reckon.crew.runs import _write_json, crew_home, pointer_path, run_dir
from tests.helpers.git import git as _git

PROJECT = "promoted-review-fixture"
PLAN = "promoted-review-target"
REVIEWED_RUN_ID = "r-20261115T010000000001-reviewed-run"
REVIEW_RUN_ID = "r-20261115T030000000000-review-of-reviewed-run"
REVIEW_NODE_ID = "review-of-reviewed-run"
REVIEWED_BASE = "c" * 40
DISPATCHED_HEAD = "a" * 40
TOTAL = 20 * len(review_module.REVIEW_DIMENSIONS)
NO_COMMIT_REASON = "a review stores a record and commits no work of its own"


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture(autouse=True)
def real_crew_home_is_not_a_fixture_target() -> None:
    """No fixture may reach the workstation's real crew pointer directory."""
    real_pointer = (
        Path.home() / ".config" / "reckon" / "crew" / "live" / f"{REVIEW_RUN_ID}.json"
    )
    assert not real_pointer.exists()
    yield
    assert not real_pointer.exists()


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    """A committed repository whose docs directory is the project's mount."""
    assert crew_home().is_relative_to(isolated_reckon_home)
    root = tmp_path / "repo"
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Promoted Review Target",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (root / "docs" / "state" / "proj-placeholder").mkdir(parents=True, exist_ok=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "chore: seed repository")
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _record_directory() -> Path:
    return review_module.run_review_record_directory(Path(run_dir(REVIEWED_RUN_ID)))


def _record_path() -> Path:
    return _record_directory() / f"{REVIEWED_RUN_ID}.at-{DISPATCHED_HEAD}.json"


def _declared_paths() -> list[str]:
    return review_module.run_review_record_paths(
        Path(run_dir(REVIEWED_RUN_ID)),
        REVIEWED_RUN_ID,
        reviewed_head_sha=DISPATCHED_HEAD,
    )


def _store_review_record() -> Path:
    """Write the complete review record the reviewer was dispatched to store."""
    body = {
        "status": "parsed",
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN_ID,
        "review_run_id": REVIEW_RUN_ID,
        "reviewed_base_sha": REVIEWED_BASE,
        "reviewed_head_sha": DISPATCHED_HEAD,
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20),
        "findings": [],
        "total": TOTAL,
        "timestamp": "2026-11-15T03:00:00Z",
    }
    path = _record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _write_pointer(repository: Path, tmp_path: Path, *, worker_pid: int | None) -> None:
    manifest = tmp_path / "manifests" / f"{REVIEW_RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: review-of-reviewed-run\n"
        "status: complete\n"
        "checkpoint: the review record is stored\n",
        encoding="utf-8",
    )
    pointer: dict = {
        "run_id": REVIEW_RUN_ID,
        "project": PROJECT,
        "repo": str(repository),
        "launch": "in-harness",
        "role": "review",
        "backend": "native",
        "phase": "working",
        "created_at": "2026-11-15T03:00:00Z",
        "manifest_path": str(manifest),
        "node": {
            "id": REVIEW_NODE_ID,
            "plan": PLAN,
            "section": "s1",
            "time_budget": "25m",
            "role": "review",
            "write_paths": _declared_paths(),
        },
    }
    if worker_pid is not None:
        pointer["pid"] = worker_pid
    _write_json(pointer_path(REVIEW_RUN_ID), pointer)


def _write_exit_record() -> None:
    directory = Path(run_dir(REVIEW_RUN_ID))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "exit.json").write_text(
        json.dumps({"run_id": REVIEW_RUN_ID, "exit_code": 0}), encoding="utf-8"
    )


def _deliver(repository: Path, tmp_path: Path, *, worker_pid: int | None) -> None:
    """Stage the review run's pointer, record and exit as delivered."""
    _store_review_record()
    _write_pointer(repository, tmp_path, worker_pid=worker_pid)
    _write_exit_record()


def _live_worker_script(path: Path) -> None:
    """A worker-shaped child: it holds the pid and writes its own pointer."""
    path.write_text(
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "pointer, go, done = (Path(arg) for arg in sys.argv[1:4])\n"
        "deadline = time.time() + 60\n"
        "while not go.exists():\n"
        "    if time.time() > deadline:\n"
        "        raise SystemExit(1)\n"
        "    time.sleep(0.005)\n"
        "current = json.loads(pointer.read_text(encoding='utf-8'))\n"
        "current['phase'] = 'finishing'\n"
        "current['heartbeat'] = 'the worker is still running'\n"
        "pointer.write_text(json.dumps(current), encoding='utf-8')\n"
        "done.write_text('written', encoding='utf-8')\n"
        "time.sleep(600)\n",
        encoding="utf-8",
    )


def _move_the_pointer_while_promotion_runs(
    monkeypatch: pytest.MonkeyPatch, go: Path, done: Path
) -> None:
    """Let the live worker write its own pointer after the promotion reads it.

    The promotion appends and commits its stores before it retires the pointer,
    so this is where a worker that is still running writes its next phase. The
    worker writes the file itself, so the move is the live process's own.
    """
    from reckon.crew import promotion as promotion_module

    real_commit = promotion_module._commit_landing_writes

    def commit_then_move(*args: object, **kwargs: object) -> object:
        written = real_commit(*args, **kwargs)
        go.touch()
        deadline = time.time() + 60
        while not done.exists() and time.time() < deadline:
            time.sleep(0.005)
        assert done.exists(), "the live worker never rewrote its pointer"
        return written

    monkeypatch.setattr(promotion_module, "_commit_landing_writes", commit_then_move)


def _sweep() -> dict:
    return recovery.dispatch_awaiting_reviews(
        project=PROJECT, launcher=lambda *args, **kwargs: 0
    )


def _named_in(entries: list[dict]) -> list[dict]:
    return [
        entry for entry in entries if str(entry.get("run_id") or "") == REVIEW_RUN_ID
    ]


def test_a_promoted_review_pointer_is_retired_after_its_worker_exits(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded promotion is retired once, by the sweep, with no request."""
    script = tmp_path / "live-worker.py"
    _live_worker_script(script)
    go = tmp_path / "go"
    done = tmp_path / "done"
    child = subprocess.Popen(
        [
            sys.executable,
            str(script),
            str(pointer_path(REVIEW_RUN_ID)),
            str(go),
            str(done),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _deliver(repository, tmp_path, worker_pid=child.pid)
        _move_the_pointer_while_promotion_runs(monkeypatch, go, done)

        crew.complete(
            REVIEW_RUN_ID, gate="not-run", no_commit=NO_COMMIT_REASON, root=repository
        )

        [row] = ledger.runs(PROJECT, root=repository)
        assert row["run_id"] == REVIEW_RUN_ID
        assert row["gate"] == "not-run"
        pointer = json.loads(pointer_path(REVIEW_RUN_ID).read_text(encoding="utf-8"))
        assert pointer.get("promoted") is True
    finally:
        child.kill()
        child.wait()

    for _ in range(3):
        report = _sweep()
        assert _named_in(report["review_self_refused"]) == [], report
        assert _named_in(report["failed"]) == [], report

    assert not pointer_path(REVIEW_RUN_ID).exists()
    assert read_promotion(REVIEW_RUN_ID) == {}
    [row] = ledger.runs(PROJECT, root=repository)
    assert row["run_id"] == REVIEW_RUN_ID
    assert recovery.overdue_unreconciled_runs(project=PROJECT, grace="0s") == []


def test_an_unpromoted_review_still_files_its_request(
    repository: Path, tmp_path: Path
) -> None:
    """The mark gates retirement only: an unmarked review keeps its own path."""
    _deliver(repository, tmp_path, worker_pid=None)

    report = _sweep()

    assert report["self_promoted"] == [REVIEW_RUN_ID], report
    assert not pointer_path(REVIEW_RUN_ID).exists()
