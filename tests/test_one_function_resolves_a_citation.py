"""Every commit citation uses the same bounded Git lookup."""

from __future__ import annotations

import ast
import importlib
import subprocess
from pathlib import Path

from reckon import interface_counts
from reckon.crew.node import PlanVisibilityError

reports = importlib.import_module("reckon.crew.reports")
promotion = importlib.import_module("reckon.crew.promotion")
recovery = importlib.import_module("reckon.crew.recovery")
routing = importlib.import_module("reckon.crew.routing")


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def test_only_the_shared_resolver_peels_a_revision_to_a_commit() -> None:
    root = Path(__file__).resolve().parents[1]
    sites: set[tuple[str, str]] = set()
    for path in (root / "reckon").rglob("*.py"):
        module = ast.parse(path.read_text(encoding="utf-8"))

        def visit(node: ast.AST, owner: str = "", current: Path = path) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = node.name
            if isinstance(node, ast.Call):
                literals = (
                    item.value
                    for item in ast.walk(node)
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                )
                if any("^{commit}" in literal for literal in literals):
                    sites.add((current.relative_to(root).as_posix(), owner))
            for child in ast.iter_child_nodes(node):
                visit(child, owner)

        visit(module)
    assert sites == {("reckon/crew/recovery_review_subject.py", "_resolve_commit")}


def test_former_readers_agree_on_resolving_and_refused_revisions(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "chore: seed")
    sha = _git(repo, "rev-parse", "HEAD")
    typo = sha[:37] + ("0" if sha[37] != "0" else "1")

    for revision, expected in (
        (sha, True),
        (sha[:8], True),
        (typo, False),
        ("--help", False),
    ):
        try:
            routing_result = bool(routing._base_commit(repo, revision))
        except PlanVisibilityError:
            routing_result = False
        try:
            interface_counts.count_revision_cached(
                repo, revision, cache_root=tmp_path / "cache"
            )
            interface_result = True
        except subprocess.CalledProcessError:
            interface_result = False
        results = {
            "reports": reports._commit_resolves_in(repo, revision),
            "promotion": promotion._commit_resolves_in(repo, revision),
            "canonical": bool(promotion._commit_canonical_id(repo, revision)),
            "recovery": bool(recovery._resolve_commit(repo, revision)),
            "abbreviation": bool(recovery._resolve_abbreviated_commit(repo, revision)),
            "routing": routing_result,
            "interface": interface_result,
        }
        assert results == dict.fromkeys(results, expected), (revision, results)
