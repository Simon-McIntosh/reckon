"""One predicate decides whether a manifest cites a commit, for both readers.

The write-time audit and the promotion guard ask one question of one field, and
each used to answer it with its own reader: the audit refused an all-zero
``commits`` value while promotion read it as a citation, so an implement run
recording ``commits: 0`` beside changes inside its repository read not ok at
check-manifest and could still promote. The predicate is defined once and both
readers apply it, and this module pins the shapes it decides on — ``none``,
``[]``, ``0`` and a resolving sha — for a role that owes a commit and a role
that does not.

Both readers are driven through their own entry points, so the agreement is
measured rather than restated: the audit through
:func:`reckon.crew.reports.audit_manifest` and the guard through
:func:`reckon.crew.promotion._require_commit_for_changed_manifest`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import promotion, reports
from reckon.crew.node import TaskNode
from tests.conftest import EXECUTABLE_GATE_COMMAND

CITATION_SHAPES = ("none", "[]", "0", "sha")

# A shape's spelling is not a filename, so each one gets its own manifest path.
SHAPE_SLUGS = {"none": "none", "[]": "empty-list", "0": "zero", "sha": "sha"}

CITES_A_COMMIT = {"none": False, "[]": False, "0": False, "sha": True}


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def scratch(tmp_path: Path) -> dict[str, object]:
    """A repository one commit past the run's recorded base.

    The extra commit is what a commitless role's worktree must show for the
    guard's worktree measurement to have anything to read.
    """
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    (repository / "candidate.txt").write_text("seed\n", encoding="utf-8")
    _git(repository, "add", "candidate.txt")
    _git(repository, "commit", "-q", "-m", "chore: seed")
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "work.txt").write_text("work\n", encoding="utf-8")
    _git(repository, "add", "work.txt")
    _git(repository, "commit", "-q", "-m", "feat: work beyond the run's base")
    return {
        "repository": repository,
        "base": base,
        "head": _git(repository, "rev-parse", "HEAD"),
    }


def _written(scratch: dict[str, object], shape: str) -> str:
    return str(scratch["head"]) if shape == "sha" else shape


def _manifest_text(*, commits: str, changed_paths: str) -> str:
    return (
        "node: citation\n"
        "status: complete\n"
        f"commits: {commits}\n"
        f"changed_paths: {changed_paths}\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n"
    )


def _delivered_manifest(
    tmp_path: Path, *, shape: str, commits: str, changed_paths: str
) -> Path:
    path = tmp_path / "manifests" / f"{SHAPE_SLUGS[shape]}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _manifest_text(commits=commits, changed_paths=changed_paths), encoding="utf-8"
    )
    return path


def _node(role: str) -> TaskNode:
    return TaskNode(
        id="citation",
        goal="Pin what counts as a commit citation",
        plan="citation-fixture",
        role=role,
        write_paths=["candidate.txt"],
    )


def _guard_refuses(manifest_path: Path, scratch: dict[str, object], role: str) -> bool:
    repository = scratch["repository"]
    record = {
        "role": role,
        "manifest_path": str(manifest_path),
        "worktree": str(repository),
        "repo": str(repository),
        "base_sha": scratch["base"],
    }
    try:
        promotion._require_commit_for_changed_manifest("r-citation", record)
    except crew.CrewError:
        return True
    return False


def _audit_refuses(manifest_path: Path, scratch: dict[str, object], role: str) -> bool:
    repository = scratch["repository"]
    audit = reports.audit_manifest(
        Path(manifest_path).read_text(encoding="utf-8"),
        _node(role),
        worktree=repository,
        repository=repository,
        manifest_path=manifest_path,
    )
    return "status is complete but no commit is recorded" in audit["findings"]


@pytest.mark.parametrize("role", ["implement", "review"])
@pytest.mark.parametrize("shape", CITATION_SHAPES)
def test_one_predicate_decides_the_four_shapes_for_both_roles(
    scratch: dict[str, object], role: str, shape: str
) -> None:
    text = _manifest_text(
        commits=_written(scratch, shape), changed_paths="candidate.txt"
    )
    manifest = reports.parse_manifest(text)

    assert (
        promotion._manifest_cites_a_commit(manifest, {"role": role}, text)
        is CITES_A_COMMIT[shape]
    )


@pytest.mark.parametrize("shape", CITATION_SHAPES)
def test_the_audit_and_the_guard_agree_for_a_role_that_owes_a_commit(
    scratch: dict[str, object], shape: str, tmp_path: Path
) -> None:
    """A repository path is named, so the field must cite the commit holding it."""
    manifest_path = _delivered_manifest(
        tmp_path,
        shape=shape,
        commits=_written(scratch, shape),
        changed_paths="candidate.txt",
    )

    audit_refused = _audit_refuses(manifest_path, scratch, "implement")
    guard_refused = _guard_refuses(manifest_path, scratch, "implement")

    assert audit_refused == (not CITES_A_COMMIT[shape])
    assert guard_refused == (not CITES_A_COMMIT[shape])
    assert audit_refused == guard_refused


@pytest.mark.parametrize("shape", CITATION_SHAPES)
def test_the_audit_and_the_guard_agree_for_a_commitless_role(
    scratch: dict[str, object], shape: str, tmp_path: Path
) -> None:
    """The role owes no commit, so the audit asks nothing of the field.

    The guard still asks it, and still refuses the shapes the predicate does not
    read as a citation, because this run's worktree holds a commit of its own:
    a commitless role promotes with no commits only when it has no repository
    work to cite.
    """
    manifest_path = _delivered_manifest(
        tmp_path,
        shape=shape,
        commits=_written(scratch, shape),
        changed_paths="none",
    )

    audit_refused = _audit_refuses(manifest_path, scratch, "review")
    guard_refused = _guard_refuses(manifest_path, scratch, "review")

    assert not audit_refused
    assert guard_refused == (not CITES_A_COMMIT[shape])
