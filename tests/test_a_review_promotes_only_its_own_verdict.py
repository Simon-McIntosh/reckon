"""A review run is delivered only by its own record of the head it read.

A run review is dispatched to read one revision, and the dispatch spells that
revision as the ``.at-<head>`` suffix of the record path it grants. The reader
that decides delivery therefore accepts a record only when it sits at that
head-keyed path, describes that head, names this review run when it names one,
and was written after this run was dispatched. Without the four, a complete
record an earlier round left at the legacy path stands as this run's verdict:
measured 2026-10-18, a review run that never wrote its record was promoted on a
predecessor round's head and total. These tests fix the four refusals and the
delivery they guard:

- a predecessor round's complete record at the legacy path is no delivery, and
  the run's own record at the head-keyed path both reads as delivered and
  promotes the run with that head and that total on its ledger row;
- a record at the head-keyed path describing another head, naming another
  review run, or written before this run was dispatched is no delivery;
- a run review declaring only the legacy path is refused by
  ``promote_review_run_itself`` naming the head it never declared.

The fixture runs a temporary crew home and repository and asserts afterwards
that the workstation's real crew pointer directory is untouched, because an
isolated read does not prove an isolated write.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import _plan_html, ledger
from reckon.crew import recovery
from reckon.crew import review as review_module
from reckon.crew.recovery_review_acceptance import promote_review_run_itself
from reckon.crew.recovery_review_delivery import _delivered_review_record
from reckon.crew.runs import _write_json, crew_home, pointer_path, run_dir
from tests.helpers.git import git as _git

PROJECT = "review-head-fixture"
PLAN = "review-head-target"
REVIEWED_RUN_ID = "r-20261012T120000000001-reviewed-run"
REVIEW_RUN_ID = "r-20261012T130000000000-review-of-reviewed-run"
REVIEW_NODE_ID = f"review-of-{REVIEWED_RUN_ID}"
OTHER_REVIEW_RUN_ID = "r-20261012T125900000000-review-of-reviewed-run"
# The head the review was dispatched to read, and the head an earlier round
# left a complete record for.
DISPATCHED_HEAD = "a" * 40
PREDECESSOR_HEAD = "b" * 40
REVIEWED_BASE = "c" * 40
TOTAL = 20 * len(review_module.REVIEW_DIMENSIONS)
DISPATCH_INSTANT = datetime(2026, 10, 12, 13, 0, 0, tzinfo=UTC).timestamp()


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture(autouse=True)
def real_crew_home_is_not_a_fixture_target() -> None:
    """No fixture may reach the workstation's real crew pointer directory."""
    real_pointer = (
        Path.home() / ".config" / "reckon" / "crew" / "live" / f"{REVIEW_RUN_ID}.json"
    )
    assert not real_pointer.exists()
    yield
    assert not real_pointer.exists()


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    """A committed repository whose docs directory is the project's mount."""
    assert crew_home().is_relative_to(isolated_reckon_home)
    root = tmp_path / "repo"
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Review Head Target",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (root / "docs" / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "chore: seed repository")
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}),
        encoding="utf-8",
    )
    return root


def _record_directory() -> Path:
    """The ``review-records`` directory of the run this review reads."""
    return review_module.run_review_record_directory(Path(run_dir(REVIEWED_RUN_ID)))


def _legacy_path() -> Path:
    """The unsuffixed record path, keyed on no head."""
    return _record_directory() / f"{REVIEWED_RUN_ID}.json"


def _head_path() -> Path:
    """The head-keyed record path the dispatch names."""
    return _record_directory() / f"{REVIEWED_RUN_ID}.at-{DISPATCHED_HEAD}.json"


def _declared_paths() -> list[str]:
    """The two record paths the composer declares for this review."""
    return review_module.run_review_record_paths(
        Path(run_dir(REVIEWED_RUN_ID)),
        REVIEWED_RUN_ID,
        reviewed_head_sha=DISPATCHED_HEAD,
    )


def _round(head: str, *, review_run_id: str = REVIEW_RUN_ID) -> dict:
    """A complete review record describing ``head``."""
    return {
        "status": "parsed",
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN_ID,
        "review_run_id": review_run_id,
        "reviewed_base_sha": REVIEWED_BASE,
        "reviewed_head_sha": head,
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20),
        "total": TOTAL,
        "findings": [],
        "timestamp": "2026-10-12T13:00:00Z",
    }


