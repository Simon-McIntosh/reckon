"""Shared roots admit file co-claims while exclusive paths keep their guard."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import queue_admission, queue_edit, runs
from tests.helpers.git import git
from tests.test_a_queued_run_is_admitted import (
    CONFIG,
    PROJECT,
    _config_for,
    _lane_document,
    _queue,
    admit_repo,  # noqa: F401 - imported fixture
)

SOURCE = "pkg/source.py"
EXCLUSIVE = "docs/plans/*.html"


@pytest.fixture()
def repository(admit_repo: Path) -> Path:  # noqa: F811 - imported fixture
    (admit_repo / "pkg").mkdir()
    (admit_repo / SOURCE).write_text("def value():\n    return 1\n")
    (admit_repo / "docs/plans/x.html").write_text("<h2>Another plan</h2>\n")
    git(admit_repo, "add", "--", SOURCE, "docs/plans/x.html")
    git(
        admit_repo,
        "commit",
        "-q",
        "-m",
        "test: seed claimed files",
        "-m",
        "Provide file and directory claims in the temporary repository.",
    )
    _declare(admit_repo)
    return admit_repo


def _declare(repo: Path, *, roots: bool = True, paths: tuple[str, ...] = ()) -> None:
    declaration = {"paths": list(paths), "exclusive_patterns": [EXCLUSIVE]}
    if roots:
        declaration["shared_roots"] = ["pkg/"]
    (repo / "docs/state" / PROJECT / "shared-write-paths.json").write_text(
        json.dumps(declaration)
    )


def _node(node_id: str, path: str) -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        plan="plan-a",
        section="dispatch",
        goal="edit one declared file",
        done_when="the shared claim check passes",
        write_paths=[path],
        spec_level="exact",
        time_budget="20m",
    )


def _dispatch(repo: Path, node_id: str, path: str) -> dict:
    return crew.dispatch(
        node=_node(node_id, path),
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session=f"session-{node_id}",
        check_budget=False,
        launcher=lambda *_args, **_kwargs: os.getpid(),
    )


def test_two_dispatches_share_a_file_under_a_declared_root(repository: Path) -> None:
    owner = _dispatch(repository, "owner", SOURCE)
    joiner = _dispatch(repository, "joiner", SOURCE)

    assert owner["run_id"] != joiner["run_id"]
    claimants = {
        claim["node"]
        for claim in crew.scope_claims(PROJECT, repository)
        if claim["path"] == SOURCE
    }
    assert claimants == {"owner", "joiner"}


def test_a_queued_file_claim_is_admitted(repository: Path) -> None:
    _dispatch(repository, "owner", SOURCE)
    config, resolve = _config_for()
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(repository.parent, worker_slots=2)
    )
    request = _queue(repository, node_id="joiner", run_id="r-joiner")
    request["node"]["write_paths"] = [SOURCE]
    runs.queue_dispatch(request)

    result = queue_admission.admit_once(
        config_for=resolve, launcher=lambda *_args, **_kwargs: os.getpid()
    )

    row = next(row for row in result["verdicts"] if row["run_id"] == "r-joiner")
    assert row["verdict"] == "launched", row
    assert result["launched"] == ["r-joiner"]
    assert runs.read_pointer("r-joiner")["phase"] != "queued"


def test_an_exclusive_pattern_is_named_in_the_refusal(repository: Path) -> None:
    # An exclusive pattern takes precedence even over an explicit shared file.
    _declare(repository, paths=("docs/plans/x.html",))
    owner = _dispatch(repository, "owner", "docs/plans/x.html")

    with pytest.raises(crew.ScopeConflict) as refusal:
        _dispatch(repository, "joiner", "docs/plans/x.html")

    assert refusal.value.run_id == owner["run_id"]
    assert EXCLUSIVE in str(refusal.value)


@pytest.mark.parametrize(
    "owner_path,joiner_path", [(SOURCE, "pkg"), ("pkg", SOURCE), ("pkg", "pkg")]
)
def test_directory_claims_share_their_files_under_a_shared_root(
    repository: Path, owner_path: str, joiner_path: str
) -> None:
    owner = _dispatch(repository, "owner", owner_path)
    joiner = _dispatch(repository, "joiner", joiner_path)

    assert owner["run_id"] != joiner["run_id"]


def test_a_project_without_shared_roots_keeps_its_default(repository: Path) -> None:
    _declare(repository, roots=False)
    owner = _dispatch(repository, "owner", SOURCE)

    with pytest.raises(crew.ScopeConflict) as refusal:
        _dispatch(repository, "joiner", SOURCE)

    assert refusal.value.run_id == owner["run_id"]


def test_explicit_shared_paths_still_work_without_roots(repository: Path) -> None:
    _declare(repository, roots=False, paths=(SOURCE,))
    _dispatch(repository, "owner", SOURCE)
    assert _dispatch(repository, "joiner", SOURCE)["run_id"]


def test_queue_edits_keep_a_directory_claim_exclusive(repository: Path) -> None:
    owner = _dispatch(repository, "owner", "pkg")
    request = _queue(repository, node_id="joiner", run_id="r-joiner")

    refusal = queue_edit._live_claim_conflict(_node("joiner", SOURCE), request)

    assert refusal is not None
    assert refusal.run_id == owner["run_id"]


def test_admission_admits_a_file_under_a_directory_claim(repository: Path) -> None:
    _dispatch(repository, "owner", "pkg")
    request = _queue(repository, node_id="joiner", run_id="r-joiner")
    request["node"]["write_paths"] = [SOURCE]

    hold = queue_admission._live_claim_hold(
        request,
        runs.list_live(project=PROJECT),
        dispositions={},
        shared_files=runs._shared_write_paths(PROJECT, repository),
    )

    assert hold is None, hold


def test_queue_edits_admit_an_exact_shared_file_claim(repository: Path) -> None:
    _dispatch(repository, "owner", SOURCE)
    request = _queue(repository, node_id="joiner", run_id="r-joiner")

    assert queue_edit._live_claim_conflict(_node("joiner", SOURCE), request) is None


def test_exclusive_patterns_are_reported_by_both_queue_checks(repository: Path) -> None:
    path = "docs/plans/x.html"
    _dispatch(repository, "owner", path)
    request = _queue(repository, node_id="joiner", run_id="r-joiner")
    request["node"]["write_paths"] = [path]
    shared = runs._shared_write_paths(PROJECT, repository)

    hold = queue_admission._live_claim_hold(
        request, runs.list_live(project=PROJECT), dispositions={}, shared_files=shared
    )
    refusal = queue_edit._live_claim_conflict(_node("joiner", path), request)

    assert hold is not None and EXCLUSIVE in hold
    assert refusal is not None and EXCLUSIVE in str(refusal)


def test_root_membership_respects_file_and_directory_boundaries(
    repository: Path,
) -> None:
    (repository / "pkg/extensionless").write_text("file\n")
    (repository / "pkg/directory.py").mkdir()
    shared = runs._shared_write_paths(PROJECT, repository)

    assert SOURCE in shared
    assert "pkg/extensionless" in shared
    assert "pkg/future.py" in shared
    # A directory under a shared root shares as the files beneath it do, so a
    # claim on it admits a peer; an absent suffixless path is still read as a
    # directory claim rather than a file, and is not itself a shared file.
    assert "pkg" in shared
    assert "pkg/directory.py" in shared
    assert "pkg/future-directory" not in shared
    assert "pkg-other/source.py" not in shared
    assert "../outside.py" not in shared


def test_an_exclusive_pattern_overrides_a_shared_root(repository: Path) -> None:
    declaration = repository / "docs/state" / PROJECT / "shared-write-paths.json"
    declaration.write_text(
        json.dumps({"shared_roots": ["pkg/"], "exclusive_patterns": ["pkg/private/"]})
    )
    shared = runs._shared_write_paths(PROJECT, repository)

    assert SOURCE in shared
    assert "pkg/private/source.py" not in shared
    assert (
        runs._exclusive_write_pattern(shared, "pkg/private/source.py") == "pkg/private/"
    )
