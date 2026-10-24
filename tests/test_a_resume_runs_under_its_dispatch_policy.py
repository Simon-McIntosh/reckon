"""A resume resolves its flight config under the policy its run was dispatched with.

A dispatch may answer a ``--set`` override that changes a resolved flight value.
That answer is recorded on the run, and every way a run moves — a plain resume
and a backend move — must resolve the flight config under it, through one shared
reading. Otherwise a run resumed after an outage would come back under the base
policy while the same run moved to another lane keeps the policy it was launched
under.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew
from reckon.crew import runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")

HOST_FLIGHT = """\
default_backend: alpha
backends:
  alpha:
    launch: cli
    command: codex
    model: base-model
    effort: high
    sandbox: worktree-full
    session_reuse: true
    time_budget: 25m
  beta:
    launch: cli
    command: codex
    model: second-model
    effort: high
    sandbox: worktree-full
    session_reuse: true
    time_budget: 25m
roles:
  implement: {}
fences:
  time_budget: 25m
  needs_help_after_failures: 2
"""

OVERRIDE = "backends.alpha.model=overridden-model"


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary config home holding one host flight layer, no real state."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    flight_file = home / "flight.yaml"
    flight_file.write_text(HOST_FLIGHT, encoding="utf-8")
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(flight_file))
    return home


def _record_run(config_home: Path, run_id: str, *, overrides: list[str]) -> None:
    """Write a live pointer for a dispatched run, recording its overrides."""
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "sample",
            "repo": str(config_home / "repo"),
            "node": {"id": run_id},
            "phase": "blocked",
            "backend": "alpha",
            "launch": "cli",
            "dispatch_options": {"overrides": overrides},
        },
    )


def _capture_resolution(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Intercept both move operations and record the config each was handed."""
    seen: dict = {}

    def fake_change_lane(run_id, backend, reason, **kwargs):
        seen["change_lane"] = kwargs["config"]
        return {"run_id": run_id, "lane_change": {"to_backend": backend}}

    def fake_resume_plan(run_id, advice, *, config=None):
        seen["resume_plan"] = config

        class _Plan:
            def as_dict(self) -> dict:
                return {"run_id": run_id}

        return _Plan()

    monkeypatch.setattr(dispatch_module, "change_lane", fake_change_lane)
    monkeypatch.setattr(crew, "resume_plan", fake_resume_plan)
    return seen


def _resume_backend(runner: CliRunner, run_id: str):
    return runner.invoke(
        cli_module.main,
        [
            "crew",
            "resume",
            "--run",
            run_id,
            "--advice",
            "continue",
            "--backend",
            "beta",
            "--reason",
            "another lane has room now",
            "--print-only",
        ],
    )


def _plain_resume(runner: CliRunner, run_id: str):
    return runner.invoke(
        cli_module.main,
        ["crew", "resume", "--run", run_id, "--advice", "continue", "--print-only"],
    )


def _retired_redispatch(runner: CliRunner, run_id: str):
    """The folded name, answered by a tombstone rather than a second path."""
    return runner.invoke(
        cli_module.main,
        [
            "crew",
            "redispatch",
            "--run",
            run_id,
            "--backend",
            "beta",
            "--reason",
            "another lane has room now",
            "--print-only",
        ],
    )


def test_resume_resolves_the_overrides_its_dispatch_recorded(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_resolution(monkeypatch)
    _record_run(config_home, "r-overridden", overrides=[OVERRIDE])
    runner = CliRunner()

    assert _resume_backend(runner, "r-overridden").exit_code == 0
    assert seen["change_lane"]["backends"]["alpha"]["model"] == "overridden-model"

    assert _plain_resume(runner, "r-overridden").exit_code == 0
    assert seen["resume_plan"]["backends"]["alpha"]["model"] == "overridden-model"

    # The retired spelling reaches the same command through a tombstone, so it
    # resolves no policy of its own and cannot drift from the one above.
    retired = _retired_redispatch(runner, "r-overridden")
    assert retired.exit_code != 0
    assert json.loads(retired.output)["successor"] == "crew resume --backend"


def test_a_run_without_recorded_overrides_resolves_the_base_value(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_resolution(monkeypatch)
    _record_run(config_home, "r-plain", overrides=[])
    runner = CliRunner()

    assert _resume_backend(runner, "r-plain").exit_code == 0
    assert seen["change_lane"]["backends"]["alpha"]["model"] == "base-model"

    assert _plain_resume(runner, "r-plain").exit_code == 0
    assert seen["resume_plan"]["backends"]["alpha"]["model"] == "base-model"


def test_both_move_commands_read_the_overrides_through_one_helper(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded-overrides reading is one helper, called by every way a run
    moves; the retired spelling adds no second reading."""
    calls: list[dict] = []
    real = runs.recorded_dispatch_overrides

    def spy(record: dict):
        calls.append(record)
        return real(record)

    monkeypatch.setattr(runs, "recorded_dispatch_overrides", spy)
    _capture_resolution(monkeypatch)
    _record_run(config_home, "r-one-helper", overrides=[OVERRIDE])
    runner = CliRunner()

    assert _resume_backend(runner, "r-one-helper").exit_code == 0
    assert _plain_resume(runner, "r-one-helper").exit_code == 0

    assert len(calls) == 2
    assert calls[0]["dispatch_options"]["overrides"] == [OVERRIDE]
    assert calls[1]["dispatch_options"]["overrides"] == [OVERRIDE]

    # The folded name refuses and names the successor, so it reads nothing.
    retired = _retired_redispatch(runner, "r-one-helper")
    assert retired.exit_code != 0
    assert json.loads(retired.output)["successor"] == "crew resume --backend"
    assert len(calls) == 2
