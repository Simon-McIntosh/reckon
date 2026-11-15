"""A refused reflex review names its remedy instead of promising a sweep.

For two days the stop hook printed ``[review-missing]`` with "the review reflex
owns this dispatch ... no manual dispatch is needed", while every sweep the
reflex made was refused for the same reason: the run's newest review still
carried a blocking finding with no disposition. The duty read only that a
follower was armed, so the refusal the reflex recorded on the run's own pointer
was never read and the coordinator was told to wait for a dispatch that could
not happen.

Five properties are measured here, through the obligations derivation on a
synthesised repository, a temporary crew home and an armed follower:

* a scoring run whose recorded refusal names its current head raises the
  refused-dispatch duty, and its next action is the ``crew dispose`` command
  that lifts the finding;
* the same refusal recorded for an earlier head leaves the reflex's own text in
  place, because a refusal speaks only for the revision it was made against;
* a run with nothing recorded still reads as the reflex owning the dispatch;
* the refusal ``dispatch_review_for_run`` writes records the run's current head
  and the findings it named, and the derivation reads the duty from it;
* a stored refusal that names no head covers no revision, so the reflex's own
  text stands — every entry written before the writer took the head looks like
  this one.
"""

from __future__ import annotations

import importlib
import json
import os
import time
from pathlib import Path

import pytest

from reckon.crew import review as review_module
from reckon.crew import runs
from tests.helpers.git import git as _git

obligations_module = importlib.import_module("reckon.crew.obligations")
dispatch_module = importlib.import_module("reckon.crew.recovery_review_dispatch")

PROJECT = "refused-reflex-fixture"
SESSION = "refused-reflex-session"
RUN_ID = "run-refused-reflex"

# The text the duty prints while an armed reflex owns the dispatch, asserted by
# its stable opening rather than by the whole sentence: the window it quotes
# moves with the follower's cadence.
REFLEX_OWNS = "the review reflex owns this dispatch"
DISPOSE = "reckon crew dispose"
REFUSAL_REASON = (
    f"run {RUN_ID!r} cannot be reviewed: its newest review carries 1 blocking "
    "finding(s) with no disposition, so a review dispatched now would repeat them"
)
BLOCKING = {
    "file": "reckon/crew/thing.py",
    "line": "10",
    "text": "the guard never fires",
    "severity": review_module.BLOCKING_FINDING_SEVERITY,
}


def _commit(repository: Path, name: str, body: str) -> str:
    (repository / name).parent.mkdir(parents=True, exist_ok=True)
    (repository / name).write_text(body, encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-q", "-m", f"test: {body.strip()}")
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def repository(tmp_path: Path, home: Path) -> Path:
    """A synthesised checkout the run's head and its review record resolve in."""
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    _commit(root, "seed.txt", "seed\n")
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _delivered_run(repository: Path, tmp_path: Path) -> tuple[dict, str]:
    """One complete, unpromoted run standing at the checkout's head."""
    head = _git(repository, "rev-parse", "HEAD")
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {RUN_ID}-node\nstatus: complete\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    # Stamped with the clock the derivation ages it by, so the reflex's own
    # window has not passed and the duty's answer can only come from what the
    # run's pointer records.
    stamped = time.time()
    os.utime(manifest, (stamped, stamped))
    pointer = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repository),
        "worktree": str(repository),
        "base_sha": head,
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": f"{RUN_ID}-node",
            "plan": "fixture-plan",
            "section": "fixture-section",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
    }
    runs._write_json(runs.pointer_path(RUN_ID), pointer)
    return pointer, head


def _store_review(*, head: str, findings: list[dict] | None = None) -> None:
    """Store a complete review of the run at the revision it read."""
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN_ID,
            "review_run_id": f"review-of-{RUN_ID}",
            "reviewed_base_sha": head,
            "reviewed_head_sha": head,
            "findings": list(findings or []),
        }
    )
    review_module.store_review(record)


def _record_refusal(
    *,
    head: str | None,
    findings: list[int] | None = None,
    reason: str = REFUSAL_REASON,
) -> None:
    """Write the refusal the reflex records when its dispatch was refused."""
    field = obligations_module.recovery.REVIEW_DISPATCH_FIELD
    pointer = runs.read_pointer(RUN_ID)
    pointer[field] = {
        "status": "refused",
        "reason": reason,
        "run_id": None,
        "backend": None,
        "head": head,
        "at": runs._utc_now(),
        "attempt": 1,
        **({"findings": findings} if findings is not None else {}),
    }
    runs._write_json(runs.pointer_path(RUN_ID), pointer)


