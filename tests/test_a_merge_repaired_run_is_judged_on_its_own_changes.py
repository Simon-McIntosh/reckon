"""Landing attributes only the changes a run retained after merging primary."""

import json

import pytest

from reckon import crew, ledger
from reckon.crew import routing
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git

PROJECT = "sample"


def _commit(tree, *paths):
    git(tree, "add", *paths)
    git(
        tree,
        "commit",
        "-q",
        "-m",
        "test: record fixture",
        "-m",
        "Record the fixture contents.",
    )
    return git(tree, "rev-parse", "HEAD")


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    home = tmp_path / "crew-home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Worker")
    git(repo, "config", "user.email", "worker@example.invalid")
    (repo / "allowed.txt").write_text("seed\n")
    base = _commit(repo, "allowed.txt")
    (repo / "docs").mkdir()
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repo / "docs")}))
    tree = tmp_path / "worker"
    git(repo, "worktree", "add", "-q", "--detach", str(tree), base)
    record = {
        "run_id": "r-scope",
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(tree),
        "base_sha": base,
        "launch": "in-harness",
        "role": "implement",
        "backend": "native",
        "repository_tree_snapshot": routing._repository_tree_snapshot(repo),
        "node": {"id": "scope", "write_paths": ["allowed.txt"]},
    }
    _write_json(pointer_path(record["run_id"]), record)
    (tree / "allowed.txt").write_text("seed\nworker result\n")
    own = _commit(tree, "allowed.txt")
    main_paths = [f"primary-{index:02}.txt" for index in range(20)]
    for path in main_paths:
        (repo / path).write_text("primary content\n")
    _commit(repo, *main_paths)
    return repo, tree, record, own, main_paths


def _merge_primary(repo, tree):
    git(
        tree,
        "merge",
        "-q",
        "--no-ff",
        "main",
        "-m",
        "Merge primary",
        "-m",
        "Reconcile primary contents.",
    )
    return git(tree, "rev-parse", "HEAD")


@pytest.mark.parametrize("dirty_primary", [False, True])
@pytest.mark.parametrize("cite_own", [False, True])
def test_merged_primary_files_are_not_charged_to_the_run(
    delivery, dirty_primary, cite_own
):
    repo, tree, record, own, main_paths = delivery
    head = _merge_primary(repo, tree)
    if dirty_primary:
        record["node"]["write_paths"] = ["."]
        _write_json(pointer_path(record["run_id"]), record)
        (repo / main_paths[0]).write_text("peer work remains uncommitted\n")
    commits = [own, head] if cite_own else [head]
    result = crew.complete(record["run_id"], gate="passed", commits=commits, root=repo)
    stored = result["record"]
    assert not stored.get("scope_acceptances")
    assert stored["changed_lines"] == {"added": 1, "removed": 0, "files": 1}
    assert ledger.runs(PROJECT, root=repo)[0]["run_id"] == record["run_id"]
    assert not pointer_path(record["run_id"]).exists()
    if dirty_primary:
        assert (repo / main_paths[0]).read_text() == "peer work remains uncommitted\n"


def test_scope_escape_names_only_the_runs_own_file(delivery):
    repo, tree, record, own, main_paths = delivery
    _merge_primary(repo, tree)
    (tree / "outside.txt").write_text("outside grant\n")
    head = _commit(tree, "outside.txt")
    with pytest.raises(crew.CrewError) as refused:
        crew.complete(record["run_id"], gate="passed", commits=[own, head], root=repo)
    message = str(refused.value)
    assert "outside.txt" in message
    assert all(path not in message for path in main_paths)
    assert ledger.runs(PROJECT, root=repo) == []
    assert pointer_path(record["run_id"]).exists()


@pytest.mark.parametrize("escape", [False, True])
def test_unmerged_run_keeps_its_scope_guard(delivery, escape):
    repo, tree, record, own, main_paths = delivery
    commits = [own]
    if escape:
        (tree / "outside.txt").write_text("outside grant\n")
        commits.append(_commit(tree, "outside.txt"))
        with pytest.raises(crew.CrewError, match=r"outside\.txt") as refused:
            crew.complete(record["run_id"], gate="passed", commits=commits, root=repo)
        assert all(path not in str(refused.value) for path in main_paths)
    else:
        result = crew.complete(
            record["run_id"], gate="passed", commits=commits, root=repo
        )
        assert result["record"]["changed_lines"] == {
            "added": 1,
            "removed": 0,
            "files": 1,
        }
