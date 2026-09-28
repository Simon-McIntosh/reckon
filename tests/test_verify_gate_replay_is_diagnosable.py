"""A replayed gate names its own tree and keeps its own output.

Measured 2026-09-27: a stored gate command that opened with ``env -C <worker
worktree>`` and named its tests under that tree was replayed after promotion
released the worktree, so ``env`` exited 125 and the ledger recorded the merged
tree as failing a gate that had passed when run from the checkout. Measured
2026-09-28: verify-gate recorded exit 1 for a replayed gate and kept only the
status, so the failing ids behind it and the runner's stderr were unrecoverable
and a dedicated node had to re-measure the same two files to find out why.

These cases pin both halves of the repair: the replay rewrites each recorded
worker worktree root to the checkout it is replaying at, and its captured
stdout and stderr land in a log under the run directory whose path the report
cites. Every case runs in a temporary config home and asserts the real one
gained nothing under this run's own id.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, ledger
from reckon.cli import main as cli_main
from reckon.crew.runs import run_dir

PROJECT = "proj"
PLAN = "plan-a"
RUN_ID = "r-20260928T120000000000-replayed-gate"

PASSING_PROBE = "tests/test_replay_probe.py"
FAILING_PROBE = "tests/test_replay_probe_fails.py"

# The probe writes the marker into the directory the gate ran in, so its
# presence on the checkout is the receipt that the replay executed there rather
# than only that it exited zero.
PASSING_TEST = (
    "from pathlib import Path\n"
    "\n"
    "\n"
    "def test_the_replayed_gate_runs_where_it_was_pointed():\n"
    '    Path("replay-ran-here.marker").write_text("the replayed gate ran here\\n")\n'
)
FAILING_TEST = (
    "def test_the_replayed_gate_reports_its_failure():\n"
    "    raise AssertionError('the replayed gate fails')\n"
)
STDERR_MARKER = "replay-stderr-marker"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


def _write_probes(root: Path) -> None:
    """The two probe tests, written identically into the checkout and the worktree."""
    (root / PASSING_PROBE).parent.mkdir(parents=True, exist_ok=True)
    (root / PASSING_PROBE).write_text(PASSING_TEST, encoding="utf-8")
    (root / FAILING_PROBE).write_text(FAILING_TEST, encoding="utf-8")


def _real_run_directory(run_id: str) -> Path:
    """Where this run's replay log would land if the test escaped its config home."""
    return Path.home() / ".config" / "reckon" / "crew" / "runs" / run_id


@dataclass
class ReplayEnvironment:
    config_home: Path
    repository: Path
    worktree: Path
    run_id: str = RUN_ID

    def promote(self, *, command: str) -> None:
        """The committed row promotion writes: the gate it recorded, and the
        worktree audit of the tree that command was authored in."""
        ledger.append_run(
            PROJECT,
            {
                "run_id": self.run_id,
                "plan": PLAN,
                "section": "replayed-gate",
                "node": "replayed-gate",
                "role": "implement",
                "gate": "passed",
                "gate_check": {
                    "command": command,
                    "exit_status": 0,
                    "log_digest": "x",
                },
                "release": {
                    "worktree_released": True,
                    "worktree_audit": {
                        "counts": {},
                        "worktrees": [
                            {
                                "path": str(self.worktree),
                                "available": False,
                                "detail": "tree is no longer available",
                            }
                        ],
                    },
                },
            },
            root=self.repository,
        )

    def stored_command(self, probe: str, *, stderr_marker: bool = False) -> str:
        """A gate that pins the worker worktree in both of the ways measured:
        the directory it runs in, and the absolute path of every test it names."""
        command = (
            f"env -C {self.worktree} {sys.executable} -m pytest "
            f"-p no:cacheprovider -q {self.worktree}/{probe}"
        )
        if stderr_marker:
            command += f"; rc=$?; printf '{STDERR_MARKER}\\n' >&2; exit $rc"
        return command

    def verify_gate(self, *extra: str):
        return CliRunner().invoke(
            cli_main,
            [
                "crew",
                "verify-gate",
                "--project",
                PROJECT,
                "--run",
                self.run_id,
                "--checkout-path",
                str(self.repository),
                *extra,
            ],
        )

    def replay_log(self, report: dict) -> Path:
        """The log the report cites, checked to sit under the temp home's run dir."""
        log_path = report.get("log_path")
        assert log_path, f"the report cites no replay log: {report}"
        path = Path(log_path)
        assert path.is_file(), f"the cited replay log does not resolve: {path}"
        directory = run_dir(self.run_id).resolve()
        assert path.resolve().is_relative_to(directory), (
            f"the replay log {path} is not under the run directory {directory}"
        )
        assert str(self.config_home) in log_path, (
            f"the replay log {log_path} does not sit under the temporary config "
            f"home {self.config_home}, so the run wrote outside the test's own home"
        )
        return path


