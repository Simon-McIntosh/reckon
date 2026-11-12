"""A floor-driven repair and a promotion never both proceed.

A review that leaves one dimension below its declared floor composes one repair
round for that dimension. Promotion and the round both rewrite the same run: a
promotion settles its record and takes its pointer, while a round opened under a
promotion either writes that settled record or commits in a worktree no landing path
reads. The two are therefore made exclusive by construction, and the work a
promotion lands over is named in its own output rather than left to resurface as
commits with nowhere to land.

Three facts are measured on a seeded run whose review scores one dimension below
its floor. While a promotion of the run holds its claim, the reflex's pass opens
no repair round for it and reports that it is held. A promotion that follows
names the floor repair it lands over. A run whose floor was answered by a
disposition before its promotion lands with no such obligation, so the naming is
bound to the owed dimension and not to the presence of a declared floor.

Every assertion stands against the base revision as the negative control: the
base carries no promotion claim for the reflex to read, so its pass holds no
round and reports no holder, and no promotion it reaches names a floor.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reckon import ledger
from reckon.crew import promotion as promotion_module
from reckon.crew import recovery
from reckon.crew import review as review_module
from reckon.crew.host_lease import HostLease
from reckon.crew.recovery_review_dispatch import REPAIR_DISPATCH_FIELD
from reckon.crew.runs import _write_json, crew_home, pointer_path, read_pointer
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import (
    PLAN,
    PROJECT,
    _write_origin_and_checkout,
)

RUN_ID = "r-20261112T220000000000-floor-repair-target"
GATE_COMMAND = "sh gate.sh"
LOW_DIMENSION = review_module.REVIEW_DIMENSIONS[0]
FLOOR = 10
LOW_SCORE = 5
FOLD_NODE = "answer-the-floor-finding"
TEXTUAL_FINDING = {
    "id": "textual-defect",
    "file": "candidate.txt",
    "line": "1",
    "text": "one textual defect",
    "severity": "blocking",
}


@pytest.fixture(autouse=True)
def real_crew_home_is_not_a_fixture_target() -> None:
    """No fixture may reach the workstation's real crew pointer directory."""
    real_pointer = (
        Path.home() / ".config" / "reckon" / "crew" / "live" / f"{RUN_ID}.json"
    )
    assert not real_pointer.exists()
    yield
    assert not real_pointer.exists()


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    """A merged run's checkout, with a green gate and one dimension floored.

    The floor is declared in the project's own flight layer rather than in this
    test, so the reflex and the promotion resolve the same standard from the
    same file, exactly as a real project does.
    """
    assert crew_home().is_relative_to(isolated_reckon_home)
    _origin, root = _write_origin_and_checkout(tmp_path)
    (root / "gate.sh").write_text(
        '#!/bin/sh\ngrep -q "satisfied" node.txt\n', encoding="utf-8"
    )
    (root / "node.txt").write_text("satisfied\n", encoding="utf-8")
    state = root / "docs" / "state" / PROJECT
    state.mkdir(parents=True, exist_ok=True)
    (state / "flight.yaml").write_text(
        f"gates:\n  dimension_floors:\n    {LOW_DIMENSION}: {FLOOR}\n",
        encoding="utf-8",
    )
    _git(root, "add", "gate.sh", "node.txt", "docs")
    _git(root, "commit", "-q", "-m", "chore: seed the recorded gate and the floor")
    _git(root, "push", "-q", "origin", "main")
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}),
        encoding="utf-8",
    )
    return root


def _land_run_commit(repository: Path) -> str:
    """Land the run's deliverable on main, as a merged run leaves it."""
    (repository / "candidate.txt").write_text("delivered\n", encoding="utf-8")
    _git(repository, "add", "candidate.txt")
    _git(repository, "commit", "-q", "-m", "feat: land the run deliverable")
    _git(repository, "push", "-q", "origin", "main")
    return _git(repository, "rev-parse", "HEAD")


def _gate_log(tmp_path: Path) -> Path:
    log = tmp_path / "gate.log"
    log.write_text("gate ran\nEXIT=0\n", encoding="utf-8")
    return log


def _write_run(
    repository: Path, tmp_path: Path, *, base: str, commit: str, worktree: Path
) -> None:
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: floor-repair-target\n"
        "status: complete\n"
        f"commits: {commit}\n"
        "changed_paths: candidate.txt\n"
        f"tests: {GATE_COMMAND}\n"
        f"test_logs:\n  - {_gate_log(tmp_path)}\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "base_sha": base,
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-11-12T22:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "floor-repair-target",
                "plan": PLAN,
                "section": "s1",
                "time_budget": "25m",
                "role": "implement",
                "write_paths": ["candidate.txt"],
            },
        },
    )


