"""A temporary repository and the driver a landing test runs ``crew complete`` against.

Several landing tests need the same three things: a bare origin and a checkout
kept apart from the suite's own tree, a run whose manifest and gate log are
already written, and a ``crew complete --land`` invocation that lands the run's
head. They live here rather than in one test module so a second test builds the
same fixture without copying it.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from click.testing import CliRunner

from reckon import _plan_html
from reckon import cli as cli_module
from tests.helpers.git import git as _git

PROJECT = "proj"
PLAN = "plan-a"


def in_place_scheduler(seen: list[list[str]] | None = None):
    """Return a scheduler transport that runs the partition's script in place.

    A merged-head gate is measured on the project's compute partition, so
    acceptance submits a batch job whose ``--wrap`` argument is the script the
    product wrote. Only the transport is replaced: the script runs here as the
    ``bash -c`` the submission asked for, and its output is appended to the log
    the submission named, so the archive script, the gate command and the
    ``EXIT=`` receipt a test reads are the product's own. ``seen`` collects the
    argument vectors when a test asserts what was asked for.
    """

    def scheduler(argv, **options):
        options.pop("check", None)
        if argv[0] != "sbatch":
            return subprocess.run(argv, check=False, **options)
        if seen is not None:
            seen.append(argv)
        step = shlex.split(argv[argv.index("--wrap") + 1])
        script = step[step.index("-c") + 1]
        destination = Path(step[step.index(">>") + 1])
        assert destination.is_file() and not destination.is_symlink()
        completed = subprocess.run(["bash", "-c", script], check=False, **options)
        with destination.open("a", encoding="utf-8") as stream:
            stream.write((completed.stdout or "") + (completed.stderr or ""))
        return subprocess.CompletedProcess(argv, completed.returncode, "12345\n", "")

    return scheduler


def _write_origin_and_checkout(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "commit.gpgsign", "false")
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    plan = repository / "docs" / "plans" / f"{PLAN}.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": PLAN,
                "title": "One call lands a run",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repository, "add", "docs", "seed.txt")
    _git(repository, "commit", "-q", "-m", "test: seed repository")
    _git(repository, "remote", "add", "origin", str(origin))
    _git(repository, "push", "-q", "-u", "origin", "main")
    return origin, repository


def _complete_land(run: dict, *extra: str):
    return CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "complete",
            "--wait",
            "--run",
            run["run_id"],
            "--gate",
            "passed",
            "--commit",
            run["head"],
            "--outcome",
            "one call promoted, merged, pushed and released the run",
            "--checkout-path",
            str(run["repository"]),
            "--no-impl-change",
            "the fixture lands one file and moves no plan",
            "--gate-command",
            "true",
            "--gate-exit-status",
            "0",
            "--gate-log-path",
            str(run["gate_log"]),
            "--land",
            *extra,
        ],
    )
