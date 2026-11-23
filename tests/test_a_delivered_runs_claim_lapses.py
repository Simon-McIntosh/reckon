"""A delivered run's claim lapses once nobody is left to act on it.

A claim keeps binding after its worker stops for as long as the run's worktree
holds work nobody has landed. That protects a promotable commit from being
buried under a second writer. It assumes somebody will land the work. On 22
November two imas-ambix requests waited a day ``held-on-claim`` behind a run
that finished on 18 November with a ``blocked`` manifest, was never promoted,
reviewed or discarded, and whose dispatching session had ended: no actor was
left to act on the claim, so it stood for as long as the pointer did.

The cases here are the four the rule distinguishes, plus the record that keeps
a claim alive: a delivered run of an ended session is walked past after a day
and still held before it; a delivered run of a live session still holds its
paths; a working run holds them whatever its session; and a landing attempt an
hour ago keeps the claim. Every arm also proves the lapsed run was left exactly
as delivered — its pointer, manifest and worktree unchanged — because the rule
sets a claim aside, never the run.
"""

# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import queue_admission, runs
from tests.helpers.git import git as _git
from tests.test_a_queued_run_is_admitted import (  # noqa: F401 - fixture
    PROJECT,
    _config_for,
    _queue,
    admit_repo,
)

HOLDER_ID = "r-delivered-holder"
REQUEST_ID = "r-waiting-request"
# A figure directory is claimed as one path and no merge reconciles two writers
# inside it, so a request writing a file under it collides with the claim.
CLAIMED = "docs/figures/a-topic"
REQUESTED = "docs/figures/a-topic/figure.svg"
ENDED_SESSION = "8e41a6d3-ended"
LIVE_SESSION = "1a2b3c4d-live"
HOUR = 3600.0


def _dead_pid() -> int:
    """A pid no process holds, so a holder reads as a stopped run."""
    for candidate in (999996, 999997, 999998, 999999):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
    raise AssertionError("no dead pid available")


def _session_record(directory: Path, session: str, pid: int) -> None:
    """Write one harness session record naming a process that may be gone."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{session}.json").write_text(
        json.dumps({"sessionId": session, "pid": pid, "startedAt": 1, "name": session}),
        encoding="utf-8",
    )


def _harness_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the harness's session store at a directory of this test's own."""
    directory = tmp_path / "harness-sessions"
    monkeypatch.setenv("RECKON_HARNESS_SESSIONS_DIR", str(directory))
    _session_record(directory, ENDED_SESSION, _dead_pid())
    _session_record(directory, LIVE_SESSION, os.getpid())


def _holder_worktree(repository: Path, tmp_path: Path) -> tuple[Path, str, str]:
    """A stopped holder's worktree, carrying a commit the base does not have."""
    base = _git(repository, "rev-parse", "HEAD")
    tree = tmp_path / f"{HOLDER_ID}-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(tree), base)
    held = f"{CLAIMED}/existing.txt"
    (tree / held).parent.mkdir(parents=True)
    (tree / held).write_text("the delivered run's evidence\n", encoding="utf-8")
    _git(tree, "add", "--", held)
    _git(tree, "commit", "-q", "-m", "test: the delivered run's evidence")
    return tree, base, _git(tree, "rev-parse", "HEAD")


def _delivered_holder(
    repository: Path,
    tmp_path: Path,
    *,
    status: str = "blocked",
    session: str = ENDED_SESSION,
    pid: int | None = None,
    age_hours: float = 25.0,
    with_manifest: bool = True,
) -> dict[str, Any]:
    """Record a stopped run that delivered and then went quiet.

    Its worktree carries a commit past the base, so only the lapse rule walks
    past its claim: the settled rules read it as a stopped run holding unlanded
    work and keep it binding.
    """
    tree, base, head = _holder_worktree(repository, tmp_path)
    manifest = runs.run_dir(HOLDER_ID) / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True, exist_ok=True)
    if with_manifest:
        manifest.write_text(
            f"node: {HOLDER_ID}\nstatus: {status}\ncommits: [{head}]\n"
            "landing: the delivered record is not landed\n",
            encoding="utf-8",
        )
    pointer = {
        "run_id": HOLDER_ID,
        "project": PROJECT,
        "repo": str(repository),
        "session": session,
        "phase": "working",
        "worktree": str(tree),
        "base_sha": base,
        "head": head,
        "pid": _dead_pid() if pid is None else pid,
        "manifest_path": str(manifest) if with_manifest else "",
        "node": {
            "id": HOLDER_ID,
            "plan": "plan-a",
            "section": "dispatch",
            "time_budget": "20m",
            "write_paths": [CLAIMED],
        },
    }
    runs._write_json(runs.pointer_path(HOLDER_ID), pointer)
    aged = time.time() - age_hours * HOUR
    for path in (runs.pointer_path(HOLDER_ID), manifest):
        if path.exists():
            os.utime(path, (aged, aged))
    return {"pointer": pointer, "worktree": tree, "manifest": manifest}


