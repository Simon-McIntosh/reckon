"""A promoted run's review resolves its run over the durable records.

A review node id minted from a run's node id is the only identity its dispatch
and its later readers share. While the run is in flight a live pointer carries
that node id, so the id resolves. Once the run is promoted the pointer is gone,
and a reader that matched live pointers alone handed the grant a bare node id,
which names no run directory — the record directory was refused and the
obligation's own command could not be dispatched, the refusal recorded against
power_convertor's review of a promoted run.

These cases write durable run rows into a temporary store and leave the fleet
empty, so every resolution below runs on the fallback. They assert that the
head suffix the node id carries selects exactly one of a node's runs, that an
ambiguous or absent match still returns the bare source, that a live pointer
still wins, and that an unreadable store is answered as it was before the
fallback existed.

Every store and crew home read here resolves under the test's temporary config
home; the real ones are read only to prove nothing this test minted landed in
them.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from reckon import run_store
from reckon.crew import runs
from tests.real_home_traces import traces_naming

delivery = importlib.import_module("reckon.crew.recovery_review_delivery")
dispatch_admission = importlib.import_module("reckon.crew.dispatch_admission")

PROJECT = "here"
OTHER_PROJECT = "other"

# The run-review node-id prefix, spelled here rather than imported so the test
# pins the identity grammar rather than whatever a module happens to export.
REVIEW_NODE_PREFIX = "review-of-"

# Heads are full object ids so the records that name them are read as commit
# citations rather than abbreviations needing a repository to resolve.
H_SOLO = "a" * 40
H_TWICE_OLD = "b" * 40
H_TWICE_NEW = "c" * 40
H_SAME = "d" * 40
H_FAR = "e" * 40
H_RAISE = "f" * 40

OLD_AT = "2026-10-18T00:00:00Z"
NEW_AT = "2026-10-19T00:00:00Z"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary config home and run store; both the real ones stay unread."""
    config = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(config))
    monkeypatch.setenv("RECKON_RUN_STORE", str(config / "crew" / "run_store.db"))
    return config


def _real_config_home() -> Path:
    xdg = Path.home() / ".config" / "reckon"
    return xdg if xdg.exists() else Path.home() / "docs-server"


def _real_crew_home() -> Path:
    return _real_config_home() / "crew"


def _review_node(node: str, head: str) -> str:
    """The node id a review of ``node`` at ``head`` is minted with."""
    return f"{REVIEW_NODE_PREFIX}{node}-{head[:12]}"


def _manifest(tmp_path: Path, run_id: str, head: str) -> Path:
    """A completed manifest whose last commit is ``head``.

    A promoted run's worktree has been reclaimed by the time its review is
    dispatched, so this is the head the run's own record supplies: the last
    revision its manifest claims.
    """
    path = tmp_path / "reclaimed" / f"{run_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"status": "complete", "commits": [head]}), encoding="utf-8"
    )
    return path


def _promoted_run(
    tmp_path: Path,
    *,
    run_id: str,
    node: str,
    head: str,
    project: str = PROJECT,
    completed_at: str = OLD_AT,
) -> dict:
    """Write one promoted run's durable row and the record directory it owns.

    The row names a worktree that is no longer on disk, which is what a
    promoted run looks like, so the head a review of it reads comes from the
    manifest beside it rather than from a tree.
    """
    record = {
        "run_id": run_id,
        "project": project,
        "node": node,
        "head": head,
        "role": "implement",
        "member": "worker-fixture",
        "worktree": str(tmp_path / "reclaimed" / f"{run_id}-worktree"),
        "manifest_path": str(_manifest(tmp_path, run_id, head)),
        "repo": str(tmp_path / "repo"),
        "completed_at": completed_at,
    }
    run_store.append(project, record)
    (runs.run_dir(run_id) / "review-records").mkdir(parents=True, exist_ok=True)
    return record


def _live_pointer(run_id: str, node: str, project: str = PROJECT) -> None:
    directory = runs.live_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{run_id}.json").write_text(
        json.dumps({"run_id": run_id, "project": project, "node": {"id": node}}),
        encoding="utf-8",
    )


def _declared_record_path(run_id: str) -> str:
    return str(runs.run_dir(run_id) / "review-records" / f"{run_id}.json")


# ── One run, its head suffix ───────────────────────────────────────────────


def test_one_promoted_run_resolves_by_its_head_suffix(home: Path, tmp_path: Path):
    _promoted_run(tmp_path, run_id="r-solo", node="solo-node", head=H_SOLO)
    node_id = _review_node("solo-node", H_SOLO)

    resolved = dispatch_admission._reviewed_run_id_for_node(node_id)

    assert resolved == "r-solo"
    grant = dispatch_admission._review_record_directory_grant(
        _declared_record_path("r-solo"), reviewed_run_id=resolved
    )
    assert grant == (runs.run_dir("r-solo") / "review-records").resolve()


