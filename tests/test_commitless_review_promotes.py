"""A commitless run promotes when its worktree shows no change, and not otherwise.

Three local-lane reviews wrote a sentence into ``commits:`` —
``commits: none (review node; no repository change; ...)`` — because a review
has no repository work to commit. ``crew complete`` then read the sentence as a
citation, refused it for not resolving to an object, and the coordinator blanked
the line by hand each time.

The mirror case is the one that matters for safety: a run that really changed
something must not hide it behind a declaration. Deciding that from the prose was
tried and walked around three times — ``none.txt`` is a filename as plausibly as
``none`` is a declaration, and each prose shape closed exposed the next one — so
the decision is taken from the worktree instead.

For a role that carries no repository work, review or investigate, promotion with
no commits requires the run's own worktree to show no repository change against
the run's recorded base sha. That means no commits beyond the base, no tracked
modification, and no untracked repository file apart from the ``.venv`` symlink
provisioning plants in every worktree. The declared fields then stand in their
remaining role: say whether the run *claims* no change, and refuse a run that
claims an in-repository path while citing no commit.

The fixture runs a repository, a real worktree for the run, and a pointer under
``tmp_path``, and asserts afterwards that the workstation's real crew pointer
directory is untouched, because an isolated read does not prove an isolated
write.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "commitless-review-fixture"
PLAN = "commitless-review-target"
RUN_IDS = [
    f"r-20260928T1000000000{index:02d}-review-worktree-evidence"
    for index in range(1, 25)
]

# The sentence a review writes when it has no repository change to cite, in the
# shape the failing reviews delivered.
REVIEW_COMMITS_PROSE = (
    "none (review node; no repository change; the review is the deliverable)"
)

# The same declaration with a comma inside its parenthetical, which the manifest
# parser splits into several entries.
COMMA_COMMITS_PROSE = "none (review node, no repository change)"

# The ``changed_paths`` values a report-only node writes, none of which names a
# repository path. Whatever their shape, a review whose worktree is clean
# promotes under each of them, and a review whose worktree really changed a file
# called ``none`` is refused under each of them too — the shape is not what
# decides.
DECLARED_NO_PATHS = ["none", "none, no repository change", "none. review only"]


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_plan(root: Path) -> Path:
    path = root / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Commitless review target",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def real_crew_home_is_not_a_fixture_target() -> None:
    """No fixture may reach the workstation's real crew pointer directory."""
    real_live = Path.home() / ".config" / "reckon" / "crew" / "live"
    real_pointers = [real_live / f"{run_id}.json" for run_id in RUN_IDS]
    assert not any(path.exists() for path in real_pointers)
    yield
    assert not any(path.exists() for path in real_pointers)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    _write_plan(root)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "candidate.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs", "candidate.txt"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


@pytest.fixture()
def run_tree(repository: Path, tmp_path: Path) -> tuple[Path, str]:
    """A real worktree at the repository's HEAD, and the base sha it sits on."""
    base = _git(repository, "rev-parse", "HEAD")
    tree = tmp_path / "worktrees" / "run"
    tree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", "--quiet", str(tree), base)
    return tree, base


def _manifest(
    tmp_path: Path,
    run_id: str,
    *,
    changed_paths: str,
    commits: str = "none",
) -> Path:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: commitless-review\n"
        "status: complete\n"
        f"commits: {commits}\n"
        f"changed_paths: {changed_paths}\n"
        "tests: focused commitless-review promotion check passed\n",
        encoding="utf-8",
    )
    return manifest


def _pointer(
    repository: Path,
    run_id: str,
    manifest: Path,
    *,
    role: str,
    node_id: str,
    tree: Path,
    base_sha: str,
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(tree),
            "base_sha": base_sha,
            "launch": "in-harness",
            "role": role,
            "backend": "native",
            "created_at": "2026-09-28T10:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": node_id,
                "plan": PLAN,
                "section": "commitless-review",
                "time_budget": "25m",
                "write_paths": ["candidate.txt"],
            },
        },
    )


def _review_run(
    repository: Path,
    tmp_path: Path,
    run_id: str,
    *,
    tree: Path,
    base_sha: str,
    changed_paths: str,
    commits: str = "none",
) -> None:
    manifest = _manifest(
        tmp_path, run_id, changed_paths=changed_paths, commits=commits
    )
    _pointer(
        repository,
        run_id,
        manifest,
        role="review",
        node_id=f"review-of-{PLAN}",
        tree=tree,
        base_sha=base_sha)


def _implement_run(
    repository: Path,
    tmp_path: Path,
    run_id: str,
    *,
    tree: Path,
    base_sha: str,
    changed_paths: str,
    commits: str,
) -> None:
    manifest = _manifest(
        tmp_path, run_id, changed_paths=changed_paths, commits=commits
    )
    _pointer(
        repository,
        run_id,
        manifest,
        role="implement",
        node_id=f"build-{PLAN}",
        tree=tree,
        base_sha=base_sha,
    )


