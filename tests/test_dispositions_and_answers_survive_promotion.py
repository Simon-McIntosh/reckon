"""A disposition or an answer recorded around a promotion reaches the committed record.

Since promotion began committing a review run's records, the run-review reader
``stored_record`` answers from the committed copy before the staging one, and
``crew dispose`` writes back to whichever copy that reader selected. The two
sides can therefore name different files: a disposition is written to a staging
sibling a promotion does not read, so it is left behind in the host store, and
an answer written to a committed record is left as a modified tracked file that
never enters a commit. These cases hold a promotion to carrying the disposition
and answer fields out of every staging copy of the record it commits, hold the
plan-review answer verb to writing the committed copy when one exists, and hold
a disposition written after promotion to landing in a commit rather than in the
working tree.

Every crew directory is environment-resolved under ``tmp_path``; nothing touches
the operator's own store.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import crew, flight
from reckon.crew import plan_review
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path
from tests.conftest import EXECUTABLE_GATE_COMMAND

PROJECT = "survives-promotion"
PLAN = "survives-promotion-plan"
PLAN_SLUG = "survives-promotion-plan"
PLAN_VERSION = 2

SUBJECT = "r-20261007T100000000000-reviewed-subject"
REVIEW = "r-20261007T110000000000-review-run"
PLAN_REVIEW_RUN = "r-20261007T120000000000-plan-review"
HEAD = "775001135fd133d170a379fcf690336ce649cd11"
BASE = "a" * 40
BLOB = "b" * 40
DISPATCH_TS = "2026-10-07T10:00:00Z"
COMPLETION_TS = "2026-10-07T10:30:00Z"

_FINDING = "reuse_search-1"


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


def _section(identity: str) -> str:
    return (
        f'<section data-reckon="section" data-id="{identity}" '
        'data-effort-hours="0.5" data-capability-version="1.0" '
        'data-capability-class="general" data-capability-reasoning="standard" '
        'data-capability-verification="strict" data-capability-risk="low" '
        'data-status="implementable" data-links=""></section>'
    )


def _plan_html() -> str:
    declarations = json.dumps({"s1": "implementable"})
    return (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{PLAN_SLUG}">'
        '<meta name="plan-title" content="Fixture">'
        '<meta name="plan-status" content="active">'
        f'<meta name="plan-version" content="{PLAN_VERSION}">'
        f"<meta name=\"plan-section-declarations\" content='{declarations}'>"
        "</head><body><h1>Fixture</h1>"
        '<h2 id="s1">First section</h2>'
        + _section("s1")
        + "<p>Authored prose for the first section.</p>"
        "</body></html>"
    )


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path, monkeypatch) -> Path:
    """A git checkout whose docs directory is the project's mount."""
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    plan_path = plans / f"{PLAN_SLUG}.html"
    plan_path.write_text(_plan_html(), encoding="utf-8")
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
    monkeypatch.setattr(
        flight,
        "resolve",
        lambda **kw: SimpleNamespace(config={"default_backend": "worker"}),
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


def _run_review_record(
    *, disposition: dict | None = None, head: str | None = HEAD
) -> dict:
    record = {
        "project": PROJECT,
        "reviewed_run_id": SUBJECT,
        "review_run_id": REVIEW,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 18),
        "absent": [],
        "total": 18 * len(review_module.REVIEW_DIMENSIONS),
    }
    if head is not None:
        record["reviewed_base_sha"] = BASE
        record["reviewed_head_sha"] = head
    if disposition is not None:
        record[review_module.DIMENSION_DISPOSITIONS_KEY] = disposition
    return record


