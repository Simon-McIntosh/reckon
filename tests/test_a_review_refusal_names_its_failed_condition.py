"""``crew complete`` names the one delivery condition a review run failed.

A review run's outcome is taken from the record it stored, so a promotion of a
review whose record has not delivered is refused with the reason. The reason was
one generic sentence for every cause, and the cause that actually happens — a
record that parses and scores every dimension but carries no total — read as a
corrupt file, sending a coordinator to inspect a good record.

Five conditions make a record no delivery, and the refusal names the one that
failed, with the path it concerns and the remedy that condition calls for:

- the run declares no head-keyed record path;
- the record there is not a complete review;
- the head it describes is not the head its path names;
- it names a different review run;
- it was written before this run was dispatched.

Each is built on a temporary project and refused through ``crew complete
--gate not-run`` with no ``--outcome``. No refusal for a record that parses says
the record cannot be read. A delivered record still promotes, with its outcome
taken from the stored review.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import _plan_html, crew
from reckon.crew import review as review_module
from reckon.crew.node import CrewError
from reckon.crew.promotion_checks import _resolve_promotion_outcome
from reckon.crew.recovery_review_delivery import review_delivery_refusal
from reckon.crew.recovery_review_subject import (
    _review_is_complete,
    _review_missing_measures,
)
from reckon.crew.runs import _write_json, pointer_path, run_dir
from tests.helpers.git import git as _git

PROJECT = "review-refusal-fixture"
PLAN = "review-refusal-target"
REVIEWED_RUN_ID = "r-20261019T214105811291-reviewed-run"
REVIEW_RUN_ID = "r-20261019T230558980003-review-of-reviewed-run"
REVIEW_NODE_ID = f"review-of-{REVIEWED_RUN_ID}"
OTHER_REVIEW_RUN_ID = "r-20261019T230000000000-review-of-reviewed-run"
DISPATCHED_HEAD = "a" * 40
OTHER_HEAD = "b" * 40
REVIEWED_BASE = "c" * 40
TOTAL = 20 * len(review_module.REVIEW_DIMENSIONS)
DISPATCH_TIME = "2026-10-19T23:05:58Z"
DISPATCH_INSTANT = datetime(2026, 10, 19, 23, 5, 58, tzinfo=UTC).timestamp()

# The condition each case must be refused for, and the tokens its refusal must
# carry. ``cannot_be_read`` cases are the only ones allowed to say so.
CASES = {
    "no-head-keyed-path": ["no head-keyed record path"],
    "not-a-complete-review": ["record is not a complete review", "total"],
    "describes-another-head": [
        "record describes a different head",
        DISPATCHED_HEAD,
        OTHER_HEAD,
    ],
    "names-another-review": [
        "record names a different review run",
        REVIEW_RUN_ID,
        OTHER_REVIEW_RUN_ID,
    ],
    "written-before-dispatch": ["record written before the dispatch"],
}
PARSING_CASES = tuple(name for name in CASES if name != "no-head-keyed-path")


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    """A committed repository whose docs directory is the project's mount."""
    root = tmp_path / "repo"
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Review Refusal Target",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (root / "docs" / "state" / "proj-placeholder").mkdir(parents=True, exist_ok=True)
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
    return review_module.run_review_record_directory(Path(run_dir(REVIEWED_RUN_ID)))


def _legacy_path() -> Path:
    return _record_directory() / f"{REVIEWED_RUN_ID}.json"


def _head_path() -> Path:
    return _record_directory() / f"{REVIEWED_RUN_ID}.at-{DISPATCHED_HEAD}.json"


def _declared_paths() -> list[str]:
    return review_module.run_review_record_paths(
        Path(run_dir(REVIEWED_RUN_ID)),
        REVIEWED_RUN_ID,
        reviewed_head_sha=DISPATCHED_HEAD,
    )


def _round(head: str, *, total: int | None = TOTAL) -> dict:
    """A review record describing ``head``, scored on all six dimensions.

    ``total`` of ``None`` builds the shape the cited run had: every dimension
    scored and no total, which is a record that parses and is not complete.
    """
    record = {
        "status": "parsed",
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN_ID,
        "review_run_id": REVIEW_RUN_ID,
        "reviewed_base_sha": REVIEWED_BASE,
        "reviewed_head_sha": head,
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20),
        "findings": [],
        "timestamp": "2026-10-19T23:05:00Z",
    }
    if total is not None:
        record["total"] = total
    return record


def _write_round(path: Path, body: dict, *, mtime: float | None = None) -> Path:
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
            "created_at": DISPATCH_TIME,
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


