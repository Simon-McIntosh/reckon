"""Recording a disposition already stored writes no file and makes no commit.

A disposition answers a finding or a sub-floor dimension, and every writer of
one rewrites the review record and commits it. A caller that records the same
answer twice — a repair round re-folded on each sweep, an operator repeating a
command — therefore re-stamps ``recorded_at`` and lands a fresh commit on a
record whose content did not change. This test pins the opposite: two identical
calls leave the record's bytes and the checkout's commit count untouched, and a
changed answer is still written and committed.

The record lives in the project's committed reviews tree so the commit path is
exercised rather than a staging write that would be left alone anyway.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import review as review_module
from reckon.crew import review_dispositions
from reckon.crew.recovery_repair_dispatch import _fold_findings_into_round

PROJECT = "disposition-fixture"
REVIEWED_RUN = "r-20260101T000000000000-reviewed-run"
REVIEW_RUN = "r-20260101T010000000000-review-run"
BASE = "a" * 40
HEAD = "b" * 40
NODE = "repair-of-fixture-node"
OTHER_NODE = "repair-of-another-node"
DIMENSION = review_module.REVIEW_DIMENSIONS[0]
FINDINGS = [
    {"file": "reckon/crew/first.py", "line": "10", "text": "first defect"},
    {"file": "reckon/crew/second.py", "line": "20", "text": "second defect"},
]


def _record() -> dict[str, Any]:
    return {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": REVIEW_RUN,
        "reviewed_base_sha": BASE,
        "reviewed_head_sha": HEAD,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15),
        "findings": [dict(finding) for finding in FINDINGS],
    }


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def _commit_count(repository: Path) -> int:
    return int(_git(repository, "rev-list", "--count", "HEAD").stdout.strip())


@pytest.fixture()
def committed_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A project checkout whose committed reviews tree holds one committed record.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repository = tmp_path / "checkout"
    docs = repository / "docs"
    run_directory = docs / "state" / PROJECT / "reviews" / "run" / REVIEWED_RUN
    run_directory.mkdir(parents=True)
    record_path = run_directory / f"{REVIEW_RUN}.json"
    record_path.write_text(json.dumps(_record(), indent=2) + "\n", encoding="utf-8")

    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", "docs")
    _git(repository, "commit", "-q", "-m", "chore: seed the committed reviews tree")

    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(docs)}), encoding="utf-8"
    )
    return {
        "repo": repository,
        "record": record_path,
        "record_bytes": record_path.read_bytes(),
        "commits": _commit_count(repository),
    }


def _fold_finding(node: str = NODE) -> Path:
    return review_dispositions.record_finding_disposition(
        PROJECT,
        REVIEWED_RUN,
        0,
        kind="folded",
        node=node,
        reviewed_head_sha=HEAD,
    )


def _fold_dimension(node: str = NODE) -> Path:
    return review_dispositions.record_dimension_disposition(
        PROJECT,
        REVIEWED_RUN,
        DIMENSION,
        kind="folded",
        node=node,
        reviewed_head_sha=HEAD,
    )


def _stored_answers() -> dict[str, Any]:
    _path, record = review_module.stored_record(PROJECT, REVIEWED_RUN, reviewed_head_sha=HEAD)
    assert record is not None
    return record


def test_recording_an_identical_finding_disposition_writes_nothing(
    committed_record: dict[str, Any],
) -> None:
    _fold_finding()
    after_first = committed_record["record"].read_bytes()
    commits_after_first = _commit_count(committed_record["repo"])
    assert commits_after_first == committed_record["commits"] + 1
    assert after_first != committed_record["record_bytes"]

    path = _fold_finding()

    assert path == committed_record["record"]
    assert committed_record["record"].read_bytes() == after_first
    assert _commit_count(committed_record["repo"]) == commits_after_first


def test_recording_an_identical_dimension_disposition_writes_nothing(
    committed_record: dict[str, Any],
) -> None:
    _fold_dimension()
    after_first = committed_record["record"].read_bytes()
    commits_after_first = _commit_count(committed_record["repo"])
    assert commits_after_first == committed_record["commits"] + 1

    _fold_dimension()

    assert committed_record["record"].read_bytes() == after_first
    assert _commit_count(committed_record["repo"]) == commits_after_first


def test_a_second_sweep_folds_a_round_once(committed_record: dict[str, Any]) -> None:
    first = _fold_findings_into_round(
        PROJECT, REVIEWED_RUN, head=HEAD, node_id=NODE, dimensions=(DIMENSION,)
    )
    assert first == [0, 1]
    after_first = committed_record["record"].read_bytes()
    commits_after_first = _commit_count(committed_record["repo"])

    _fold_findings_into_round(
        PROJECT, REVIEWED_RUN, head=HEAD, node_id=NODE, dimensions=(DIMENSION,)
    )

    assert committed_record["record"].read_bytes() == after_first
    assert _commit_count(committed_record["repo"]) == commits_after_first


def test_a_changed_node_is_recorded_and_committed(
    committed_record: dict[str, Any],
) -> None:
    _fold_finding()
    after_first = committed_record["record"].read_bytes()
    commits_after_first = _commit_count(committed_record["repo"])

    _fold_finding(OTHER_NODE)

    assert committed_record["record"].read_bytes() != after_first
    assert _commit_count(committed_record["repo"]) == commits_after_first + 1
    recorded = _stored_answers()[review_module.FINDING_DISPOSITIONS_KEY]["0"]
    assert recorded["node"] == OTHER_NODE