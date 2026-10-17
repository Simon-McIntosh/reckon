"""A run review's delivery scope is accepted by dispatch and by its fence."""

# ruff: noqa: F811 - the imported fixture is also a test parameter.

from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest

from reckon import _backends
from reckon.crew import dispatch_admission, recovery_review_dispatch, runs
from reckon.crew.node import TaskNode
from tests.test_a_run_review_cannot_remove_a_siblings_record import (
    Store,
    _reviewed_record,
)
from tests.test_scoring_dispatches_its_review import (
    CONFIG,
    _scoring_pointer,
    isolated_project,  # noqa: F401 - fixture
)


def _review_node(write_paths: list[str]) -> TaskNode:
    return TaskNode(
        id="review-under-test",
        goal="review a delivered run",
        plan="",
        role="review",
        done_when="the review record is delivered",
        write_paths=write_paths,
        time_budget="20m",
    )


def _reachability(store: Store, paths: list[str]):
    return dispatch_admission._sandbox_reachability(
        _review_node(paths),
        backend={"sandbox": "read-only", "execution_capable": True},
        repository=store.base / "repo",
        run_directory=store.manifest.parent,
    )


def _separate_sandbox_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox_temp = tmp_path / "sandbox-temp"
    sandbox_temp.mkdir()
    monkeypatch.setattr(_backends.tempfile, "gettempdir", lambda: str(sandbox_temp))


def test_composed_run_review_records_pass_scope_and_match_the_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    store.point_the_stores(monkeypatch)
    _separate_sandbox_temp(tmp_path, monkeypatch)
    if os.environ.get("RECKON_TEST_REVIEW_RECORD_ADMISSION_NEGATIVE") == "1":
        monkeypatch.setattr(
            dispatch_admission, "_review_record_directory_grant", lambda declared: None
        )
    paths = recovery_review_dispatch._review_dispatch_fields(
        _reviewed_record(), delta=None
    )["write_paths"]
    roots, findings = _reachability(store, paths)
    assert not [item for item in findings if item["property"] == "scoped"], findings
    assert roots is not None
    assert store.record_directory().resolve() in roots
    fence = set(store.fence_roots(paths))
    assert {
        root for root in fence if root.is_relative_to(store.reviewed_run_directory())
    } == {store.record_directory().resolve()}
    assert store.project_directory.resolve() not in fence


def test_review_cannot_declare_a_different_runs_nonrecord_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    store.point_the_stores(monkeypatch)
    _separate_sandbox_temp(tmp_path, monkeypatch)
    forbidden = store.reviewed_run_directory() / "manifest.md"
    _, findings = _reachability(store, [str(forbidden)])
    assert [item for item in findings if item["property"] == "scoped"], findings


def test_reflex_launches_a_delivered_runs_review_on_a_read_only_backend(
    isolated_project: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _separate_sandbox_temp(tmp_path, monkeypatch)
    config_home, repo = isolated_project
    record = _scoring_pointer(config_home, repo, "r-review-subject")
    config = copy.deepcopy(CONFIG)
    config["backends"]["alpha"].update(
        {"sandbox": "read-only", "execution_capable": True}
    )
    with runs.follower_claim("sample", "session-orchestrating", delivery="stream"):
        report = recovery_review_dispatch.dispatch_review_for_run(
            record, config=config, launcher=lambda *args, **kwargs: os.getpid()
        )
    assert report["dispatched"] is True, report
    assert report["review_run_id"]
