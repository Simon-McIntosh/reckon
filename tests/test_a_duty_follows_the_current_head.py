"""A resume withdraws the duties that speak for the head it replaced.

A complete run owing a review is resumed from the revision it delivered. Until
its worker delivers the next head, the coordinator owes nothing against that
revision: the review duty is withdrawn, and a review already queued for it is
withdrawn rather than launched. Once the run delivers a head of its own, the
duty returns naming that head. A run nobody resumed keeps its duties, which is
the control that shows the withdrawal is the resume's doing rather than the
reader answering nothing.

Every case is driven against a real git history, so the head a duty names is a
revision the run's own tree carries.
"""

from __future__ import annotations

import importlib
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew.node import CrewError

obligations_module = importlib.import_module("reckon.crew.obligations")
resumption_module = importlib.import_module("reckon.crew.resumption")

PROJECT = "duty-head-fixture"
SESSION = "coordinator-fixture"
OBSERVED_AT = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)

pytestmark = pytest.mark.arms_watch_producer


def _minutes_ago(minutes: int) -> str:
    """A stamp a fixture recorded that many minutes before the test runs.

    Derived from the clock the resume itself is stamped against, because the
    withdrawal compares the two instants: a literal date would place the queued
    attempt after the resume the moment the suite ran on a later day, and the
    case would pass without the withdrawal ever being exercised.
    """
    return (datetime.now(tz=UTC) - timedelta(minutes=minutes)).isoformat()


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _commit(repository: Path, paths: dict[str, str], message: str) -> str:
    for name, body in paths.items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    subprocess.run(
        ["git", "add", *paths], cwd=repository, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repo = tmp_path / "repo"
    repo.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(repo, *arguments)
    _commit(repo, {"seed.txt": "seed\n"}, "chore: seed")
    delivered = _commit(
        repo,
        {
            "pkg/mod.py": "VALUE = 1\n",
            "docs/plans/fixture-plan.html": (
                '<meta name="docs-project" content="' + PROJECT + '">'
                '<meta name="reckon-type" content="plan">'
                '<meta name="plan-slug" content="fixture-plan">'
                '<h2 id="delivery">A duty follows the run current head</h2>'
            ),
        },
        "feat: the delivered change",
    )
    (config_home / "mounts.json").write_text(
        '{"' + PROJECT + '": "' + str(repo / "docs") + '"}'
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return {"config_home": config_home, "repo": repo, "delivered": delivered}


def _write_run(
    world: dict, *, run_id: str, head: str, extra: dict | None = None
) -> dict:
    """One complete, unpromoted implement run whose worktree is the repo."""
    repo = world["repo"]
    manifest = world["config_home"] / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n", encoding="utf-8"
    )
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repo),
        "worktree": str(repo),
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": run_id,
            "plan": "fixture-plan",
            "write_paths": ["pkg/mod.py"],
        },
    }
    pointer.update(extra or {})
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _duties(run_id: str) -> list[dict]:
    """Every duty the fixture session owes for one run, by kind."""
    report = obligations_module.obligations(PROJECT, SESSION)
    return [item for item in report["obligations"] if str(item.get("run_id")) == run_id]


def _kinds(run_id: str) -> list[str]:
    return sorted(str(item["kind"]) for item in _duties(run_id))


def test_a_complete_run_owing_a_review_carries_review_missing(world):
    """The control: nothing resumed, so the duty stands at the delivered head."""
    run_id = "r-not-resumed"
    _write_run(world, run_id=run_id, head=world["delivered"])

    duties = _duties(run_id)

    assert [item["kind"] for item in duties] == ["review-missing"]
    assert recovery.same_revision(duties[0]["head"], world["delivered"])


def test_a_resume_withdraws_the_review_duty_for_the_head_it_replaced(world):
    run_id = "r-resumed-withdrawn"
    _write_run(world, run_id=run_id, head=world["delivered"])
    assert _kinds(run_id) == ["review-missing"]

    recorded = resumption_module._record_resume_supersedes_head(run_id)

    assert recovery.same_revision(recorded, world["delivered"])
    assert _kinds(run_id) == []


def test_a_run_that_delivers_a_new_head_owes_a_review_for_that_head(world):
    run_id = "r-resumed-redelivered"
    _write_run(world, run_id=run_id, head=world["delivered"])
    resumption_module._record_resume_supersedes_head(run_id)
    assert _kinds(run_id) == []

    delivered_again = _commit(
        world["repo"], {"pkg/mod.py": "VALUE = 2\n"}, "fix: carry the run on"
    )

    duties = _duties(run_id)

    assert [item["kind"] for item in duties] == ["review-missing"]
    assert recovery.same_revision(duties[0]["head"], delivered_again)


def test_a_queued_review_is_withdrawn_and_recorded_as_superseded(world):
    run_id = "r-resumed-queued-review"
    review_run_id = "r-review-of-the-replaced-head"
    _write_run(
        world,
        run_id=run_id,
        head=world["delivered"],
        extra={
            recovery.REVIEW_DISPATCH_FIELD: {
                "status": "dispatched",
                "reason": "the review dispatched automatically",
                "run_id": review_run_id,
                "head": world["delivered"],
                "at": _minutes_ago(30),
                "attempt": 1,
            }
        },
    )
    runs._write_json(
        runs.pointer_path(review_run_id),
        {
            "run_id": review_run_id,
            "project": PROJECT,
            "session": SESSION,
            "classification": "queued",
            "role": "review",
            "node": {"id": review_run_id, "plan": "fixture-plan"},
        },
    )

    resumption_module._record_resume_supersedes_head(run_id)
    report = recovery.withdraw_superseded_review(runs.read_pointer(run_id))

    assert report is not None and report["withdrawn"] is True
    record = runs.read_pointer(run_id)[recovery.REVIEW_DISPATCH_FIELD]
    assert record["status"] == "withdrawn"
    assert "superseded by that resume" in record["reason"]
    # The queued review's own pointer is gone, so no admission pass can launch
    # it for the head the resume replaced.
    with pytest.raises(CrewError):
        runs.read_pointer(review_run_id)
