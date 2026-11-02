"""A request's wait always names the project its run lands in.

A request stored ``awaiting-upstream`` on a ``run:`` ref is released when that
run's promoted revision reaches the base branch. The release is read from the
ref's project, so a pointer that names no project for the run it waits on never
leaves ``awaiting-upstream``: it reads as a wait with nothing to land, and no
pass clears it. That is what held a request for three hours after its upstream
landed. Every writer of a request's waits records the project beside the ref in
the same mutation, and the reader derives a missing entry the same way, so a
pointer written before the record carried projects is still released. A ref no
project holds is reported under its own obligation kind instead of reading as
waiting upstream.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import obligations, queue_admission, runs
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures
from tests import test_queue_dag_waits as dag

PROJECT = fixtures.PROJECT
SESSION = fixtures.SESSION


@pytest.fixture()
def projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Two mounted checkouts, so a wait can name a run in either project."""
    first = fixtures.chain_repo.__wrapped__(tmp_path, monkeypatch)
    second = tmp_path / "other-checkout"
    (second / "docs" / "plans").mkdir(parents=True)
    (second / "docs" / "state" / "other").mkdir(parents=True)
    (second / "docs" / "state" / "other" / "index.json").write_text(
        json.dumps({"project": "other", "data": {"_version": 0}}),
        encoding="utf-8",
    )
    dag._write_other_plan(second)
    dag._git(second, "init", "-q", "-b", "main")
    dag._git(second, "config", "user.email", "worker@example.invalid")
    dag._git(second, "config", "user.name", "Worker")
    dag._git(second, "add", "--", "docs")
    dag._git(second, "commit", "-q", "-m", "chore: seed checkout")
    (tmp_path / "config" / "mounts.json").write_text(
        json.dumps(
            {fixtures.PROJECT: str(first / "docs"), "other": str(second / "docs")}
        ),
        encoding="utf-8",
    )
    return first, second


def _pointer(run_id: str) -> dict:
    return runs.read_pointer(run_id)


def _forget_wait_projects(run_id: str) -> None:
    """Strip the projects a request names, as a pre-record pointer carries none."""

    def forget(pointer: dict) -> dict:
        pointer.pop("wait_projects", None)
        return pointer

    runs._mutate_pointer(run_id, forget)


def _waiting_request(first: Path, *, upstream: bool = True) -> dict:
    """One request stored waiting on a run ref, with the plan declaring none.

    The plan's own section waits are cleared, so the request's stored waits are
    exactly its own ``--after`` refs: a pass re-derives that same list and
    writes nothing, which is what makes the reading below the reader's alone.
    """
    fixtures._declare_wait_refs(first, section="s2", refs=[])
    if upstream:
        dag._upstream_record(first, "r-prior", status="complete", revision=None)
    stored = dag._queue(
        first,
        dag._node("waiting", "s2", after=["r-prior"]),
        run_ids={"waiting": "r-waiting"},
    )
    assert stored["r-waiting"]["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
    return stored


def test_a_pointer_naming_no_project_is_released_by_the_reader(
    projects: tuple[Path, Path],
) -> None:
    """The reader derives the project a stored pointer does not name.

    The pointer's waits already equal what the plan derives, so the writer has
    nothing to rewrite: only the reader can resolve a ref whose project is
    missing, and the request is released by the first pass after its upstream's
    promoted revision is on the base branch.
    """
    first, _ = projects
    _waiting_request(first)
    _forget_wait_projects("r-waiting")
    assert not _pointer("r-waiting").get("wait_projects")

    dag._upstream_record(
        first,
        "r-prior",
        status="complete",
        revision=dag._git(first, "rev-parse", "HEAD"),
    )
    result, launched = dag._admit()

    assert result["promoted_upstream"] == ["r-waiting"]
    assert launched, "the request is released by the first pass after the landing"
    assert not queue_admission.unresolvable_wait_obligations(_pointer("r-waiting"))


def test_the_writer_records_the_project_of_a_ref_it_adds(
    projects: tuple[Path, Path],
) -> None:
    """A ref the plan edit adds carries its project in the same write."""
    first, _ = projects
    fixtures._declare_wait_refs(first, section="s2", refs=[])
    stored = dag._queue(
        first,
        dag._node("waiter", "s2"),
        run_ids={"waiter": "r-waiting"},
    )
    assert stored["r-waiting"]["phase"] == queue_admission.QUEUED_PHASE
    assert not _pointer("r-waiting").get("waits")
    dag._upstream_record(first, "r-prior", status="blocked", revision=None)

    fixtures._declare_wait_refs(first, section="s2", refs=["run:r-prior"])
    dag._admit()

    pointer = _pointer("r-waiting")
    assert "run:r-prior" in (pointer.get("waits") or ())
    assert pointer["wait_projects"] == {"r-prior": PROJECT}


def test_every_writer_leaves_every_run_ref_with_a_project(
    projects: tuple[Path, Path],
) -> None:
    """Whichever writer last touched the waits, every run ref names a project."""
    first, _ = projects
    _waiting_request(first)
    _forget_wait_projects("r-waiting")
    fixtures._declare_wait_refs(first, section="s2", refs=["run:r-other"])
    dag._upstream_record(first, "r-other", status="blocked", revision=None)
    dag._admit()

    pointer = _pointer("r-waiting")
    run_refs = {
        ref[len("run:") :]
        for ref in pointer.get("waits") or ()
        if str(ref).startswith("run:")
    }
    assert run_refs, "the fixture stores at least one run ref"
    named = dict(pointer.get("wait_projects") or {})
    assert run_refs <= set(named)
    assert named["r-prior"] == PROJECT


def test_a_run_no_project_holds_raises_wait_unresolvable(
    projects: tuple[Path, Path],
) -> None:
    """A wait nothing can land raises its own obligation, not a waiting row."""
    first, _ = projects
    _waiting_request(first)
    _forget_wait_projects("r-waiting")
    # Nothing holds the run: no live pointer, and no ledger row in any project.
    ledger.run_path(PROJECT, "r-prior").unlink()

    dag._admit()

    recorded = queue_admission.unresolvable_wait_obligations(_pointer("r-waiting"))
    assert recorded, "the pass records the wait it could not resolve"
    assert "run:r-prior" in recorded[0]

    payload = obligations.obligations(PROJECT, SESSION)
    kinds = [item["kind"] for item in payload["obligations"]]
    assert obligations.WAIT_UNRESOLVABLE_DUTY_KIND in kinds
    row = next(
        item
        for item in payload["obligations"]
        if item["kind"] == obligations.WAIT_UNRESOLVABLE_DUTY_KIND
    )
    assert row["run_id"] == "r-waiting"
    assert "run:r-prior" in row["reason"]


def test_an_open_upstream_still_reads_as_waiting(
    projects: tuple[Path, Path],
) -> None:
    """A wait whose upstream is genuinely open is still a wait."""
    first, _ = projects
    # The upstream is dispatched and open, not delivered: its run is known to
    # the dispatch and its manifest carries no verdict yet.
    dag._upstream_record(first, "r-prior", status="running", revision=None)
    _waiting_request(first, upstream=False)

    result, launched = dag._admit()

    assert launched == []
    assert result["upstream_obligations"] == []
    pointer = _pointer("r-waiting")
    assert pointer["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
    assert not queue_admission.unresolvable_wait_obligations(pointer)
