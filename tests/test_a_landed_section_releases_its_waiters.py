"""A landed section releases its waiters without waiting for an admission pass.

The resolve-and-move idiom has one owner in ``queue_admission``. The queue-time
check in a dispatch, the release step of an admission pass, a promotion that
lands a section, a discard that removes a request and the service's own release
tick all reach a waiting request through it, so a request moves to ``queued``
the moment its last ref lands rather than when the next pass happens to run.

Each case builds its own repository and crew home under a temporary directory
and reads nothing outside it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import promotion, queue_admission, runs
from reckon.crew.runs import _write_json, pointer_path
from tests import test_a_queued_request_waits_on_its_dependencies as waits

PROJECT = waits.PROJECT
PLAN = waits.PLAN
SESSION = waits.SESSION
RUN_ID = "r-20260901T000000000000-landed-section"


@pytest.fixture()
def chain_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The chain fixture: a checkout whose plan waits s2 on s1 and s3 on s2."""
    return waits.chain_repo.__wrapped__(tmp_path, monkeypatch)


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _release_tick_state() -> dict[str, float]:
    """The service's release-tick clock, reset per case so a case is its own."""
    queue_admission._RELEASE_TICK_LAST["at"] = 0.0
    return queue_admission._RELEASE_TICK_LAST


def _pointer(run_id: str) -> dict:
    return runs.read_pointer(run_id)


def _promotable_run(repository: Path, *, section: str = "s1") -> str:
    """A complete run whose commits deliver ``section``, ready to promote.

    The run's own manifest and pointer are written over a commit the run made,
    so promotion reads the same evidence a dispatched worker leaves behind.
    """
    base = _git(repository, "rev-parse", "HEAD")
    path = repository / f"{section}.txt"
    path.write_text(f"{section}\n", encoding="utf-8")
    _git(repository, "add", "--", path.name)
    _git(repository, "commit", "-q", "-m", f"feat: deliver {section}")
    head = _git(repository, "rev-parse", "HEAD")
    manifest = repository.parent / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    log = manifest.with_suffix(".log")
    log.write_text(
        f"# command: pytest tests/test_a_landed_section_releases_its_waiters.py\n"
        f"# revision: {head}\n"
        "4 passed\n"
        "EXIT=0\n",
        encoding="utf-8",
    )
    manifest.write_text(
        "node: landed-section\n"
        "status: complete\n"
        f"commits: {head}\n"
        f"changed_paths: {path.name}\n"
        "tests: pytest tests/test_a_landed_section_releases_its_waiters.py\n"
        f"test_logs: {log}\n"
        "artifacts: none\n"
        "evidence_inputs: none\n"
        "blockers: none\n"
        f"orientation_base_sha: {base}\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": base,
            "launch": "in-harness",
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {
                "id": "landed-section",
                "plan": PLAN,
                "section": section,
                "write_paths": [path.name],
            },
        },
    )
    return head


def _queue_chain(repository: Path) -> dict:
    return waits._queue_chain(repository)


def test_a_promotion_releases_the_waiters_of_the_section_it_delivered(
    chain_repo: Path,
) -> None:
    _release_tick_state()
    _queue_chain(chain_repo)
    assert waits._phase_of(chain_repo, "s2") == queue_admission.AWAITING_UPSTREAM_PHASE

    # The landing beat records the section as delivered before the promotion
    # lands: the plan is the authority readiness is derived from, and a run is
    # not.
    waits._declare(chain_repo, "s1")
    _promotable_run(chain_repo, section="s1")
    promoted = crew.complete(RUN_ID, gate="passed", root=chain_repo)

    assert promoted["record"]["run_id"] == RUN_ID
    assert promoted["released"]["promoted"] == ["r-n2"], promoted["released"]
    # No admission pass ran: the promotion itself released the waiter.
    assert waits._phase_of(chain_repo, "s2") == queue_admission.QUEUED_PHASE
    # A request waiting on another section is untouched by this landing.
    assert waits._phase_of(chain_repo, "s3") == (
        queue_admission.AWAITING_UPSTREAM_PHASE
    )