def _write_staging(path: Path, payload: dict) -> Path:
    """Write one staging record directly, as a hand-written store may hold one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _review_pointer(repository: Path, tmp_path: Path) -> None:
    """A live review pointer whose declared record is the plain staging file."""
    manifest = tmp_path / "review.manifest.md"
    manifest.write_text(
        "node: review-of-a-subject\n"
        "status: complete\n"
        "commits: none\n"
        "changed_paths: []\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n",
        encoding="utf-8",
    )
    declared = review_module.review_path(PROJECT, SUBJECT)
    _write_json(
        pointer_path(REVIEW),
        {
            "run_id": REVIEW,
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
                "section": "s2",
                "time_budget": "25m",
                "role": "review",
                "write_paths": [str(declared)],
            },
        },
    )


def _committed_run_review(repository: Path) -> Path:
    committed = review_module.committed_review_root(PROJECT, root=repository)
    assert committed is not None
    return committed / "run" / SUBJECT / f"{REVIEW}.json"


# ── (1) A promotion carries a sibling copy's disposition ─────────────────────


def test_promotion_carries_a_disposition_from_a_staging_sibling(
    repository: Path, tmp_path: Path
) -> None:
    _write_run_record(repository, SUBJECT)
    # The record the promotion reads carries no disposition; a head-keyed
    # sibling of it does, exactly as ``crew dispose`` leaves one when its reader
    # selects the head-keyed file rather than the plain one a promotion reads.
    plain = _write_staging(
        review_module.review_path(PROJECT, SUBJECT), _run_review_record()
    )
    sibling = _write_staging(
        review_module.review_path(PROJECT, SUBJECT, reviewed_head_sha=HEAD),
        _run_review_record(
            disposition={"evidence": {"kind": "exempted", "reason": "no evidence"}}
        ),
    )
    assert plain.is_file() and sibling.is_file() and plain != sibling
    _review_pointer(repository, tmp_path)

    crew.complete(REVIEW, gate="passed", root=repository)

    path, record = review_module.stored_record(PROJECT, SUBJECT)
    assert path == _committed_run_review(repository)
    assert record is not None
    # The disposition written to the sibling survives into the committed record.
    assert record[review_module.DIMENSION_DISPOSITIONS_KEY]["evidence"]["kind"] == (
        "exempted"
    )


# ── (2) An answer to a promoted plan review reaches the committed copy ────────


def _covering_plan_record(document: str) -> dict:
    return {
        "project": PROJECT,
        "plan_slug": PLAN_SLUG,
        "plan_version": PLAN_VERSION,
        "review_run_id": PLAN_REVIEW_RUN,
        "reviewed_blob_sha": BLOB,
        "plan_fingerprint": "0" * 64,
        "section_digests": plan_review._section_digests(document),
        "rubric": "design",
        "status": "ready",
        "findings": [
            {"id": _FINDING, "type": "reuse_search", "text": "Name the owner."}
        ],
        "responses": {},
    }


def test_an_answer_to_a_promoted_plan_review_reaches_the_committed_copy(
    repository: Path,
) -> None:
    _write_run_record(repository, PLAN_REVIEW_RUN)
    document = (repository / "docs" / "plans" / f"{PLAN_SLUG}.html").read_text(
        encoding="utf-8"
    )
    record = _covering_plan_record(document)
    committed = review_module.store_committed_review(record, root=repository)
    assert committed.is_file()

    stored = plan_review.read_plan_review(PROJECT, PLAN_SLUG, plan=document)
    assert stored is not None and _FINDING in plan_review.finding_ids(stored)
    assert plan_review.unanswered_findings(stored) == [_FINDING]

    plan_review.record_response(stored, _FINDING, action="acted", by="coordinator")

    # The gate's call — ``read_plan_review(..., plan=...)`` through
    # ``review_coverage`` and ``list_plan_reviews`` — sees the answer.
    answered = plan_review.read_plan_review(PROJECT, PLAN_SLUG, plan=document)
    assert answered is not None
    assert plan_review.unanswered_findings(answered) == []
    assert answered["responses"][_FINDING]["action"] == "acted"
    # The answer landed in the committed file, not a staging copy beside it.
    assert _read(committed)["responses"][_FINDING]["action"] == "acted"


# ── (3) A disposition after promotion lands in a commit ──────────────────────


def test_a_disposition_after_promotion_lands_in_a_commit(repository: Path) -> None:
    _write_run_record(repository, SUBJECT)
    committed = review_module.store_committed_review(
        _run_review_record(), root=repository
    )
    before = _git(repository, "rev-parse", "HEAD")

    review_module.record_dimension_disposition(
        PROJECT, SUBJECT, "evidence", kind="exempted", reason="no evidence"
    )

    relative = str(committed.relative_to(repository))
    after = _git(repository, "rev-parse", "HEAD")
    assert after != before
    # The change is in the commit and the working tree is clean for that path,
    # rather than the write sitting as a modified tracked file.
    assert _git(repository, "status", "--porcelain", "--", relative) == ""
    assert (
        _read(committed)[review_module.DIMENSION_DISPOSITIONS_KEY]["evidence"]["kind"]
        == "exempted"
    )
    shown = _git(repository, "show", f"HEAD:{relative}")
    assert (
        json.loads(shown)[review_module.DIMENSION_DISPOSITIONS_KEY]["evidence"]["kind"]
        == "exempted"
    )
