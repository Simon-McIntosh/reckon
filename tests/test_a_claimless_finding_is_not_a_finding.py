"""A finding that states no claim is not a finding.

A review worker writes its record by hand, so nothing between the reviewer and
the repair reader checks what an entry in ``findings`` actually says. A record
whose findings were ``[{"file": "reckon_placeholder"}]`` was read as a blocking
finding: the repair reflex resumed the reviewed run with advice naming only the
placeholder and a write scope of that name. These cases hold the one normaliser
every repair reader shares — :func:`repair.review_findings` — to the rule that
a finding states a claim as a non-empty string or a mapping carrying a non-empty
``text``, ``summary``, ``detail`` or ``title``, and that a finding stating none
is reported as malformed rather than read as work.

Every store is synthesised under the test's own temporary path; the promotion
case builds a temporary repository and configuration home. Nothing touches the
operator's store.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import repair
from reckon.crew import review as review_module
from reckon.crew.review import review_path
from reckon.crew.runs import _write_json, crew_home, pointer_path
from tests.conftest import EXECUTABLE_GATE_COMMAND

PROJECT = "claimless-finding-fixture"
RUN_ID = "r-20260101T000000000000-reviewed-run"
REVIEW_RUN_ID = "r-20260101T010000000000-review-of-the-run"
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40

# The placeholder shape the live record carried: a file and nothing else.
PLACEHOLDER = {"file": "reckon_placeholder"}
REAL_FINDING = {"file": "reckon/crew/thing.py", "line": "10", "text": "a real claim"}


def _expected_id(file: str, line: str, text: str) -> str:
    """The id the documented rule yields, re-derived here as the oracle.

    The rule is a fixed prefix plus the first ten hex digits of the sha256 of
    the NUL-joined ``file``, ``line`` and ``text`` fields. It is written out
    here so the assertion holds against an expectation the module cannot satisfy
    by returning a constant.
    """
    material = "\x00".join((file.strip(), line.strip(), text.strip()))
    return "f" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]


def _store(tmp_path: Path, findings: list[object]) -> str:
    """Write a review record into a temporary store root and return that root."""
    base_dir = str(tmp_path / "reviews")
    review_module.store_review(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN_ID,
            "review_run_id": REVIEW_RUN_ID,
            "reviewed_base_sha": BASE_SHA,
            "reviewed_head_sha": HEAD_SHA,
            "status": "parsed",
            "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15),
            "findings": findings,
        },
        base_dir=base_dir,
    )
    return base_dir


def _report_for(failures: list[dict[str, str]], path: Path | None) -> dict[str, str] | None:
    """The report naming one file, or ``None`` when it is not named."""
    for entry in failures:
        if entry["path"] == str(path):
            return entry
    return None


# ── (1) A claimless-only record composes no repair and is reported ───────────


def test_a_claimless_only_record_composes_no_repair_and_is_reported(
    tmp_path: Path,
) -> None:
    base_dir = _store(tmp_path, [PLACEHOLDER])

    # The reflex composes nothing: the composition its resume is cut from is
    # None, and no finding blocks.
    assert repair.blocking_findings({"findings": [PLACEHOLDER]}) == []
    assert repair.compose_repair_for_run(PROJECT, RUN_ID, base_dir=base_dir) is None

    # The record is kept as written, and reading it names the file as malformed
    # in the collection region a sweep opens.
    with review_module.collect_read_failures() as failures:
        path, record = review_module.stored_record(PROJECT, RUN_ID, base_dir=base_dir)

    assert record is not None and path is not None
    assert record["findings"] == [PLACEHOLDER]
    report = _report_for(failures, path)
    assert report is not None, failures
    assert "1" in report["error"] and "claim" in report["error"].lower()


# ── (2) A claimless finding beside a real one repairs only the real one ──────


def test_a_claimless_finding_beside_a_real_one_repairs_only_the_real_one(
    tmp_path: Path,
) -> None:
    base_dir = _store(tmp_path, [PLACEHOLDER, REAL_FINDING])

    node = repair.compose_repair_for_run(PROJECT, RUN_ID, base_dir=base_dir)

    assert isinstance(node, dict)
    ids = [finding["id"] for finding in node["findings"]]
    assert ids == [_expected_id("reckon/crew/thing.py", "10", "a real claim")]
    assert REAL_FINDING["text"] in node["brief"]
    # The claimless finding is neither repaired nor granted a write path.
    assert "reckon_placeholder" not in node["write_paths"]

    # The record is still reported malformed.
    with review_module.collect_read_failures() as failures:
        path, _record = review_module.stored_record(PROJECT, RUN_ID, base_dir=base_dir)
    assert _report_for(failures, path) is not None, failures


# ── (3) A bare string and a detail/title finding are blocking findings ───────


def test_a_bare_string_and_a_detail_finding_are_blocking_findings() -> None:
    review = {
        "findings": [
            "a bare-string claim",
            {"file": "reckon/crew/a.py", "line": "1", "detail": "a detail claim"},
            {"file": "reckon/crew/b.py", "line": "2", "title": "a title claim"},
        ]
    }

    blocking = repair.blocking_findings(review)

    assert {finding["text"] for finding in blocking} == {
        "a bare-string claim",
        "a detail claim",
        "a title claim",
    }
    # The bare string is included, not dropped, and names no file.
    bare = [finding for finding in blocking if finding["text"] == "a bare-string claim"]
    assert len(bare) == 1 and bare[0]["file"] == ""


def test_a_mapping_with_no_claim_field_is_dropped() -> None:
    assert repair.review_findings({"findings": [PLACEHOLDER]}) == []


# ── (4) The ids of existing findings are unchanged ───────────────────────────


def test_ids_stay_derived_from_the_text_key_whatever_claim_is_carried() -> None:
    parsed = repair.review_findings(
        {
            "findings": [
                {"file": "reckon/crew/thing.py", "line": "7", "text": "a stated claim"},
                {
                    "file": "reckon/crew/thing.py",
                    "line": "9",
                    "detail": "only a detail field",
                },
            ]
        }
    )

    assert [finding["id"] for finding in parsed] == [
        _expected_id("reckon/crew/thing.py", "7", "a stated claim"),
        # The detail-only finding keeps the id today's rule yields: the ``text``
        # key is absent, so the id derives from an empty text.
        _expected_id("reckon/crew/thing.py", "9", ""),
    ]
    # The claim it carries is read, even though it does not move the id.
    assert parsed[1]["text"] == "only a detail field"


# ── (5) A promoted record carrying a claimless finding is committed ──────────


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    """A repository whose docs directory is the project's mount."""
    assert crew_home().is_relative_to(isolated_reckon_home)
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the claimless-finding repository"),
    ):
        _git(root, *arguments)
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def test_a_promoted_claimless_record_is_committed_and_still_reported(
    repository: Path, tmp_path: Path
) -> None:
    # The review run's whole deliverable is the record it stores for the run it
    # reviewed, so a claimless record is landed by promotion exactly as written.
    record_path = review_path(PROJECT, RUN_ID)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "reviewed_run_id": RUN_ID,
                "review_run_id": REVIEW_RUN_ID,
                "reviewed_base_sha": BASE_SHA,
                "reviewed_head_sha": HEAD_SHA,
                "status": "parsed",
                "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15),
                "findings": [PLACEHOLDER],
            }
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "review.manifest.md"
    manifest.write_text(
        "node: review-of-the-run\n"
        "status: complete\n"
        "commits: none\n"
        f"changed_paths:\n  - {record_path}\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(REVIEW_RUN_ID),
        {
            "run_id": REVIEW_RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "backend": "native",
            "role": "review",
            "manifest_path": str(manifest),
            "node": {
                "id": "review-of-the-run",
                "plan": "claimless-plan",
                "section": "s1",
                "time_budget": "20m",
                "role": "review",
                "write_paths": [str(record_path)],
            },
        },
    )

    crew.complete(REVIEW_RUN_ID, gate="passed", root=repository)

    committed = review_path(
        PROJECT,
        RUN_ID,
        committed_root=review_module.committed_review_root(PROJECT, root=repository),
        review_run_id=REVIEW_RUN_ID,
    )
    assert committed.is_file()
    assert json.loads(committed.read_text(encoding="utf-8"))["findings"] == [PLACEHOLDER]

    # The malformed report still stands over the committed store.
    with review_module.collect_read_failures() as failures:
        path, record = review_module.stored_record(
            PROJECT, RUN_ID, reviewed_head_sha=HEAD_SHA
        )
    assert path == committed and record is not None
    assert _report_for(failures, path) is not None, failures