"""One batched blob reader serves both the plan census and the interface counter.

The counter no longer carries its own ``cat-file --batch`` loop; it imports the
reader from ``reckon.velocity``. This test proves the two consumers reach the
same bytes through that one function: it pins the identity of the reader the
counter holds, and records what each consumer passes and receives, so a private
loop reintroduced on either side is caught.
"""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

from reckon import interface_counts, velocity

_DISPATCH_IDENTITY = ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT")
ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = "docs/plans/example.html"
MODULE_PATH = "reckon/example.py"
LATER_PATH = "reckon/later.py"

_PLAN = (
    '<meta name="plan-status" content="active">\n'
    '<meta name="plan-archived" content="">\n'
)
_MODULE = "def example():\n    return 1\n"
_LATER = "def later():\n    return 2\n"


def _git_env() -> dict[str, str]:
    # A git wrapper keyed on the running worker's identity refuses a mutating
    # verb outside the worker's own worktree, so drop the inherited identity
    # before pointing the subprocess at the synthesised repository.
    return {
        name: value
        for name, value in os.environ.items()
        if name not in _DISPATCH_IDENTITY
    }


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    return result.stdout.strip()


def _git_reports_present(repo: Path, spec: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", spec],
        check=False,
        capture_output=True,
        env=_git_env(),
    )
    return result.returncode == 0


def _build_repository(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    for path, content in ((PLAN_PATH, _PLAN), (MODULE_PATH, _MODULE)):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        _git(repo, "add", path)
    _git(
        repo,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=f@example.invalid",
        "commit",
        "-q",
        "-m",
        "feat: seed",
    )
    return repo, _git(repo, "rev-parse", "HEAD")


def _build_repository_with_a_later_path(root: Path) -> tuple[Path, str, str]:
    """Two commits where ``LATER_PATH`` appears only at the second."""
    repo, first = _build_repository(root)
    target = repo / LATER_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_LATER)
    _git(repo, "add", LATER_PATH)
    _git(
        repo,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=f@example.invalid",
        "commit",
        "-q",
        "-m",
        "feat: add later",
    )
    return repo, first, _git(repo, "rev-parse", "HEAD")


def test_missing_object_answers_none_beside_present_bytes_in_one_call(tmp_path: Path):
    repo, first, second = _build_repository_with_a_later_path(tmp_path)

    # The same path is absent at the first commit and present at the second:
    # git reports the missing object, so the reader must answer None for it.
    assert not _git_reports_present(repo, f"{first}:{LATER_PATH}")
    assert _git_reports_present(repo, f"{second}:{LATER_PATH}")
    # One call carries both specs, so the missing-object branch and the
    # present-bytes branch are exercised together.
    blobs = velocity.read_blobs(repo, [(first, LATER_PATH), (second, LATER_PATH)])

    assert blobs[(first, LATER_PATH)] is None
    assert blobs[(second, LATER_PATH)] == (repo / LATER_PATH).read_bytes()


def test_counter_and_plan_metas_read_the_same_bytes_through_one_reader(
    tmp_path: Path, monkeypatch
):
    repo, revision = _build_repository(tmp_path)

    # One reader: the counter holds the very function velocity defines.
    assert interface_counts.read_blobs is velocity.read_blobs

    seen = []
    real = velocity.read_blobs

    def spy(repo_arg, specs):
        blobs = real(repo_arg, specs)
        seen.append((list(specs), blobs))
        return blobs

    monkeypatch.setattr(velocity, "read_blobs", spy)
    monkeypatch.setattr(interface_counts, "read_blobs", spy)

    metas = velocity._plan_metas(repo, [(revision, PLAN_PATH)])
    trees = interface_counts.read_trees(repo, revision)

    # Both consumers reached the shared reader: the plan census for its one
    # spec, the counter for the revision's module set.
    specs_seen = [specs for specs, _ in seen]
    assert [(revision, PLAN_PATH)] in specs_seen
    assert any((revision, MODULE_PATH) in specs for specs, _ in seen)
    assert all(
        revision == spec_revision for specs, _ in seen for spec_revision, _ in specs
    )

    # The bytes the reader answered for the plan are the bytes on disk, and the
    # same bytes feed the plan census's parsing.
    plan_bytes = (repo / PLAN_PATH).read_bytes()
    plan_answers = [
        blobs[(revision, PLAN_PATH)]
        for specs, blobs in seen
        if (revision, PLAN_PATH) in specs
    ]
    assert plan_answers == [plan_bytes]
    assert metas[(revision, PLAN_PATH)] == ("active", "")

    # The counter parses no module of its own: it parsed the module set of the
    # same revision through the reader.
    assert MODULE_PATH in trees
    assert trees[MODULE_PATH].body[0].name == "example"


def _functions_invoking_batch() -> set[tuple[str, str]]:
    found = set()
    for path in sorted((ROOT / "reckon").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                isinstance(item, ast.Constant) and item.value == "--batch"
                for item in ast.walk(node)
            ):
                found.add((path.relative_to(ROOT).as_posix(), node.name))
    return found


def test_the_batched_read_happens_in_one_function():
    assert _functions_invoking_batch() == {("reckon/velocity.py", "read_blobs")}
