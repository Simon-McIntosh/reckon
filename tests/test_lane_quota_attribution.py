"""A lane with its own quota is reported by that quota, never the shared one.

The budget view reads whichever horizon its block happens to carry, so a lane
whose receipt reports both its own 300-minute window at 97% and a shared
account-weekly at 21% was described by the shared figure and read clear for
dispatch at the moment it was almost exhausted. The keyed per-window quota map
is the authority: the view reports the lane's own window when the block names
one, keeps the shared weekly for a lane that names only it, and reads
``unmeasured`` when its own figure cannot be read rather than substituting the
shared number.

Every test is hermetic: ``RECKON_HOME`` moves the crew home into a temp tree
and the ledger is written under a throwaway repository, so nothing touches a
real account surface or a real configuration home.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import _backends, budget, ledger

MOMENT = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
SHARED_WEEKLY = 7 * 24 * 60
CEILING_95 = {
    "utilisation_ceiling_pct": 95,
    "resume_reserve_pct": 5,
    "exhausted_statuses": [],
}

CONFIG = {
    "default_backend": "own",
    "backends": {
        "own": {"launch": "cli", "command": "codex", "time_budget": "25m"},
        "shared": {"launch": "cli", "command": "claude", "time_budget": "25m"},
    },
    "budget": CEILING_95,
}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point the crew home at a temp tree, leaving this workstation's alone."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path):
    """A throwaway git repository carrying the ledger."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "seed.txt").write_text("seed\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "w@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def _stamp(minutes: int) -> str:
    return (MOMENT + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _base_block() -> dict:
    return _backends.unknown_budget("recorded by the backend's own report")


def _own_quota_block(*, own_used: object = 97, weekly_used: object = 43) -> dict:
    """A lane declaring a 300-minute own quota beside the shared weekly.

    The compatibility fields still describe the shared weekly (the figure the
    old view substituted); the keyed windows are the authority.
    """
    block = _base_block()
    block.update(
        {
            "headroom": "known",
            "utilisation_pct": 21.0,
            "rate_limit_period_minutes": SHARED_WEEKLY,
            "resets_at": _stamp(7 * 24 * 60),
            "quota_windows": {
                300: {
                    "window_minutes": 300,
                    "used_percent": own_used,
                    "resets_at": _stamp(120),
                },
                SHARED_WEEKLY: {
                    "window_minutes": SHARED_WEEKLY,
                    "used_percent": weekly_used,
                    "resets_at": _stamp(7 * 24 * 60),
                },
            },
        }
    )
    return block


def _shared_weekly_block(*, weekly_used: object = 21) -> dict:
    """A lane that names only the shared weekly horizon."""
    block = _base_block()
    block.update(
        {
            "headroom": "known",
            "utilisation_pct": 21.0,
            "rate_limit_period_minutes": SHARED_WEEKLY,
            "resets_at": _stamp(7 * 24 * 60),
            "quota_windows": {
                SHARED_WEEKLY: {
                    "window_minutes": SHARED_WEEKLY,
                    "used_percent": weekly_used,
                    "resets_at": _stamp(7 * 24 * 60),
                },
            },
        }
    )
    return block


def _record(
    project: str, root: Path, *, backend: str, block: dict, run_id: str
) -> None:
    """Promote one completed run carrying a budget block into the ledger."""
    record = ledger.build_record(
        run_id=run_id,
        plan="plan-a",
        gate="passed",
        agent={"backend": backend},
        completed_at=MOMENT.isoformat(),
        budget=block,
    )
    ledger.append_run(project, record, root=root)


def _view(project: str, root: Path) -> dict[str, dict]:
    """Run the budget view and return each backend's state and verdict."""
    report = budget.preflight(project, CONFIG, root=root, now=MOMENT)
    return {
        verdict["backend"]: {"state": verdict["state"], "held": verdict["held"]}
        for verdict in report["backends"]
    }


def test_a_lane_with_its_own_quota_reports_its_own_window_not_the_shared_weekly(
    home, repo
) -> None:
    _record("proj", repo, backend="own", block=_own_quota_block(), run_id="r-own")

    own = _view("proj", repo)["own"]

    assert own["state"]["utilisation_pct"] == 97.0
    assert own["state"]["rate_limit_period_minutes"] == 300
    assert own["state"]["resets_at"] == _stamp(120)
    assert own["state"]["source"] == "ledger"


def test_a_lane_without_a_separate_horizon_still_reports_the_shared_weekly(
    home, repo
) -> None:
    _record(
        "proj",
        repo,
        backend="shared",
        block=_shared_weekly_block(),
        run_id="r-shared",
    )

    shared = _view("proj", repo)["shared"]

    assert shared["state"]["utilisation_pct"] == 21.0
    assert shared["state"]["rate_limit_period_minutes"] == SHARED_WEEKLY
    assert shared["held"] is False


def test_an_own_figure_that_cannot_be_read_reports_unmeasured_not_the_shared_weekly(
    home, repo
) -> None:
    _record(
        "proj",
        repo,
        backend="own",
        block=_own_quota_block(own_used="unmeasured"),
        run_id="r-own-unreadable",
    )

    own = _view("proj", repo)["own"]

    assert own["state"]["headroom"] == "unknown"
    assert own["state"]["utilisation_pct"] is None
    assert "not substituted from the shared weekly" in own["state"]["detail"]
    assert own["held"] is False


def test_a_legacy_block_without_keyed_windows_keeps_the_shared_figure(
    home, repo
) -> None:
    block = _base_block()
    block.update(
        {
            "headroom": "known",
            "utilisation_pct": 21.0,
            "rate_limit_period_minutes": SHARED_WEEKLY,
            "resets_at": _stamp(7 * 24 * 60),
        }
    )
    _record("proj", repo, backend="own", block=block, run_id="r-legacy")

    own = _view("proj", repo)["own"]

    assert own["state"]["utilisation_pct"] == 21.0
    assert own["state"]["rate_limit_period_minutes"] == SHARED_WEEKLY


def test_the_own_quota_judges_admission_not_the_shared_weekly(home, repo) -> None:
    """A lane 97% of the way through its own window is held even though the
    shared weekly compatibility figure reads 21% — the direction the old view
    got wrong, reading a nearly-exhausted lane as clear for dispatch."""
    _record("proj", repo, backend="own", block=_own_quota_block(), run_id="r-own-hold")

    own = _view("proj", repo)["own"]

    assert own["held"] is True


def test_the_furthest_through_own_window_is_the_binding_one(home, repo) -> None:
    block = _own_quota_block()
    block["quota_windows"][121_600] = {
        "window_minutes": 121_600,
        "used_percent": 30,
        "resets_at": _stamp(121_600 * 4),
    }
    _record("proj", repo, backend="own", block=block, run_id="r-own-two")

    own = _view("proj", repo)["own"]

    assert own["state"]["utilisation_pct"] == 97.0
    assert own["state"]["rate_limit_period_minutes"] == 300
