"""A review written to both of its granted paths is committed once.

A reviewer writes its one record to both of the paths its dispatch grants, so
the same bytes sit at the delivered path and at its sibling. These cases hold
the promotion that commits every staged round of a subject to filing that one
round once, and to leaving a round it has already committed untouched, so a
disposition recorded on the committed copy survives the subject's next review.
A staged round the subject has not committed yet is still committed under its
derived legacy id.

Every crew directory and the committed tree are resolved under temporary
directories; nothing touches the operator's own store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path
from tests.conftest import EXECUTABLE_GATE_COMMAND
from tests.helpers.git import git as _git

PROJECT = "a-review-written-twice-fixture"
PLAN = "a-review-written-twice-target"

SUBJECT = "r-20261006T110000000000-reviewed-subject"
FIRST_REVIEW = "r-20261006T120000000000-first-review"
LATER_REVIEW = "r-20261006T130000000000-later-review"
HEAD_ONE = "2e05df7191c5413efcfb9f3cc40b5f18b1f7a0bc"
HEAD_TWO = "7c9b1e4d5a2f3b6c8d0e1f2a3b4c5d6e7f8a9b0c"
HEAD_THREE = "0f1e2d3c4b5a69788796a5b4c3d2e1f00a1b2c3d"
BASE = "a" * 40
DISPATCH_TS = "2026-10-06T11:00:00Z"
COMPLETION_TS = "2026-10-06T11:30:00Z"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    """A repository whose docs directory is the project's mount."""
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n',
        encoding="utf-8",
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _write_run_record(
    repository: Path, run_id: str, *, dispatched: str, completed: str
) -> Path:
    path = repository / "docs" / "state" / PROJECT / "runs" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "completed_at": completed,
                "dispatched_at": dispatched,
            }
        ),
        encoding="utf-8",
    )
    _git(repository, "add", str(path.relative_to(repository)))
    _git(repository, "commit", "-q", "-m", "chore: land a run record")
    return path


def _manifest_body() -> str:
    return (
        "node: review-of-a-subject\n"
        "status: complete\n"
        "commits: none\n"
        "changed_paths: []\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n"
    )


def _review_pointer(
    repository: Path,
    tmp_path: Path,
    *,
    run_id: str,
    declared: list[Path],
    repo: Path | None = None,
) -> None:
    manifest = tmp_path / f"{run_id}.manifest.md"
    manifest.write_text(_manifest_body(), encoding="utf-8")
    # ``repo`` names the checkout the run's own record carries, which a landing
    # given an explicit root may differ from.
    run_repo = repository if repo is None else repo
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(run_repo),
            "worktree": str(run_repo),
            "base_sha": _git(run_repo, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "backend": "native",
            "created_at": DISPATCH_TS,
            "role": "review",
            "manifest_path": str(manifest),
            "node": {
                "id": f"review-of-{SUBJECT}",
                "plan": PLAN,
                "section": "s3",
                "time_budget": "25m",
                "role": "review",
                "write_paths": [str(path) for path in declared],
            },
        },
    )


def _complete_review(head: str | None, *, review_run_id: str | None = None) -> dict:
    record: dict = {
        "project": PROJECT,
        "reviewed_run_id": SUBJECT,
        "reviewed_base_sha": BASE,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 18),
        "absent": [],
        "total": 18 * len(review_module.REVIEW_DIMENSIONS),
    }
    if review_run_id is not None:
        record["review_run_id"] = review_run_id
    if head is not None:
        record["reviewed_head_sha"] = head
    return record


def _write_staged(record: dict, *, head: str | None = None) -> Path:
    path = review_module.review_path(PROJECT, SUBJECT, reviewed_head_sha=head)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _committed_run_dir(repository: Path) -> Path:
    committed = review_module.committed_review_root(PROJECT, root=repository)
    assert committed is not None
    return committed / review_module.COMMITTED_RUN_DIRNAME / SUBJECT


def _other_checkout(tmp_path: Path, name: str) -> Path:
    """A second checkout: a git repository holding no committed review tree."""
    root = tmp_path / name
    keep = root / "docs" / "state" / PROJECT / ".keep"
    keep.parent.mkdir(parents=True)
    keep.write_text("", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "test: seed a second checkout"),
    ):
        _git(root, *arguments)
    return root


# One record, both granted paths -> one committed round, under the run's id ----


def test_one_record_to_both_granted_paths_is_committed_once(
    repository: Path, tmp_path: Path
) -> None:
    _write_run_record(
        repository, SUBJECT, dispatched=DISPATCH_TS, completed=COMPLETION_TS
    )
    # The reviewer wrote one record naming no review run to both of the paths
    # its dispatch granted: the recorded head keys the sibling, the plain path
    # is its pair, and both hold the same bytes.
    record = _complete_review(HEAD_ONE)
    plain = _write_staged(record)
    sibling = _write_staged(record, head=HEAD_ONE)
    assert plain.read_bytes() == sibling.read_bytes()
    assert plain != sibling
    _review_pointer(
        repository, tmp_path, run_id=FIRST_REVIEW, declared=[plain, sibling]
    )

    crew.complete(FIRST_REVIEW, gate="passed", root=repository)

    names = sorted(path.name for path in _committed_run_dir(repository).glob("*.json"))
    assert names == [f"{FIRST_REVIEW}.json"]
    filed = _read(_committed_run_dir(repository) / f"{FIRST_REVIEW}.json")
    assert filed["review_run_id"] == FIRST_REVIEW


