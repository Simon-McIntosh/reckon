"""A temporary repository and the driver a landing test runs ``crew complete`` against.

Several landing tests need the same three things: a bare origin and a checkout
kept apart from the suite's own tree, a run whose manifest and gate log are
already written, and a ``crew complete --land`` invocation that lands the run's
head. They live here rather than in one test module so a second test builds the
same fixture without copying it.

The same rule covers the checkout a departure test promotes into and the ledger
row a landed promotion leaves behind: a run's departure word reads the row's
head against that checkout's primary branch, so every test that words a
departure needs both, and two copies of the git setup would have to be changed
twice.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path

from click.testing import CliRunner

from reckon import _plan_html, ledger
from reckon import cli as cli_module
from tests.helpers.git import git as _git

PROJECT = "proj"
PLAN = "plan-a"

_WRAP_LOG = re.compile(r"[ \t]+>>[ \t]+(?P<log>\S+)[ \t]+2>&1[ \t]*$", re.DOTALL)


def _wrap_command_and_log(wrap: str) -> tuple[list[str], Path | None]:
    """The command a submission's ``--wrap`` runs, and the log it appends to.

    A submission wraps a command that redirects its own output into the measure
    log — ``srun … bash -c <script> >> <log> 2>&1`` — so the redirect is taken
    off the command here and applied by the caller: a step that ran the redirect
    itself would reach the scheduler the script names, which in a test is the
    fence's shim rather than a batch job.
    """
    destination = None
    match = _WRAP_LOG.search(wrap)
    if match is not None:
        destination = Path(shlex.split(match.group("log"))[0])
        wrap = wrap[: match.start()]
    tokens = shlex.split(wrap)
    if tokens and Path(tokens[0]).name == "srun":
        tokens = tokens[tokens.index("bash") :] if "bash" in tokens else tokens
    if destination is None and ">>" in tokens:
        destination = Path(tokens[tokens.index(">>") + 1])
        tokens = tokens[: tokens.index(">>")]
    return tokens, destination


def in_place_scheduler(seen: list[list[str]] | None = None):
    """Return a scheduler transport that runs the partition's work in place.

    A merged-head gate is measured on the project's compute partition, so the
    product submits a batch job whose ``--wrap`` argument is the script it wrote
    and then asks the queue whether that job ended. Only the transport is
    replaced: the script runs here as the ``bash -c`` the submission asked for,
    its output reaches the log the submission named, and the queue answers for
    the job id the submission was given, so the archive script, the gate command,
    the measurement record and the ``EXIT=`` receipt a test reads are the
    product's own. ``seen`` collects the submission argument vectors when a test
    asserts what was asked for.

    A submission carrying ``--wait`` answers with the script's own status, since
    that caller reads the status of the run rather than of the queue; a
    submission without it answers with the job id, and the queue reports the
    script's outcome as the state of that job.
    """

    outcomes: dict[str, int] = {}
    ids = iter(range(41000, 42000))

    def option(argv: list[str], name: str) -> str | None:
        for index, argument in enumerate(argv):
            if argument == name and index + 1 < len(argv):
                return argv[index + 1]
            if argument.startswith(name + "="):
                return argument.split("=", 1)[1]
        return None

    def scheduler(argv, **options):
        options.pop("check", None)
        verb = Path(str(argv[0])).name
        if verb in {"squeue", "sacct"}:
            job_id = option(argv, "-j") or ""
            state = "COMPLETED" if outcomes.get(job_id, 1) == 0 else "FAILED"
            if verb == "squeue":
                # The job ran to its end at submission, so the queue no longer
                # holds it and the accounting row is the reader that answers.
                return subprocess.CompletedProcess(argv, 1, "", "")
            return subprocess.CompletedProcess(argv, 0, f"{state}\n", "")
        if verb == "scancel":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if verb != "sbatch":
            return subprocess.run(argv, check=False, **options)
        if seen is not None:
            seen.append(argv)
        command, destination = _wrap_command_and_log(argv[argv.index("--wrap") + 1])
        completed = subprocess.run(command, check=False, **options)
        job_id = str(next(ids))
        outcomes[job_id] = completed.returncode
        if destination is not None:
            assert destination.is_file() and not destination.is_symlink()
            with destination.open("a", encoding="utf-8") as stream:
                stream.write((completed.stdout or "") + (completed.stderr or ""))
        status = completed.returncode if "--wait" in argv else 0
        return subprocess.CompletedProcess(argv, status, f"{job_id}\n", "")

    return scheduler


def install_in_place_scheduler(monkeypatch, seen: list[list[str]] | None = None):
    """Install the in-place transport on every name the product submits by.

    The product reaches the scheduler through two module names — the one the
    gate scripts run under and the one a queued submission is made with — so a
    test that replaced only one would leave the other reaching the shim.
    """
    from reckon.crew import landing_measure

    scheduler = in_place_scheduler(seen)
    monkeypatch.setattr(landing_measure, "_RUN", scheduler)
    monkeypatch.setattr(landing_measure, "_SCHEDULER_RUN", scheduler)
    return scheduler


def landed_checkout(
    home: Path, tmp_path: Path, *, project: str = PROJECT
) -> tuple[Path, str]:
    """A checkout whose project state resolves to the temporary home's own.

    A promoted run's ledger row names the checkout it lives in, and the row is
    where a departure reader takes the row's delivered head from. The config
    home's state directory is the store the fixture writes, so the checkout
    carries it as the symlink a mounted project carries in the real system: one
    row, reached by the discovery path and by the checkout's own path alike. The
    returned head is the commit the branch carries, so a row naming it is a
    landing that has already reached primary.
    """
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    project_state = home / "state" / project
    project_state.mkdir(parents=True)
    (project_state / "index.json").write_text("{}\n", encoding="utf-8")
    (repo / "docs" / "state").symlink_to(home / "state")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "chore: seed the checkout")
    return repo, _git(repo, "rev-parse", "HEAD")


def record_promoted_row(
    run_id: str, repo: Path, head: str, *, project: str = PROJECT
) -> None:
    """Write the per-run ledger file a landed promotion leaves behind.

    The row names the checkout the run was promoted into and the head its
    landing delivered. That head is on the checkout's primary branch, which is
    what a departure reader asks before it words the run promoted: a row whose
    head no branch carries is a landing still owed, and reads delivered instead.
    """
    path = ledger.run_path(project, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"run_id": run_id, "repo": str(repo), "commits": [head]})
    )


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
