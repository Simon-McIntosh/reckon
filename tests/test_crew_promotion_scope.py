"""Promotion scope and cumulative-diff guards."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import crew, ledger
from reckon.cli import main as cli_main
from reckon.crew import routing
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "sample"


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
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "allowed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "allowed.txt"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _pointer(
    repository: Path,
    run_id: str,
    base: str,
    *,
    write_paths: tuple[str, ...] = ("allowed.txt",),
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": base,
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-08-26T12:00:00Z",
            "node": {
                "id": "scope-check",
                "plan": "fixture",
                "section": "guard",
                "time_budget": "25m",
                "write_paths": list(write_paths),
            },
        },
    )


def _guarded_pointer(repository: Path, run_tree: Path, run_id: str, base: str) -> None:
    _pointer(repository, run_id, base)
    pointer = json.loads(pointer_path(run_id).read_text(encoding="utf-8"))
    pointer["worktree"] = str(run_tree)
    pointer["repository_tree_snapshot"] = routing._repository_tree_snapshot(repository)
    _write_json(pointer_path(run_id), pointer)


def _detached_tree(repository: Path, path: Path) -> Path:
    _git(repository, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    return path


def _commit_allowed(run_tree: Path) -> str:
    (run_tree / "allowed.txt").write_text("seed\nallowed\n", encoding="utf-8")
    _git(run_tree, "add", "allowed.txt")
    _git(run_tree, "commit", "-q", "-m", "test: update declared path")
    return _git(run_tree, "rev-parse", "HEAD")


def _commit_artifact_with_companion(run_tree: Path) -> str:
    (run_tree / "artifact.json").write_text('{"result": "ready"}\n')
    (run_tree / "artifact.png").write_bytes(b"companion image\n")
    _git(run_tree, "add", "artifact.json", "artifact.png")
    _git(run_tree, "commit", "-q", "-m", "test: generate artifact pair")
    return _git(run_tree, "rev-parse", "HEAD")


def _advance_main_and_merge_run(repository: Path, commit: str) -> None:
    for name in ("first.txt", "second.txt"):
        (repository / name).write_text(f"{name}\n", encoding="utf-8")
        _git(repository, "add", name)
        _git(repository, "commit", "-q", "-m", f"test: add {name}")
    _git(repository, "merge", "-q", "--no-ff", commit, "-m", "Merge worker commit")


def test_main_checkout_write_is_refused_and_pointer_is_retained(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / "run-tree")
    run_id = "r-main-tree-write"
    _guarded_pointer(repository, run_tree, run_id, base)
    commit = _commit_allowed(run_tree)
    (repository / "allowed.txt").write_text("stray\n", encoding="utf-8")

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    message = str(refusal.value)
    assert "allowed.txt" in message
    assert f"main checkout {repository}" in message
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()


def test_unchanged_main_checkout_allows_promotion(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / "run-tree")
    run_id = "r-main-tree-clean"
    _guarded_pointer(repository, run_tree, run_id, base)
    commit = _commit_allowed(run_tree)

    stored = crew.complete(run_id, gate="passed", commits=[commit], root=repository)[
        "record"
    ]

    assert stored["commits"] == [commit]
    assert not pointer_path(run_id).exists()


def test_peer_worktree_write_is_refused_and_pointer_is_retained(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / "run-tree")
    peer_tree = _detached_tree(repository, tmp_path / "peer-tree")
    run_id = "r-peer-tree-write"
    _guarded_pointer(repository, run_tree, run_id, base)
    commit = _commit_allowed(run_tree)
    (peer_tree / "allowed.txt").write_text("stray\n", encoding="utf-8")

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    message = str(refusal.value)
    assert "allowed.txt" in message
    assert f"peer worktree {peer_tree}" in message
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()


def test_unchanged_peer_worktree_allows_promotion(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / "run-tree")
    _detached_tree(repository, tmp_path / "peer-tree")
    run_id = "r-peer-tree-clean"
    _guarded_pointer(repository, run_tree, run_id, base)
    commit = _commit_allowed(run_tree)

    stored = crew.complete(run_id, gate="passed", commits=[commit], root=repository)[
        "record"
    ]

    assert stored["commits"] == [commit]
    assert not pointer_path(run_id).exists()


def test_worktree_registered_after_dispatch_is_not_charged_to_the_run(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / "run-tree")
    run_id = "r-late-tree"
    _guarded_pointer(repository, run_tree, run_id, base)
    commit = _commit_allowed(run_tree)
    late_tree = _detached_tree(repository, tmp_path / "late-tree")
    (late_tree / "stray.txt").write_text("outside\n", encoding="utf-8")

    stored = crew.complete(run_id, gate="passed", commits=[commit], root=repository)[
        "record"
    ]

    assert stored["commits"] == [commit]
    assert not pointer_path(run_id).exists()


def test_main_checkout_commit_movement_including_run_merge_allows_promotion(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / "run-tree")
    run_id = "r-main-advanced"
    _guarded_pointer(repository, run_tree, run_id, base)
    commit = _commit_allowed(run_tree)
    _advance_main_and_merge_run(repository, commit)

    stored = crew.complete(run_id, gate="passed", commits=[commit], root=repository)[
        "record"
    ]

    assert stored["commits"] == [commit]
    assert not pointer_path(run_id).exists()


def test_peer_uncommitted_edit_outside_declared_scope_allows_promotion(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / "run-tree")
    peer_tree = _detached_tree(repository, tmp_path / "peer-tree")
    run_id = "r-peer-unrelated-edit"
    _guarded_pointer(repository, run_tree, run_id, base)
    commit = _commit_allowed(run_tree)
    (peer_tree / "unrelated.txt").write_text("peer work\n", encoding="utf-8")

    stored = crew.complete(run_id, gate="passed", commits=[commit], root=repository)[
        "record"
    ]

    assert stored["commits"] == [commit]
    assert not pointer_path(run_id).exists()


def test_main_commit_movement_and_unrelated_peer_edit_allow_promotion(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / "run-tree")
    peer_tree = _detached_tree(repository, tmp_path / "peer-tree")
    run_id = "r-working-fleet"
    _guarded_pointer(repository, run_tree, run_id, base)
    commit = _commit_allowed(run_tree)
    _advance_main_and_merge_run(repository, commit)
    (peer_tree / "unrelated.txt").write_text("peer work\n", encoding="utf-8")

    stored = crew.complete(run_id, gate="passed", commits=[commit], root=repository)[
        "record"
    ]

    assert stored["commits"] == [commit]
    assert not pointer_path(run_id).exists()


def test_two_commit_scope_escape_is_refused_before_ledger_write(
    repository: Path,
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "allowed.txt").write_text("seed\nallowed\n", encoding="utf-8")
    _git(repository, "add", "allowed.txt")
    _git(repository, "commit", "-q", "-m", "test: update declared path")
    first = _git(repository, "rev-parse", "HEAD")
    (repository / "outside.txt").write_text("undeclared\n", encoding="utf-8")
    _git(repository, "add", "outside.txt")
    _git(repository, "commit", "-q", "-m", "test: update undeclared path")
    tip = _git(repository, "rev-parse", "HEAD")
    run_id = "r-scope-escape"
    _pointer(repository, run_id, base)

    with pytest.raises(crew.CrewError, match=r"outside\.txt"):
        crew.complete(run_id, gate="passed", commits=[first, tip], root=repository)

    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()


def test_complete_command_accepts_reasoned_companion_path(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / "run-tree")
    run_id = "r-companion-acceptance-fixture"
    real_pointer = (
        Path.home() / ".config" / "reckon" / "crew" / "live" / f"{run_id}.json"
    )
    assert not real_pointer.exists()
    _pointer(
        repository,
        run_id,
        base,
        write_paths=("artifact.json",),
    )
    commit = _commit_artifact_with_companion(run_tree)
    _git(repository, "merge", "-q", "--no-ff", commit, "-m", "Merge producer")

    arguments = [
        "crew",
        "complete",
        "--run",
        run_id,
        "--gate",
        "passed",
        "--commit",
        commit,
        "--gate-command",
        "pytest tests/test_crew_promotion_scope.py",
        "--gate-exit-status",
        "0",
        "--gate-log-path",
        "/durable/promotion-scope.log",
    ]
    refused = CliRunner().invoke(cli_main, arguments)

    assert refused.exit_code == 1
    assert "artifact.png" in refused.output
    assert "--accept-path" in refused.output
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()

    accepted = CliRunner().invoke(
        cli_main,
        [
            *arguments,
            "--accept-path",
            "artifact.png",
            "rendered with the declared JSON artifact",
        ],
    )

    assert accepted.exit_code == 0, accepted.output
    stored = json.loads(accepted.output)["record"]
    assert stored["scope_acceptances"] == [
        {
            "path": "artifact.png",
            "reason": "rendered with the declared JSON artifact",
        }
    ]
    assert stored["commits"] == [commit]
    assert not pointer_path(run_id).exists()
    assert not real_pointer.exists()


def test_complete_help_describes_the_refused_state_acceptance_resolves() -> None:
    result = CliRunner().invoke(cli_main, ["crew", "complete", "--help"])

    assert result.exit_code == 0
    assert "--accept-path PATH REASON" in result.output
    assert "promotion refused" in result.output


@pytest.mark.parametrize(
    "existing_waiver",
    [
        {"scope_changed": True},
        {"boundary_waiver": "the companion is expected"},
    ],
)
def test_existing_waivers_do_not_accept_an_undeclared_companion(
    repository: Path, tmp_path: Path, existing_waiver: dict[str, object]
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / "run-tree")
    run_id = "r-unaccepted-companion"
    _pointer(repository, run_id, base, write_paths=("artifact.json",))
    commit = _commit_artifact_with_companion(run_tree)

    with pytest.raises(crew.CrewError, match=r"artifact\.png"):
        crew.complete(
            run_id,
            gate="passed",
            commits=[commit],
            root=repository,
            **existing_waiver,
        )

    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()


def test_complete_command_refuses_companion_path_without_a_reason(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / "run-tree")
    run_id = "r-unreasoned-companion"
    _pointer(repository, run_id, base, write_paths=("artifact.json",))
    commit = _commit_artifact_with_companion(run_tree)

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "complete",
            "--run",
            run_id,
            "--gate",
            "passed",
            "--commit",
            commit,
            "--gate-command",
            "pytest tests/test_crew_promotion_scope.py",
            "--gate-exit-status",
            "0",
            "--gate-log-path",
            "/durable/promotion-scope.log",
            "--accept-path",
            "artifact.png",
            "  ",
        ],
    )

    assert result.exit_code == 1
    assert "requires a stated reason" in result.output
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()


def test_companion_path_acceptance_refuses_another_live_run_claim(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / "run-tree")
    run_id = "r-colliding-companion"
    _pointer(repository, run_id, base, write_paths=("artifact.json",))
    _pointer(
        repository,
        "r-companion-owner",
        base,
        write_paths=("artifact.png",),
    )
    commit = _commit_artifact_with_companion(run_tree)

    with pytest.raises(crew.CrewError, match=r"r-companion-owner.*artifact\.png"):
        crew.complete(
            run_id,
            gate="passed",
            commits=[commit],
            root=repository,
            accepted_paths={"artifact.png": "rendered companion"},
        )

    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()


def test_stored_line_counts_cover_the_cumulative_run_commit_span(
    repository: Path,
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "allowed.txt").write_text("seed\nfirst\nsecond\n", encoding="utf-8")
    _git(repository, "add", "allowed.txt")
    _git(repository, "commit", "-q", "-m", "test: add declared lines")
    first = _git(repository, "rev-parse", "HEAD")
    (repository / "allowed.txt").write_text(
        "seed\nfirst\nsecond\nthird\n", encoding="utf-8"
    )
    _git(repository, "add", "allowed.txt")
    _git(repository, "commit", "-q", "-m", "test: extend declared lines")
    tip = _git(repository, "rev-parse", "HEAD")
    run_id = "r-cumulative-stat"
    _pointer(repository, run_id, base)

    stored = crew.complete(
        run_id, gate="passed", commits=[first, tip], root=repository
    )["record"]

    assert stored["changed_lines"] == {"added": 3, "removed": 0, "files": 1}


def test_primary_advance_before_first_run_commit_is_outside_the_measured_span(
    repository: Path,
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "primary.txt").write_text("advanced\n", encoding="utf-8")
    _git(repository, "add", "primary.txt")
    _git(repository, "commit", "-q", "-m", "test: advance primary")
    (repository / "allowed.txt").write_text("seed\nworker\n", encoding="utf-8")
    _git(repository, "add", "allowed.txt")
    _git(repository, "commit", "-q", "-m", "test: update declared path")
    commit = _git(repository, "rev-parse", "HEAD")
    run_id = "r-advanced-primary"
    _pointer(repository, run_id, base)

    stored = crew.complete(run_id, gate="passed", commits=[commit], root=repository)[
        "record"
    ]

    assert stored["changed_lines"] == {"added": 1, "removed": 0, "files": 1}
    assert stored["commits"] == [commit]


def test_unresolvable_commit_is_refused_before_ledger_write(
    repository: Path,
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "allowed.txt").write_text("seed\nworker\n", encoding="utf-8")
    _git(repository, "add", "allowed.txt")
    _git(repository, "commit", "-q", "-m", "test: update declared path")
    commit = _git(repository, "rev-parse", "HEAD")
    missing = "0123456789abcdef0123456789abcdef01234567"
    run_id = "r-missing-commit"
    _pointer(repository, run_id, base)

    with pytest.raises(crew.CrewError, match=missing):
        crew.complete(
            run_id,
            gate="passed",
            commits=[commit, missing, commit],
            root=repository,
        )

    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()


def test_abbreviated_commit_is_stored_as_canonical_object_id(
    repository: Path,
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "allowed.txt").write_text("seed\nworker\n", encoding="utf-8")
    _git(repository, "add", "allowed.txt")
    _git(repository, "commit", "-q", "-m", "test: update declared path")
    commit = _git(repository, "rev-parse", "HEAD")
    run_id = "r-abbreviated-commit"
    _pointer(repository, run_id, base)

    stored = crew.complete(
        run_id, gate="passed", commits=[commit[:12]], root=repository
    )["record"]

    assert stored["commits"] == [commit]


def test_citing_a_merge_says_it_is_the_wrong_commit_not_a_scope_breach(tmp_path):
    """Same paths, two very different causes, and the message must separate them.

    A merge's first-parent diff carries everything its other parent brought — an
    orchestrator's own plan edits included — so citing the merge attributes them
    to the worker. Measured: a promotion read as a worker scope violation when
    the worker had stayed inside its fence and the orchestrator had named the
    wrong commit.
    """
    from reckon.crew import promotion

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "worker@example.invalid")
    git("config", "user.name", "Worker")
    (repo / "seed.txt").write_text("seed\n")
    git("add", "seed.txt")
    git("commit", "-q", "-m", "chore: seed")

    # the worker's own commit, inside its fence
    git("checkout", "-q", "-b", "node")
    (repo / "in_scope.py").write_text("worker\n")
    git("add", "in_scope.py")
    git("commit", "-q", "-m", "feat: the node's own work")
    worker_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # the orchestrator's unrelated edit, on the branch being merged into
    git("checkout", "-q", "main")
    (repo / "orchestrator_plan.html").write_text("plan\n")
    git("add", "orchestrator_plan.html")
    git("commit", "-q", "-m", "docs: orchestrator plan state")
    git("merge", "-q", "--no-ff", "node", "-m", "Merge node")
    merge_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert promotion._merge_revisions(repo, [merge_sha]) == [merge_sha]
    assert promotion._merge_revisions(repo, [worker_sha]) == []


def test_a_passing_gate_may_not_leave_the_run_s_own_commits_uncited(
    tmp_path, monkeypatch
):
    """The ledger row is the binding between a node and its work.

    Measured in one project's ledger: a run recorded `gate: passed` with
    `commits: []` while its worktree held a commit unreachable from the
    integration branch. Only the workspace collector's `unintegrated`
    classification stood between that work and deletion, and it would have gone
    the moment someone believed the ledger. A commitless promotion stays legal —
    a report-only node has no commit and that is its deliverable — so the check
    reads whether the worktree actually committed rather than guessing from the
    node's role.
    """
    from reckon.crew import promotion

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    tree = tmp_path / "worktree"
    tree.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=tree, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "worker@example.invalid")
    git("config", "user.name", "Worker")
    (tree / "seed.txt").write_text("seed\n")
    git("add", "seed.txt")
    git("commit", "-q", "-m", "chore: seed")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    record = {"worktree": str(tree), "base_sha": base, "node": {"role": "implement"}}

    # A worktree still at its base committed nothing: silence is honest.
    promotion._require_gate_evidence(
        "r-report-only", record, verdict="passed", commits=(), no_commit_reason=""
    )

    (tree / "work.py").write_text("real work\n")
    git("add", "work.py")
    git("commit", "-q", "-m", "feat: the work the ledger would have lost")

    with pytest.raises(crew.CrewError) as refusal:
        promotion._require_gate_evidence(
            "r-uncited", record, verdict="passed", commits=(), no_commit_reason=""
        )
    message = str(refusal.value)
    assert "cites no commit" in message
    assert "discarded the moment someone believes the ledger" in message
    assert "--no-commit" in message

    # Citing the commit, or declaring the absence deliberately, both pass.
    promotion._require_gate_evidence(
        "r-cited", record, verdict="passed", commits=("HEAD",), no_commit_reason=""
    )
    promotion._require_gate_evidence(
        "r-declared",
        record,
        verdict="passed",
        commits=(),
        no_commit_reason="artifact retained out-of-tree by design",
    )

    # A failing gate is already answerable through --outcome, and an
    # unmeasurable worktree is never refused on a guess.
    promotion._require_gate_evidence(
        "r-failed", record, verdict="failed", commits=(), no_commit_reason=""
    )
    promotion._require_gate_evidence(
        "r-unmeasurable",
        {"worktree": str(tmp_path / "gone"), "base_sha": base},
        verdict="passed",
        commits=(),
        no_commit_reason="",
    )


def test_the_manifest_answers_before_the_repository_does(tmp_path, monkeypatch):
    """Reckon was holding the answer and discarding it.

    The worker's manifest records the commits it made. `complete` recorded only
    what `--commit` passed and never consulted that line, so a coordinator who
    omitted one flag produced a ledger row saying the node succeeded with
    nothing pointing at the work — found five days later only because the
    workspace collector classified the worktree `unintegrated`. Naming the exact
    revisions the worker already reported is more use than describing the
    condition, so the manifest is read before the repository.

    The line is free text, so the check resolves each entry against the
    repository rather than pattern-matching it: a report-only node writes
    `commits: none (repository worktree remained clean)`, which is neither a
    revision nor an omission, and a literal "none" test would refuse it.
    """
    from reckon.crew import promotion

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    tree = tmp_path / "worktree"
    tree.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=tree, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "worker@example.invalid")
    git("config", "user.name", "Worker")
    (tree / "seed.txt").write_text("seed\n")
    git("add", "seed.txt")
    git("commit", "-q", "-m", "chore: seed")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (tree / "cohort.yaml").write_text("names: [a, b]\n")
    git("add", "cohort.yaml")
    git("commit", "-q", "-m", "chore: refresh the review artifact")
    landed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    manifest = tmp_path / "manifest.md"
    record = {
        "manifest_path": str(manifest),
        "manifest_baseline_mtime_ns": 0,
        "worktree": str(tree),
        "base_sha": base,
        "node": {"role": "implement"},
    }

    manifest.write_text(
        f"node: n-west-demo-rc2\nstatus: complete\n"
        f"commits: {landed[:8]}\nblockers: none\n"
    )
    with pytest.raises(crew.CrewError) as refusal:
        promotion._require_gate_evidence(
            "r-uncited", record, verdict="passed", commits=(), no_commit_reason=""
        )
    message = str(refusal.value)
    assert "manifest records 1" in message
    assert landed[:8] in message
    assert "Reckon is holding the answer" in message

    # Free text that names no revision is the honest commitless report, and the
    # worktree agrees with it once its HEAD is back at base.
    manifest.write_text(
        "node: n\nstatus: complete\n"
        "commits: none (report-only node; repository worktree remained clean)\n"
        "blockers: none\n"
    )
    git("reset", "-q", "--hard", base)
    promotion._require_gate_evidence(
        "r-report-only", record, verdict="passed", commits=(), no_commit_reason=""
    )
