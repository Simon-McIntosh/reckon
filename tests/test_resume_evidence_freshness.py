"""A resume is judged by the freshest evidence, not by which store it came from.

The resume gate reuses the dispatch budget gate, but it recovers the backend
from the run's own argv rather than from configuration, so the ``budget_check``
opt-in on the configured backend never reaches the surface read. An opted-in
backend then reads a two-day-old held record on resume while dispatch on the
same backend reads the account surface that same minute — a disagreement that
lets a stale record hold the one operation the reserve is withheld to permit.
These measures pin the fix: the surface opt-in travels on the recorded reading,
so the resume gate consults the account surface exactly as dispatch does, and a
verdict says which reading it acted on and when that reading was observed.

Everything here is hermetic: the crew home and the ledger live under
``tmp_path`` trees and no probe spawns a process — the account-surface answer is
injected by patching the probe seam ``state_for`` calls.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import _backends, ledger
from reckon.crew.routing import _budget_verdict
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
# A pid the test can be sure is not running, so a resumed pointer still reads as
# a stopped run rather than a live one.
DEAD_PID = 4_194_303

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        },
        "beta": {
            "launch": "cli",
            "command": "claude",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        },
    },
    "roles": {"implement": {}, "review": {"backend": "beta"}},
    "budget": {
        "utilisation_ceiling_pct": 100,
        "resume_reserve_pct": 5,
        "exhausted_statuses": [],
    },
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

# The backend settings a resume recovers from a run's recorded argv: the command
# survives, the configuration's budget_check opt-in does not. This is the shape
# that used to send the resume path back to a stale recorded refusal.
RECOVERED_BACKEND = {"launch": "cli", "command": "codex"}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point the crew home at a temp tree, leaving this workstation's alone."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path):
    """A throwaway git repository carrying a ledger for this project."""
    root = tmp_path / "repo"
    (root / "skills" / "reckon-ship" / "scripts").mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-ship" / "scripts" / "worktree_fleet.py").write_text(
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


def _known(utilisation: float, *, resets_in: int = 3600, status=None) -> dict:
    """A budget block from a backend that reports headroom."""
    block = _backends.unknown_budget("recorded by the backend's own report")
    block.update(
        {
            "headroom": "known",
            "utilisation_pct": utilisation,
            "resets_at": _stamp(resets_in),
            "threshold_status": status,
        }
    )
    return block


def _undated(utilisation: float) -> dict:
    """A known block whose refusal names no reset time."""
    block = _backends.unknown_budget("recorded by the backend's own report")
    block.update({"headroom": "known", "utilisation_pct": utilisation})
    return block


def _surface(utilisation: float, *, resets_in: int = 7200) -> dict:
    """A block in the shape the account-surface probe returns."""
    block = _backends.unknown_budget("")
    block.update(
        {
            "headroom": "known",
            "utilisation_pct": utilisation,
            "rate_limit_period_minutes": 10080,
            "resets_at": _stamp(resets_in),
            "detail": "backend's account surface reports utilisation and reset time",
        }
    )
    return block


def _record(
    root: Path,
    *,
    backend: str = "alpha",
    budget_block: dict,
    run_id: str,
    completed_at: str | None = None,
):
    """Promote one completed run carrying a budget block into the ledger."""
    record = ledger.build_record(
        run_id=run_id,
        plan="plan-a",
        gate="passed",
        agent={"backend": backend},
        completed_at=completed_at or _stamp(-60),
        budget=budget_block,
    )
    ledger.append_run(PROJECT, record, root=root)
    return record


def _asking(surface: bool = True) -> dict:
    """The configured backend, opting its account surface into a read."""
    return {
        **CONFIG,
        "backends": {
            **CONFIG["backends"],
            "alpha": {**CONFIG["backends"]["alpha"], "budget_check": surface},
        },
    }


def _resume_verdict(*, root: Path, config: dict) -> dict:
    """The exact gate a resume passes: recovered settings, resume purpose."""
    return _budget_verdict(
        project=PROJECT,
        root=root,
        config=config,
        backend_name="alpha",
        backend=RECOVERED_BACKEND,
        purpose="resume",
    )


def _pointer(tmp_path: Path, run_id: str, repo: Path) -> dict:
    """A stopped, resumable run on the account-surface backend, as dispatch leaves it."""
    directory = tmp_path / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    tree = tmp_path / "trees" / run_id
    tree.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text("node: node-a\nstatus: blocked\n", encoding="utf-8")
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(tree),
        "launch": "cli",
        "argv": ["codex", "exec"],
        "backend": "alpha",
        "role": "implement",
        "pid": DEAD_PID,
        "session_id": "sess-on-the-pointer",
        "created_at": "2026-09-06T00:00:00Z",
        "log_path": str(directory / "stream.jsonl"),
        "manifest_path": str(manifest),
        "phase": "working",
        "node": {
            "id": "node-a",
            "plan": "plan-a",
            "section": "",
            "time_budget": "30m",
            "write_paths": ["reckon/one.py"],
        },
    }
    _write_json(pointer_path(run_id), record)
    return record


# ── The freshest evidence decides the resume ─────────────────────────────────


def test_a_stale_ledger_refusal_yields_to_a_fresh_account_reading(
    home, repo, monkeypatch
) -> None:
    """A held record that describes a spent account no longer holds a resume when the surface is read fresh and clear."""
    _record(
        repo,
        backend="alpha",
        budget_block=_known(100.0, resets_in=7200),
        run_id="r-held",
        completed_at=_stamp(-7200),
    )
    monkeypatch.setattr(_backends, "probe_budget", lambda **kw: _surface(40.0))

    verdict = _resume_verdict(root=repo, config=_asking())

    assert verdict["held"] is False
    assert verdict["state"]["source"] == "account-surface"
    assert verdict["state"]["utilisation_pct"] == 40.0
    # The clearance says which reading decided and when it was observed.
    assert "account surface was read at" in verdict["reason"]


def test_a_stale_ledger_refusal_with_no_fresh_reading_still_holds(
    home, repo, monkeypatch
) -> None:
    """A held record holds a resume when the surface cannot be read: absence of a fresher signal is never clearance."""
    _record(
        repo,
        backend="alpha",
        budget_block=_known(100.0, resets_in=7200),
        run_id="r-held",
        completed_at=_stamp(-7200),
    )
    monkeypatch.setattr(
        _backends,
        "probe_budget",
        lambda **kw: _backends.unknown_budget("surface read failed"),
    )

    verdict = _resume_verdict(root=repo, config=_asking())

    assert verdict["held"] is True
    assert verdict["state"]["source"] == "ledger"
    assert "account surface was read at" not in verdict["reason"]


def test_a_current_account_reading_at_or_above_the_ceiling_holds_whatever_the_ledger_says(
    home, repo, monkeypatch
) -> None:
    """A lane that is genuinely spent holds a resume even when the ledger reports headroom."""
    _record(
        repo,
        backend="alpha",
        budget_block=_known(10.0, resets_in=7200),
        run_id="r-clear",
        completed_at=_stamp(-60),
    )
    monkeypatch.setattr(_backends, "probe_budget", lambda **kw: _surface(100.0))

    verdict = _resume_verdict(root=repo, config=_asking())

    assert verdict["held"] is True
    assert verdict["state"]["source"] == "account-surface"
    assert verdict["state"]["utilisation_pct"] == 100.0
    # The refusal names the reading that held it, and when it was observed.
    assert "account surface was read at" in verdict["reason"]


def test_a_backend_without_an_account_surface_behaves_as_it_does_today(
    home, repo, monkeypatch
) -> None:
    """No opt-in means no surface read, and the recorded refusal holds as before."""
    probed: list[dict] = []
    monkeypatch.setattr(
        _backends,
        "probe_budget",
        lambda **kw: probed.append(kw) or _surface(40.0),
    )
    _record(
        repo,
        backend="alpha",
        budget_block=_known(100.0, resets_in=7200),
        run_id="r-held",
        completed_at=_stamp(-7200),
    )

    verdict = _resume_verdict(root=repo, config=CONFIG)

    assert probed == []
    assert verdict["held"] is True
    assert verdict["state"]["source"] == "ledger"
    assert "account surface" not in verdict["reason"]


def test_a_no_surface_backend_keeps_the_existing_ageing_rule(
    home, repo, monkeypatch
) -> None:
    """An undated refusal that has outlived its shelf life ages out on resume exactly as it does today."""
    probed: list[dict] = []
    monkeypatch.setattr(
        _backends,
        "probe_budget",
        lambda **kw: probed.append(kw) or _surface(40.0),
    )
    _record(
        repo,
        backend="alpha",
        budget_block=_undated(100.0),
        run_id="r-aged",
        completed_at=_stamp(-7200),
    )

    verdict = _resume_verdict(root=repo, config=CONFIG)

    assert probed == []
    assert verdict["held"] is False
    assert "shelf life" in verdict["reason"]


def test_dispatch_and_resume_resolve_the_same_opt_in_backend_surface(
    home, repo, monkeypatch
) -> None:
    """A backend opted into a surface read reports it to both gates, so the reserve keeps its shape from the same reading."""
    _record(
        repo,
        backend="alpha",
        budget_block=_known(100.0, resets_in=7200),
        run_id="r-held",
        completed_at=_stamp(-7200),
    )
    monkeypatch.setattr(_backends, "probe_budget", lambda **kw: _surface(97.0))
    configured = _asking()
    dispatch_settings = configured["backends"]["alpha"]

    dispatching = _budget_verdict(
        project=PROJECT,
        root=repo,
        config=configured,
        backend_name="alpha",
        backend=dispatch_settings,
        purpose="dispatch",
    )
    resuming = _resume_verdict(root=repo, config=configured)

    # The same 97% reading: a fresh dispatch stops below the reserve-adjusted
    # ceiling, while the resume the reserve protects is allowed the full one.
    assert resuming["state"]["source"] == "account-surface"
    assert dispatching["state"]["source"] == "account-surface"
    assert dispatching["held"] is True
    assert resuming["held"] is False


def test_a_resume_plan_is_not_held_by_a_superseded_ledger_refusal(
    home, tmp_path, repo, monkeypatch
) -> None:
    """The command a stuck worker gets answered by resumes when the read lane is open, end to end."""
    from reckon.crew.dispatch import resume_plan

    run_id = "r-20260906T000000000000-resume-me"
    _record(
        repo,
        backend="alpha",
        budget_block=_known(100.0, resets_in=7200),
        run_id="r-held",
        completed_at=_stamp(-7200),
    )
    _pointer(tmp_path, run_id, repo)
    monkeypatch.setattr(_backends, "probe_budget", lambda **kw: _surface(40.0))

    plan = resume_plan(run_id, "Continue.", config=_asking())

    assert plan.dialect == "codex"
    assert "resume" in " ".join(plan.argv)
    assert plan.resumed_session == "sess-on-the-pointer"
