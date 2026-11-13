"""A review's own promotion waits for the record the reviewer has not written.

A review run's promotion was requested as soon as the run was seen, which is
while the reviewer is still working and before its record exists. The request
moved the pointer out of its working phase and the drain then refused it for a
record that had not been written yet.

- a reviewer still writing its record is not requested: the sweep reads no
  ``review-self`` request until the record is stored, and then the review is
  promoted;
- a request that exists anyway waits while its reviewer is live: it takes no
  promotion slot, the pointer keeps its working phase, and a request queued
  behind it is promoted;
- a reviewer that ends without a record leaves a request refused, naming the
  missing record.

The fixture runs a temporary crew home and repository and asserts afterwards
that the workstation's real crew pointer directory is untouched.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon import _plan_html, ledger
from reckon.crew import promotion_requests as requests
from reckon.crew import recovery
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, crew_home, pointer_path, run_dir
from tests.helpers.git import git as _git

PROJECT = "review-waits-fixture"
PLAN = "review-waits-target"
FIRST_RUN_ID = "r-20261113T100000000001-review-of-first"
FIRST_REVIEWED_RUN_ID = "r-20261113T090000000001-first"
SECOND_RUN_ID = "r-20261113T100000000002-review-of-second"
SECOND_REVIEWED_RUN_ID = "r-20261113T090000000002-second"
REVIEWED_BASE = "b" * 40
REVIEWED_HEAD = "a" * 40


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
    real = Path.home() / ".config" / "reckon" / "crew" / "live"
    before = set(real.glob("r-20261113T10000000*.json")) if real.is_dir() else set()
    yield
    after = set(real.glob("r-20261113T10000000*.json")) if real.is_dir() else set()
    assert not (after - before)


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
            "title": "Review Waits Target",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (root / "docs" / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
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


def _record_path(reviewed_run_id: str) -> Path:
    return review_module.review_path(
        PROJECT, reviewed_run_id, reviewed_head_sha=REVIEWED_HEAD
    )


def _record_body(review_run_id: str, reviewed_run_id: str) -> str:
    return json.dumps(
        {
            "status": "parsed",
            "project": PROJECT,
            "reviewed_run_id": reviewed_run_id,
            "review_run_id": review_run_id,
            "reviewed_base_sha": REVIEWED_BASE,
            "reviewed_head_sha": REVIEWED_HEAD,
            "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20),
            "total": 20 * len(review_module.REVIEW_DIMENSIONS),
            "findings": [],
            "scores_comment": "",
            "timestamp": "2026-10-12T13:00:00Z",
        }
    )


def _store_record(review_run_id: str, reviewed_run_id: str) -> Path:
    path = _record_path(reviewed_run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_record_body(review_run_id, reviewed_run_id), encoding="utf-8")
    return path


class StubReviewer:
    """A reviewer process that writes its record only when it is told to.

    While unsignalled its pid is alive and its record is absent: the state a
    live reviewer is in before it delivers. Signalling it (``deliver()``)
    writes the record and ends the process. A timed delay would race the
    sweep, because how long a pass takes is not the test's subject; the
    trigger makes the reviewer's progress controllable and the state it is in
    observable, while liveness is still read from the kernel.
    """

    def __init__(self, record: Path, body: str) -> None:
        self.record = record
        self.body = body
        self.trigger = record.with_suffix(".deliver")
        self.process: subprocess.Popen | None = None

    def start(self) -> int:
        self.record.parent.mkdir(parents=True, exist_ok=True)
        self.trigger.unlink(missing_ok=True)
        script = (
            "import os, sys, time\n"
            "trigger, record, body = sys.argv[1], sys.argv[2], sys.argv[3]\n"
            "deadline = time.monotonic() + 300\n"
            "while not os.path.exists(trigger):\n"
            "    if time.monotonic() > deadline:\n"
            "        sys.exit(2)\n"
            "    time.sleep(0.02)\n"
            "open(record, 'w').write(body)\n"
        )
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(self.trigger),
                str(self.record),
                self.body,
            ]
        )
        return self.process.pid

    def deliver(self, timeout: float = 60.0) -> None:
        self.trigger.write_text("deliver\n", encoding="utf-8")
        deadline = time.monotonic() + timeout
        while not self.record.exists():
            assert time.monotonic() < deadline, (
                "the stub reviewer never wrote its record"
            )
            time.sleep(0.02)
        assert self.process is not None
        self.process.wait(timeout=timeout)

    def terminate(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)


def _write_pointer(
    repository: Path,
    tmp_path: Path,
    run_id: str,
    reviewed_run_id: str,
    *,
    pid: int | None,
) -> None:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: review-of-target\n"
        "status: complete\n"
        "checkpoint: the review record is stored\n",
        encoding="utf-8",
    )
    pointer: dict = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "launch": "in-harness",
        "role": "review",
        "backend": "native",
        "created_at": "2026-11-13T10:00:00Z",
        "phase": "working",
        "manifest_path": str(manifest),
        "node": {
            "id": f"review-of-{reviewed_run_id}",
            "plan": PLAN,
            "section": "s48",
            "time_budget": "25m",
            "role": "review",
            "write_paths": [str(_record_path(reviewed_run_id))],
        },
    }
    if pid is not None:
        pointer["pid"] = pid
    _write_json(pointer_path(run_id), pointer)


def _write_exit_record(run_id: str) -> None:
    directory = Path(run_dir(run_id))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "exit.json").write_text(
        json.dumps({"run_id": run_id, "exit_code": 0}), encoding="utf-8"
    )


def _sweep() -> dict:
    """One reflex pass that hands promotions to the durable request queue."""
    return recovery.dispatch_awaiting_reviews(
        project=PROJECT,
        launcher=lambda *args, **kwargs: 0,
        promotions=requests.PromotionRequests(),
    )


def _requested(run_id: str) -> bool:
    return requests._request_path(run_id).exists()


def test_a_review_is_requested_only_once_its_record_is_stored(
    repository: Path, tmp_path: Path
) -> None:
    """No request while the reviewer works; promoted once it has written."""
    reviewer = StubReviewer(
        _record_path(FIRST_REVIEWED_RUN_ID),
        _record_body(FIRST_RUN_ID, FIRST_REVIEWED_RUN_ID),
    )
    pid = reviewer.start()
    try:
        _write_pointer(
            repository, tmp_path, FIRST_RUN_ID, FIRST_REVIEWED_RUN_ID, pid=pid
        )

        first = _sweep()

        # The reviewer is mid-flight: its record does not exist yet, so no
        # request may exist and the pointer stays in its working phase.
        assert not reviewer.record.exists()
        assert not _requested(FIRST_RUN_ID), first
        assert first["promotion_requested"] == [], first
        assert [entry["run_id"] for entry in first["review_self_waiting"]] == [
            FIRST_RUN_ID
        ], first
        assert requests.runs.read_pointer(FIRST_RUN_ID)["phase"] == "working"

        reviewer.deliver()
        _write_exit_record(FIRST_RUN_ID)
    finally:
        reviewer.terminate()

    second = _sweep()
    assert second["promotion_requested"] == [FIRST_RUN_ID], second
    assert requests.read_promotion(FIRST_RUN_ID)["phase"] == requests.REQUESTED

    service = requests.PromotionService(width=1)
    service.tick()
    service.settle(timeout=120)

    receipt = requests.read_promotion(FIRST_RUN_ID)
    assert receipt["phase"] == requests.PROMOTED, receipt
    assert not pointer_path(FIRST_RUN_ID).exists()
    [row] = ledger.runs(PROJECT, root=repository)
    assert row["run_id"] == FIRST_RUN_ID
    assert row["promoted_by"] == "review-self"


def test_a_request_for_a_live_reviewer_waits_and_lets_the_queue_past(
    repository: Path, tmp_path: Path
) -> None:
    """An early request takes no slot and does not hold the head of the queue."""
    reviewer = StubReviewer(
        _record_path(FIRST_REVIEWED_RUN_ID),
        _record_body(FIRST_RUN_ID, FIRST_REVIEWED_RUN_ID),
    )
    pid = reviewer.start()
    try:
        _write_pointer(
            repository, tmp_path, FIRST_RUN_ID, FIRST_REVIEWED_RUN_ID, pid=pid
        )
        requests.request_promotion(FIRST_RUN_ID, kind="review-self")
        assert requests.read_promotion(FIRST_RUN_ID)["phase"] == requests.REQUESTED

        # A ready review queued behind the waiting one: its record is stored
        # and its worker has ended, so nothing withholds its promotion.
        _write_pointer(
            repository, tmp_path, SECOND_RUN_ID, SECOND_REVIEWED_RUN_ID, pid=None
        )
        _store_record(SECOND_RUN_ID, SECOND_REVIEWED_RUN_ID)
        _write_exit_record(SECOND_RUN_ID)
        requests.request_promotion(SECOND_RUN_ID, kind="review-self")

        service = requests.PromotionService(width=1)
        launched = service.tick()
        service.settle(timeout=120)

        # The waiting request was not launched, and its run keeps its own
        # working phase rather than reading as a promotion in progress.
        assert launched == [SECOND_RUN_ID], launched
        waiting = requests.read_promotion(FIRST_RUN_ID)
        assert waiting["phase"] == requests.WAITING, waiting
        assert "worker is still alive" in str(waiting.get("waiting_reason") or "")
        assert requests.runs.read_pointer(FIRST_RUN_ID)["phase"] == "working"

        # The request behind it was promoted.
        assert not pointer_path(SECOND_RUN_ID).exists()
        rows = {row["run_id"]: row for row in ledger.runs(PROJECT, root=repository)}
        assert rows[SECOND_RUN_ID]["promoted_by"] == "review-self"
        assert FIRST_RUN_ID not in rows
    finally:
        reviewer.terminate()


def test_a_reviewer_that_ends_without_a_record_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """Nothing more will arrive, so the request names the missing record."""
    _write_pointer(repository, tmp_path, FIRST_RUN_ID, FIRST_REVIEWED_RUN_ID, pid=None)
    _write_exit_record(FIRST_RUN_ID)
    assert not _record_path(FIRST_REVIEWED_RUN_ID).exists()

    report = _sweep()
    assert report["promotion_requested"] == [FIRST_RUN_ID], report

    service = requests.PromotionService(width=1)
    service.tick()
    service.settle(timeout=120)

    receipt = requests.read_promotion(FIRST_RUN_ID)
    assert receipt["phase"] == requests.REFUSED, receipt
    assert "stored no readable record" in receipt["error"], receipt
    assert pointer_path(FIRST_RUN_ID).is_file()
    assert ledger.runs(PROJECT, root=repository) == []