def test_two_runs_sharing_a_node_resolve_by_their_own_suffixes(
    home: Path, tmp_path: Path
):
    _promoted_run(
        tmp_path,
        run_id="r-twice-old",
        node="twice-node",
        head=H_TWICE_OLD,
        completed_at=OLD_AT,
    )
    _promoted_run(
        tmp_path,
        run_id="r-twice-new",
        node="twice-node",
        head=H_TWICE_NEW,
        completed_at=NEW_AT,
    )

    older = _review_node("twice-node", H_TWICE_OLD)
    newer = _review_node("twice-node", H_TWICE_NEW)

    assert (
        delivery._resolved_reviewed_run_id({"node": {"id": older}}, PROJECT)
        == "r-twice-old"
    )
    assert (
        delivery._resolved_reviewed_run_id({"node": {"id": newer}}, PROJECT)
        == "r-twice-new"
    )
    unmatched = _review_node("twice-node", "0" * 12)
    assert delivery._resolved_reviewed_run_id({"node": {"id": unmatched}}, PROJECT) == (
        "twice-node"
    )


def test_two_runs_at_one_head_return_the_bare_source_and_refuse_the_grant(
    home: Path, tmp_path: Path
):
    _promoted_run(tmp_path, run_id="r-same-a", node="same-head", head=H_SAME)
    _promoted_run(tmp_path, run_id="r-same-b", node="same-head", head=H_SAME)
    node_id = _review_node("same-head", H_SAME)

    resolved = delivery._resolved_reviewed_run_id({"node": {"id": node_id}}, PROJECT)

    assert resolved == "same-head"
    declared = str(runs.run_dir("same-head") / "review-records" / "r-same-a.json")
    assert (
        dispatch_admission._review_record_directory_grant(
            declared, reviewed_run_id=resolved
        )
        is None
    )


# ── The live fleet still comes first ───────────────────────────────────────


def test_a_live_pointer_still_wins(home: Path, tmp_path: Path):
    _promoted_run(tmp_path, run_id="r-solo", node="solo-node", head=H_SOLO)
    _live_pointer("r-live-solo", "solo-node")
    node_id = _review_node("solo-node", H_SOLO)

    assert (
        delivery._resolved_reviewed_run_id({"node": {"id": node_id}}, PROJECT)
        == "r-live-solo"
    )


# ── A store that cannot be read ────────────────────────────────────────────


def test_an_unreadable_store_returns_the_bare_source(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _promoted_run(tmp_path, run_id="r-raise", node="raise-node", head=H_RAISE)
    node_id = _review_node("raise-node", H_RAISE)

    def unreadable(*_args: object, **_kwargs: object) -> list[dict]:
        raise OSError("the store is not readable")

    monkeypatch.setattr(run_store, "runs_for_node", unreadable)

    assert (
        delivery._resolved_reviewed_run_id({"node": {"id": node_id}}, PROJECT)
        == "raise-node"
    )


# ── The scope the caller gave ──────────────────────────────────────────────


def test_a_run_in_another_project_is_reached_only_without_a_project(
    home: Path, tmp_path: Path
):
    _promoted_run(
        tmp_path,
        run_id="r-far",
        node="far-node",
        head=H_FAR,
        project=OTHER_PROJECT,
    )
    node_id = _review_node("far-node", H_FAR)

    assert (
        delivery._resolved_reviewed_run_id({"node": {"id": node_id}}, PROJECT)
        == "far-node"
    )
    assert (
        delivery._resolved_reviewed_run_id({"node": {"id": node_id}}, None) == "r-far"
    )


# ── Nothing lands in the real home ─────────────────────────────────────────


def test_the_real_crew_home_and_store_carry_nothing_this_test_minted(
    home: Path, tmp_path: Path
):
    _promoted_run(tmp_path, run_id="r-real-guard", node="guard-node", head=H_RAISE)
    _live_pointer("r-real-guard-live", "guard-live-node")
    run_store.append(
        PROJECT,
        {"run_id": "r-real-guard-row", "project": PROJECT, "node": "guard-node"},
    )

    assert run_store.store_path() == home / "crew" / "run_store.db"
    assert (
        delivery._resolved_reviewed_run_id(
            {"node": {"id": _review_node("guard-node", H_RAISE)}}, PROJECT
        )
        == "r-real-guard"
    )

    # The real store is one file and the real pointers are one directory; the
    # needles are ids only this test mints, so a peer's own rows and pointers
    # never read as a hit and the guard cannot fail on a peer's write.
    assert (
        traces_naming(
            _real_crew_home(),
            ("run_store.db", "live"),
            ("r-real-guard", "guard-node", "guard-live-node"),
        )
        == []
    )
