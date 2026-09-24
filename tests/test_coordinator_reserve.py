"""A declared coordinator reserve keeps headroom for the coordinator itself.

The reported account position already counts the coordinator's traffic, but
reckon cannot see the total. A wave sized to fill the remaining headroom leaves
nothing for the coordinator that must survive the wave to audit manifests, merge
commits and record outcomes — so the coordinator can be refused mid-wave with
every worker's output stranded in a worktree. The first version of the reserve is
declared rather than computed, with its reasoning beside it; a cadence of
account-surface readings is what calibrates it later.

The measures this file exists to demonstrate:

  - the reserve is shipped data: it resolves from the shipped layer with its
    provenance reported as shipped, and the budget policy reads it from there
  - a fresh dispatch stops at the ceiling less both the resume reserve and the
    coordinator reserve, while a resume still clears between the two — the
    escape hatch is not held by the headroom withheld for the coordinator
  - the refusal reason names which reserve was subtracted, so a reader who sees
    a lane stop below the ceiling can tell what was protecting whom

Every test is hermetic: ``RECKON_HOME`` moves the crew home into a temp tree,
the ledger is written under a throwaway repository, and the shipped-layer
resolution isolates host and project layers at empty temp paths.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import _backends, budget, flight, ledger

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "backends"

# Mirrors the shipped defaults, so resolving the shipped layer and this dict
# reach the same numbers: ceiling 100, resume reserve 5, coordinator reserve 3.
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


def _known(utilisation: float, *, resets_in: int = 3600) -> dict:
    """A budget block from a backend that reports headroom."""
    block = _backends.unknown_budget("recorded by the backend's own report")
    block.update(
        {
            "headroom": "known",
            "utilisation_pct": utilisation,
            "resets_at": _stamp(resets_in),
        }
    )
    return block


def _record(project: str, root: Path, *, backend: str, utilisation: float) -> None:
    """Promote one completed run carrying a known reading into the ledger."""
    record = ledger.build_record(
        run_id="r-reserve",
        plan="plan-a",
        gate="passed",
        agent={"backend": backend},
        completed_at=_stamp(-60),
        budget=_known(utilisation),
    )
    ledger.append_run(project, record, root=root)


# ── The reserve is shipped data with its provenance intact ──────────────────


def test_the_coordinator_reserve_ships_in_the_shipped_layer(tmp_path) -> None:
    """The reserve is declared in shipped defaults and reports its origin."""
    resolved = flight.resolve(
        host_path=tmp_path / "host" / "flight.yaml",
        project_path=tmp_path / "project" / "flight.yaml",
    )
    thresholds = resolved.config["budget"]
    assert thresholds["coordinator_reserve_pct"] == 3
    assert thresholds["resume_reserve_pct"] == 5
    assert resolved.origin("budget.coordinator_reserve_pct") == "shipped"

    policy_block = budget.policy(resolved.config)
    assert policy_block["coordinator_reserve_pct"] == 3
    # Fresh dispatch: both reserves withheld (100 - 5 - 3). No other purpose.
    assert budget.effective_ceiling(policy_block, "dispatch") == 92.0
    assert budget.effective_ceiling(policy_block, "resume") == 100.0


# ── A dispatch is held where a resume still clears ──────────────────────────


def test_a_dispatch_is_held_where_a_resume_still_clears(home, repo) -> None:
    """A wave that would crowd out the coordinator is held; the escape hatch
    between the reserves still opens."""
    _record("proj", repo, backend="alpha", utilisation=95.0)

    dispatching = budget.preflight("proj", CONFIG, root=repo, purpose="dispatch")
    resuming = budget.preflight("proj", CONFIG, root=repo, purpose="resume")

    assert dispatching["held_backends"] == ["alpha"]
    assert resuming["held_backends"] == []
    held = next(item for item in dispatching["backends"] if item["backend"] == "alpha")
    assert held["ceiling_pct"] == 100.0
    assert held["effective_ceiling_pct"] == 92.0


def test_a_resume_between_the_reserves_is_not_held(home, repo) -> None:
    """At 96% only the coordinator headroom separates dispatch from resume."""
    _record("proj", repo, backend="alpha", utilisation=96.0)

    dispatching = budget.preflight("proj", CONFIG, root=repo, purpose="dispatch")
    resuming = budget.preflight("proj", CONFIG, root=repo, purpose="resume")

    assert dispatching["held_backends"] == ["alpha"]
    assert resuming["held_backends"] == []


def test_a_resume_is_held_when_the_coordinator_headroom_is_gone(home, repo) -> None:
    """Once the utilisation reaches the full ceiling no reserve can clear."""
    _record("proj", repo, backend="alpha", utilisation=100.0)

    resuming = budget.preflight("proj", CONFIG, root=repo, purpose="resume")
    assert resuming["held_backends"] == ["alpha"]


# ── The refusal names the reserve it subtracted ─────────────────────────────


def test_the_refusal_reason_names_each_withheld_reserve(home, repo) -> None:
    """The reason says which headroom was withheld, not merely that a lane held."""
    _record("proj", repo, backend="alpha", utilisation=92.0)

    dispatching = budget.preflight("proj", CONFIG, root=repo, purpose="dispatch")
    held = next(item for item in dispatching["backends"] if item["backend"] == "alpha")
    assert "3% coordinator reserve" in held["reason"]
    assert "5% resume reserve" in held["reason"]
