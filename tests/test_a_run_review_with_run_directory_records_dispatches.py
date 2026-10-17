"""A run review's delivery scope is accepted by dispatch and by its fence."""

# ruff: noqa: F811 - the imported fixture is also a test parameter.

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from reckon import _backends
from reckon.crew import (
    dispatch_admission,
    recovery_review_dispatch,
    runs,
)
from reckon.crew import (
    review as review_module,
)
from reckon.crew.node import TaskNode
from tests.test_a_run_review_cannot_remove_a_siblings_record import (
    REVIEWED_ID,
    SIBLING_ID,
    Store,
    _reviewed_record,
)
from tests.test_scoring_dispatches_its_review import (
    CONFIG,
    _scoring_pointer,
    isolated_project,  # noqa: F401 - fixture
)

# Set to reproduce the widened grant the node replaces: with it, a review node
# naming run A that declares run B's record path is admitted rather than refused.
RUN_NEGATIVE = os.environ.get("RECKON_TEST_REVIEW_RECORD_RUN_NEGATIVE") == "1"


def _any_runs_record_directory_grant(
    declared: str, *, reviewed_run_id: str
) -> Path | None:
    """The pre-change grant: any existing run's ``review-records/`` is admitted."""
    raw = Path(declared).expanduser()
    if not raw.is_absolute():
        return None
    path = raw.resolve()
    try:
        within_runs = path.relative_to(runs.runs_dir().resolve())
    except ValueError:
        return None
    if (
        len(within_runs.parts) != 3
        or within_runs.parts[1] != review_module.RUN_REVIEW_RECORD_DIRNAME
        or not path.parent.is_dir()
    ):
        return None
    return path.parent


def _review_node(write_paths: list[str], *, node_id: str) -> TaskNode:
    return TaskNode(
        id=node_id,
        goal="review a delivered run",
        plan="",
        role="review",
        done_when="the review record is delivered",
        write_paths=write_paths,
        time_budget="20m",
    )


def _reviewed_run_node_id() -> str:
    """The node id a review of the reviewed run is minted with, from production.

    The composer keys a review's node id on the source it read, so a reviewed
    run whose node id is its own run id mints ``review-of-<run id>``. Reading it
    from the composer rather than writing it here is what keeps this check on the
    same spelling a dispatched re-review carries.
    """
    record = {
        "run_id": REVIEWED_ID,
        "project": "reckon",
        "node": {"id": REVIEWED_ID, "time_budget": "20m"},
    }
    return str(
        recovery_review_dispatch._review_dispatch_fields(record, delta=None)["node_id"]
    )


# A reviewed run whose node id is not its run id, which is the ordinary shape of
# a real run: its review's id then carries the node id, and the reviewed run is
# recoverable only through the live fleet.
SEMANTIC_NODE_ID = "review-records-admission-subject"


def _composed_semantic_fields() -> dict:
    """A review's dispatch fields composed from a node-id (not run-id) source."""
    record = {
        "run_id": REVIEWED_ID,
        "project": "reckon",
        "node": {"id": SEMANTIC_NODE_ID, "time_budget": "20m"},
    }
    return recovery_review_dispatch._review_dispatch_fields(record, delta=None)


def _seed_live_pointer(store: Store, run_id: str, node_id: str) -> None:
    """Publish one live pointer into the isolated crew home's live directory."""
    path = runs.pointer_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"run_id": run_id, "project": "reckon", "node": {"id": node_id}})
    )


def _reachability(store: Store, node: TaskNode):
    return dispatch_admission._sandbox_reachability(
        node,
        backend={"sandbox": "read-only", "execution_capable": True},
        repository=store.base / "repo",
        run_directory=store.manifest.parent,
    )


def _scoped(findings: list[dict[str, str]]) -> list[dict[str, str]]:
    return [item for item in findings if item["property"] == "scoped"]