def _build_case(repository: Path, tmp_path: Path, case: str) -> None:
    """Stage one review run failing exactly the named delivery condition."""
    _record_directory().mkdir(parents=True, exist_ok=True)
    if case == "no-head-keyed-path":
        _write_round(_legacy_path(), _round(DISPATCHED_HEAD))
        _write_pointer(repository, tmp_path, [str(_legacy_path())])
        return
    _write_pointer(repository, tmp_path, _declared_paths())
    if case == "not-a-complete-review":
        _write_round(_head_path(), _round(DISPATCHED_HEAD, total=None))
    elif case == "describes-another-head":
        _write_round(_head_path(), _round(OTHER_HEAD))
    elif case == "names-another-review":
        body = _round(DISPATCHED_HEAD)
        body["review_run_id"] = OTHER_REVIEW_RUN_ID
        _write_round(_head_path(), body)
    elif case == "written-before-dispatch":
        _write_round(
            _head_path(), _round(DISPATCHED_HEAD), mtime=DISPATCH_INSTANT - 60.0
        )
    else:  # pragma: no cover - a misspelt case is a test author's error
        raise AssertionError(f"unknown case {case!r}")


def _refusal(repository: Path) -> str:
    """The refusal ``crew complete --gate not-run`` gives with no outcome."""
    with pytest.raises(CrewError) as raised:
        crew.complete(REVIEW_RUN_ID, gate="not-run", root=repository)
    return str(raised.value)


def _write_corrupt_record(path: Path) -> Path:
    """A file at the record path that does not parse as a review record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json\n", encoding="utf-8")
    return path


def test_an_unreadable_record_is_refused_saying_it_cannot_be_read(
    repository: Path, tmp_path: Path
) -> None:
    """A corrupt file is the one delivery condition the refusal says unreadable."""
    _write_pointer(repository, tmp_path, _declared_paths())
    _write_corrupt_record(_head_path())

    refusal = _refusal(repository)

    assert "record cannot be read" in refusal, refusal
    assert _head_path().name in refusal, refusal


def test_an_absent_value_that_is_not_a_list_reads_as_incomplete() -> None:
    """A malformed ``absent`` value reads as incomplete rather than raising.

    The completeness rule is read from four classification paths, so a record
    carrying ``absent: true`` — which cannot name the dimensions it excludes —
    must leave the record incomplete instead of taking the pass down with a
    TypeError.
    """
    for absent in (True, 1, "goal_fidelity"):
        review = _round(DISPATCHED_HEAD)
        review["absent"] = absent

        missing = _review_missing_measures(review)

        assert missing, (absent, missing)
        assert _review_is_complete(review) is False, absent


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_review_refusal_names_the_condition_that_failed(
    repository: Path, tmp_path: Path, case: str
) -> None:
    _build_case(repository, tmp_path, case)

    refusal = _refusal(repository)

    for token in CASES[case]:
        assert token in refusal, (case, token, refusal)
    if case in PARSING_CASES:
        assert "cannot be read" not in refusal, (case, refusal)


@pytest.mark.parametrize("case", PARSING_CASES)
def test_no_refusal_for_a_parsing_record_says_it_cannot_be_read(
    repository: Path, tmp_path: Path, case: str
) -> None:
    """A record that parses is never reported as one that cannot be read."""
    _build_case(repository, tmp_path, case)

    refusal = _refusal(repository)

    assert "cannot be read" not in refusal.lower(), (case, refusal)
    # The refusal still names the failing condition rather than only the file.
    assert CASES[case][0] in refusal, (case, refusal)


def test_the_incomplete_case_names_the_missing_total(
    repository: Path, tmp_path: Path
) -> None:
    """Six scored dimensions and no total is refused naming the missing total."""
    _build_case(repository, tmp_path, "not-a-complete-review")

    refusal = _refusal(repository)

    assert "total" in refusal, refusal
    assert "not a complete review" in refusal, refusal


def test_a_delivered_record_still_promotes_on_its_stored_review(
    repository: Path, tmp_path: Path
) -> None:
    """A complete record delivers, and its promotion reads the stored review."""
    _write_pointer(repository, tmp_path, _declared_paths())
    _write_round(_head_path(), _round(DISPATCHED_HEAD))
    Path(run_dir(REVIEW_RUN_ID)).mkdir(parents=True, exist_ok=True)
    (Path(run_dir(REVIEW_RUN_ID)) / "exit.json").write_text(
        json.dumps({"run_id": REVIEW_RUN_ID, "exit_code": 0}), encoding="utf-8"
    )
    pointer = json.loads(pointer_path(REVIEW_RUN_ID).read_text(encoding="utf-8"))

    assert review_delivery_refusal(pointer, PROJECT) is None

    result = crew.complete(
        REVIEW_RUN_ID,
        gate="not-run",
        root=repository,
        promoted_by="review-self",
    )

    # ``crew complete --gate not-run`` with no outcome takes the summary from
    # the stored review rather than refusing.
    assert result["record"]["review"]["total"] == TOTAL
    assert result["record"]["run_id"] == REVIEW_RUN_ID
    assert (
        _resolve_promotion_outcome(REVIEW_RUN_ID, pointer, verdict="not-run")
        == f"review scored {TOTAL}, 0 finding(s)"
    )
    assert not pointer_path(REVIEW_RUN_ID).exists()