def _write_round(path: Path, body: dict, *, mtime: float | None = None) -> Path:
    """Write a record to ``path``, dated now unless a write time is named."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")
    stamp = os.path.getmtime(path) if mtime is None else mtime
    os.utime(path, (stamp, stamp))
    return path


def _write_pointer(repository: Path, tmp_path: Path, declared: list[str]) -> None:
    manifest = tmp_path / "manifests" / f"{REVIEW_RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: review-of-reviewed-run\n"
        "status: complete\n"
        "checkpoint: the review record is stored\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(REVIEW_RUN_ID),
        {
            "run_id": REVIEW_RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "launch": "in-harness",
            "role": "review",
            "backend": "native",
            "created_at": "2026-10-12T13:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": REVIEW_NODE_ID,
                "plan": PLAN,
                "section": "s1",
                "time_budget": "25m",
                "role": "review",
                "write_paths": declared,
            },
        },
    )


def _write_exit_record() -> None:
    directory = Path(run_dir(REVIEW_RUN_ID))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "exit.json").write_text(
        json.dumps({"run_id": REVIEW_RUN_ID, "exit_code": 0}),
        encoding="utf-8",
    )


def _pointer() -> dict:
    return json.loads(pointer_path(REVIEW_RUN_ID).read_text(encoding="utf-8"))


def _sweep() -> dict:
    return recovery.dispatch_awaiting_reviews(
        project=PROJECT, launcher=lambda *args, **kwargs: 0
    )


def test_a_predecessor_round_is_no_delivery_and_its_own_round_promotes(
    repository: Path, tmp_path: Path
) -> None:
    """The legacy path holds an earlier head; only the head-keyed round delivers."""
    _write_round(_legacy_path(), _round(PREDECESSOR_HEAD))
    _write_pointer(repository, tmp_path, _declared_paths())
    _write_exit_record()
    pointer = _pointer()

    assert _delivered_review_record(pointer, PROJECT) is None

    refusal = promote_review_run_itself(pointer)
    assert refusal["promoted"] is False, refusal
    assert DISPATCHED_HEAD in refusal["reason"], refusal["reason"]

    _write_round(_head_path(), _round(DISPATCHED_HEAD))
    assert _delivered_review_record(pointer, PROJECT) is not None, (
        "the run's own round at the head-keyed path is a delivery"
    )

    report = _sweep()

    assert report["self_promoted"] == [REVIEW_RUN_ID], report
    [row] = ledger.runs(PROJECT, root=repository)
    assert row["run_id"] == REVIEW_RUN_ID
    assert row["review"]["reviewed_head_sha"] == DISPATCHED_HEAD
    assert row["review"]["total"] == TOTAL


@pytest.mark.parametrize(
    "case",
    ["describes-another-head", "names-another-review", "written-before-dispatch"],
)
def test_a_record_that_is_not_this_runs_round_is_no_delivery(
    repository: Path, tmp_path: Path, case: str
) -> None:
    """Each of the three mismatches refuses the record at the head-keyed path."""
    body = _round(DISPATCHED_HEAD)
    mtime = None
    if case == "describes-another-head":
        body["reviewed_head_sha"] = PREDECESSOR_HEAD
    if case == "names-another-review":
        body["review_run_id"] = OTHER_REVIEW_RUN_ID
    if case == "written-before-dispatch":
        mtime = DISPATCH_INSTANT - 60.0
    _write_round(_head_path(), body, mtime=mtime)
    _write_pointer(repository, tmp_path, _declared_paths())

    assert _delivered_review_record(_pointer(), PROJECT) is None


def test_a_review_declaring_only_the_legacy_path_is_refused_naming_the_head(
    repository: Path, tmp_path: Path
) -> None:
    """A review that names no head-keyed path is refused, naming the head."""
    _write_round(_legacy_path(), _round(DISPATCHED_HEAD))
    _write_pointer(repository, tmp_path, [str(_legacy_path())])
    _write_exit_record()

    refusal = promote_review_run_itself(_pointer())

    assert refusal["promoted"] is False, refusal
    assert "declares no head-keyed record path" in refusal["reason"], refusal["reason"]
    assert "head" in refusal["reason"].lower()
