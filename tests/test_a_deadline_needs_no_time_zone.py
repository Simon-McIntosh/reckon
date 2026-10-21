"""The deadline statement answers the time left without a zone conversion.

A worker told which clock to read still compares two clock readings. Measured
2026-10-19: a run whose prompt stated a UTC deadline and told it to check with
``date -u`` read the host's file times and log stamps, which print two hours
ahead of UTC, as if they were UTC, judged its deadline spent an hour early and
stopped. The fix is to stop asking for a clock reading at all: the statement now
carries one shell command that prints the seconds left from epoch arithmetic —
the deadline's epoch less the current epoch — so the answer is the same whatever
the host's ``TZ`` is, and tells the worker to judge the deadline by that command
alone.

These tests build the statement, extract the command it states, and run it under
two time zones to show the number does not move. They also drive the real
``dispatch()`` prompt with the grace multiple patched at the one owner it reads,
so a first attempt states the hard stop at twice the budget whatever the raw
configuration value says.

The declared negative controls are applied by mutation, not by this file: make
the command print ``date`` rather than the epoch difference, and the two-zone
case must fail; drop the command from the statement, and the extraction case
must fail.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew.dispatch_sessions import _restate_time_fence
from reckon.crew.node import TaskNode
from reckon.crew.prompts import time_fence_statement

dispatch_module = importlib.import_module("reckon.crew.dispatch")

LAUNCH = "2026-10-19T15:07:32Z"
BUDGET = "120m"
DEADLINE = "2026-10-19T17:07:32Z"
HARD_STOP = "2026-10-19T19:07:32Z"

RESUMED_LAUNCH = "2026-10-19T16:00:00Z"
RESUMED_DEADLINE = "2026-10-19T18:00:00Z"

PROJECT = "proj"


# ── Reading the statement: the command, and the seconds it prints ────────────


def _stated_command(statement: str) -> str:
    """The one seconds-left command the statement carries, verbatim.

    A statement that states none is the failure the extraction case exists to
    catch, so it is reported rather than returned as nothing.
    """
    for line in statement.splitlines():
        stripped = line.strip()
        if stripped.startswith("echo "):
            return stripped
    raise AssertionError(f"the statement states no seconds-left command:\n{statement}")


def _run(command: str, tz: str) -> str:
    """Run the stated command through a shell with ``tz`` set, returning stdout."""
    completed = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        env={**os.environ, "TZ": tz},
        check=True,
    )
    return completed.stdout.strip()


def _figures(stdout: str) -> list[int]:
    return [int(token) for token in stdout.split()]


def _epoch(instant: str) -> int:
    return int(datetime.fromisoformat(instant).timestamp())


# ── The stated command reads the same answer in any zone ─────────────────────


def test_the_stated_command_prints_the_same_seconds_in_two_zones():
    """The two readings agree within a second, and not merely within a zone.

    Each run of the command reads the clock at its own instant, so two runs
    that straddle a second boundary print figures one apart; that one second
    carries no information about the zone. A zone-dependent command instead
    differs by the offset, 3600 seconds or more, so the comparison tolerates
    the straddle and still fails on any offset of an hour or more.
    """
    statement = time_fence_statement(time_budget=BUDGET, launch_instant=LAUNCH)
    command = _stated_command(statement)

    utc = _figures(_run(command, "UTC"))
    paris = _figures(_run(command, "Europe/Paris"))

    assert len(utc) == len(paris), (
        "the command prints a different number of figures by zone (UTC "
        f"{utc!r}, Paris {paris!r})"
    )
    assert utc, "the command prints no figure to compare"
    drift = max(abs(a - b) for a, b in zip(utc, paris, strict=True))
    assert drift <= 1, (
        "the command reads differently by zone, so a worker comparing one of "
        f"its readings to the deadline is wrong by the offset (UTC {utc!r}, "
        f"Paris {paris!r})"
    )


def test_the_stated_seconds_are_the_deadlines_epoch_less_now():
    statement = time_fence_statement(time_budget=BUDGET, launch_instant=LAUNCH)
    command = _stated_command(statement)

    seconds = _figures(_run(command, "UTC"))

    assert len(seconds) == 1
    assert (
        abs(seconds[0] - (_epoch(DEADLINE) - int(datetime.now(tz=UTC).timestamp())))
        <= 2
    )


# ── A grace multiple adds the hard stop's seconds 3600 later ─────────────────


def test_the_command_prints_the_hard_stop_a_further_3600_seconds_out():
    statement = time_fence_statement(
        time_budget=BUDGET, launch_instant=LAUNCH, grace_multiple=1.5
    )
    command = _stated_command(statement)

    figures = _figures(_run(command, "UTC"))

    assert len(figures) == 2, (
        "the command prints no figure for a hard stop that differs from the "
        f"deadline (printed {figures!r})"
    )
    assert abs((figures[1] - figures[0]) - 3600) <= 2, (
        "the hard stop's figure does not fall 3600 seconds past the deadline's "
        f"(printed {figures!r})"
    )


# ── The statement judges the deadline only by the command ────────────────────


def test_the_statement_judges_the_deadline_only_by_the_command():
    statement = time_fence_statement(time_budget=BUDGET, launch_instant=LAUNCH)
    flat = " ".join(statement.split())

    assert "Judge the deadline only by this one command" in flat
    assert "file time" in flat
    assert "log stamp" in flat
    assert "local zone" in flat


# ── A resumed attempt states the command from its own launch ─────────────────


def test_a_resumed_fence_states_the_command_from_its_own_launch():
    resumed = _restate_time_fence(
        "the coordinator's advice",
        {"node": {"time_budget": BUDGET}},
        attempt_started_at=RESUMED_LAUNCH,
    )

    command = _stated_command(resumed)
    own = _stated_command(
        time_fence_statement(time_budget=BUDGET, launch_instant=RESUMED_LAUNCH)
    )

    assert command == own, (
        "the resumed fence states a command measured from another attempt's "
        f"launch (resumed {command!r}, own {own!r})"
    )
    assert RESUMED_DEADLINE[:-1] in command
    assert DEADLINE[:-1] not in command, (
        "the resumed fence still carries the first attempt's deadline"
    )


# ── A first attempt reads the resolved multiple, not the raw value ───────────


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _plus(instant: str, seconds: int) -> str:
    moment = datetime.fromisoformat(instant)
    return (
        (moment + timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repository carrying a plan, mounted under a temporary home."""
    config_home = tmp_path / "config"
    (config_home / "crew").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "plans" / "plan-a.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="plan-a">
