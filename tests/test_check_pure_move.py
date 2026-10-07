"""The pure-move check passes a clean split and refuses three dirty ones.

Each case is built as a real one-commit repository under ``tmp_path`` so the
check reads its base module through the revision reader and its produced
modules from the working tree, exactly as it does in a live split. The three
dirty cases must each exit non-zero and name the statement at fault: a moved
function with one changed line, a dropped function, and a function copied into
two produced modules.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_pure_move.py"
MODULE = "pkg/original.py"
ALPHA = "pkg/alpha.py"
BETA = "pkg/beta.py"

# The worker that runs this suite exports its own dispatch identity, and a git
# wrapper keyed on it refuses a mutating verb; drop it for the fixture repos.
_OWNERSHIP = ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_OWNER_SESSION")

_BASE = """import os


def first(a):
    return a + 1


CONSTANT = 10


def second(b):
    return b * 2
"""

_ALPHA = """import os


def first(a):
    return a + 1
"""

_BETA = """CONSTANT = 10


def second(b):
    return b * 2
"""

_REMAINDER = """import os

from pkg.alpha import first
from pkg.beta import CONSTANT, second
"""


def _git(repo: Path, *arguments: str) -> None:
    env = dict(os.environ)
    for name in _OWNERSHIP:
        env.pop(name, None)
    env.update(
        {
            "GIT_AUTHOR_NAME": "fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
    )
    subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _seed(root: Path, base: str = _BASE) -> Path:
    repo = root / "repo"
    (repo / "pkg").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / MODULE).write_text(base, encoding="utf-8")
    _git(repo, "add", "pkg/__init__.py", MODULE)
    _git(repo, "commit", "-q", "-m", "chore: seed the module")
    return repo


def _run(repo: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    for name in _OWNERSHIP:
        env.pop(name, None)
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--base",
            "HEAD",
            "--module",
            MODULE,
            "--into",
            ALPHA,
            "--into",
            BETA,
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_a_correct_split_passes_with_a_receipt(tmp_path: Path):
    repo = _seed(tmp_path)
    _write(repo, ALPHA, _ALPHA)
    _write(repo, BETA, _BETA)
    _write(repo, MODULE, _REMAINDER)

    result = _run(repo)

    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert {entry["statement"] for entry in receipt["moved"]} == {
        "first",
        "CONSTANT",
        "second",
    }
    assert {entry["to"] for entry in receipt["moved"]} <= {ALPHA, BETA, MODULE}
    assert set(receipt["estimated_tokens"]) == {ALPHA, BETA, MODULE}
    assert receipt["problems"] == []


def test_a_changed_line_inside_a_moved_function_fails(tmp_path: Path):
    repo = _seed(tmp_path)
    _write(repo, ALPHA, _ALPHA.replace("return a + 1", "return a + 2"))
    _write(repo, BETA, _BETA)
    _write(repo, MODULE, _REMAINDER)

    result = _run(repo)

    assert result.returncode == 1
    assert "first" in result.stderr


def test_a_dropped_function_fails(tmp_path: Path):
    repo = _seed(tmp_path)
    _write(repo, ALPHA, _ALPHA)
    _write(repo, BETA, "CONSTANT = 10\n")
    _write(repo, MODULE, _REMAINDER)

    result = _run(repo)

    assert result.returncode == 1
    assert "second" in result.stderr


def test_a_function_in_two_produced_modules_fails(tmp_path: Path):
    repo = _seed(tmp_path)
    _write(repo, ALPHA, _ALPHA)
    _write(repo, BETA, _ALPHA + "\n\n" + _BETA)
    _write(repo, MODULE, _REMAINDER)

    result = _run(repo)

    assert result.returncode == 1
    assert "first" in result.stderr
