"""A run's recorded dispatch overrides have exactly one reader.

A dispatch may answer a ``--set`` override that changes a resolved flight value.
The answer is recorded on the run as ``dispatch_options.overrides``. Every
reader that resolves a run's flight config under that recorded policy — a plain
resume, a backend move and the queued-admission pass — reads it through one
owner, ``reckon.crew.runs.recorded_dispatch_overrides``, so the copies cannot
drift.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew
from reckon.crew import queue_admission, runs

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

SENTINEL = "backends.alpha.model=sentinel-model"


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


def test_the_owner_reads_a_records_overrides_for_every_shape() -> None:
    """The owner distinguishes overrides, an empty options block and no options."""
    assert runs.recorded_dispatch_overrides(
        {"dispatch_options": {"overrides": ["a", "b"]}}
    ) == ("a", "b")
    assert runs.recorded_dispatch_overrides({"dispatch_options": {}}) == ()
    assert runs.recorded_dispatch_overrides({}) == ()


def test_every_reader_resolves_its_config_through_the_owner(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patching the owner's answer reaches every reader's resolved config."""
    monkeypatch.setattr(runs, "recorded_dispatch_overrides", lambda record: (SENTINEL,))

    admission_config = queue_admission._resolve_lane_config(
        {
            "run_id": "r-owner-admission",
            "project": "sample",
            "repo": str(config_home / "repo"),
            "dispatch_options": {},
        }
    )
    assert admission_config["backends"]["alpha"]["model"] == "sentinel-model"

    seen = _capture_resolution(monkeypatch)
    _record_run(config_home, "r-owner-move", overrides=[])
    runner = CliRunner()

    assert _resume_backend(runner, "r-owner-move").exit_code == 0
    assert seen["change_lane"]["backends"]["alpha"]["model"] == "sentinel-model"

    assert _plain_resume(runner, "r-owner-move").exit_code == 0
    assert seen["resume_plan"]["backends"]["alpha"]["model"] == "sentinel-model"

    # The folded name resolves nothing of its own: it answers with the
    # successor, so it can never become a third reader of the record.
    retired = _retired_redispatch(runner, "r-owner-move")
    assert retired.exit_code != 0
    assert json.loads(retired.output)["successor"] == "crew resume --backend"
