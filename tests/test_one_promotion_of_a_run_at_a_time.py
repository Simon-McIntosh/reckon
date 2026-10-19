"""One promotion of a run at a time, across hosts.

A promotion of a review run can be reached at once by a producer seat and a
review sweep on two hosts. The run's pointer lock is a node-local ``flock``,
which does not exclude the second promotion across hosts, so the two can
interleave their landing steps and leave a landed path staged back. This module
fixes the run's cross-host claim and its three behaviours:

- a second promotion of the same run from another host refuses at once without
  writing, naming the holder's host, pid and job;
- a lease whose holder stopped renewing past the stale window is taken over, and
  the next promotion lands;
- a sweep's self-promotion of a review run while the lease is held reports a
  refused self-promotion naming the holder.

The fixture runs a temporary crew home and repository, and the cross-host case
runs two real processes so their pids differ as they would on two hosts. It
asserts afterwards that the workstation's real crew pointer directory is
untouched, because an isolated read does not prove an isolated write.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon import _plan_html, ledger
from reckon.crew import promotion
from reckon.crew import review as review_module
from reckon.crew.host_lease import LEASE_STALE_SECONDS, HostLease
from reckon.crew.recovery_review_acceptance import promote_review_run_itself
from reckon.crew.runs import _write_json, crew_home, pointer_path, run_dir
from tests.helpers.git import git as _git

PROJECT = "one-promotion-fixture"
PLAN = "one-promotion-target"
REVIEWED_RUN_ID = "r-20261019T120000000001-reviewed-run"
REVIEW_RUN_ID = "r-20261019T130000000000-review-of-reviewed-run"
REVIEW_NODE_ID = f"review-of-{REVIEWED_RUN_ID}"
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


def _record_path() -> Path:
    """The head-keyed review-store path the reviewer was dispatched to write."""
    return review_module.review_path(
        PROJECT, REVIEWED_RUN_ID, reviewed_head_sha=REVIEWED_HEAD
    )


def _store_review_record() -> Path:
    """Write a complete parsed review record and return its path."""
    record = {
        "status": "parsed",
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN_ID,
        "review_run_id": REVIEW_RUN_ID,
        "reviewed_base_sha": REVIEWED_BASE,
        "reviewed_head_sha": REVIEWED_HEAD,
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20),
        "total": 20 * len(review_module.REVIEW_DIMENSIONS),
        "findings": [],
        "timestamp": "2026-09-19T13:00:00Z",
    }
    path = _record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def real_crew_home_is_not_a_fixture_target() -> None:
    """No fixture may reach the workstation's real crew pointer directory."""
    real_pointer = (
        Path.home() / ".config" / "reckon" / "crew" / "live" / f"{REVIEW_RUN_ID}.json"
    )
    assert not real_pointer.exists()
    yield
    assert not real_pointer.exists()


def _build_review_run(repository: Path, tmp_path: Path) -> None:
    """A completed review run whose stored record is on disk and worker exited."""
    record_path = _store_review_record()
    manifest = tmp_path / "manifests" / f"{REVIEW_RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: review-of-reviewed-run\n"
        "status: complete\n"
        "checkpoint: the review record is stored\n",
        encoding="utf-8",
    )
    pointer = {
        "run_id": REVIEW_RUN_ID,
        "project": PROJECT,
        "repo": str(repository),
        "launch": "in-harness",
        "role": "review",
        "backend": "native",
        "created_at": "2026-09-19T13:00:00Z",
        "manifest_path": str(manifest),
        "node": {
            "id": REVIEW_NODE_ID,
            "plan": PLAN,
            "section": "s2",
            "time_budget": "25m",
            "role": "review",
            "write_paths": [str(record_path)],
        },
    }
    _write_json(pointer_path(REVIEW_RUN_ID), pointer)
    directory = Path(run_dir(REVIEW_RUN_ID))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "exit.json").write_text(
        json.dumps({"run_id": REVIEW_RUN_ID, "exit_code": 0}),
        encoding="utf-8",
    )


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
            "title": "One Promotion Target",
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
        json.dumps({PROJECT: str(root / "docs")}),
        encoding="utf-8",
    )
    return root


def _promote(repository: Path) -> dict:
    return promotion.complete(
        REVIEW_RUN_ID,
        gate="not-run",
        root=repository,
        promoted_by="review-self",
    )


def _divergent_paths(repository: Path) -> list[str]:
    """Every path whose HEAD, index and working-tree contents are not all equal."""
    listed = _git(repository, "status", "--porcelain")
    return [line for line in listed.splitlines() if line.strip()]


def _held_lease(run_id: str, *, host: str, pid: int, job: str) -> HostLease:
    """Claim the run's promotion lease as a foreign holder would."""
    lease = HostLease(crew_home(), f"promotion-{run_id}", host, pid, job)
    assert lease.claim(), "the fixture could not claim the lease it holds"
    return lease


# ── the cross-host case ─────────────────────────────────────────────────────


