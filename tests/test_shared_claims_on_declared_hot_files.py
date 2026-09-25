"""A project's shared-write declaration admits co-claimants of one file only.

Four cases run on a synthesized repository whose list names ``pkg/hot.py``:
two nodes claiming that file both pass the live check; a node claiming an
unlisted file is refused against a live claim on it; a directory claim
overlapping the shared file is refused; and with the list absent the shared
file is refused. A fifth case shows both co-claimants remain visible in the
read model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import crew

PROJECT = "shared-proj"
SHARED_FILE = "pkg/hot.py"
UNLISTED_FILE = "pkg/cold.py"
DIRECTORY = "pkg"
REASON = "concurrent editors work in separate functions"


def _repository(root: Path) -> Path:
    repository = root / "repository"
    (repository / DIRECTORY).mkdir(parents=True)
    (repository / SHARED_FILE).write_text("# shared hot module\n")
    (repository / UNLISTED_FILE).write_text("# unlisted module\n")
    return repository


def _declare(repository: Path, paths: list[dict[str, str]]) -> None:
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True, exist_ok=True)
    (state / "shared-write-paths.json").write_text(
        json.dumps({"project": PROJECT, "paths": paths})
    )


def _node(node_id: str, *write_paths: str) -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id, goal="edit one region", plan="plan", write_paths=list(write_paths)
    )


def _claim(run_id: str, node_id: str, path: str) -> crew._LiveScopeClaim:
    return crew._LiveScopeClaim(
        run_id=run_id, node_id=node_id, path=path, declared_path=path
    )


def test_a_declared_shared_file_admits_two_live_claimants(tmp_path: Path) -> None:
    """Case one: both co-claimants of the declared file pass the check."""
    repository = _repository(tmp_path)
    _declare(repository, [{"path": SHARED_FILE, "reason": REASON}])

    # A live claimant holds the file, and the second node claiming exactly that
    # file is admitted rather than refused.
    crew._raise_live_scope_conflict(
        _node("joiner", SHARED_FILE),
        [_claim("r-owner", "owner", SHARED_FILE)],
        repository,
        project=PROJECT,
    )
    # The reverse direction holds too: the first claimant is not displaced.
    crew._raise_live_scope_conflict(
        _node("owner", SHARED_FILE),
        [_claim("r-joiner", "joiner", SHARED_FILE)],
        repository,
        project=PROJECT,
    )


def test_an_unlisted_file_is_refused_against_a_live_claim(tmp_path: Path) -> None:
    """Case two: a path not named in the list keeps the whole-file refusal."""
    repository = _repository(tmp_path)
    _declare(repository, [{"path": SHARED_FILE, "reason": REASON}])

    with pytest.raises(crew.ScopeConflict):
        crew._raise_live_scope_conflict(
            _node("joiner", UNLISTED_FILE),
            [_claim("r-joiner", "joiner", UNLISTED_FILE)],
            repository,
            project=PROJECT,
        )


def test_a_directory_claim_containing_a_shared_file_is_refused(
    tmp_path: Path,
) -> None:
    """Case three: only the named file is shareable, not the directory."""
    repository = _repository(tmp_path)
    _declare(repository, [{"path": SHARED_FILE, "reason": REASON}])

    # A candidate directory that merely contains the shared file is not the
    # named file, so the live claim on the file still refuses it.
    with pytest.raises(crew.ScopeConflict):
        crew._raise_live_scope_conflict(
            _node("joiner", DIRECTORY),
            [_claim("r-owner", "owner", SHARED_FILE)],
            repository,
            project=PROJECT,
        )
    # A live directory claim is not the named file either, so the node that
    # claims the shared file is refused against it.
    with pytest.raises(crew.ScopeConflict):
        crew._raise_live_scope_conflict(
            _node("joiner", SHARED_FILE),
            [_claim("r-owner", "owner", DIRECTORY)],
            repository,
            project=PROJECT,
        )


def test_a_shared_file_is_refused_when_no_list_is_present(tmp_path: Path) -> None:
    """Case four: an absent list leaves the whole-file refusal unchanged."""
    repository = _repository(tmp_path)

    with pytest.raises(crew.ScopeConflict):
        crew._raise_live_scope_conflict(
            _node("joiner", SHARED_FILE),
            [_claim("r-owner", "owner", SHARED_FILE)],
            repository,
            project=PROJECT,
        )


def test_both_co_claimants_stay_visible_in_the_read_model(tmp_path: Path) -> None:
    """Case five: the permitted claims remain, so scope_claims lists both."""
    repository = _repository(tmp_path)
    _declare(repository, [{"path": SHARED_FILE, "reason": REASON}])
    for run_id, node_id in (("r-owner", "owner"), ("r-joiner", "joiner")):
        crew._write_json(
            crew.pointer_path(run_id),
            {
                "run_id": run_id,
                "project": PROJECT,
                "repo": str(repository),
                "node": {"id": node_id, "plan": "plan", "write_paths": [SHARED_FILE]},
                "phase": "working",
                "process_alive": False,
            },
        )

    shared_claimants = sorted(
        claim["node"]
        for claim in crew.scope_claims(PROJECT, repository)
        if claim["path"] == SHARED_FILE
    )
    assert shared_claimants == ["joiner", "owner"]
