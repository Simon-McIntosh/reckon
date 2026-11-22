"""A request waiting on a run launches when that run has landed.

A batch entry's ``after`` becomes a wait on a named run. The readiness check
answered that wait by opening the run's own document under the project's
per-run state directory, and promotions had stopped writing there, so a
promoted and landed run read as never promoted and its waiter sat
awaiting-upstream indefinitely. The check now reads the run's promotion from
the run store, which every promotion writes, with the run's document still
read for a checkout whose rows live only there.

Three cases: a run promoted and landed through the promotion path releases its
waiter on the next release pass; a run still live holds it; and a run no record
names raises one obligation naming that run rather than waiting for the life of
the queue.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import queue_admission, runs
from tests import test_a_landed_section_releases_its_waiters as landed
from tests import test_a_queued_request_waits_on_its_dependencies as waits

PROJECT = waits.PROJECT
PLAN = waits.PLAN
SESSION = waits.SESSION
UPSTREAM_RUN = landed.RUN_ID
WAITER = "r-n2"


@pytest.fixture()
def chain_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The chain fixture: a checkout whose plan declares each section's waits."""
    return waits.chain_repo.__wrapped__(tmp_path, monkeypatch)


def _reset_release_tick() -> None:
    """The release step is paced by its own clock, reset per case."""
    queue_admission._RELEASE_TICK_LAST["at"] = 0.0


def _queue_waiter(chain_repo: Path, ref: str) -> dict:
    """Queue one request on a section whose plan declares ``ref`` as its wait."""
    waits._declare_wait_refs(chain_repo, section="s2", refs=[ref])
    return queue_admission.queue_requests(
        [waits._node("n2", "s2")],
        project=PROJECT,
        session=SESSION,
        repo_root=chain_repo,
        config=waits.CONFIG,
        run_ids={"n2": WAITER},
    )


def _release(chain_repo: Path) -> dict:
    """One release pass, at its own clock, launching nothing."""
    return queue_admission._release_tick(now=1000.0)


def _phase(chain_repo: Path, section: str) -> str:
    return waits._phase_of(chain_repo, section)


def _obligations() -> list[str]:
    return list(runs.read_pointer(WAITER).get("upstream_obligations") or ())


def _deliver_manifest(run_id: str) -> None:
    """Place a finished run's manifest where its own delivery is read from.

    A dispatch writes the manifest into the run's directory; the fixture leaves
    it beside the checkout, so the run is given the manifest at the path the
    readiness check reads a run's status from.
    """
    pointer = runs.read_pointer(run_id)
    source = Path(str(pointer.get("manifest_path") or ""))
    target = runs.run_dir(run_id) / runs.DEFAULT_MANIFEST_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def _live_pointer(chain_repo: Path, run_id: str) -> None:
    """A dispatched run's own pointer, before anything has recorded its result."""
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(chain_repo),
            "phase": "running",
            "node": {"id": run_id, "plan": PLAN, "section": "s3"},
        },
    )


def test_a_promoted_and_landed_run_releases_its_waiter_on_the_next_pass(
    chain_repo: Path,
) -> None:
    _reset_release_tick()
    # The upstream is dispatched first, so the request naming it is queued
    # while the run is live -- the sequence that leaves the wait holding once
    # the run's pointer is gone.
    waits._declare(chain_repo, "s1")
    landed._promotable_run(chain_repo, section="s1")
    stored = _queue_waiter(
        chain_repo, f"{queue_admission.REQUEST_REF_PREFIX}{UPSTREAM_RUN}"
    )
    assert stored[WAITER]["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
    _deliver_manifest(UPSTREAM_RUN)

    # The upstream is promoted through the promotion path, and its promoted
    # revision is the primary branch's head, so it has landed.
    promoted = crew.complete(UPSTREAM_RUN, gate="passed", root=chain_repo)
    assert promoted["record"]["run_id"] == UPSTREAM_RUN
    # The promotion records the run where the readiness check reads one.
    data, _version = ledger.load(PROJECT, run_ids=[UPSTREAM_RUN])
    rows = [row for row in data.get("runs") or () if row.get("run_id") == UPSTREAM_RUN]
    assert rows, "the promotion recorded no row for the run it landed"
    assert rows[0]["promoted_revision"]
    # A promotion releases section waiters; this wait names a run, so only the
    # release pass moves it.
    assert _phase(chain_repo, "s2") == queue_admission.AWAITING_UPSTREAM_PHASE

    released = _release(chain_repo)
    assert released["ran"] is True, released
    assert _phase(chain_repo, "s2") == queue_admission.QUEUED_PHASE
    assert WAITER in released["promoted"], released


def test_a_request_waiting_on_a_run_still_live_is_held(chain_repo: Path) -> None:
    _reset_release_tick()
    _live_pointer(chain_repo, "r-live")
    _queue_waiter(chain_repo, f"{queue_admission.REQUEST_REF_PREFIX}r-live")
    assert _phase(chain_repo, "s2") == queue_admission.AWAITING_UPSTREAM_PHASE

    _release(chain_repo)
    assert _phase(chain_repo, "s2") == queue_admission.AWAITING_UPSTREAM_PHASE
    assert _obligations() == [], "a run still on its way is not an obligation"


def test_a_request_waiting_on_a_run_no_record_names_raises_an_obligation(
    chain_repo: Path,
) -> None:
    _reset_release_tick()
    # Queued while the upstream was live, then every record of it is gone: its
    # pointer, its document and its run directory. Nothing can land it, so the
    # wait is raised rather than held for the life of the queue.
    _live_pointer(chain_repo, "r-nowhere")
    _queue_waiter(chain_repo, f"{queue_admission.REQUEST_REF_PREFIX}r-nowhere")
    runs.pointer_path("r-nowhere").unlink()
    assert not ledger.run_path(PROJECT, "r-nowhere").exists()
    assert not runs.run_dir("r-nowhere").is_dir()

    _release(chain_repo)
    assert _phase(chain_repo, "s2") == queue_admission.AWAITING_UPSTREAM_PHASE
    obligations = _obligations()
    assert len(obligations) == 1, obligations
    assert "r-nowhere" in obligations[0]
    assert "no record names this run" in obligations[0]