_DRIVER = '''
"""Promote the run once, holding this process's own node-local pointer lock.

Each process takes a lock path of its own, so the two locks do not exclude one
another as a node-local flock would not across hosts. A barrier releases both
into the promotion body together."""

import contextlib
import fcntl
import json
import os
import sys
import time
from pathlib import Path

repo, run_id, tag, barrier = sys.argv[1], sys.argv[2], sys.argv[3], Path(sys.argv[4])

import reckon.crew.promotion as promotion
from reckon.crew.runs import crew_home

_barrier_done = False


@contextlib.contextmanager
def _lock(lock_run_id):
    global _barrier_done
    path = crew_home() / "locks" / f"{lock_run_id}.{tag}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            if not _barrier_done:
                _barrier_done = True
                (barrier / f"{tag}.ready").write_text("1", encoding="utf-8")
                deadline = time.time() + 30
                while not (barrier / "a.ready").exists() or not (
                    barrier / "b.ready"
                ).exists():
                    if time.time() > deadline:
                        print(json.dumps({"ok": False, "error": "barrier timeout"}))
                        sys.exit(3)
                    time.sleep(0.005)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


promotion._pointer_lock = _lock
try:
    result = promotion.complete(
        run_id, gate="not-run", root=repo, promoted_by="review-self"
    )
    print(json.dumps({"ok": True, "pid": os.getpid(),
                      "job": os.environ.get("SLURM_JOB_ID", "")}))
except Exception as error:  # noqa: BLE001 - the driver reports the refusal verbatim
    print(json.dumps({"ok": False, "error": str(error), "type": type(error).__name__}),
          file=sys.stdout)
'''


def test_a_second_promotion_from_another_host_refuses_without_writing(
    repository: Path, isolated_reckon_home: Path, tmp_path: Path
) -> None:
    """Two promotions race from separate hosts; exactly one lands."""
    _build_review_run(repository, tmp_path)
    before = _git(repository, "rev-list", "--count", "HEAD")

    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    reckon_root = str(Path(promotion.__file__).resolve().parents[2])
    environment = {
        **os.environ,
        "RECKON_HOME": str(isolated_reckon_home),
        "PYTHONPATH": reckon_root,
        "WATCH_ARMING_ENV": "off",
    }

    processes = []
    for tag in ("a", "b"):
        environment["SLURM_JOB_ID"] = f"host-{tag}"
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(driver),
                    str(repository),
                    REVIEW_RUN_ID,
                    tag,
                    str(barrier),
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    reports = []
    for process in processes:
        out, err = process.communicate(timeout=120)
        reports.append(
            json.loads(out.strip().splitlines()[-1])
            if out.strip()
            else {"ok": False, "error": err}
        )

    landed = [report for report in reports if report.get("ok")]
    refused = [report for report in reports if not report.get("ok")]
    assert len(landed) == 1, reports
    assert len(refused) == 1, reports
    refusal = refused[0]
    assert refusal["type"] == "CrewError", reports
    holder = landed[0]
    assert str(holder["pid"]) in refusal["error"], refusal
    assert f"job {holder['job']}" in refusal["error"], refusal

    # Exactly one promotion landed, and the refused one wrote nothing: the run
    # is promoted once, its pointer is gone, and every written path reads equal
    # in HEAD, the index and the working tree.
    assert int(_git(repository, "rev-list", "--count", "HEAD")) > int(before)
    assert not pointer_path(REVIEW_RUN_ID).exists()
    assert _divergent_paths(repository) == []
    rows = ledger.runs(PROJECT, root=repository)
    assert [row["run_id"] for row in rows] == [REVIEW_RUN_ID]


# ── a stale lease is taken over ─────────────────────────────────────────────


def test_a_stale_lease_is_taken_over_and_the_promotion_lands(
    repository: Path, tmp_path: Path
) -> None:
    """A holder that stopped renewing never blocks the run forever."""
    _build_review_run(repository, tmp_path)
    lease = _held_lease(REVIEW_RUN_ID, host="gone-host", pid=4242, job="gone-job")
    old = time.time() - (LEASE_STALE_SECONDS + 5)
    os.utime(lease.path, (old, old))
    assert lease.holder() is None

    promoted = _promote(repository)

    assert promoted["run_id"] == REVIEW_RUN_ID
    assert [row["run_id"] for row in ledger.runs(PROJECT, root=repository)] == [
        REVIEW_RUN_ID
    ]


# ── a sweep's refused self-promotion ────────────────────────────────────────


def test_a_sweep_self_promotion_names_the_holder_that_refused_it(
    repository: Path, tmp_path: Path
) -> None:
    """A held lease reports a refused self-promotion naming the holder."""
    _build_review_run(repository, tmp_path)
    _held_lease(REVIEW_RUN_ID, host="other-host", pid=4242, job="other-job")
    pointer = json.loads(pointer_path(REVIEW_RUN_ID).read_text(encoding="utf-8"))

    report = promote_review_run_itself(pointer)

    assert report is not None
    assert report["promoted"] is False
    assert "other-host" in report["reason"], report
    assert "4242" in report["reason"], report
    assert "other-job" in report["reason"], report
    assert pointer_path(REVIEW_RUN_ID).is_file()
    assert ledger.runs(PROJECT, root=repository) == []
