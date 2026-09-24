"""A raised account severity holds a dispatch the configured ceiling alone admits.

The account reports its own severity, on whichever horizon it chooses, and a
configured ceiling is blind to it: a lane can sit comfortably under the ceiling
while the account has already flagged its window. The fence therefore holds no
looser than the account's severity. The measures are the four facts that make
the floor trustworthy:

  - a raised severity holds a dispatch the configured ceiling alone would admit
  - a reading below both the ceiling and any raised severity admits
  - the refusal names which input produced the hold, so it is arguable
  - a severity the account reports for a period other than the one that gates a
    request does not hold on that figure

Severity rides the same window its utilisation does: per-window on the binding
``quota_windows`` row, or on the flat figure a shared block reports. Only the
gating window's severity reaches the decision, so a raised label for some other
horizon is structurally dropped rather than held on.

Every test is hermetic: ``RECKON_HOME`` moves the crew home into a temp tree and
the ledger is written under a throwaway repository.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import _backends, budget, ledger

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "backends"

# A dispatch stops at the ceiling less both reserves (100 - 5 - 3 = 92), so a
# utilisation of 60 would be admitted by the ceiling alone.
CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        },
    },
    "roles": {"implement": {}},
    "budget": {
        "utilisation_ceiling_pct": 100,
        "resume_reserve_pct": 5,
        "coordinator_reserve_pct": 3,
        "exhausted_statuses": [],
    },
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
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
    """A throwaway git repository carrying the worktree fleet script."""
    root = tmp_path / "repo"
    (root / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").write_text(
        source.read_text()
    )
    (root / "seed.txt").write_text("seed\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def _stamp(offset_seconds: int) -> str:
    moment = datetime.now(tz=UTC) + timedelta(seconds=offset_seconds)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _window(minutes: int, used: float, severity: str, *, resets_in: int = 3600) -> dict:
    """One keyed quota window with the account's own severity on it."""
    return {
        "window_minutes": minutes,
        "used_percent": used,
        "resets_at": _stamp(resets_in),
        "severity": severity,
    }


def _block_windows(*windows: dict) -> dict:
    """A known budget block whose utilisation lives on keyed quota windows."""
    block = _backends.unknown_budget("recorded by the backend's own report")
    block.update(
        {
            "headroom": "known",
            "quota_windows": {int(row["window_minutes"]): row for row in windows},
            "detail": "recorded by the backend's own report",
        }
    )
    return block


def _flat_block(utilisation: float, *, severity: str | None) -> dict:
    """A flat known block whose figure and severity describe the same window."""
    block = _backends.unknown_budget("recorded by the backend's own report")
    block.update(
        {
            "headroom": "known",
            "utilisation_pct": utilisation,
            "rate_limit_period_minutes": 300,
            "resets_at": _stamp(3600),
            "detail": "recorded by the backend's own report",
        }
    )
    if severity is not None:
        block["severity"] = severity
    return block


def _record(project: str, root: Path, *, block: dict) -> None:
    """Promote one completed run carrying the block into the ledger."""
    record = ledger.build_record(
        run_id="r-severity",
        plan="plan-a",
        gate="passed",
        agent={"backend": "alpha"},
        completed_at=_stamp(-60),
        budget=block,
    )
    ledger.append_run(project, record, root=root)


def _alpha(report: dict) -> dict:
    """The alpha backend's verdict out of a preflight report."""
    return next(item for item in report["backends"] if item["backend"] == "alpha")


# ── A raised severity holds where the ceiling alone would admit ─────────────


@pytest.mark.parametrize("severity", ["warning", "critical"])
def test_a_raised_severity_holds_a_dispatch_the_ceiling_admits(
    home, repo, severity
) -> None:
    """At 60% against a 92% limit the ceiling clears, but the account's raised
    severity on the gating window does not."""
    _record("proj", repo, block=_block_windows(_window(300, 60.0, severity)))

    report = budget.preflight("proj", CONFIG, root=repo, purpose="dispatch")
    verdict = _alpha(report)

    assert verdict["held"] is True
    assert verdict["effective_ceiling_pct"] == 92.0
    assert verdict["state"]["severity"] == severity
    assert "at or above" not in verdict["reason"]