def _duty(result: dict) -> dict:
    duties = [item for item in result["obligations"] if item.get("run_id") == RUN_ID]
    assert len(duties) == 1, [(item["kind"], item.get("run_id")) for item in duties]
    return duties[0]


def _derive() -> dict:
    with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
        return obligations_module.obligations(PROJECT, SESSION)


def test_a_refusal_recorded_for_the_current_head_names_answers_the_coordinator_can_run(
    repository: Path, tmp_path: Path
) -> None:
    """The refusal's own record decides the duty, not the follower's existence."""
    _delivered_run(repository, tmp_path)
    _record_refusal(head=_git(repository, "rev-parse", "HEAD"), findings=[0])

    duty = _duty(_derive())

    assert duty["kind"] == obligations_module.REVIEW_DISPATCH_REFUSED_DUTY_KIND, duty
    # The duty is the coordinator's to act on, so it is not a wait on the
    # reflex: a row claiming both would be read as the one it replaced.
    assert "review_owner" not in duty, duty
    command = str(duty["next_command"])
    assert DISPOSE in command, duty
    assert "inspect" not in command and "no manual dispatch is needed" not in command
    assert str(duty["reason"]) == REFUSAL_REASON, duty


def test_a_refusal_recorded_for_an_earlier_head_still_reads_as_the_reflex_owning_it(
    repository: Path, tmp_path: Path
) -> None:
    """A refusal speaks for the revision it was made against, and no other."""
    earlier = _git(repository, "rev-parse", "HEAD")
    _commit(repository, "reckon/repair_source.py", "VALUE = 1\n")
    _delivered_run(repository, tmp_path)
    _record_refusal(head=earlier, findings=[0])

    duty = _duty(_derive())

    assert duty["kind"] == "review-missing", duty
    assert REFLEX_OWNS in str(duty["next_command"]), duty


def test_a_run_with_no_recorded_dispatch_reads_as_the_reflex_owning_it(
    repository: Path, tmp_path: Path
) -> None:
    """The ordinary reading is unchanged: nothing recorded, the reflex owns it."""
    _delivered_run(repository, tmp_path)

    duty = _duty(_derive())

    assert duty["kind"] == "review-missing", duty
    assert REFLEX_OWNS in str(duty["next_command"]), duty


def test_the_reflex_records_the_head_and_findings_its_refusal_spoke_for(
    repository: Path, tmp_path: Path
) -> None:
    """A real dispatch refusal records what it refused, and the duty reads it."""
    # The review that holds the dispatch was raised at a revision the run has
    # since repaired past: nothing covers the head the run carries now, so the
    # run reads as owing a review, while the still-open record's undisposed
    # finding is what every sweep is refused for. It is the refusal, not a
    # repair of a review selected for the current head, that holds this node's
    # duty, which is the state the incident was reported in.
    earlier = _git(repository, "rev-parse", "HEAD")
    _store_review(head=earlier, findings=[BLOCKING])
    head = _commit(repository, "reckon/repair_source.py", "VALUE = 1\n")
    _delivered_run(repository, tmp_path)

    report = dispatch_module.dispatch_review_for_run(runs.read_pointer(RUN_ID))

    assert report.get("refused") is True, report
    recorded = runs.read_pointer(RUN_ID)[obligations_module.recovery.REVIEW_DISPATCH_FIELD]
    assert recorded["status"] == "refused", recorded
    assert recorded["head"] == head, recorded
    assert recorded["findings"] == [0], recorded

    duty = _duty(_derive())

    assert duty["kind"] == obligations_module.REVIEW_DISPATCH_REFUSED_DUTY_KIND, duty
    command = str(duty["next_command"])
    fold, exempt = dispatch_module._finding_dispose_commands(PROJECT, RUN_ID, 0)
    assert fold in command and exempt in command, duty


def test_a_stored_refusal_with_no_head_covers_no_revision(
    repository: Path, tmp_path: Path
) -> None:
    """Every entry written before the writer took a head looks like this one."""
    _delivered_run(repository, tmp_path)
    _record_refusal(head=None, findings=[0])

    duty = _duty(_derive())

    assert duty["kind"] == "review-missing", duty
    assert REFLEX_OWNS in str(duty["next_command"]), duty