def _seed_other_run_record(store: Store) -> Path:
    """Bring run B's ``review-records/`` directory into being and name a record."""
    directory = runs.run_dir(SIBLING_ID) / review_module.RUN_REVIEW_RECORD_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{SIBLING_ID}.json"


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
            dispatch_admission,
            "_review_record_directory_grant",
            lambda declared, *, reviewed_run_id: None,
        )
    fields = recovery_review_dispatch._review_dispatch_fields(
        _reviewed_record(), delta=None
    )
    paths = fields["write_paths"]
    node = _review_node(paths, node_id=_reviewed_run_node_id())
    roots, findings = _reachability(store, node)
    assert not _scoped(findings), findings
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
    node = _review_node([str(forbidden)], node_id=_reviewed_run_node_id())
    _, findings = _reachability(store, node)
    assert _scoped(findings), findings


def test_review_cannot_declare_another_runs_record_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record path in another run's record directory is refused as scoped."""
    store = Store(tmp_path)
    store.point_the_stores(monkeypatch)
    _separate_sandbox_temp(tmp_path, monkeypatch)
    if RUN_NEGATIVE:
        monkeypatch.setattr(
            dispatch_admission,
            "_review_record_directory_grant",
            _any_runs_record_directory_grant,
        )
    # Run A's own record directory exists, so the refusal below is the identity
    # check holding rather than the directory being absent.
    store.record_directory().mkdir(parents=True, exist_ok=True)
    foreign = _seed_other_run_record(store)
    node = _review_node([str(foreign)], node_id=_reviewed_run_node_id())
    _, findings = _reachability(store, node)
    assert _scoped(findings), findings
    assert all(node.id in item["detail"] for item in _scoped(findings)), findings


def test_review_admits_its_own_runs_record_directory_and_fence_grants_it_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewed run's record path is admitted; the fence grants only it."""
    store = Store(tmp_path)
    store.point_the_stores(monkeypatch)
    _separate_sandbox_temp(tmp_path, monkeypatch)
    _seed_other_run_record(store)
    paths = review_module.run_review_record_paths(
        store.reviewed_run_directory(), REVIEWED_ID
    )
    store.record_directory().mkdir(parents=True, exist_ok=True)
    node = _review_node(paths, node_id=_reviewed_run_node_id())
    roots, findings = _reachability(store, node)
    assert not _scoped(findings), findings
    assert roots is not None
    assert store.record_directory().resolve() in roots
    fence = set(store.fence_roots(paths))
    assert {
        root for root in fence if root.is_relative_to(store.reviewed_run_directory())
    } == {store.record_directory().resolve()}
    assert store.project_directory.resolve() not in fence


def test_reflex_composed_review_resolves_its_run_through_the_live_fleet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A review minted from a node-id source is admitted via the fleet lookup."""
    store = Store(tmp_path)
    store.point_the_stores(monkeypatch)
    _separate_sandbox_temp(tmp_path, monkeypatch)
    fields = _composed_semantic_fields()
    paths = fields["write_paths"]
    _seed_live_pointer(store, REVIEWED_ID, SEMANTIC_NODE_ID)
    node = _review_node(paths, node_id=fields["node_id"])
    roots, findings = _reachability(store, node)
    assert not _scoped(findings), findings
    assert roots is not None
    assert store.record_directory().resolve() in roots
    fence = set(store.fence_roots(paths))
    assert {
        root for root in fence if root.is_relative_to(store.reviewed_run_directory())
    } == {store.record_directory().resolve()}
    assert store.project_directory.resolve() not in fence


def test_reflex_composed_review_with_no_live_pointer_is_refused_as_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node-id source naming no live run is refused, naming the node id."""
    store = Store(tmp_path)
    store.point_the_stores(monkeypatch)
    _separate_sandbox_temp(tmp_path, monkeypatch)
    fields = _composed_semantic_fields()
    node = _review_node(fields["write_paths"], node_id=fields["node_id"])
    _, findings = _reachability(store, node)
    scoped = _scoped(findings)
    assert scoped, findings
    assert all(node.id in item["detail"] for item in scoped), findings


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
