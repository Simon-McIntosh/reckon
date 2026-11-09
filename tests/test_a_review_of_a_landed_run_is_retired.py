"""A review of a run that has already landed is retired, not retried.

A run review writes its record into the reviewed run's ``review-records``
directory, and the dispatch grants that directory by resolving the reviewed run
from the review's node id against the fleet. Once the reviewed run is promoted
its live pointer is gone, so the grant resolves no run directory, the record
path falls outside every writable root, and the launch is refused as a node
contract failure. Admission then held such a request ``until-changed``, which
nothing ever changes: two figure reviews sat queued for over a day against runs
promoted before they were admitted.

These cases drive one admission pass over a queued figure review. The first is
that shape: the subject's promotion record is on disk, the refusal names the
review's own record path, and the pass retires the request, recording why and
raising one obligation naming the landed run. The second is a review of a run
still in the fleet, which is admitted exactly as before. The third is the same
refused request with a live subject, which must not be retired: the refusal is
curable while the run is still there.

Every store and crew home read here resolves under the test's temporary config
home; the real ones are read only to prove nothing this test minted landed in
them.
"""

# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from reckon import ledger
from reckon.crew import promotion, queue_admission, runs
from reckon.crew.node import TaskNode
from tests.real_home_traces import traces_naming
from tests.test_a_queued_run_is_admitted import (  # noqa: F401 - fixture
    _config_for,
    _lane_document,
    admit_repo,
)

PROJECT = "proj"
SESSION = "coordinator-a"
PROMOTED_HEAD = "b818dab4d60ff8048ccfc03cd9ecb04d1286d3f9"

REVIEWED_RUN = "r-fixture-bank-drive"
LIVE_RUN = "r-fixture-bank-drive-live"
RETIRED_REVIEW = "r-fixture-review-retired"
LIVE_SUBJECT_REVIEW = "r-fixture-review-live"


def _real_crew_home() -> Path:
    xdg = Path.home() / ".config" / "reckon"
    return (xdg if xdg.exists() else Path.home() / "docs-server") / "crew"


def _promote(repository: Path, run_id: str) -> Path:
    """Write one promoted run's record where a promotion writes it.

    The record is what makes a run landed to every reader that asks the ledger,
    and its presence is asserted rather than assumed: a promotion lookup that
    silently found nothing would make every case below pass for the wrong
    reason.
    """
    row = {"run_id": run_id, "project": PROJECT, "promoted_revision": PROMOTED_HEAD}
    path = ledger.run_path(PROJECT, run_id, repository)
    path.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_run_file(PROJECT, row, repository)
    assert path.is_file()
    assert runs._promoted_record_project(run_id, PROJECT) == PROJECT, (
        f"the promotion record at {path} is not read back by the lookup under test"
    )
    return path


def _record_path(reviewed_run_id: str) -> Path:
    return (
        runs.run_dir(reviewed_run_id)
        / "review-records"
        / f"{reviewed_run_id}.figure.json"
    )


def _refusal(reviewed_run_id: str) -> str:
    """The refusal a review is recorded with when its record path is fenced."""
    return (
        "CrewError: node is not dispatchable - scoped: write path "
        f"{str(_record_path(reviewed_run_id))!r} is unreachable in resolved "
        "sandbox tier 'read-only'; writable grants: none"
    )


def _review_request(repository: Path, run_id: str, reviewed_run_id: str) -> dict:
    node = TaskNode(
        id=f"review-of-{reviewed_run_id}",
        role="review",
        goal=f"attach a figure review to run {reviewed_run_id}",
        plan="plan-a",
        section="dispatch",
        done_when="pytest tests/test_a_review_of_a_landed_run_is_retired.py reports passed",
        write_paths=[str(_record_path(reviewed_run_id))],
        time_budget="20m",
        spec_level="exact",
    )
    joined = datetime.now(UTC).isoformat()
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repository),
        "node": node.as_dict(),
        "backend": "clive",
        "launch": "cli",
        "local": True,
        "phase": "queued",
        "queued_at": joined,
        "created_at": joined,
        "dispatch_options": {
            "project": PROJECT,
            "plan_slug": "plan-a",
            "section": "dispatch",
            "session": SESSION,
            "local": True,
            "no_watch": True,
            "overrides": [],
        },
    }