def test_the_release_tick_moves_a_wait_whose_upstream_landed_by_another_route(
    chain_repo: Path,
) -> None:
    state = _release_tick_state()
    _queue_chain(chain_repo)

    # The head lands by a hand merge of the plan: no promotion, no pass.
    waits._declare(chain_repo, "s1")
    assert waits._phase_of(chain_repo, "s2") == queue_admission.AWAITING_UPSTREAM_PHASE

    released = queue_admission._release_tick(now=1000.0)
    assert released["ran"] is True, released
    assert "r-n2" in released["promoted"], released
    assert waits._phase_of(chain_repo, "s2") == queue_admission.QUEUED_PHASE

    # The tick is paced by its own clock: a second call inside the window reads
    # nothing, so a service looping every few seconds does not re-read the
    # roadmap for each pass.
    paused = queue_admission._release_tick(now=1005.0)
    assert paused["ran"] is False, paused
    assert state["at"] == 1000.0
    again = queue_admission._release_tick(
        now=1000.0 + queue_admission.RELEASE_TICK_SECONDS
    )
    assert again["ran"] is True, again


def test_a_release_that_raises_leaves_the_promotion_landed_and_records_it(
    chain_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _release_tick_state()
    _queue_chain(chain_repo)

    def refuse(*_args, **_kwargs):
        raise RuntimeError("readiness read failed")

    monkeypatch.setattr(queue_admission, "_resolve_and_move", refuse)
    _promotable_run(chain_repo, section="s1")
    promoted = crew.complete(RUN_ID, gate="passed", root=chain_repo)

    assert promoted["record"]["run_id"] == RUN_ID, "the promotion still landed"
    released = promoted["released"]
    assert list(released["failed"]) == ["r-n2"], released
    assert "readiness read failed" in released["failed"]["r-n2"]
    # The failure is recorded on the request that could not be moved, and the
    # request keeps the phase it held.
    recorded = _pointer("r-n2")
    assert "readiness read failed" in str(recorded.get("release_failure") or "")
    assert recorded["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE


def test_the_queue_time_check_and_the_pass_reach_the_one_function(
    chain_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _release_tick_state()
    calls: list[str] = []
    original = queue_admission._resolve_and_move

    def counted(pointer, **kwargs):
        calls.append(str(pointer.get("waits")))
        return original(pointer, **kwargs)

    monkeypatch.setattr(queue_admission, "_resolve_and_move", counted)
    _queue_chain(chain_repo)
    at_queue_time = len(calls)
    assert at_queue_time >= 2, calls

    calls.clear()
    queue_admission.admit_once(config_for=waits._config_for(), launcher=lambda **_: 0)
    assert calls, "the pass's release step reaches the same function"
    assert any(f"{PLAN}#s1" in entry for entry in calls), calls


def test_a_release_counts_its_candidates_by_the_refs_they_wait_on(
    chain_repo: Path,
) -> None:
    _release_tick_state()
    _queue_chain(chain_repo)

    released = queue_admission._release_waiters([f"{PLAN}#s2"])
    assert released["candidates"] == ["r-n3"], released
    assert released["promoted"] == [], "s2 has not landed, so nothing is released"
    assert waits._phase_of(chain_repo, "s3") == (
        queue_admission.AWAITING_UPSTREAM_PHASE
    )
    nothing = queue_admission._release_waiters([f"{PLAN}#s9"])
    assert nothing["candidates"] == []


def test_a_discard_releases_the_requests_waiting_on_the_run_it_removed(
    chain_repo: Path,
) -> None:
    _release_tick_state()
    waits._declare_wait_refs(
        chain_repo, section="s3", refs=[f"{queue_admission.REQUEST_REF_PREFIX}r-n1"]
    )
    _queue_chain(chain_repo)
    assert waits._phase_of(chain_repo, "s3") == queue_admission.AWAITING_UPSTREAM_PHASE

    promotion.discard("r-n1")
    assert waits._phase_of(chain_repo, "s3") == (
        queue_admission.BLOCKED_UPSTREAM_PHASE
    ), "a discarded upstream is one that will never deliver"


def _json_line(record: dict) -> str:
    return json.dumps(record, sort_keys=True)
