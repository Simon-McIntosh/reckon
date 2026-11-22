"""A landed run still reads as landed after every retention pass.

A request waiting on a ``run:`` ref is released when that run's promoted
revision reaches the base branch. That revision used to be read from the run's
own document, so a retention pass that removed the document — the store's
archive rebuild, which follows the committed ledger — left nothing to read, and
a landed run answered as one that was never promoted: its waiters never
released. The store now keeps the landing apart from the row it came with, and
the read the admission service uses answers it after the row is retired.

The negative case is the other half: a run that was discarded without landing
never writes that record, so it is still reported as not landed after the same
passes.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import ledger, run_store
from reckon.crew import promotion, runs
from reckon.crew.admission import readiness
from reckon.crew.admission.shared import SATISFIED
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures
from tests import test_queue_dag_waits as dag

PROJECT = fixtures.PROJECT
LANDED = "r-landed"
KEEPER = "r-keeper"
DISCARDED = "r-discarded"
WAITER = "r-waiting"


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A mounted checkout whose config-home state resolves to its own tree.

    The store rebuild reads a project's committed documents through the mount,
    while an admission reader resolves the same ledger through the config home.
    A real machine joins the two with a symlink, so the fixture does too.
    """
    repository = fixtures.chain_repo.__wrapped__(tmp_path, monkeypatch)
    state = tmp_path / "config" / "state"
    state.mkdir(parents=True, exist_ok=True)
    link = state / PROJECT
    if not link.exists():
        os.symlink(repository / "docs" / "state" / PROJECT, link)
    return repository


def _manifest(run_id: str, status: str) -> None:
    """Write the run's manifest, which is where its delivery verdict is read."""
    manifest = runs.run_dir(run_id) / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"status: {status}\n", encoding="utf-8")


def _seed(first: Path, run_id: str, *, revision: str | None, status: str) -> None:
    """Record one run the way a promotion does: a manifest, document and row."""
    _manifest(run_id, status)
    record = {
        "run_id": run_id,
        "completed_at": "2026-11-01T00:00:00+00:00",
        "status": status,
    }
    if revision:
        record["promoted_revision"] = revision
    ledger.append_run(PROJECT, record, root=first, allow_create=True)


def _land(first: Path, run_id: str, revision: str) -> None:
    """Rewind a delivered run's recorded revision to the one now on the branch."""
    document = ledger.run_path(PROJECT, run_id)
    row = json.loads(document.read_text(encoding="utf-8"))
    row["promoted_revision"] = revision
    ledger.write_run_file(PROJECT, row)


def _retention_passes(first: Path, run_id: str) -> list[str]:
    """Run every retention pass the store and the ledger offer, in order.

    Each name returned is a pass that ran, so a failure names the pass that
    took the row rather than leaving a reader to infer it from an absence.
    """
    applied: list[str] = []
    before = datetime.now(tz=UTC) + timedelta(days=1)
    with run_store.RunStore() as store:
        report = store.retire(
            before=before, protected=set(), projects={PROJECT}, apply=True
        )
    assert run_id in report["retired"], "the archive pass always takes an aged row"
    applied.append("retire")

    # The ledger's own retirement: the run's document leaves the tree. The
    # store's next membership read sees history the committed ledger no longer
    # carries and rebuilds the project from what is committed, which removes
    # the archived copy too. A run that stays committed keeps the ledger
    # reading as a ledger rather than as an absent mount.
    ledger.run_path(PROJECT, KEEPER).exists()
    ledger.run_path(PROJECT, run_id).unlink()
    applied.append("document")

    with run_store.RunStore() as store:
        store.durable_rows(PROJECT)
    applied.append("rebuild")
    return applied


def _request_state(run_id: str) -> str:
    return readiness._request_state(f"run:{run_id}", live=[], project=PROJECT)


def test_a_landed_run_reads_as_landed_after_its_row_is_retired(
    checkout: Path,
) -> None:
    """Every retention pass can remove the row; the landing still answers."""
    revision = _git(checkout, "rev-parse", "HEAD")
    _seed(checkout, KEEPER, revision=revision, status="complete")
    _seed(checkout, LANDED, revision=revision, status="complete")
    assert _request_state(LANDED) == SATISFIED

    applied = _retention_passes(checkout, LANDED)

    with run_store.RunStore() as store:
        assert store.get_run(LANDED) is None, f"the row is gone after {applied}"
    assert not ledger.run_path(PROJECT, LANDED).exists()

    # The admission read is the claim under test: with the row and the
    # document both removed, a landed run must still answer as landed.
    assert _request_state(LANDED) == SATISFIED
    assert run_store.promoted_revision(PROJECT, LANDED) == revision


def test_a_discarded_run_is_not_reported_as_landed(checkout: Path) -> None:
    """A run with no landing writes no landing record, whatever it kept."""
    revision = _git(checkout, "rev-parse", "HEAD")
    _seed(checkout, KEEPER, revision=revision, status="complete")
    _seed(checkout, DISCARDED, revision=None, status="failed")
    marker = promotion.discard_spool_path(DISCARDED)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"run_id": DISCARDED}), encoding="utf-8")

    _retention_passes(checkout, DISCARDED)

    assert run_store.promoted_revision(PROJECT, DISCARDED) == ""
    assert _request_state(DISCARDED) != SATISFIED


def test_a_request_waiting_on_the_landed_run_is_released(checkout: Path) -> None:
    """The pass releases the request after the landed run's revision is kept."""
    head = _git(checkout, "rev-parse", "HEAD")
    # A revision that exists but is not an ancestor of the base branch: the run
    # has delivered and its work is not on the branch yet.
    tree = _git(checkout, "rev-parse", f"{head}^{{tree}}")
    unmerged = _git(checkout, "commit-tree", tree, "-p", head, "-m", "wip: unmerged")
    _seed(checkout, KEEPER, revision=head, status="complete")
    _seed(checkout, LANDED, revision=unmerged, status="complete")

    fixtures._declare_wait_refs(checkout, section="s2", refs=[])
    dag._queue(
        checkout,
        dag._node("waiter", "s2", after=[LANDED]),
        run_ids={"waiter": WAITER},
    )
    assert runs.read_pointer(WAITER)["phase"] == "awaiting-upstream", (
        "the wait is held while the run's revision is off the branch"
    )

    _land(checkout, LANDED, head)
    _retention_passes(checkout, LANDED)

    result, launched = dag._admit()
    assert result["promoted_upstream"] == [WAITER]
    assert launched, result
