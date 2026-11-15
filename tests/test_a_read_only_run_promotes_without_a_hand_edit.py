"""A read-only run promotes on its own commits line, with or without a reason.

A review run has no repository work to commit and writes what it did into the
manifest's ``commits`` line as prose: ``commits: none — this node writes a
review record outside the repository``. Promotion read that sentence as a
citation, refused it for naming no commit object, and left the coordinator
blanking the line by hand before promoting with ``--no-commit``.

The line is a declaration, not a citation, so it is read as an empty commit
list. The safety the refusal exists for is unchanged: a line naming a sha that
resolves to no object is still refused, and a stated ``--no-commit`` reason
does not license a bad citation — it only records deliberately that a genuine
absence needs no commit.

The fixture runs a real repository and a real worktree for the run, and writes
its pointer under a temporary configuration home; it asserts afterwards that
the workstation's real pointer directory is untouched, because an isolated
read does not prove an isolated write.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew.runs import _write_json, pointer_path
from tests.conftest import EXECUTABLE_GATE_COMMAND
from tests.helpers.git import git as _git

PROJECT = "read-only-promotion-fixture"
PLAN = "read-only-promotion-target"
RUN_IDS = [
    f"r-20261015T1200000000{index:02d}-review-read-only-promotion"
    for index in range(1, 12)
]

# The sentence the failing reviews delivered, verbatim.
REVIEW_COMMITS_PROSE = "none — this node writes a review record outside the repository"

# A citation-shaped value that names no object in any store.
UNRESOLVABLE_SHA = "0123456789abcdef0123456789abcdef01234567"

REASON = "the review record is the deliverable and lives outside the repository"


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
        "title": "Read-only promotion target",
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
    """A real worktree at the run's base, and the sha it sits on."""
    base = _git(repository, "rev-parse", "HEAD")
    tree = tmp_path_worktree(tmp_path)
    _git(repository, "worktree", "add", "--detach", "--quiet", str(tree), base)
    return tree, base


def tmp_path_worktree(tmp_path: Path) -> Path:
    tree = tmp_path / "worktrees" / "run"
    tree.parent.mkdir(parents=True, exist_ok=True)
    return tree


def _review_run(
    repository: Path,
    tmp_path: Path,
    run_id: str,
    *,
    tree: Path,
    base_sha: str,
    commits: str,
) -> None:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: read-only-review\n"
        "status: complete\n"
        f"commits: {commits}\n"
        f"changed_paths: {tmp_path / 'crew' / 'reviews' / f'{run_id}.json'}\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(tree),
            "base_sha": base_sha,
            "launch": "in-harness",
            "role": "review",
            "backend": "native",
            "created_at": "2026-10-15T12:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": f"review-of-{PLAN}",
                "plan": PLAN,
                "section": "read-only-promotion",
                "time_budget": "25m",
                "write_paths": ["candidate.txt"],
            },
        },
    )


@pytest.mark.parametrize(
    "pass_reason",
    [
        pytest.param(True, id="with-a-stated-reason"),
        pytest.param(False, id="with-no-flag-at-all"),
    ],
)
def test_a_read_only_run_promotes_on_its_own_commits_prose(
    repository: Path,
    tmp_path: Path,
    run_tree: tuple[Path, str],
    pass_reason: bool,
) -> None:
    """The declaration is read as no commits, whichever way it is promoted."""
    tree, base_sha = run_tree
    run_id = RUN_IDS[0]
    _review_run(
        repository,
        tmp_path,
        run_id,
        tree=tree,
        base_sha=base_sha,
        commits=REVIEW_COMMITS_PROSE,
    )

    promoted = (
        crew.complete(run_id, gate="passed", root=repository, no_commit=REASON)
        if pass_reason
        else crew.complete(run_id, gate="passed", root=repository)
    )

    assert promoted["record"]["commits"] == []
    assert not pointer_path(run_id).exists()


@pytest.mark.parametrize(
    "pass_reason",
    [
        pytest.param(True, id="with-a-stated-reason"),
        pytest.param(False, id="with-no-flag-at-all"),
    ],
)
def test_an_unresolvable_commit_is_refused_however_the_run_is_promoted(
    repository: Path,
    tmp_path: Path,
    run_tree: tuple[Path, str],
    pass_reason: bool,
) -> None:
    """A reason records an absence; it never licenses a citation naming nothing."""
    tree, base_sha = run_tree
    run_id = RUN_IDS[1]
    _review_run(
        repository,
        tmp_path,
        run_id,
        tree=tree,
        base_sha=base_sha,
        commits=UNRESOLVABLE_SHA,
    )

    def promote() -> dict:
        if pass_reason:
            return crew.complete(
                run_id, gate="passed", root=repository, no_commit=REASON
            )
        return crew.complete(run_id, gate="passed", root=repository)

    with pytest.raises(crew.CrewError, match="does not resolve to a commit object"):
        promote()

    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []
