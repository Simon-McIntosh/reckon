"""A gate re-run's bound follows the replayed gate's recorded duration.

Measured 2026-09-29: a worker's gate log recorded its suite finishing in 378 s,
and every verify-gate of that run reported not-run on the integrated revision
because the replay stopped at a fixed 300 s bound. A gate that legitimately
takes longer than the constant could never be verified as recorded.

These cases pin the three inputs the bound resolves from: a run whose recorded
gate outran the default is replayed under a bound derived from that duration,
a run whose log records no duration keeps the fixed default, and an explicit
--timeout-seconds is taken as given. The replayed command's duration is stubbed
rather than waited out: the stub's virtual duration sits above the fixed 300 s
default and below the bound derived from the recorded duration, so the verdict
alone says which bound reached the process without a real multi-minute gate.

Every case runs in a temporary config home and asserts the real one gained
nothing under this run's own id.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import _plan_html, ledger
from reckon.cli import main as cli_main
from reckon.crew import promotion
from reckon.crew.runs import run_dir

PROJECT = "proj"
PLAN = "plan-a"
RUN_ID = "r-20260929T120000000000-replay-bound"
STORED_COMMAND = "sh recorded-gate.sh"

# The duration the run's own gate log records: longer than the fixed default,
# so the derived bound and the constant disagree on it.
RECORDED_GATE_SECONDS = 378.29
GATE_LOG_BODY = (
    f"# gate log\n213 passed in {RECORDED_GATE_SECONDS}s (0:06:18)\nEXIT=0\n"
)
# The stub's virtual duration: longer than the fixed 300 s default, shorter
# than the bound derived from the recorded duration. A run of exactly this
# length would pass under one bound and time out under the other.
STUBBED_REPLAY_SECONDS = 330.0


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


def _stub_replay_duration(monkeypatch: pytest.MonkeyPatch) -> list[float | None]:
    """Replace the replayed command's duration with a virtual one.

    The gate replay is the one ``sh -c`` call this path makes; every other
    subprocess call (git, and the landing commit) is delegated unchanged. A
    replay whose virtual duration exceeds the bound raises the same
    ``TimeoutExpired`` a real one would, so the resolved bound is the only
    thing that decides the verdict and no real multi-minute gate runs. Returns
    the bounds the stub was handed, in order.
    """
    real_run = subprocess.run
    observed: list[float | None] = []

    def fake_run(argv, *args: Any, **kwargs: Any):
        if list(argv[:2]) == ["sh", "-c"]:
            timeout = kwargs.get("timeout")
            observed.append(timeout)
            if timeout is not None and timeout < STUBBED_REPLAY_SECONDS:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout, output="")
            return subprocess.CompletedProcess(
                argv, 0, stdout=f"1 passed in {STUBBED_REPLAY_SECONDS:.2f}s\n"
            )
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return observed


@dataclass
class ReplayEnvironment:
    config_home: Path
    repository: Path
    run_id: str = RUN_ID

    def promote(self, *, gate_check: dict | None) -> None:
        """The committed row promotion writes for a run that passed its gate."""
        record: dict = {
            "run_id": self.run_id,
            "plan": PLAN,
            "section": "replay-bound",
            "node": "replay-bound",
            "role": "implement",
            "gate": "passed",
        }
        if gate_check is not None:
            record["gate_check"] = gate_check
        ledger.append_run(PROJECT, record, root=self.repository)

    def preserved_gate_log(self, body: str = GATE_LOG_BODY) -> Path:
        """The log promotion preserves under the run directory."""
        path = run_dir(self.run_id) / "gate.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def gate_check_citing(self, log: Path | None) -> dict:
        check = {"command": STORED_COMMAND, "exit_status": 0}
        if log is not None:
            check["log_path"] = str(log)
        return check

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


@pytest.fixture()
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ReplayEnvironment:
    """A temp config home and a checkout the replayed gate runs against."""
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


def _report(result) -> dict:
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["report"]


def test_a_recorded_gate_longer_than_the_default_derives_a_bound_above_it(
    environment: ReplayEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose gate log records 378 s is replayed under a bound derived
    from that duration, so a stub whose virtual duration sits above the fixed
    300 s default finishes and reports passed rather than not-run."""
    escaped = _real_run_directory(environment.run_id)
    assert not escaped.exists(), "a stale run directory of this run's own id is present"
    observed = _stub_replay_duration(monkeypatch)
    log = environment.preserved_gate_log()
    environment.promote(gate_check=environment.gate_check_citing(log))

    result = environment.verify_gate()

    report = _report(result)
    assert report["integrated_verdict"] == "passed", (
        f"a stub whose virtual duration ({STUBBED_REPLAY_SECONDS:g}s) sits above "
        f"the fixed 300s bound and below the bound derived from the recorded "
        f"gate ({RECORDED_GATE_SECONDS}s) was reported "
        f"{report['integrated_verdict']}: {report}"
    )
    assert report["replay_bound_source"] == "derived", report
    assert report["recorded_gate_seconds"] == pytest.approx(RECORDED_GATE_SECONDS)
    bound = report["replay_bound_seconds"]
    assert bound > RECORDED_GATE_SECONDS, (
        f"the derived bound {bound} does not clear the recorded duration "
        f"{RECORDED_GATE_SECONDS}: {report}"
    )
    assert bound > STUBBED_REPLAY_SECONDS
    assert bound < promotion._REPLAY_BOUND_CEILING_SECONDS, report
    # The bound reached the process, not only the record.
    assert observed == [bound], observed
    # The bound is on the committed row too, so a reader of the durable record
    # can see which bound applied without the CLI's own output.
    row = ledger.load(PROJECT, root=environment.repository)[0]["runs"][0]
    recorded = row["integrated_gate_check"]
    assert recorded["replay_bound_source"] == "derived"
    assert recorded["replay_bound_seconds"] == bound
    assert not escaped.exists(), "the run wrote into the real config home"


def test_a_run_with_no_recorded_duration_keeps_the_default_bound(
    environment: ReplayEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cited log that carries no runner duration leaves the bound at the
    fixed default, which still bounds the replay: the same stub that passes
    under the derived bound is reported not-run."""
    observed = _stub_replay_duration(monkeypatch)
    log = environment.preserved_gate_log("# gate log\nEXIT=0\n")
    environment.promote(gate_check=environment.gate_check_citing(log))

    result = environment.verify_gate()

    report = _report(result)
    assert report["replay_bound_source"] == "default", report
    assert report["replay_bound_seconds"] == pytest.approx(300.0)
    assert report["recorded_gate_seconds"] is None
    assert report["integrated_verdict"] == "not-run", report
    assert observed == [300.0], observed


def test_an_explicit_timeout_overrides_the_derived_bound(
    environment: ReplayEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--timeout-seconds names the bound outright: the same run that passes
    under the derived bound is reported not-run when the caller names 120 s,
    and the report records the explicit source and value."""
    observed = _stub_replay_duration(monkeypatch)
    log = environment.preserved_gate_log()
    environment.promote(gate_check=environment.gate_check_citing(log))

    result = environment.verify_gate("--timeout-seconds", "120")

    report = _report(result)
    assert report["replay_bound_source"] == "explicit", report
    assert report["replay_bound_seconds"] == pytest.approx(120.0)
    # The derivation ran and is reported, but the explicit value is the bound.
    assert report["recorded_gate_seconds"] == pytest.approx(RECORDED_GATE_SECONDS)
    assert report["integrated_verdict"] == "not-run", report
    assert observed == [120.0], observed
