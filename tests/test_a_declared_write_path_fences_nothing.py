"""A declared write path is a hint, and fences nothing a merge can reconcile.

Work happens in isolated worktrees and git reconciles the edits, so the paths a
node declares tell a reviewer what it intends; they do not decide what it may
write. Three surfaces used to disagree with that, and each case here enters the
project's real entry:

* dispatch refused a second node naming the same source file whenever the
  project's own sharing declaration did not name the region that file was in,
  so a plan's width could not be built;
* the write-time manifest audit reported a path beside the declared one as
  "changed paths outside the write scope";
* promotion refused the same change until a coordinator named the companion
  path with ``--accept-path``.

What still fences is exactly what git cannot reconcile - a plan document,
generated state, a cumulative evidence record, a figure topic directory and a
dependency lock - and the last case shows that refusal still standing, naming
the pattern, because a rule relaxed everywhere is a rule that was never there.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import routing, runs
from reckon.crew.reports import audit_manifest
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git
from tests.test_a_queued_run_is_admitted import (
    CONFIG,
    PROJECT,
    admit_repo,  # noqa: F401 - imported fixture
)
from tests.test_a_source_file_is_shared_by_default import (
    harness_handshake,  # noqa: F401 - imported fixture
)

SOURCE = "notes.txt"
FIGURE_TOPIC = "docs/figures/topic-x"
DECLARED_FILE = "a.py"
UNDECLARED_FILE = "b.py"


def _declare(repository: Path, *, roots: bool = True) -> None:
    """Publish a sharing declaration, by default naming an unrelated root."""
    declaration: dict[str, object] = {
        "shared_roots": ["pkg/"] if roots else [],
    }
    path = repository / "docs" / "state" / PROJECT / "shared-write-paths.json"
    path.write_text(json.dumps(declaration), encoding="utf-8")


@pytest.fixture()
def repository(admit_repo: Path) -> Path:  # noqa: F811 - imported fixture
    """The dispatch fixture's checkout, with one source file committed."""
    (admit_repo / SOURCE).write_text("one\n", encoding="utf-8")
    git(admit_repo, "add", "--", SOURCE)
    git(admit_repo, "commit", "-q", "-m", "chore: seed a source file")
    _declare(admit_repo)
    return admit_repo


def _node(node_id: str, path: str) -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        plan="plan-a",
        section="dispatch",
        goal="edit one declared path",
        done_when="the claim check passes",
        write_paths=[path],
        manifest_path=str(runs.run_dir(f"r-{node_id}") / "manifest.md"),
        spec_level="exact",
        time_budget="20m",
    )


def _dispatch(repository: Path, node_id: str, path: str) -> dict:
    return crew.dispatch(
        node=_node(node_id, path),
        project=PROJECT,
        repo=repository,
        config=CONFIG,
        session=f"session-{node_id}",
        check_budget=False,
        launcher=lambda *_args, **_kwargs: os.getpid(),
    )


def test_two_nodes_declaring_one_source_file_both_run(repository: Path) -> None:
    """A declaration naming another root no longer serializes a source file."""

    owner = _dispatch(repository, "owner", SOURCE)
    joiner = _dispatch(repository, "joiner", SOURCE)

    assert owner["run_id"] != joiner["run_id"]
    claimants = {
        claim["node"]
        for claim in crew.scope_claims(PROJECT, repository)
        if claim["path"] == SOURCE
    }
    assert claimants == {"owner", "joiner"}


def test_the_manifest_audit_accepts_an_undeclared_source_path(repository: Path) -> None:
    """A companion source file is not a scope finding; the run reports it."""

    text = "\n".join(
        [
            "status: complete",
            "changed_paths:",
            f"  - {DECLARED_FILE}",
            f"  - {UNDECLARED_FILE}",
        ]
    )

    audit = audit_manifest(
        text,
        _node("report", DECLARED_FILE),
        worktree=repository,
        repository=repository,
        project=PROJECT,
    )

    assert not [row for row in audit["findings"] if UNDECLARED_FILE in row], audit


def test_the_manifest_audit_still_reports_a_merge_unsafe_stray(
    repository: Path,
) -> None:
    """The other half: a path git cannot reconcile is still a finding."""

    text = "\n".join(
        [
            "status: complete",
            "changed_paths:",
            f"  - {DECLARED_FILE}",
            "  - docs/plans/plan-a.html",
        ]
    )

    audit = audit_manifest(
        text,
        _node("report", DECLARED_FILE),
        worktree=repository,
        repository=repository,
        project=PROJECT,
    )

    assert [row for row in audit["findings"] if "docs/plans/plan-a.html" in row], audit


def _promotion_pointer(
    repository: Path, run_tree: Path, run_id: str, base: str
) -> None:
    """A live pointer whose node declared one file and delivers two."""
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(run_tree),
        "base_sha": base,
        "launch": "in-harness",
        "role": "implement",
        "backend": "native",
        "created_at": "2026-08-26T12:00:00Z",
        "node": {
            "id": "declared-path",
            "plan": "plan-a",
            "section": "plan-a#dispatch",
            "time_budget": "25m",
            "write_paths": [DECLARED_FILE],
        },
        "repository_tree_snapshot": routing._repository_tree_snapshot(repository),
    }
    _write_json(pointer_path(run_id), pointer)


def test_a_node_promotes_a_file_it_did_not_declare_with_no_acceptance(
    repository: Path, tmp_path: Path
) -> None:
    """A companion source file lands on its own; no --accept-path is asked."""

    base = git(repository, "rev-parse", "HEAD")
    run_tree = tmp_path / "run-tree"
    git(repository, "worktree", "add", "-q", "--detach", str(run_tree), "HEAD")
    run_id = "r-undeclared-companion"
    _promotion_pointer(repository, run_tree, run_id, base)
    (run_tree / DECLARED_FILE).write_text("declared\n", encoding="utf-8")
    (run_tree / UNDECLARED_FILE).write_text("companion\n", encoding="utf-8")
    git(run_tree, "add", "--", DECLARED_FILE, UNDECLARED_FILE)
    git(run_tree, "commit", "-q", "-m", "test: deliver a companion file")
    commit = git(run_tree, "rev-parse", "HEAD")

    stored = crew.complete(run_id, gate="passed", commits=[commit], root=repository)[
        "record"
    ]

    assert stored["commits"] == [commit]
    assert not stored.get("scope_acceptances")
    git(repository, "worktree", "remove", "--force", str(run_tree))


def test_a_figure_topic_directory_a_live_peer_claims_is_still_refused(
    repository: Path,
) -> None:
    """A figure topic is replaced wholesale, so the claim stands."""

    _dispatch(repository, "owner", FIGURE_TOPIC)

    with pytest.raises(crew.ScopeConflict) as refusal:
        _dispatch(repository, "joiner", FIGURE_TOPIC)

    assert "docs/figures/" in str(refusal.value), refusal.value
