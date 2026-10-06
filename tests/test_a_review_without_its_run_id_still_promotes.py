"""A review run whose stored record lost its own id still promotes.

A record a reviewer writes by hand can lose its ``review_run_id``, and the
committed store keys every file by that id: filed under the reviewed run
instead, two review runs of one subject would name the same committed file.
These cases hold a promoting review run to supplying its own id for the round
that run delivered — the run that produced it is exactly the run being promoted
— so the round lands rather than being abandoned. They hold the promotion to
skipping a round it cannot key at all, one that is neither this run's own
delivered round nor carries a review run id, with a note naming the file rather
than refusing the whole landing, and hold a round that does carry its id to
being committed beside it.

Every crew directory is environment-resolved under ``tmp_path``; nothing touches
the operator's own store.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path
from tests.conftest import EXECUTABLE_GATE_COMMAND

PROJECT = "review-run-id-fixture"
PLAN = "review-run-id-target"

SUBJECT = "r-20261006T110000000000-reviewed-subject"
REVIEW = "r-20261006T120000000000-promoting-review"
OTHER_REVIEW = "r-20261006T115000000000-earlier-review"
HEAD_ONE = "2e05df7191c5413efcfb9f3cc40b5f18b1f7a0bc"
HEAD_TWO = "775001135fd133d170a379fcf690336ce649cd11"
BASE = "a" * 40
DISPATCH_TS = "2026-10-06T11:00:00Z"
COMPLETION_TS = "2026-10-06T11:30:00Z"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


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


def _write_run_record(repository: Path, run_id: str) -> Path:
    """Write one run's committed per-run record beside the ledger."""
    path = repository / "docs" / "state" / PROJECT / "runs" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": PROJECT,
                "dispatched_at": DISPATCH_TS,
                "completed_at": COMPLETION_TS,
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
    declared: list[str],
) -> None:
    manifest = tmp_path / f"{run_id}.manifest.md"
    manifest.write_text(_manifest_body(), encoding="utf-8")
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": _git(repository, "rev-parse", "HEAD"),
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
                "write_paths": list(declared),
            },
        },
    )


def _complete_run_review(head: str | None, *, review_run_id: str | None) -> dict:
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


def _store(record: dict, *, head: str | None = None) -> Path:
    path = review_module.review_path(
        PROJECT, SUBJECT, reviewed_head_sha=head
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _committed_run_dir(repository: Path) -> Path:
    committed = review_module.committed_review_root(PROJECT, root=repository)
    assert committed is not None
    return committed / "run" / SUBJECT


def test_a_review_without_its_run_id_lands_under_the_promoting_run_id(
    repository: Path, tmp_path: Path
) -> None:
    _write_run_record(repository, SUBJECT)
    # The round this run delivered, stored where the dispatch told it to write,
    # carrying no review run id of its own.
    delivered_path = _store(_complete_run_review(None, review_run_id=None))
    # A round carrying its own id is committed beside it.
    _store(_complete_run_review(HEAD_TWO, review_run_id=OTHER_REVIEW), head=HEAD_TWO)
    _review_pointer(
        repository, tmp_path, run_id=REVIEW, declared=[str(delivered_path)]
    )

    crew.complete(REVIEW, gate="passed", root=repository)

    promoted = _committed_run_dir(repository) / f"{REVIEW}.json"
    assert promoted.is_file()
    assert _read(promoted)["review_run_id"] == REVIEW

    other_committed = _committed_run_dir(repository) / f"{OTHER_REVIEW}.json"
    assert other_committed.is_file()
    assert _read(other_committed)["review_run_id"] == OTHER_REVIEW

    row = next(
        item
        for item in ledger.runs(PROJECT, root=repository)
        if item.get("run_id") == REVIEW
    )
    assert row["review"]["id"] == REVIEW


def test_an_unnameable_round_is_skipped_with_a_note_not_refused(
    repository: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_run_record(repository, SUBJECT)
    delivered_path = _store(_complete_run_review(None, review_run_id=None))
    # Another round of the same subject, also carrying no id: it is not the
    # round this run delivered, so it cannot be filed under any run id and must
    # be skipped rather than refusing the landing.
    unnameable_path = _store(
        _complete_run_review(HEAD_ONE, review_run_id=None), head=HEAD_ONE
    )
    _review_pointer(
        repository, tmp_path, run_id=REVIEW, declared=[str(delivered_path)]
    )

    with caplog.at_level(logging.WARNING, logger="reckon.crew.promotion"):
        crew.complete(REVIEW, gate="passed", root=repository)

    committed_names = sorted(
        path.name for path in _committed_run_dir(repository).glob("*.json")
    )
    assert committed_names == [f"{REVIEW}.json"]

    notes = [
        record.getMessage()
        for record in caplog.records
        if record.name == "reckon.crew.promotion"
    ]
    assert any(str(unnameable_path) in note for note in notes)