def _store_review(
    *,
    base: str,
    head: str,
    findings: list[dict] | None = None,
    floor_disposition: str = "",
    finding_disposition: str = "",
    severity: str = "blocking",
) -> None:
    """Store the run's review: one dimension below its floor, findings answered.

    ``floor_disposition`` answers the low dimension, which is what the second
    case disposes before promotion. ``finding_disposition`` answers the textual
    finding, which the first case has already disposed so the only work left on
    the run is the floor.
    """
    scores = dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20)
    scores[LOW_DIMENSION] = LOW_SCORE
    emitted = "\n".join(
        f"SCORE {dimension}: {scores[dimension]}"
        for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    if findings:
        record["findings"] = [dict(finding, severity=severity) for finding in findings]
    if finding_disposition:
        record[review_module.FINDING_DISPOSITIONS_KEY] = {
            "0": {
                "kind": finding_disposition,
                "node": FOLD_NODE,
                "recorded_at": "2026-11-12T22:10:00+00:00",
            }
        }
    if floor_disposition:
        record[review_module.DIMENSION_DISPOSITIONS_KEY] = {
            LOW_DIMENSION: {
                "kind": floor_disposition,
                "node": FOLD_NODE,
                "reason": "answered before promotion",
                "recorded_at": "2026-11-12T22:10:00+00:00",
            }
        }
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN_ID,
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
            "review_run_id": f"review-of-{RUN_ID}",
        }
    )
    review_module.store_review(record)


def _seed(
    repository: Path, tmp_path: Path, *, severity: str = "blocking", **review: Any
) -> Path:
    """A merged, reviewed run whose review owes one floor repair."""
    base = _git(repository, "rev-parse", "HEAD")
    commit = _land_run_commit(repository)
    worktree = tmp_path / "worker"
    _git(repository, "worktree", "add", "-q", "--detach", str(worktree), commit)
    _write_run(repository, tmp_path, base=base, commit=commit, worktree=worktree)
    _store_review(
        base=base,
        head=commit,
        findings=[TEXTUAL_FINDING] if severity else [],
        finding_disposition="folded" if severity else "",
        severity=severity,
        **review,
    )
    return worktree


def _sweep() -> dict:
    """Run the reflex sweep as a hand-run caller does, with no session filter."""
    return recovery.dispatch_awaiting_reviews(
        project=PROJECT, session="", launcher=lambda *args, **kwargs: 0
    )


def _claim(run_id: str) -> HostLease:
    """Hold the run's promotion claim, as a promotion in flight holds it."""
    host, pid, job = promotion_module._promotion_lease_identity()
    lease = HostLease(
        crew_home(), promotion_module._promotion_lease_name(run_id), host, pid, job
    )
    assert lease.claim(), "premise: the run's promotion claim is free"
    return lease


def test_a_promotion_in_flight_opens_no_floor_repair_round(
    repository: Path, tmp_path: Path
) -> None:
    """The reflex leaves a run alone while a promotion of it holds the claim.

    The base revision has no claim to read, so it passes the run to its repair
    door and records a round on the run's pointer while the promotion is under
    way. The head refuses to open one and says which holder refused it.
    """
    worktree = _seed(repository, tmp_path)
    lease = _claim(RUN_ID)
    try:
        claim_reader = getattr(promotion_module, "promotion_in_flight", None)
        if claim_reader is not None:
            assert claim_reader(RUN_ID), "premise: the promotion claim reads as held"
        report = _sweep()
    finally:
        lease.release()

    assert report["repaired"] == [], report
    assert [entry["run_id"] for entry in report.get("repair_held", [])] == [RUN_ID], report
    assert read_pointer(RUN_ID).get(REPAIR_DISPATCH_FIELD) is None, (
        "no repair round is recorded on a run whose promotion has begun"
    )

    # The promotion then runs, and its own output names the floor it lands over.
    receipt = promotion_module.complete(RUN_ID, gate="passed", root=repository)

    owed = receipt["owed_floor_repair"]
    assert owed["dimensions"] == [
        {"dimension": LOW_DIMENSION, "score": LOW_SCORE, "floor": FLOOR}
    ], owed
    assert LOW_DIMENSION in owed["note"], owed["note"]
    assert not pointer_path(RUN_ID).exists(), "the promotion settled the run"
    assert [row["run_id"] for row in ledger.runs(PROJECT, root=repository)] == [RUN_ID]

    # A pass after the promotion opens no round either, and the repository gains
    # no work: the promotion reclaimed the run's worktree, so the obligation is
    # named rather than repaired in a tree no landing path reads.
    settled_tip = _git(repository, "rev-parse", "HEAD")
    assert not worktree.exists(), "the promotion reclaimed the run's worktree"
    after = _sweep()

    assert after["repaired"] == [], after
    assert after.get("repair_held", []) == [], after
    assert _git(repository, "rev-parse", "HEAD") == settled_tip
    assert not pointer_path(RUN_ID).exists()


def test_a_disposed_floor_lands_with_no_obligation(
    repository: Path, tmp_path: Path
) -> None:
    """A floor answered before promotion names nothing, and the run lands.

    Nothing blocks the run: its only textual finding was disposed and its low
    dimension is answered as folded, so the review leaves no work owed and the
    sweep promotes it as it promotes any clean review.
    """
    _seed(repository, tmp_path, severity="non-blocking", floor_disposition="folded")

    report = _sweep()

    assert report["accepted"] == [RUN_ID], report
    assert report.get("repair_held", []) == [], report
    assert report["repaired"] == [], report
    assert not pointer_path(RUN_ID).exists()
    accepted = next(
        entry
        for entry in report["reports"]
        if entry.get("run_id") == RUN_ID and entry.get("accepted")
    )
    assert "owed_floor_repair" not in accepted, accepted