def _queue_review(
    repository: Path,
    *,
    run_id: str,
    reviewed_run_id: str,
    refused: bool,
    resolve,
) -> None:
    """Queue one review, recording its refusal and the guard of that failure.

    The guard is taken through the module's own fingerprint rather than spelled
    here, because the hold this retirement replaces is keyed to it: a guard that
    is certainly current is what leaves the request ``held-until-changed`` when
    the retirement does not fire.
    """
    runs.queue_dispatch(_review_request(repository, run_id, reviewed_run_id))
    if not refused:
        return
    failure = _refusal(reviewed_run_id)
    guard = queue_admission._dependency_guard(
        runs.read_pointer(run_id), config_for=resolve
    )

    def stamp(current: dict) -> dict:
        current["failure"] = failure
        current["failure_guard"] = guard
        return current

    runs._mutate_pointer(run_id, stamp)


def _live_subject(repository: Path, run_id: str) -> None:
    """Leave one run in the fleet, as a run whose review is still dispatchable.

    The pointer is written directly rather than queued: a run in flight is not
    a queued request, and the queue writer only stores requests that are.
    """
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(repository),
            "node": {
                "id": "bank-drive-continuation",
                "role": "implement",
                "goal": "implement the bank drive",
                "plan": "plan-a",
                "section": "dispatch",
                "done_when": "the work lands",
                "write_paths": ["src/bank_drive.py"],
                "time_budget": "20m",
                "spec_level": "exact",
            },
            "backend": "clive",
            "launch": "cli",
            "local": True,
            "phase": "running",
            "queued_at": datetime.now(UTC).isoformat(),
            "created_at": datetime.now(UTC).isoformat(),
        }
    )


def _launcher(recorded: list):
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(plan)
        return 0

    return launch


def _lane(
    admit_repo: Path, tmp_path: Path, resolve_config: dict, *, slots: float = 1
) -> None:
    resolve_config["roles"]["review"] = {}
    resolve_config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=slots)
    )


# ── The subject landed before the review was admitted ──────────────────────


def test_a_review_of_a_promoted_run_is_retired_and_names_the_landed_run(
    admit_repo: Path, tmp_path: Path
) -> None:
    config, resolve = _config_for()
    _lane(admit_repo, tmp_path, config)
    _promote(admit_repo, REVIEWED_RUN)
    _queue_review(
        admit_repo,
        run_id=RETIRED_REVIEW,
        reviewed_run_id=REVIEWED_RUN,
        refused=True,
        resolve=resolve,
    )
    assert runs.read_pointer(RETIRED_REVIEW)["phase"] == "queued"
    recorded: list = []

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(recorded)
    )

    assert result["retired"] == [RETIRED_REVIEW]
    assert result["launched"] == []
    assert result["held"] == []
    assert result["failed"] == []
    assert recorded == []
    # The retirement is recorded where every discard is read, and its reason
    # names the landed run.
    assert promotion.discard_recorded(RETIRED_REVIEW)
    marker = next(
        path
        for path in (
            promotion.discard_record_path(RETIRED_REVIEW),
            promotion.discard_spool_path(RETIRED_REVIEW),
        )
        if path.is_file()
    )
    assert REVIEWED_RUN in json.loads(marker.read_text(encoding="utf-8"))["reason"]
    # One obligation, naming the landed run and how to dispose of what the
    # review would have carried.
    assert len(result["obligations"]) == 1
    obligation = result["obligations"][0]
    assert obligation["run_id"] == RETIRED_REVIEW
    assert REVIEWED_RUN in obligation["detail"]
    assert "reckon crew dispose" in obligation["detail"]
    assert result["verdicts"][0]["verdict"] == "retired-landed-review"