</head><body><h2 id="s3">§3 — Dispatch</h2></body></html>
""",
        encoding="utf-8",
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt", "docs/plans/plan-a.html"),
        ("commit", "-q", "-m", "chore: seed fixture"),
    ):
        _git(root, *args)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def test_a_first_attempt_reads_the_resolved_multiple_for_its_hard_stop(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolved multiple fixes the hard stop, not the raw configuration."""
    base_sha = _git(repo, "rev-parse", "HEAD")

    def prepare_worktree(_repo: Path, session: str, node: str, base: str) -> dict:
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": base, "base_sha": base_sha}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    # The watchdog and the resumed fence read this owner; the first attempt must
    # read the same answer, so the value it returns fixes the hard stop even when
    # the raw configuration key says otherwise.
    monkeypatch.setattr(dispatch_module, "resolved_grace_multiple", lambda _config: 2.0)
    config = {
        "default_backend": "alpha",
        "backends": {
            "alpha": {
                "launch": "cli",
                "command": "codex",
                "sandbox": "worktree-full",
                "time_budget": "60m",
                "session_reuse": True,
                "usable_input_window": 512_000,
            }
        },
        "roles": {"implement": {}},
        "budget": {
            "utilisation_ceiling_pct": 100,
            "resume_reserve_pct": 5,
            "exhausted_statuses": [],
        },
        # The raw value is 1.5, so a prompt reading it would place the hard stop
        # at 90m; the patched resolver's 2.0 places it at 120m.
        "fences": {
            "implement": {"time_budget": "60m"},
            "needs_help_after_failures": 2,
            "budget_grace_multiple": 1.5,
        },
    }
    node = TaskNode(
        id="deadline-grace",
        goal="state the hard stop from the resolved multiple",
        plan="plan-a",
        section="s3",
        role="implement",
        spec_level="exact",
        done_when=(
            "the gate command pytest tests/test_a_deadline_needs_no_time_zone.py "
            "exits 0 and prints its summary line"
        ),
        write_paths=["result.json"],
        time_budget="60m",
        manifest_path=str(tmp_path / "manifests" / "deadline-grace.md"),
    )

    record = crew.dispatch(
        node=node,
        project=PROJECT,
        repo=repo,
        config=config,
        session="session-deadline-grace",
        launcher=lambda plan, **kwargs: os.getpid(),
    )

    started = str(record["attempt_started_at"])
    prompt = Path(record["prompt_path"]).read_text(encoding="utf-8")

    assert f"hard stop {_plus(started, 120 * 60)}" in prompt, (
        "the first attempt's hard stop is not the budget times the resolved "
        f"multiple (started {started})"
    )
    assert _plus(started, 90 * 60) not in prompt, (
        "the first attempt read the raw grace multiple rather than its resolver"
    )