def _queue_the_request(repository: Path) -> None:
    """Queue a request writing a file inside the directory the holder claims."""
    pointer = _queue(repository, node_id="figure", run_id=REQUEST_ID)
    pointer["node"]["write_paths"] = [REQUESTED]
    runs.queue_dispatch(pointer)


class _LaunchProbe:
    """Stand in for the launch, recording the requests a pass attempts."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.attempts: list[str] = []
        monkeypatch.setattr(queue_admission, "_launch_queued", self)

    def __call__(self, pointer, **_):
        self.attempts.append(str(pointer["run_id"]))
        return {"run_id": pointer["run_id"], "phase": "starting"}


def _verdicts(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row["run_id"]): row for row in result["verdicts"]}


def _snapshot(holder: dict[str, Any]) -> dict[str, Any]:
    """Everything about a delivered run that setting its claim aside must keep."""
    worktree = holder["worktree"]
    pointer = runs.pointer_path(HOLDER_ID)
    manifest = holder["manifest"]
    return {
        "pointer": pointer.read_bytes(),
        "pointer_mtime": pointer.stat().st_mtime_ns,
        "manifest": manifest.read_bytes(),
        "manifest_mtime": manifest.stat().st_mtime_ns,
        "head": _git(worktree, "rev-parse", "HEAD"),
        "status": _git(worktree, "status", "--porcelain"),
    }


def test_a_delivered_run_of_an_ended_session_gives_up_its_claim_after_a_day(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request is admitted, and the row names the run whose claim lapsed."""
    _, resolve = _config_for()
    _harness_sessions(tmp_path, monkeypatch)
    _queue_the_request(admit_repo)
    holder = _delivered_holder(admit_repo, tmp_path)
    before = _snapshot(holder)
    probe = _LaunchProbe(monkeypatch)

    result = queue_admission.admit_once(config_for=resolve)

    assert probe.attempts == [REQUEST_ID], "the lapsed claim must not cost a pass"
    assert result["launched"] == [REQUEST_ID]
    row = _verdicts(result)[REQUEST_ID]
    assert row["verdict"] == "launched", row
    assert HOLDER_ID in str(row.get("detail") or ""), row
    assert "claim is set aside" in str(row["detail"]), row
    assert row["set_aside"][0]["run_id"] == HOLDER_ID, row
    assert _snapshot(holder) == before, "the lapsed run must be left as delivered"