def test_a_retired_review_is_retired_once(
    admit_repo: Path, tmp_path: Path
) -> None:
    config, resolve = _config_for()
    _lane(admit_repo, tmp_path, config)
    _promote(admit_repo, REVIEWED_RUN)
    _queue_review(
        admit_repo,
        run_id=RETIRED_REVIEW,
        reviewed_run_id=REVIEWED_RUN,
        refused=True,
        resolve=resolve,
    )

    queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))
    second = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))

    assert second["retired"] == []
    assert second["obligations"] == []
    assert not runs.pointer_path(RETIRED_REVIEW).is_file()


# ── A review of a run still in the fleet is admitted as before ─────────────


def test_a_review_of_a_live_run_is_admitted_unchanged(
    admit_repo: Path, tmp_path: Path
) -> None:
    config, resolve = _config_for()
    _lane(admit_repo, tmp_path, config, slots=2)
    _live_subject(admit_repo, LIVE_RUN)
    _queue_review(
        admit_repo,
        run_id=LIVE_SUBJECT_REVIEW,
        reviewed_run_id=LIVE_RUN,
        refused=False,
        resolve=resolve,
    )
    recorded: list = []

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(recorded)
    )

    assert result["launched"] == [LIVE_SUBJECT_REVIEW]
    assert result["retired"] == []
    assert len(recorded) == 1
    assert runs.read_pointer(LIVE_SUBJECT_REVIEW)["phase"] != "queued"


def test_a_refused_review_of_a_live_run_is_not_retired(
    admit_repo: Path, tmp_path: Path
) -> None:
    """The retirement's live half: the refusal is curable while the run is here."""
    config, resolve = _config_for()
    _lane(admit_repo, tmp_path, config, slots=2)
    _live_subject(admit_repo, LIVE_RUN)
    _queue_review(
        admit_repo,
        run_id=LIVE_SUBJECT_REVIEW,
        reviewed_run_id=LIVE_RUN,
        refused=True,
        resolve=resolve,
    )

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher([])
    )

    assert result["retired"] == []
    assert result["obligations"] == []
    assert runs.pointer_path(LIVE_SUBJECT_REVIEW).is_file()
    assert result["verdicts"][0]["verdict"] == "held-until-changed"
    assert promotion.discard_recorded(LIVE_SUBJECT_REVIEW) is False


def test_a_review_whose_subject_never_landed_is_not_retired(
    admit_repo: Path, tmp_path: Path
) -> None:
    """A subject that is neither in the fleet nor promoted leaves the request.

    Only a promotion strands the review for good: a run that left the fleet
    without landing may be resumed or repaired, and retiring its review would
    discard work that still has somewhere to go. The promotion record is what
    separates the two, so this case is the retirement's other half.
    """
    config, resolve = _config_for()
    _lane(admit_repo, tmp_path, config)
    _queue_review(
        admit_repo,
        run_id=RETIRED_REVIEW,
        reviewed_run_id=REVIEWED_RUN,
        refused=True,
        resolve=resolve,
    )
    assert not runs.pointer_path(REVIEWED_RUN).is_file()
    assert runs._promoted_record_project(REVIEWED_RUN, PROJECT) is None

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher([])
    )

    assert result["retired"] == []
    assert result["obligations"] == []
    assert runs.pointer_path(RETIRED_REVIEW).is_file()
    assert result["verdicts"][0]["verdict"] == "held-until-changed"
    assert promotion.discard_recorded(RETIRED_REVIEW) is False


# ── Nothing lands in the real home ─────────────────────────────────────────


def test_the_real_crew_home_carries_nothing_this_test_minted(
    admit_repo: Path, tmp_path: Path
) -> None:
    config, resolve = _config_for()
    _lane(admit_repo, tmp_path, config)
    _promote(admit_repo, REVIEWED_RUN)
    _queue_review(
        admit_repo,
        run_id=RETIRED_REVIEW,
        reviewed_run_id=REVIEWED_RUN,
        refused=True,
        resolve=resolve,
    )

    queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))

    assert promotion.discard_recorded(RETIRED_REVIEW)
    assert (
        traces_naming(
            _real_crew_home(),
            ("live", "discards"),
            (RETIRED_REVIEW, REVIEWED_RUN),
        )
        == []
    )