def _committed_file(tree: Path, name: str) -> None:
    """Commit a change to ``name`` inside the run's worktree."""
    target = tree / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n", encoding="utf-8")
    _git(tree, "add", "--", name)
    _git(tree, "commit", "-q", "-m", "test: change " + name)


def _promotes(repository: Path, run_id: str) -> dict:
    """Promote a fixture run, or fail with the refusal text for the record."""
    try:
        return crew.complete(run_id, gate="passed", root=repository)
    except crew.CrewError as refusal:  # pragma: no cover - reported, not swallowed
        raise AssertionError(f"run {run_id!r} was refused: {refusal}") from refusal


def test_a_report_only_review_promotes_over_its_own_deliverable(
    repository: Path, tmp_path: Path, run_tree: tuple[Path, str]
) -> None:
    """A review's deliverable lies outside the repository, so no commit is due."""
    tree, base_sha = run_tree
    run_id = RUN_IDS[0]
    delivered = tmp_path / "crew" / "reviews" / f"{run_id}.json"
    _review_run(
        repository,
        tmp_path,
        run_id,
        tree=tree,
        base_sha=base_sha,
        changed_paths=str(delivered),
        commits=REVIEW_COMMITS_PROSE,
    )

    promoted = _promotes(repository, run_id)

    assert promoted["record"]["commits"] == []
    assert not pointer_path(run_id).exists()


@pytest.mark.parametrize("changed_paths", DECLARED_NO_PATHS)
def test_a_review_declaring_no_paths_promotes_on_a_clean_worktree(
    repository: Path,
    tmp_path: Path,
    run_tree: tuple[Path, str],
    changed_paths: str,
) -> None:
    """The prose shape is irrelevant when the worktree holds no change."""
    tree, base_sha = run_tree
    run_id = RUN_IDS[1]
    _review_run(
        repository,
        tmp_path,
        run_id,
        tree=tree,
        base_sha=base_sha,
        changed_paths=changed_paths,
    )

    promoted = _promotes(repository, run_id)

    assert promoted["record"]["commits"] == []
    assert not pointer_path(run_id).exists()


def test_a_review_with_an_empty_changed_paths_promotes_on_a_clean_worktree(
    repository: Path, tmp_path: Path, run_tree: tuple[Path, str]
) -> None:
    """A manifest that names no path at all promotes on a clean worktree."""
    tree, base_sha = run_tree
    run_id = RUN_IDS[2]
    _review_run(
        repository,
        tmp_path,
        run_id,
        tree=tree,
        base_sha=base_sha,
        changed_paths="",
    )

    promoted = _promotes(repository, run_id)

    assert promoted["record"]["commits"] == []
    assert not pointer_path(run_id).exists()


def test_a_review_with_commits_prose_promotes_on_a_clean_worktree(
    repository: Path, tmp_path: Path, run_tree: tuple[Path, str]
) -> None:
    """The declaration is honoured when the worktree agrees with it."""
    tree, base_sha = run_tree
    run_id = RUN_IDS[3]
    _review_run(
        repository,
        tmp_path,
        run_id,
        tree=tree,
        base_sha=base_sha,
        changed_paths="none. review only",
        commits=REVIEW_COMMITS_PROSE,
    )

    promoted = _promotes(repository, run_id)

    assert promoted["record"]["commits"] == []
    assert not pointer_path(run_id).exists()


def test_a_provisioned_venv_symlink_is_not_a_repository_change(
    repository: Path, tmp_path: Path, run_tree: tuple[Path, str]
) -> None:
    """Every dispatch plants a ``.venv`` symlink; it is not the run's work."""
    tree, base_sha = run_tree
    run_id = RUN_IDS[4]
    _review_run(
        repository,
        tmp_path,
        run_id,
        tree=tree,
        base_sha=base_sha,
        changed_paths="none",
    )
    (tree / ".venv").symlink_to(tmp_path / "shared-venv", target_is_directory=True)

    promoted = _promotes(repository, run_id)

    assert promoted["record"]["commits"] == []
    assert not pointer_path(run_id).exists()