def test_the_same_claim_still_holds_the_request_before_the_day_passes(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At 23 hours nothing has lapsed yet, so the claim holds exactly as before."""
    _, resolve = _config_for()
    _harness_sessions(tmp_path, monkeypatch)
    _queue_the_request(admit_repo)
    _delivered_holder(admit_repo, tmp_path, age_hours=23.0)
    probe = _LaunchProbe(monkeypatch)

    result = queue_admission.admit_once(config_for=resolve)

    assert probe.attempts == [], "a claim inside its day must cost no dispatch"
    row = _verdicts(result)[REQUEST_ID]
    assert row["verdict"] == "held-on-claim", row
    assert HOLDER_ID in str(row["detail"]), row
    assert runs.read_pointer(REQUEST_ID)["phase"] == "queued"


def test_a_delivered_run_of_a_live_session_still_holds(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quiet records are not enough: a live dispatcher can still land the run."""
    _, resolve = _config_for()
    _harness_sessions(tmp_path, monkeypatch)
    _queue_the_request(admit_repo)
    _delivered_holder(admit_repo, tmp_path, session=LIVE_SESSION)
    probe = _LaunchProbe(monkeypatch)

    result = queue_admission.admit_once(config_for=resolve)

    assert probe.attempts == []
    row = _verdicts(result)[REQUEST_ID]
    assert row["verdict"] == "held-on-claim", row
    assert HOLDER_ID in str(row["detail"]), row


def test_a_working_runs_claim_holds_whatever_its_session(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run still working holds its paths even with an ended dispatcher."""
    _, resolve = _config_for()
    _harness_sessions(tmp_path, monkeypatch)
    _queue_the_request(admit_repo)
    _delivered_holder(
        admit_repo,
        tmp_path,
        session=ENDED_SESSION,
        pid=os.getpid(),
        with_manifest=False,
    )
    probe = _LaunchProbe(monkeypatch)

    result = queue_admission.admit_once(config_for=resolve)

    assert probe.attempts == []
    row = _verdicts(result)[REQUEST_ID]
    assert row["verdict"] == "held-on-claim", row
    assert HOLDER_ID in str(row["detail"]), row


def test_a_landing_attempt_an_hour_ago_keeps_the_claim(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Activity on any of the run's records restarts the day, not only the pointer."""
    from reckon.crew import promotion_requests

    _, resolve = _config_for()
    _harness_sessions(tmp_path, monkeypatch)
    _queue_the_request(admit_repo)
    _delivered_holder(admit_repo, tmp_path)
    request = promotion_requests._request_path(HOLDER_ID)
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text(json.dumps({"run_id": HOLDER_ID, "phase": "requested"}), "utf-8")
    recent = time.time() - HOUR
    os.utime(request, (recent, recent))
    probe = _LaunchProbe(monkeypatch)

    result = queue_admission.admit_once(config_for=resolve)

    assert probe.attempts == []
    row = _verdicts(result)[REQUEST_ID]
    assert row["verdict"] == "held-on-claim", row
    assert HOLDER_ID in str(row["detail"]), row


def _review_record_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, age_hours: float
) -> Path:
    """Stand in for the review store, answering one record of this test's own.

    The store is read through the module object, so replacing its lookup here
    exercises the same path a real record travels: a review written an hour ago
    must restart the day, because a review is somebody still acting on the run.
    """
    from reckon.crew import review_records

    record = tmp_path / "review-record.json"
    record.write_text(json.dumps({"run_id": HOLDER_ID, "status": "reviewed"}), "utf-8")
    written = time.time() - age_hours * HOUR
    os.utime(record, (written, written))
    monkeypatch.setattr(
        review_records,
        "stored_records_for_run",
        lambda _project, run_id: [(record, {"run_id": run_id})],
    )
    return record


def test_a_review_an_hour_ago_keeps_the_claim(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A review is a coordinator acting on the run, so its write restarts the day."""
    _, resolve = _config_for()
    _harness_sessions(tmp_path, monkeypatch)
    _queue_the_request(admit_repo)
    _delivered_holder(admit_repo, tmp_path)
    _review_record_for(monkeypatch, tmp_path, age_hours=1.0)
    probe = _LaunchProbe(monkeypatch)

    result = queue_admission.admit_once(config_for=resolve)

    assert probe.attempts == [], "a reviewed run is still being acted on"
    row = _verdicts(result)[REQUEST_ID]
    assert row["verdict"] == "held-on-claim", row
    assert HOLDER_ID in str(row["detail"]), row


def test_an_unreadable_review_store_keeps_the_claim(
    admit_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store that cannot be read is no evidence of quiet, so the claim stands."""
    from reckon.crew import review_records

    _, resolve = _config_for()
    _harness_sessions(tmp_path, monkeypatch)
    _queue_the_request(admit_repo)
    _delivered_holder(admit_repo, tmp_path)

    def _unreadable(_project: str, _run_id: str) -> list[tuple[Path, dict[str, Any]]]:
        raise RuntimeError("the review index cannot be read")

    monkeypatch.setattr(review_records, "stored_records_for_run", _unreadable)
    probe = _LaunchProbe(monkeypatch)

    result = queue_admission.admit_once(config_for=resolve)

    assert probe.attempts == [], "an unreadable store must never release a path"
    row = _verdicts(result)[REQUEST_ID]
    assert row["verdict"] == "held-on-claim", row
    assert HOLDER_ID in str(row["detail"]), row