@pytest.fixture()
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ReplayEnvironment:
    """A temp config home, a checkout carrying the probes, and a worker worktree."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repository = tmp_path / "checkout"
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_resource(
        repository / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(repository, *arguments)
    _write_probes(repository)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repository, "add", "seed.txt", "tests", "docs")
    _git(repository, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    worktree = tmp_path / "worktrees" / "replayed-gate"
    _write_probes(worktree)
    return ReplayEnvironment(
        config_home=config_home, repository=repository, worktree=worktree
    )


def test_the_replay_rewrites_a_released_worktree_root_to_the_checkout(
    environment: ReplayEnvironment,
) -> None:
    """A gate whose command pins a worker worktree whose directory the release
    step removed replays at the checkout, so the merged tree is verified and
    reports the probe's own status rather than ``env``'s 125."""
    escaped = _real_run_directory(environment.run_id)
    assert not escaped.exists(), "a stale run directory of this run's own id is present"
    stored = environment.stored_command(PASSING_PROBE)
    environment.promote(command=stored)
    shutil.rmtree(environment.worktree)
    assert not environment.worktree.exists(), "the released worktree still resolves"

    result = environment.verify_gate()

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)["report"]
    assert report["exit_status"] == 0, (
        f"the replay of a gate pinned to a removed worktree reported "
        f"{report['exit_status']} (125 is the shell refusing to enter the "
        f"directory it named) instead of running at the checkout: {report}"
    )
    assert report["integrated_verdict"] == "passed"
    assert report["worktree_roots_rewritten"] == [str(environment.worktree)], (
        f"the recorded worktree root was not rewritten: {report}"
    )
    # The recorded text is still the record; the rewrite is what ran. The
    # marker is the executed-command evidence: only the checkout can hold it,
    # because the tree the command names no longer exists.
    assert report["gate_command"] == stored
    assert (environment.repository / "replay-ran-here.marker").is_file(), (
        "the probe wrote no marker in the checkout, so the gate ran elsewhere"
    )
    log = environment.replay_log(report).read_text(encoding="utf-8")
    # The header records the rewrite, so the worktree is named there by design;
    # it is the command that ran that must not name it.
    command_line = next(
        line for line in log.splitlines() if line.startswith("# command: ")
    )
    assert str(environment.repository) in command_line
    assert str(environment.worktree) not in command_line
    assert log.rstrip().endswith("EXIT=0")
    assert not escaped.exists(), "the run wrote into the real config home"


def test_a_failing_replay_keeps_ids_and_stderr_in_a_log_beside_the_run(
    environment: ReplayEnvironment,
) -> None:
    """A gate that fails at the merged tree records a log under the run
    directory, and that log carries the failing id, the runner's stderr and the
    exit status, so the record can be acted on without re-measuring."""
    escaped = _real_run_directory(environment.run_id)
    assert not escaped.exists(), "a stale run directory of this run's own id is present"
    stored = environment.stored_command(FAILING_PROBE, stderr_marker=True)
    environment.promote(command=stored)
    shutil.rmtree(environment.worktree)

    result = environment.verify_gate()

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)["report"]
    assert report["exit_status"] == 1, report
    assert report["integrated_verdict"] == "failed"
    assert report["worktree_roots_rewritten"] == [str(environment.worktree)], report
    log = environment.replay_log(report).read_text(encoding="utf-8")
    assert "test_the_replayed_gate_reports_its_failure" in log
    assert STDERR_MARKER in log, f"the replay's stderr reached no log: {log}"
    assert log.rstrip().endswith("EXIT=1")
    # The finding a coordinator reads carries the ids the replay's own log
    # enumerated, so a failure is actionable from the row alone.
    assert report["finding"] is not None
    assert report["finding"]["exit_status"] == 1
    ids = report["finding"].get("failure_ids", [])
    assert any(
        entry.endswith("::test_the_replayed_gate_reports_its_failure") for entry in ids
    ), f"the finding names none of the log's failing ids: {report['finding']}"
    # The report is committed on the row, so the log's path is readable from
    # the durable record as well as from the payload.
    row = ledger.load(PROJECT, root=environment.repository)[0]["runs"][0]
    recorded = row["integrated_gate_check"]
    assert recorded["log_path"] == report["log_path"]
    assert not escaped.exists(), "the run wrote into the real config home"
