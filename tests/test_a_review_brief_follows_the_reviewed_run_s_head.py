"""A review brief follows the head the reviewed run actually delivered.

The brief a review reads is composed from the reviewed run's record, and until
now it reached the run's directory only when a review dispatch named it. Two
deliveries are therefore left without a current brief: a run that delivered,
was resumed and delivered again keeps whichever brief an earlier dispatch wrote,
and a run whose obligations a coordinator deferred advances no dispatch at all,
so no brief is ever written and the coordinator composes one by hand.

Both cases are driven through the delivery itself: the pointer is advanced to
its delivered phase the way the launch supervisor advances it, and the brief is
read back. Nothing here calls the composition directly, so a writer that runs
anywhere but at delivery fails these cases.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

import reckon.crew.dispatch_launch as dispatch_launch_module
from reckon.crew import recovery_review_subject, runs

RUN_ID = "r-fixture-review-brief-follows-head"
NODE_ID = "a-fixture-node"
PROJECT = "fixture"

#: The declared mutation: the brief is composed once, at the run's first
#: delivery, so a later delivery leaves the revision the run has moved past.
FIRST_DELIVERY_ONLY = (
    "write the brief only at the first delivery, so the resumed run's brief "
    "then names the old head"
)

NEGATIVE_CONTROL = os.environ.get("RECKON_BRIEF_FIRST_DELIVERY_ONLY", "").strip()


@pytest.fixture(autouse=True)
def _declared_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the declared mutation while its variable is set.

    The writer is replaced by one that composes the brief only when none is on
    disk: the run's first delivery writes it, and a delivery after a resume
    then leaves it naming the head the run has moved past.
    """
    if not NEGATIVE_CONTROL:
        return
    composed_once = recovery_review_subject.write_delivered_run_review_brief

    def first_delivery_only(record):
        try:
            path = recovery_review_subject._review_brief_path(record)
        except (OSError, ValueError):
            return None
        if path.exists():
            return None
        return composed_once(record)

    monkeypatch.setattr(
        recovery_review_subject,
        "write_delivered_run_review_brief",
        first_delivery_only,
    )


def _git(tree: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=tree,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _commit(tree: Path, name: str) -> str:
    (tree / "work.txt").write_text(name + "\n", encoding="utf-8")
    _git(tree, "add", "work.txt")
    _git(
        tree,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        name,
    )
    return _git(tree, "rev-parse", "HEAD")


@pytest.fixture
def delivered_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A worker run whose manifest reads complete and whose tree has a head."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    tree = tmp_path / "tree"
    tree.mkdir()
    _git(tree, "init", "-b", "main")
    base = _commit(tree, "first")
    home = tmp_path / "home"
    (home / "crew" / "live").mkdir(parents=True)
    run_directory = home / "crew" / "runs" / RUN_ID
    run_directory.mkdir(parents=True)
    manifest = run_directory / "manifest.md"
    manifest.write_text(f"node: {NODE_ID}\nstatus: complete\n", encoding="utf-8")
    record = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "worktree": str(tree),
        "base_sha": base,
        "manifest_path": str(manifest),
        "phase": "working",
        "attempt": 1,
        "node": {"id": NODE_ID},
    }
    runs.pointer_path(RUN_ID).write_text(json.dumps(record), encoding="utf-8")
    return {
        "tree": tree,
        "record": record,
        "base": base,
        "run_directory": run_directory,
    }


def _deliver(run: dict[str, Any]) -> None:
    """Advance the pointer to its delivered phase, as the supervisor does."""
    dispatch_launch_module._publish_stored_phase(
        {"run_id": RUN_ID, "attempt": run["record"]["attempt"]}, ended=True
    )


def _brief(run: dict[str, Any]) -> Path:
    return runs.run_dir(RUN_ID) / runs.RUN_REVIEW_BRIEF_NAME


def _named_head(run: dict[str, Any]) -> str:
    return recovery_review_subject.brief_named_head(_brief(run))


def test_a_delivered_run_gets_the_brief_its_review_will_read(
    delivered_run: dict[str, Any],
) -> None:
    """Delivery writes the brief, naming the head the run delivered."""
    _deliver(delivered_run)

    assert _brief(delivered_run).is_file()
    assert _named_head(delivered_run) == delivered_run["base"]


def test_a_resumed_run_s_brief_names_its_new_head(
    delivered_run: dict[str, Any],
) -> None:
    """A delivery after a resume rewrites the brief at the revision it delivered.

    The brief written by the first delivery names the head that delivery
    reached. The resume commits another revision and delivers again, so a brief
    that still names the first head is describing work the run has moved past.
    """
    _deliver(delivered_run)
    assert _named_head(delivered_run) == delivered_run["base"]

    moved = _commit(delivered_run["tree"], "second")
    _deliver(delivered_run)

    assert _named_head(delivered_run) == moved


def test_a_deferred_run_receives_its_brief_on_delivery(
    delivered_run: dict[str, Any],
) -> None:
    """A deferral of the run's obligations does not withhold its brief.

    The coordinator deferred this run's duties, which is why no review
    dispatch follows it. The brief is part of the run's deliverable rather than
    of the duties deferred, so delivery writes it either way.
    """
    runs.record_run_acknowledgement(
        RUN_ID,
        "awaiting a peer's release",
        "2999-01-01T00:00:00+00:00",
        project=PROJECT,
    )

    _deliver(delivered_run)

    assert _brief(delivered_run).is_file()
    assert _named_head(delivered_run) == delivered_run["base"]