@pytest.mark.parametrize("changed_paths", [*DECLARED_NO_PATHS, ""])
def test_a_review_whose_worktree_committed_a_file_is_refused(
    repository: Path,
    tmp_path: Path,
    run_tree: tuple[Path, str],
    changed_paths: str,
) -> None:
    """The declaration does not cover work the worktree actually holds.

    The file is called ``none`` in every case, and the manifest declares it away
    under each prose shape and under no value at all. The shape is not what the
    guard reads: the worktree holds a commit beyond the base, so the run needs the
    commit that contains it, and it is refused with the path git found.
    """
    tree, base_sha = run_tree
    run_id = RUN_IDS[5]
    _committed_file(tree, "none")
    _review_run(
        repository,
        tmp_path,
        run_id,
        tree=tree,
        base_sha=base_sha,
        changed_paths=changed_paths,
    )

    with pytest.raises(crew.CrewError, match="cites no commit") as refusal:
        crew.complete(run_id, gate="passed", root=repository)

    assert "none" in str(refusal.value)
    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_a_review_with_an_untracked_repository_file_is_refused(
    repository: Path, tmp_path: Path, run_tree: tuple[Path, str]
) -> None:
    """An uncommitted file the run changed is still a change with no commit."""
    tree, base_sha = run_tree
    run_id = RUN_IDS[6]
    (tree / "scratch.txt").write_text("work\n", encoding="utf-8")
    _review_run(
        repository,
        tmp_path,
        run_id,
        tree=tree,
        base_sha=base_sha,
        changed_paths="none",
    )

    with pytest.raises(crew.CrewError, match="cites no commit") as refusal:
        crew.complete(run_id, gate="passed", root=repository)

    assert "scratch.txt" in str(refusal.value)
    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_an_investigate_run_with_a_dirty_worktree_is_refused_without_an_override(
    repository: Path, tmp_path: Path, run_tree: tuple[Path, str]
) -> None:
    """A dirty worktree promotes commitless only under a coordinator override.

    The same run passes with --no-commit, which records the path it declined;
    without it the guard names the file git found.
    """
    tree, base_sha = run_tree
    run_id = RUN_IDS[12]
    (tree / "keep.txt").write_text("uncommitted worker result\n", encoding="utf-8")
    _pointer(
        repository,
        run_id,
        _manifest(tmp_path, run_id, changed_paths="none"),
        role="investigate",
        node_id=f"investigate-of-{PLAN}",
        tree=tree,
        base_sha=base_sha,
    )

    with pytest.raises(crew.CrewError, match="cites no commit") as refusal:
        crew.complete(run_id, gate="passed", root=repository)

    assert "keep.txt" in str(refusal.value)
    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_a_review_naming_an_in_repository_path_with_no_commit_is_refused(
    repository: Path, tmp_path: Path, run_tree: tuple[Path, str]
) -> None:
    """A declared in-repository path still needs its commit, worktree or not."""
    tree, base_sha = run_tree
    run_id = RUN_IDS[7]
    _review_run(
        repository,
        tmp_path,
        run_id,
        tree=tree,
        base_sha=base_sha,
        changed_paths="candidate.txt",
        commits=REVIEW_COMMITS_PROSE,
    )

    with pytest.raises(crew.CrewError, match="manifest field 'commits' is missing"):
        crew.complete(run_id, gate="passed", root=repository)

    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_an_implement_run_naming_an_in_repository_path_is_still_refused(
    repository: Path, tmp_path: Path, run_tree: tuple[Path, str]
) -> None:
    """A committing role keeps the guard it has always had, unchanged."""
    tree, base_sha = run_tree
    run_id = RUN_IDS[8]
    _implement_run(
        repository,
        tmp_path,
        run_id,
        tree=tree,
        base_sha=base_sha,
        changed_paths="candidate.txt",
        commits=REVIEW_COMMITS_PROSE,
    )

    with pytest.raises(crew.CrewError, match="manifest field 'commits' is missing"):
        crew.complete(run_id, gate="passed", root=repository)

    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_an_implement_run_refuses_a_full_stop_in_changed_paths(
    repository: Path, tmp_path: Path, run_tree: tuple[Path, str]
) -> None:
    """``changed_paths`` is a path list for a committing role, declaration or not."""
    tree, base_sha = run_tree
    run_id = RUN_IDS[9]
    _implement_run(
        repository,
        tmp_path,
        run_id,
        tree=tree,
        base_sha=base_sha,
        changed_paths="none. no repository change",
        commits="",
    )

    with pytest.raises(crew.CrewError, match="manifest field 'commits' is missing"):
        crew.complete(run_id, gate="passed", root=repository)

    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_an_implement_run_whose_commits_do_not_resolve_is_refused(
    repository: Path, tmp_path: Path, run_tree: tuple[Path, str]
) -> None:
    """A committing role gets no whole-field reading, so the split tail is a citation."""
    tree, base_sha = run_tree
    run_id = RUN_IDS[10]
    _implement_run(
        repository,
        tmp_path,
        run_id,
        tree=tree,
        base_sha=base_sha,
        changed_paths="candidate.txt",
        commits=COMMA_COMMITS_PROSE,
    )

    with pytest.raises(crew.CrewError, match="does not resolve to an object"):
        crew.complete(run_id, gate="passed", root=repository)

    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_a_word_that_only_begins_with_an_absence_word_is_not_a_declaration(
    repository: Path, tmp_path: Path, run_tree: tuple[Path, str]
) -> None:
    """``nonesuch`` begins with the letters of ``none`` but is a citation attempt."""
    tree, base_sha = run_tree
    run_id = RUN_IDS[11]
    delivered = tmp_path / "crew" / "reviews" / f"{run_id}.json"
    _review_run(
        repository,
        tmp_path,
        run_id,
        tree=tree,
        base_sha=base_sha,
        changed_paths=str(delivered),
        commits="nonesuch, prose",
    )

    with pytest.raises(crew.CrewError, match="does not resolve to an object"):
        crew.complete(run_id, gate="passed", root=repository)

    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []
