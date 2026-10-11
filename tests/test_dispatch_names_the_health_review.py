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

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew_dispatch_commands
from reckon.crew import plan_review
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