def test_a_raised_severity_on_a_flat_binding_figure_holds(home, repo) -> None:
    """A shared block whose own figure is flagged also holds, not just a keyed
    window."""
    _record("proj", repo, block=_flat_block(60.0, severity="critical"))

    verdict = _alpha(budget.preflight("proj", CONFIG, root=repo, purpose="dispatch"))

    assert verdict["held"] is True


# ── A reading below both admits ──────────────────────────────────────────────


@pytest.mark.parametrize("severity", ["normal", None])
def test_a_reading_below_both_ceiling_and_severity_admits(home, repo, severity) -> None:
    """A normal (or absent) severity beside utilisation under the ceiling clears."""
    _record("proj", repo, block=_flat_block(60.0, severity=severity))

    verdict = _alpha(budget.preflight("proj", CONFIG, root=repo, purpose="dispatch"))

    assert verdict["held"] is False
    assert "ceiling" in verdict["reason"]


def test_a_reading_below_the_ceiling_with_normal_severity_on_the_binding_window(
    home, repo
) -> None:
    """Same figure through the keyed-window path: normal severity does not hold."""
    _record("proj", repo, block=_block_windows(_window(300, 60.0, "normal")))

    verdict = _alpha(budget.preflight("proj", CONFIG, root=repo, purpose="dispatch"))

    assert verdict["held"] is False


# ── The refusal names which input produced the hold ─────────────────────────


def test_the_refusal_names_the_raised_severity_as_the_cause(home, repo) -> None:
    """A hold below the ceiling says the severity produced it, so a reader can
    argue with that input rather than with a ceiling that was never crossed."""
    _record("proj", repo, block=_flat_block(60.0, severity="critical"))

    verdict = _alpha(budget.preflight("proj", CONFIG, root=repo, purpose="dispatch"))

    assert verdict["held"] is True
    assert "below the 92.0% ceiling" in verdict["reason"]
    assert "critical" in verdict["reason"]
    assert "at or above" not in verdict["reason"]


def test_the_refusal_names_the_ceiling_when_utilisation_crosses_it(home, repo) -> None:
    """At 95% (> 92%) with a normal severity, the hold names the ceiling, not a
    severity input that was never raised."""
    _record("proj", repo, block=_flat_block(95.0, severity="normal"))

    verdict = _alpha(budget.preflight("proj", CONFIG, root=repo, purpose="dispatch"))

    assert verdict["held"] is True
    assert "at or above the 92.0% ceiling" in verdict["reason"]
    assert "severity" not in verdict["reason"]


# ── A severity for a non-gating period does not hold ────────────────────────


def test_a_severity_for_a_non_gating_period_does_not_hold(home, repo) -> None:
    """The account flags the 300-minute own window critical while the window
    that actually gates (the 10080-minute at 89%) reads normal. The hold must
    not come from the fixture on the period that does not gate the request."""
    _record(
        "proj",
        repo,
        block=_block_windows(
            _window(300, 40.0, "critical"),
            _window(10080, 89.0, "normal"),
        ),
    )

    verdict = _alpha(budget.preflight("proj", CONFIG, root=repo, purpose="dispatch"))

    assert verdict["held"] is False
    assert verdict["state"]["utilisation_pct"] == 89.0
    assert verdict["state"]["severity"] == "normal"


# ── A raised severity holds a reading that carries no utilisation ───────────


def test_a_raised_severity_holds_a_reading_with_no_utilisation() -> None:
    """A known headroom with a raised severity must not be cleared by the
    None-utilisation guard: the floor binds the account's raised label even
    when there is no figure to compare against the ceiling."""
    state = budget.BudgetState(
        backend="alpha",
        headroom="known",
        utilisation_pct=None,
        severity="critical",
    )

    verdict = budget.decide(state, budget.policy(CONFIG), purpose="dispatch")

    assert verdict["held"] is True
    assert "severity" in verdict["reason"]
    assert "critical" in verdict["reason"]


@pytest.mark.parametrize("severity", ["normal", None])
def test_an_unraised_severity_with_no_utilisation_still_clears(severity) -> None:
    """The guard is narrowed, not removed: without a raised label the same
    figureless reading still clears, so severity alone is what holds."""
    state = budget.BudgetState(
        backend="alpha",
        headroom="known",
        utilisation_pct=None,
        severity=severity,
    )

    verdict = budget.decide(state, budget.policy(CONFIG), purpose="dispatch")

    assert verdict["held"] is False
    assert "nothing to compare against the ceiling" in verdict["reason"]