# A second staged round is still committed under its derived legacy id ---------


def test_a_second_staged_round_lands_under_its_legacy_id(
    repository: Path, tmp_path: Path
) -> None:
    _write_run_record(
        repository, SUBJECT, dispatched=DISPATCH_TS, completed=COMPLETION_TS
    )
    record = _complete_review(HEAD_ONE)
    plain = _write_staged(record)
    sibling = _write_staged(record, head=HEAD_ONE)
    # A second round of the same subject at another head, also naming no review
    # run and not delivered by this promotion: its own bytes key it.
    other = _write_staged(_complete_review(HEAD_TWO), head=HEAD_TWO)
    derived = review_module.derived_legacy_review_run_id(other.read_bytes())
    _review_pointer(
        repository, tmp_path, run_id=FIRST_REVIEW, declared=[plain, sibling]
    )

    crew.complete(FIRST_REVIEW, gate="passed", root=repository)

    run_dir = _committed_run_dir(repository)
    names = sorted(path.name for path in run_dir.glob("*.json"))
    assert names == sorted([f"{FIRST_REVIEW}.json", f"{derived}.json"])
    filed = _read(run_dir / f"{derived}.json")
    assert filed["review_run_id"] == derived
    assert filed["review_run_id_source"] == "derived"


# A disposition on a committed round survives the subject's next promotion -----


def test_a_disposition_survives_a_later_promotion(
    repository: Path, tmp_path: Path
) -> None:
    _write_run_record(
        repository, SUBJECT, dispatched=DISPATCH_TS, completed=COMPLETION_TS
    )
    record = _complete_review(HEAD_ONE)
    plain = _write_staged(record)
    sibling = _write_staged(record, head=HEAD_ONE)
    other = _write_staged(_complete_review(HEAD_TWO), head=HEAD_TWO)
    derived = review_module.derived_legacy_review_run_id(other.read_bytes())
    _review_pointer(
        repository, tmp_path, run_id=FIRST_REVIEW, declared=[plain, sibling]
    )
    crew.complete(FIRST_REVIEW, gate="passed", root=repository)

    # A disposition is recorded on the committed copy of the second round, the
    # same field ``crew dispose`` writes.
    committed_round = _committed_run_dir(repository) / f"{derived}.json"
    committed = _read(committed_round)
    committed[review_module.FINDING_DISPOSITIONS_KEY] = {
        "0": {"kind": "folded", "recorded_at": "2026-10-06T12:30:00+00:00"}
    }
    committed_round.write_text(json.dumps(committed), encoding="utf-8")
    _git(repository, "add", str(committed_round.relative_to(repository)))
    _git(repository, "commit", "-q", "-m", "chore: record a disposition")

    # A later review of the same subject is promoted. Its round arrives at a new
    # head, and the second round is still staged for the subject.
    later = _write_staged(_complete_review(HEAD_THREE), head=HEAD_THREE)
    _review_pointer(repository, tmp_path, run_id=LATER_REVIEW, declared=[later])
    crew.complete(LATER_REVIEW, gate="passed", root=repository)

    after = _read(committed_round)
    assert after[review_module.FINDING_DISPOSITIONS_KEY] == {
        "0": {"kind": "folded", "recorded_at": "2026-10-06T12:30:00+00:00"}
    }


# The ledger root is the tree the skip reads, not the run's own repository -----


def test_a_disposition_survives_when_the_ledger_root_differs_from_the_run_repo(
    repository: Path, tmp_path: Path
) -> None:
    # The landing is committed in ``repository`` — the tree ``crew complete`` is
    # given — while the run's own record names another checkout. The
    # already-committed skip has to read the tree the write targets: read
    # against the run's repository instead and the round committed here looks
    # absent, is filed a second time, and its disposition is dropped.
    run_repo = _other_checkout(tmp_path, "run-checkout")
    _write_run_record(
        repository, SUBJECT, dispatched=DISPATCH_TS, completed=COMPLETION_TS
    )
    record = _complete_review(HEAD_ONE)
    plain = _write_staged(record)
    sibling = _write_staged(record, head=HEAD_ONE)
    other = _write_staged(_complete_review(HEAD_TWO), head=HEAD_TWO)
    derived = review_module.derived_legacy_review_run_id(other.read_bytes())
    _review_pointer(
        repository,
        tmp_path,
        run_id=FIRST_REVIEW,
        declared=[plain, sibling],
        repo=run_repo,
    )
    crew.complete(FIRST_REVIEW, gate="passed", root=repository)

    committed_round = _committed_run_dir(repository) / f"{derived}.json"
    committed = _read(committed_round)
    committed[review_module.FINDING_DISPOSITIONS_KEY] = {
        "0": {"kind": "folded", "recorded_at": "2026-10-06T12:30:00+00:00"}
    }
    committed_round.write_text(json.dumps(committed), encoding="utf-8")
    _git(repository, "add", str(committed_round.relative_to(repository)))
    _git(repository, "commit", "-q", "-m", "chore: record a disposition")

    later = _write_staged(_complete_review(HEAD_THREE), head=HEAD_THREE)
    _review_pointer(
        repository, tmp_path, run_id=LATER_REVIEW, declared=[later], repo=run_repo
    )
    crew.complete(LATER_REVIEW, gate="passed", root=repository)

    after = _read(committed_round)
    assert after[review_module.FINDING_DISPOSITIONS_KEY] == {
        "0": {"kind": "folded", "recorded_at": "2026-10-06T12:30:00+00:00"}
    }
