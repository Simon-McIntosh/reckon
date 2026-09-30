"""A recorded dropped lane is only dropped for the head the attempt covered.

The reflex demotes a lane from review routing when the run's own record says a
review there produced neither a stored nor an in-flight review: recomposing onto
that lane would repeat an attempt that has already failed. That reading was
head-blind. A run that gains commits after the dropped attempt is owed a *new*
review, and the lane that dropped the earlier revision has no bearing on the
new one — withholding it there can leave the re-review with no eligible lane at
all when the dropped lane is the only one that ever carried the run.

So the demotion is scoped to the head the recorded attempt composed for. Each
assertion here is argued inside out: an attempt recorded for the current head
must still demote the lane, because the whole point of the demotion is that an
unchanged run is not re-attempted on the lane that just dropped it; only an
attempt covering an earlier, or no, head frees the lane again.
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
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS

# The reflex is gated by the watch admission, so these tests arm the producer
# the gate reads rather than accepting the suite-wide waiver, which would let
# every dispatch through and prove nothing about the refusal.
pytestmark = pytest.mark.arms_watch_producer


LOCAL_BACKEND = "clive"
OTHER_BACKEND = "delta"


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


CONFIG = {
    "default_backend": LOCAL_BACKEND,
    "local_backend": LOCAL_BACKEND,
    "backends": {
        LOCAL_BACKEND: _backend("codex"),
        OTHER_BACKEND: _backend("claude"),
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def isolated_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path, dict]:
    """A project whose run tree carries two revisions to tell heads apart."""
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
        '<h2 id="s2">A re-review at a new head keeps its lane</h2>',
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

    earlier_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # A second commit moves the run past the revision the recorded attempt read.
    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    for arguments in (
        ["add", "later.txt"],
        ["commit", "-q", "-m", "feat: a later revision"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    current_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    (config_home / "mounts.json").write_text(
        '{"sample": "' + str(repo / "docs") + '"}', encoding="utf-8"
    )

    dispatch_module = importlib.import_module("reckon.crew.dispatch")

    def prepare_worktree(_repo: Path, session: str, node: str, base: str) -> dict:
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": base, "base_sha": current_sha}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    heads = {"earlier": earlier_sha, "current": current_sha}
    return config_home, repo, heads


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


def _scoring_pointer(
    config_home: Path,
    repo: Path,
    run_id: str,
    *,
    previous: dict | None = None,
    commits: str | None = None,
    worktree: str | None = None,
) -> dict:
    manifest = config_home / "manifests" / (run_id + ".md")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: "
        + run_id
        + "\nstatus: complete\ncommits: "
        + (commits or run_id)
        + "\n",
        encoding="utf-8",
    )
    record = {
        "run_id": run_id,
        "project": "sample",
        "repo": str(repo),
        "node": {"id": run_id, "plan": "fixture", "section": "s2"},
        "backend": LOCAL_BACKEND,
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    }
    if worktree is not None:
        record["worktree"] = worktree
    if previous is not None:
        record[recovery.REVIEW_DISPATCH_FIELD] = previous
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def _failed_attempt(
    backend: str,
    *,
    head: str | None,
    status: str = "dispatched",
    run_id: str | None = None,
) -> dict:
    """A review dispatch the run records, whose review run is no longer alive.

    ``head`` is the revision the attempt composed for: the field the reflex
    compares against the run's current head to decide whether the attempt still
    speaks for it. ``None`` models a record that names no head at all.
    ``status`` selects what the attempt reached: a ``refused`` or
    ``awaiting-lane`` attempt never started a lane. ``run_id`` names the review
    run the attempt launched, whose own records then show whether a worker ever
    started; ``None`` models a record naming no review run at all.
    """
    recorded = {
        "status": status,
        "reason": f"the review dispatched automatically as run r-{backend}-dead",
        "run_id": run_id,
        "backend": backend,
        "at": "2026-09-22T10:00:00Z",
        "attempt": 1,
    }
    if head is not None:
        recorded["head"] = head
    return recorded


def _review_run_evidence(run_id: str, *, state: str) -> None:
    """Write a dead review run's own records for the launch/withdrawal split.

    ``state`` names how far the attempt got, because the two records answer
    different questions and a class of failure reads the same in one as a
    never-launched attempt:

    - ``withdrawn``: the supervisor refused before any worker existed, so no
      worker record is written and the exit record says the launch ended with
      no stream byte read.
    - ``spawned-no-stream``: a worker was spawned and died before it read a
      single stream record, so the worker record the supervisor writes at spawn
      is present while the exit record still reads zero stream records and the
      launch ending.
    - ``launched``: a worker was spawned and read stream records before it
      ended.
    """
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    worker_record = directory / recovery.WORKER_RECORD_NAME
    if state in {"spawned-no-stream", "launched"}:
        crew._write_json(
            worker_record,
            {"run_id": run_id, "pid": 4242, "launched_at": "2026-09-22T10:00:00Z"},
        )
    else:
        worker_record.unlink(missing_ok=True)
    read_stream = state == "launched"
    crew._write_json(
        directory / recovery.EXIT_RECORD_NAME,
        {
            "run_id": run_id,
            "attempt": 1,
            "stream_records_seen": 12 if read_stream else 0,
            "ended_during": "working" if read_stream else "launch",
        },
    )


def _compose(record: dict) -> dict:
    with runs.follower_claim("sample", "session-orchestrating", delivery="stream"):
        return recovery.dispatch_review_for_run(
            record, config=CONFIG, launcher=lambda *a, **k: os.getpid()
        )


def test_a_review_at_a_new_head_keeps_the_lane_that_dropped_the_old_one(
    isolated_project: tuple[Path, Path, dict],
) -> None:
    """An attempt for an earlier head does not demote its lane at the new head."""
    config_home, repo, heads = isolated_project
    record = _scoring_pointer(
        config_home,
        repo,
        "r-rereview",
        previous=_failed_attempt(LOCAL_BACKEND, head=heads["earlier"]),
    )
    try:
        report = _compose(record)
        assert report["dispatched"] is True
        assert report.get("awaiting_lane") is not True
        assert report["backend"] == LOCAL_BACKEND
        landed = runs.read_pointer(report["review_run_id"])
        assert landed["backend"] == LOCAL_BACKEND
    finally:
        _release_watcher()


def test_a_recorded_attempt_for_the_current_head_still_demotes_its_lane(
    isolated_project: tuple[Path, Path, dict],
) -> None:
    """The unchanged run is not re-attempted on the lane that just dropped it."""
    config_home, repo, heads = isolated_project
    record = _scoring_pointer(
        config_home,
        repo,
        "r-same-head",
        previous=_failed_attempt(LOCAL_BACKEND, head=heads["current"]),
    )
    try:
        report = _compose(record)
        assert report["dispatched"] is True
        assert report["backend"] == OTHER_BACKEND
        assert report["backend"] != LOCAL_BACKEND
    finally:
        _release_watcher()


def test_a_reclaimed_worktree_reads_the_head_from_the_run_record(
    isolated_project: tuple[Path, Path, dict],
) -> None:
    """A gone worktree must not make the comparison read the shared checkout.

    The run's repository HEAD (``current``) differs from the revision its own
    record names (``earlier``), so a comparison that falls back to the
    repository would free the lane at a head its record never reached. Reading
    the record instead keeps the lane demoted for the head the attempt covered.
    """
    config_home, repo, heads = isolated_project
    record = _scoring_pointer(
        config_home,
        repo,
        "r-reclaimed",
        previous=_failed_attempt(LOCAL_BACKEND, head=heads["earlier"]),
        commits=heads["earlier"],
        worktree=str(repo.parent / "worktrees" / "reclaimed-and-gone"),
    )
    try:
        report = _compose(record)
        assert report["dispatched"] is True
        assert report["backend"] == OTHER_BACKEND
        assert report["backend"] != LOCAL_BACKEND
    finally:
        _release_watcher()


def test_a_reclaimed_worktree_composes_its_review_for_the_recorded_head(
    isolated_project: tuple[Path, Path, dict],
) -> None:
    """The composed dispatch names the run's own head, not the shared checkout.

    The run's repository HEAD (``current``) differs from the head its record
    names (``earlier``), so a composition that fell back to the checkout would
    grant the head-keyed record path for the wrong revision.
    """
    config_home, repo, heads = isolated_project
    record = _scoring_pointer(
        config_home,
        repo,
        "r-compose-reclaimed",
        commits=heads["earlier"],
        worktree=str(repo.parent / "worktrees" / "gone"),
    )
    fields = recovery._review_dispatch_fields(record)
    assert fields["head"] == heads["earlier"]
    assert any(heads["earlier"] in path for path in fields["write_paths"])


def test_a_recorded_attempt_naming_no_head_does_not_demote_its_lane(
    isolated_project: tuple[Path, Path, dict],
) -> None:
    """An attempt that cannot be tied to the current head withholds nothing."""
    config_home, repo, _heads = isolated_project
    record = _scoring_pointer(
        config_home,
        repo,
        "r-headless",
        previous=_failed_attempt(LOCAL_BACKEND, head=None),
    )
    try:
        report = _compose(record)
        assert report["dispatched"] is True
        assert report["backend"] == LOCAL_BACKEND
    finally:
        _release_watcher()


def test_a_withdrawn_before_launch_attempt_recomposes_onto_its_lane(
    isolated_project: tuple[Path, Path, dict],
) -> None:
    """A run withdrawn before a worker launched never dropped the lane.

    The attempt reached the claim and was refused there — a name, claim or
    worktree clash — so no review ever ran and the lane that carried the run is
    free to carry the next sweep's composition rather than being withheld.
    """
    config_home, repo, heads = isolated_project
    record = _scoring_pointer(
        config_home,
        repo,
        "r-withdrawn",
        previous=_failed_attempt(
            LOCAL_BACKEND,
            head=heads["current"],
            run_id="r-clive-withdrawn",
        ),
    )
    _review_run_evidence("r-clive-withdrawn", state="withdrawn")
    try:
        report = _compose(record)
        assert report["dispatched"] is True
        assert report["backend"] == LOCAL_BACKEND
    finally:
        _release_watcher()


def test_a_refused_attempt_recomposes_onto_its_lane(
    isolated_project: tuple[Path, Path, dict],
) -> None:
    """A dispatch refused at admission started no lane, so none was dropped."""
    config_home, repo, heads = isolated_project
    record = _scoring_pointer(
        config_home,
        repo,
        "r-refused",
        previous=_failed_attempt(
            LOCAL_BACKEND,
            head=heads["current"],
            status="refused",
            run_id="r-clive-refused",
        ),
    )
    try:
        report = _compose(record)
        assert report["dispatched"] is True
        assert report["backend"] == LOCAL_BACKEND
    finally:
        _release_watcher()


def test_a_launched_attempt_with_no_stored_review_steers_off_its_lane(
    isolated_project: tuple[Path, Path, dict],
) -> None:
    """An attempt whose worker ran and stored nothing is a real drop.

    The review run's own record shows a worker that read stream records and
    ended, and no review stands for the head — so the lane that carried the run
    is steered away from.
    """
    config_home, repo, heads = isolated_project
    record = _scoring_pointer(
        config_home,
        repo,
        "r-launched",
        previous=_failed_attempt(
            LOCAL_BACKEND,
            head=heads["current"],
            run_id="r-clive-launched",
        ),
    )
    _review_run_evidence("r-clive-launched", state="launched")
    try:
        report = _compose(record)
        assert report["dispatched"] is True
        assert report["backend"] == OTHER_BACKEND
        assert report["backend"] != LOCAL_BACKEND
    finally:
        _release_watcher()


def test_a_spawned_worker_that_wrote_no_stream_record_is_not_withdrawn() -> None:
    """A spawned worker is not read as withdrawn just because it wrote no stream.

    The worker record the supervisor writes at spawn is present — positive
    evidence a worker launched — even though the exit record reads zero stream
    records and the launch ending, the same shape a never-launched withdrawal
    leaves. Reading only the exit record reports the lane as one that never
    dropped the head and frees it, so the check that gates the lane reads the
    worker record too. The helper is asserted directly here rather than through
    a composed dispatch, so the assertion holds wherever the helper runs; the
    same run is then rewritten as a genuine withdrawal, which the helper must
    still read as one.
    """
    _review_run_evidence("r-spawned-no-stream", state="spawned-no-stream")
    assert (
        recovery._review_attempt_withdrawn_before_launch("r-spawned-no-stream") is False
    )
    _review_run_evidence("r-spawned-no-stream", state="withdrawn")
    assert (
        recovery._review_attempt_withdrawn_before_launch("r-spawned-no-stream") is True
    )
