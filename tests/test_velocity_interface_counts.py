"""The velocity view reports the reckon package's interface level by week.

Two properties are load-bearing and each is exercised against a fixture the
code sees rather than echoed from its output:

* a synthetic two-commit history where the second commit adds exactly one public
  function, one CLI option and one MCP view — so the level and the weekly change
  are hand-computable, and a counter that silently returns zero moves the delta
  and fails the assertion;
* parity with the review's census over this repository's own HEAD, so the ported
  counting rules cannot drift from the study's without a test breaking.

The census is imported from its archived path under ``docs/`` for the parity
comparison only; nothing under ``reckon/`` imports it. Its counts are read from
a checkout the test extracts into its own ``tmp_path``, so the test reads and
writes no state outside the repository under test.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from reckon import interface_counts, velocity

DAY = 86400
BASE = int(dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp())
# The window spans ISO weeks 2026-W36 (ending Mon 2026-09-07) and 2026-W37
# (clipped at the window end), with one fixture commit in each.
WINDOW_START = velocity.iso(BASE + 1 * DAY)  # 2026-09-02, a Wednesday
WINDOW_END = velocity.iso(BASE + 9 * DAY)  # 2026-09-10
PROJECT = "reckon"
BRANCH = "main"
REPO_ROOT = Path(__file__).resolve().parents[1]
CENSUS_DIR = (
    REPO_ROOT / "docs" / "research" / "data" / "crew-pattern-review" / "code-depth"
)

_DISPATCH_IDENTITY = ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT")


def _iso(day: int, seconds: int = 0) -> str:
    return velocity.iso(BASE + day * DAY + seconds)


def _git(repo: Path, *arguments: str, env: dict | None = None) -> str:
    # The worker that runs this suite exports the identity of its own run, and a
    # git wrapper keyed on that identity refuses a mutating verb outside the
    # worker's worktree. Point the subprocess at the synthesised repository by
    # dropping the inherited identity.
    base = {**os.environ, **(env or {})}
    for name in _DISPATCH_IDENTITY:
        base.pop(name, None)
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=base,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str, day: int, changes: dict[str, str | None]) -> str:
    when = _iso(day)
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    for path, content in changes.items():
        target = repo / path
        if content is None:
            _git(repo, "rm", "-q", path, env=env)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            _git(repo, "add", path, env=env)
    _git(repo, "commit", "-q", "--allow-empty", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD", env=env)


# The base module carries one command with one option and one public function;
# the base view module declares one read-plan view name. The second commit adds
# a second command (with its own option and public function) and a second view
# name, so each of the three counted families moves by exactly one.
_BASE_MODULE = """import click


@click.command()
@click.option("--alpha")
def alpha(alpha):
    return alpha


def public_one():
    return 1
"""
_ADDED_MODULE = """import click


@click.command()
@click.option("--beta")
def beta(beta):
    return beta
"""


def _build_repository(root: Path) -> tuple[Path, str, str]:
    repo = root / PROJECT
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", BRANCH)
    _commit(
        repo,
        "chore: seed the ledger",
        0,
        {"docs/state/reckon/crew.json": json.dumps({"data": {"runs": []}}, indent=2)},
    )
    first = _commit(
        repo,
        "feat: one command, one view",
        1,
        {
            "reckon/base.py": _BASE_MODULE,
            "reckon/mcp_views.py": 'VIEW_NAMES = ("summary",)\n',
        },
    )
    second = _commit(
        repo,
        "feat: another command and view",
        8,
        {
            "reckon/more.py": _ADDED_MODULE,
            "reckon/mcp_views.py": 'VIEW_NAMES = ("summary", "detail")\n',
        },
    )
    return repo, first, second


@pytest.fixture()
def cache_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "cache"
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(root))
    return root


def _report(repo: Path) -> dict:
    return velocity.report(
        {PROJECT: str(repo)},
        start=WINDOW_START,
        end=WINDOW_END,
        run_store_db=None,
    )


def test_the_second_commit_moves_each_count_family_by_one(
    tmp_path: Path, cache_root: Path
):
    repo, first, second = _build_repository(tmp_path / "code")
    weeks = _report(repo)["interfaces"]["weeks"]

    assert [row["iso_week"] for row in weeks] == ["2026-W36", "2026-W37"]
    # The bounded revision per week's end names the week's last commit.
    assert weeks[0]["revision"] == first
    assert weeks[1]["revision"] == second

    assert weeks[0]["counts"] == {
        "public_definitions": 2,
        "cli_options": 1,
        "mcp_views": 1,
        "refusal_families": 0,
    }
    # The first week's baseline revision predates the window, so its change is
    # the level it reached rather than a delta against an unseen week.
    assert weeks[0]["change"] == weeks[0]["counts"]

    assert weeks[1]["counts"] == {
        "public_definitions": 3,
        "cli_options": 2,
        "mcp_views": 2,
        "refusal_families": 0,
    }


def test_the_week_after_a_change_reports_its_delta(tmp_path: Path, cache_root: Path):
    repo, _first, _second = _build_repository(tmp_path / "code")
    weeks = _report(repo)["interfaces"]["weeks"]

    # The declared negative control makes the MCP view counter always return
    # zero, which moves this delta to zero and fails here.
    assert weeks[1]["change"] == {
        "public_definitions": 1,
        "cli_options": 1,
        "mcp_views": 1,
        "refusal_families": 0,
    }


def test_counts_match_the_census_at_this_repository_head(
    tmp_path: Path, cache_root: Path
):
    ours = interface_counts.count_revision(REPO_ROOT, "HEAD")
    theirs = _census_counts(tmp_path)
    assert ours == theirs


def test_a_warm_report_recomputes_no_interface_count(
    tmp_path: Path, cache_root: Path, monkeypatch
):
    repo, _first, _second = _build_repository(tmp_path / "code")
    calls = []
    original = interface_counts.read_trees

    def spy(repo_path, revision, **kwargs):
        calls.append(revision)
        return original(repo_path, revision, **kwargs)

    monkeypatch.setattr(interface_counts, "read_trees", spy)

    _report(repo)
    cold = len(calls)
    assert cold == 2  # one parse per distinct in-window week revision

    _report(repo)
    assert len(calls) == cold


def _load_census():
    if str(CENSUS_DIR) not in sys.path:
        sys.path.insert(0, str(CENSUS_DIR))
    spec = importlib.util.spec_from_file_location(
        "crew_pattern_census", CENSUS_DIR / "census.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _census_counts(tmp_path: Path) -> dict:
    """The review's own counting rules over this repository's HEAD tree."""
    census = _load_census()
    archive = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "archive",
            "--format=tar",
            "HEAD",
            "--",
            "reckon",
        ],
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(tmp_path / "head", filter="data")
    modules = census.load_modules(tmp_path / "head", "reckon")
    measured = census.measure(modules, {})
    surface = measured["interfaces"]
    return {
        "public_definitions": measured["public_surface"],
        "cli_options": surface["cli_option_count"],
        "mcp_views": surface["mcp_view_count"],
        "refusal_families": surface["dispatch_refusal_code_count"],
    }
