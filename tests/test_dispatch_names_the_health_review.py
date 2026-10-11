"""An implementation dispatch names the code review that covers its neighbourhood.

A plan build repeats a neighbourhood a code review may already have read, so the
dispatch payload carries a ``health_review`` block: the newest stored code review
whose neighbourhood covers every one of the node's product write paths, its age
and the open findings already stored against those paths, or the compose command
when no covering review is younger than the staleness window. The block is
advisory and a node that writes no product source carries a null block.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew_dispatch_commands
from reckon.crew import dispatch_admission, plan_review
from reckon.crew import review as review_module
from tests import test_dispatch_names_its_backend as existing_backend_tests

pytest_plugins = ("tests.test_dispatch_names_its_backend",)

PROJECT = "proj"
PLAN = "plan-a"
SESSION = "session"
PRODUCT_PATH = "reckon/alpha.py"
DOC_PATH = "docs/plans/plan-a.html"
COMPOSE_COMMAND = (
    f"reckon crew review --project {PROJECT} --around {PLAN} --session {SESSION}"
)


def _arguments(repo: Path, *, node: str, write_paths: list[str]) -> list[str]:
    arguments = [
        "crew",
        "dispatch",
        "--project",
        PROJECT,
        "--plan",
        PLAN,
        "--section",
        "dispatch",
        "--role",
        "implement",
        "--spec-level",
        "exact",
        "--node",
        node,
        "--goal",
        "name the covering code review",
        "--done-when",
        "pytest reports the health review block",
        "--session",
        SESSION,
        "--repo",
        str(repo),
        "--backend",
        "alpha",
        "--dry-run",
    ]
    for path in write_paths:
        arguments.extend(["--write-path", path])
    return arguments


def _invoke(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    node: str,
    write_paths: list[str],
):
    config = copy.deepcopy(existing_backend_tests.CONFIG)
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    # The lane trajectory is not what this node reads; fix it so the payload
    # carries a readable lane and the health review block is the subject.
    existing_backend_tests._observe_window(monkeypatch)
    result = CliRunner().invoke(
        cli_module.main, _arguments(repo, node=node, write_paths=write_paths)
    )
    return existing_backend_tests._payload(result), result


def _store_covering_review(*, timestamp: str) -> dict:
    """Store a code review whose neighbourhood covers the product write path."""
    record = {
        "project": PROJECT,
        "around": PLAN,
        "neighbourhood_key": "abc123def456",
        "reviewed_revision": "0" * 40,
        "named_paths": [PRODUCT_PATH],
        "modules": [PRODUCT_PATH, "reckon/beta.py"],
        "timestamp": timestamp,
        "status": "complete",
        "findings": [],
    }
    plan_review.store_code_review(record)
    return record


def test_no_stored_review_names_the_compose_command(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload, result = _invoke(
        dispatch_repo, monkeypatch, node="no-review", write_paths=[PRODUCT_PATH]
    )

    assert result.exit_code == 0
    block = payload["health_review"]
    assert block is not None
    assert block["state"] == "missing"
    assert block["review"] is None
    assert block["compose_command"] == COMPOSE_COMMAND


def test_a_review_of_the_write_path_dated_today_is_named_and_no_command(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = _store_covering_review(timestamp=datetime.now(UTC).isoformat())
    payload, result = _invoke(
        dispatch_repo, monkeypatch, node="reviewed", write_paths=[PRODUCT_PATH]
    )

    assert result.exit_code == 0
    block = payload["health_review"]
    assert block is not None
    assert block["state"] == "covered"
    assert block["review"]["key"] == stored["neighbourhood_key"]
    assert block["review"]["reviewed_revision"] == stored["reviewed_revision"]
    assert block["age_days"] is not None and block["age_days"] < 1.0
    assert block["compose_command"] is None


def test_a_docs_only_node_carries_a_null_block(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload, result = _invoke(
        dispatch_repo, monkeypatch, node="docs-only", write_paths=[DOC_PATH]
    )

    assert result.exit_code == 0
    assert payload["health_review"] is None


def test_a_stale_review_is_named_beside_the_compose_command(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = _store_covering_review(timestamp="2020-01-01T00:00:00+00:00")
    payload, result = _invoke(
        dispatch_repo, monkeypatch, node="stale", write_paths=[PRODUCT_PATH]
    )

    assert result.exit_code == 0
    block = payload["health_review"]
    assert block["state"] == "stale"
    assert block["review"]["key"] == stored["neighbourhood_key"]
    assert block["age_days"] > 7.0
    assert block["compose_command"] == COMPOSE_COMMAND


REVIEWED_RUN = "r-20261010T090000000000-reviewed-run"
FINDINGS_REVIEW_RUN = "r-20261010T100000000000-review-run"


def _store_committed_findings(checkout: Path) -> None:
    """Commit a review whose findings against the write path are two open, one disposed.

    The findings reader serves committed records, so the count the block reports
    is read from the project's committed reviews tree rather than from the
    staging code-review store; the third finding carries an exemption, keyed by
    its index in the record, so the block must report two rather than three.
    """
    record = {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": FINDINGS_REVIEW_RUN,
        "reviewed_head_sha": "0" * 40,
        "scores": {"evidence": 17},
        "findings": [
            {
                "file": PRODUCT_PATH,
                "line": "3",
                "text": "unguarded index",
                "severity": "blocking",
            },
            {
                "file": PRODUCT_PATH,
                "line": "9",
                "text": "naming would read better",
                "severity": "follow-on",
            },
            {
                "file": PRODUCT_PATH,
                "line": "12",
                "text": "accepted risk, no action",
                "severity": "follow-on",
            },
        ],
        review_module.FINDING_DISPOSITIONS_KEY: {
            "2": {"kind": "exempted", "reason": "accepted risk"}
        },
    }
    review_module.store_committed_review(record, root=checkout)


def test_open_findings_counts_only_the_open_rows_on_the_write_path(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store_covering_review(timestamp=datetime.now(UTC).isoformat())
    _store_committed_findings(dispatch_repo)
    payload, result = _invoke(
        dispatch_repo, monkeypatch, node="counted", write_paths=[PRODUCT_PATH]
    )

    assert result.exit_code == 0
    block = payload["health_review"]
    assert block["state"] == "covered"
    # Three findings name the path; one is exempted, so two are open.
    assert block["open_findings"] == 2


def test_an_unreadable_store_is_unknown_rather_than_absent(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable store reads as unknown, never as a measured absence.

    The block is called directly here: the dispatch's own review gate reads the
    same store through a second, unguarded call site, so a store made unreadable
    for the whole process never reaches the block through a dispatch. What is
    under test is the block's own reading of an unreadable store, so the block
    is handed one.
    """

    def _unreadable(*_args, **_kwargs):
        raise OSError("review store unreadable")

    monkeypatch.setattr(plan_review, "list_code_reviews", _unreadable)
    block = dispatch_admission._dispatch_health_review(
        project=PROJECT,
        plan=PLAN,
        session=SESSION,
        node=SimpleNamespace(write_paths=[PRODUCT_PATH]),
        root=dispatch_repo,
    )

    assert block is not None
    # An unreadable store is reported as unknown, never as a measured absence
    # (which would be "missing"), and the safe advice is still the command.
    assert block["state"] == "unknown"
    assert block["review"] is None
    assert block["compose_command"] == COMPOSE_COMMAND
