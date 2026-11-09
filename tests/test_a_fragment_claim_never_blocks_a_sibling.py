"""Fragment-directory claims compare the owning node's concrete files."""

from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import runs
from tests.helpers.git import git
from tests.test_a_queued_run_is_admitted import (
    PROJECT,
    admit_repo,  # noqa: F401 - imported fixture
)
from tests.test_a_source_file_is_shared_by_default import _dispatch, _node

FRAGMENTS = "docs/evidence/fragments/plan-a"


@pytest.mark.parametrize("joiner_path", [FRAGMENTS, f"{FRAGMENTS}/joiner.html"])
def test_a_directory_claim_admits_a_sibling_fragment(
    admit_repo: Path,  # noqa: F811 - imported fixture
    joiner_path: str,
) -> None:
    owner = _dispatch(admit_repo, "owner", FRAGMENTS)
    claims = runs._live_scope_claims(PROJECT, admit_repo)
    assert any(
        claim.run_id == owner["run_id"] and claim.path == FRAGMENTS for claim in claims
    )

    runs._raise_live_scope_conflict(
        _node("joiner", joiner_path), claims, admit_repo, project=PROJECT
    )
    joiner = _dispatch(admit_repo, "joiner", joiner_path)
    assert owner["run_id"] != joiner["run_id"]


@pytest.mark.parametrize("owner_path", [FRAGMENTS, f"{FRAGMENTS}/owner.html"])
def test_the_same_fragment_file_is_refused(
    admit_repo: Path,  # noqa: F811 - imported fixture
    owner_path: str,
) -> None:
    owner = _dispatch(admit_repo, "owner", owner_path)
    fragment = f"{FRAGMENTS}/owner.html"
    claims = runs._live_scope_claims(PROJECT, admit_repo)

    with pytest.raises(crew.ScopeConflict) as refusal:
        runs._raise_live_scope_conflict(
            _node("joiner", fragment), claims, admit_repo, project=PROJECT
        )
    assert refusal.value.run_id == owner["run_id"]
    assert refusal.value.claimed_path == fragment

    with pytest.raises(crew.ScopeConflict) as dispatch_refusal:
        _dispatch(admit_repo, "joiner", fragment)
    assert dispatch_refusal.value.run_id == owner["run_id"]
    assert dispatch_refusal.value.claimed_path == fragment


@pytest.mark.parametrize("committed", [False, True])
def test_a_written_fragment_keeps_its_file_claim(
    admit_repo: Path,  # noqa: F811 - imported fixture
    committed: bool,
) -> None:
    owner = _dispatch(admit_repo, "owner", FRAGMENTS)
    pointer = runs.read_pointer(owner["run_id"])
    worktree = Path(pointer["worktree"])
    fragment = f"{FRAGMENTS}/custom.html"
    target = worktree / fragment
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('<section id="custom">Evidence</section>\n')
    if committed:
        git(worktree, "add", "--", fragment)
        git(
            worktree,
            "commit",
            "-q",
            "-m",
            "test: write fragment",
            "-m",
            "Keep a concrete custom fragment in the temporary run.",
        )

    with pytest.raises(crew.ScopeConflict) as refusal:
        _dispatch(admit_repo, "joiner", fragment)
    assert refusal.value.run_id == owner["run_id"]
    assert refusal.value.claimed_path == fragment


def test_an_explicit_fragment_keeps_its_file_claim(
    admit_repo: Path,  # noqa: F811 - imported fixture
) -> None:
    owner = _dispatch(admit_repo, "owner", FRAGMENTS)
    pointer = runs.read_pointer(owner["run_id"])
    fragment = f"{FRAGMENTS}/named.html"
    pointer["node"]["write_paths"].append(fragment)
    runs._write_json(runs.pointer_path(owner["run_id"]), pointer)

    with pytest.raises(crew.ScopeConflict) as refusal:
        _dispatch(admit_repo, "joiner", fragment)
    assert refusal.value.run_id == owner["run_id"]
    assert refusal.value.claimed_path == fragment
