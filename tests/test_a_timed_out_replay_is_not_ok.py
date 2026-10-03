"""A replay that never produced an exit status is a failure, not an ok.

Measured 2026-10-03 by the coordinator: a ``reckon crew verify-gate`` replay
over a 181-file population ran past its ``--timeout-seconds`` and its JSON
reported ``"ok": true`` with ``report.exit_status`` null. The captured replay
log held only the three header lines and no runner output, so a reader taking
ok at its word read a gate that measured nothing as a gate that passed.

These cases pin the classification. A replay cut short by its bound reports
``ok: false`` with a finding naming the timeout and the seconds it ran before
it was stopped, records no passing verdict, and leaves a log that says it was
cut short. A replay that completes keeps its own exit status and verdict:
a green one reports ``ok: true`` and exits 0, while a non-zero one reports
``ok: false`` and exits non-zero, because ok reports whether the replay
finished and passed rather than only that a status was measured.

Every case runs in a temporary config home and asserts the real one gained
nothing under this run's own id.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, ledger
from reckon.cli import main as cli_main
from reckon.crew.runs import run_dir

PROJECT = "proj"
PLAN = "plan-a"
RUN_ID = "r-20261003T190000000000-timed-out-replay"
STORED_COMMAND = "sh recorded-gate.sh"
OUTLIVES_THE_BOUND = "sleep 120"
BOUND_SECONDS = 2.0
COMPLETES_GREEN = "echo gate-ran"
COMPLETES_RED = "exit 7"


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


def _real_run_directory(run_id: str) -> Path:
    """Where this run's state would land if the test escaped its config home."""
    return Path.home() / ".config" / "reckon" / "crew" / "runs" / run_id


@dataclass
class ReplayEnvironment:
    config_home: Path
    repository: Path
    run_id: str = RUN_ID

    def promote(self) -> None:
        """The committed row promotion writes for a run that passed its gate."""
        ledger.append_run(
            PROJECT,
            {
                "run_id": self.run_id,
                "plan": PLAN,
                "section": "timed-out-replay",
                "node": "timed-out-replay",
                "role": "implement",
                "gate": "passed",
                "gate_check": {
                    "command": STORED_COMMAND,
                    "exit_status": 0,
                    "log_digest": "x",
                },
            },
            root=self.repository,
        )

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

    def recorded_report(self) -> dict:
        """The re-run report as it landed on the committed ledger row."""
        row = ledger.load(PROJECT, root=self.repository)[0]["runs"][0]
        return row["integrated_gate_check"]


@pytest.fixture()
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ReplayEnvironment:
    """A temp config home and a checkout the replayed command runs against."""
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
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repository, "add", "seed.txt", "docs")
    _git(repository, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return ReplayEnvironment(config_home=config_home, repository=repository)


def _payload(result, exit_code: int = 0) -> dict:
    assert result.exit_code == exit_code, result.output
    return json.loads(result.output)


def test_a_replay_that_outlives_its_bound_reports_failure(
    environment: ReplayEnvironment,
) -> None:
    """A command that outlives a short --timeout-seconds is reported ok false,
    with a finding naming the timeout and the seconds it ran, no passing
    verdict, a non-zero exit, and a replay log that says it was cut short."""
    escaped = _real_run_directory(environment.run_id)
    assert not escaped.exists(), "a stale run directory of this run's own id is present"
    environment.promote()

    result = environment.verify_gate(
        "--command",
        OUTLIVES_THE_BOUND,
        "--timeout-seconds",
        str(BOUND_SECONDS),
    )

    payload = _payload(result, exit_code=1)
    report = payload["report"]
    assert payload["ok"] is False, (
        "a replay cut short by its bound reported ok, so a caller reading the "
        f"first field of the payload reads a gate that measured nothing as a "
        f"success: {payload}"
    )
    assert report["timed_out"] is True, report
    assert report["ran"] is True, report
    assert report["exit_status"] is None, report
    assert report["integrated_verdict"] == "not-run", report
    elapsed = report["replay_elapsed_seconds"]
    assert elapsed >= BOUND_SECONDS, (
        f"the replay was stopped by a {BOUND_SECONDS:g}s bound but reports "
        f"{elapsed!r}s elapsed: {report}"
    )

    finding = payload["finding"]
    assert finding is not None, (
        "a base-green run whose merged replay measured nothing recorded no "
        f"finding: {payload}"
    )
    assert finding["timed_out"] is True, finding
    assert finding["replay_elapsed_seconds"] >= BOUND_SECONDS, finding
    reason = str(finding["reason"])
    assert f"{BOUND_SECONDS:g}s" in reason, reason
    assert "stopped after" in reason, reason

    # No passing verdict anywhere the reader can reach: the integrated verdict
    # on the payload and on the report committed on the ledger row, both of
    # which stop at not-run.
    assert finding["integrated_verdict"] == "not-run", finding
    recorded = environment.recorded_report()
    assert recorded["ok"] is False, recorded
    assert recorded["integrated_verdict"] == "not-run", recorded
    assert recorded["timed_out"] is True, recorded

    # The log says why it stops where it does, rather than ending on the three
    # header lines a reader would take for a gate that printed nothing.
    log = Path(report["log_path"]).read_text(encoding="utf-8")
    assert "# cut short:" in log, log
    assert "EXIT=" not in log, log
    assert not escaped.exists(), "the run wrote into the real config home"


def test_a_replay_that_completes_reports_its_own_green_status(
    environment: ReplayEnvironment,
) -> None:
    """A command that completes keeps its real exit status and verdict, and is
    ok: true because a status was measured."""
    environment.promote()

    result = environment.verify_gate("--command", COMPLETES_GREEN)

    payload = _payload(result)
    report = payload["report"]
    assert payload["ok"] is True, payload
    assert report["ran"] is True, report
    assert report["timed_out"] is False, report
    assert report["exit_status"] == 0, report
    assert report["integrated_verdict"] == "passed", report
    assert report["finding"] is None, report
    assert (
        Path(report["log_path"]).read_text(encoding="utf-8").rstrip().endswith("EXIT=0")
    )


def test_a_replay_that_completes_failing_reports_its_own_failure(
    environment: ReplayEnvironment,
) -> None:
    """A completed replay's non-zero status is its verdict: ok is false and
    the verb exits non-zero, while the report keeps the real status and
    verdict and the finding names the failing verdict, not a timeout."""
    environment.promote()

    result = environment.verify_gate("--command", COMPLETES_RED)

    payload = _payload(result, exit_code=1)
    report = payload["report"]
    assert payload["ok"] is False, payload
    assert report["timed_out"] is False, report
    assert report["exit_status"] == 7, report
    assert report["integrated_verdict"] == "failed", report
    finding = payload["finding"]
    assert finding["integrated_verdict"] == "failed", finding
    assert "timed_out" not in finding, finding
    assert (
        Path(report["log_path"]).read_text(encoding="utf-8").rstrip().endswith("EXIT=7")
    )


def test_the_run_directory_written_is_the_temporary_one(
    environment: ReplayEnvironment,
) -> None:
    """The replay log lands under the temporary config home, not the real one."""
    environment.promote()

    result = environment.verify_gate("--command", COMPLETES_GREEN)

    log_path = Path(_payload(result)["report"]["log_path"]).resolve()
    assert log_path.is_relative_to(environment.config_home.resolve()), log_path
    assert log_path.parent == (run_dir(environment.run_id)).resolve(), log_path
    assert not _real_run_directory(environment.run_id).exists()
