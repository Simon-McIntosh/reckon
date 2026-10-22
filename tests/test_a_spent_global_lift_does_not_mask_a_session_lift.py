"""A spent global credit lift does not mask a live session credit lift.

A credit lift admits work past an exhausted quota window while the account holds
purchased credits, but it stops at its own cap. Two credit lifts can cover one
group at once: an older one granted globally, and a newer one granted for a
single session. The newer one carries the older one's unspent credits forward,
so for the session it names it is the lift in force and the older one's cap says
nothing about that session's own spend.

The dispatch path resolved the group's lift with no session, so the older global
lift governed every dispatch and its spent cap masked the session's live one.
The session's own dispatch was refused on the global cap. Each case below drives
the real dispatch entry, because the session the dispatch already holds is
dropped at those two judgement calls and nowhere else.

The declared negative control — judging the cap against the newest lift on the
group regardless of scope, so the global lift's spent cap governs again — fails
``test_a_live_session_lift_admits_its_own_session`` (and
``test_a_session_lift_judges_its_own_spend_on_the_fallback_path``): the global
lift's spent cap holds the session's dispatch.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import _backends, cli, crew_dispatch_commands, ledger
from reckon.crew import budget_lift as bl
from tests.real_home_traces import traces_naming

dispatch = importlib.import_module("reckon.crew.dispatch")

PROJECT = "proj"
SYSTEM_LIFT_GROUP = "alpha-group"
MASKED_GROUP = "codex-group"
SESSION = "s21-nova"
PEER = "another-session"
BALANCE_NOW = 50_593.66
GLOBAL_AT_GRANT = 60_564.37
GLOBAL_CAP = 10_000.0
SESSION_AT_GRANT = 55_157.31
SESSION_CAP = 14_604.0

CONFIG: dict[str, Any] = {
    "default_backend": "beta",
    "backends": {
        "alpha": {
            "launch": "in-harness",
            "model": "alpha-model",
            "budget_group": SYSTEM_LIFT_GROUP,
            "fallback": "beta",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        },
        "beta": {
            "launch": "in-harness",
            "model": "beta-model",
            "budget_group": MASKED_GROUP,
            "sandbox": "worktree-full",
            "time_budget": "25m",
        },
    },
    "roles": {"implement": {}},
    "budget": {
        "pace_multiple": 1.1,
        "utilisation_ceiling_pct": 100,
        "resume_reserve_pct": 5,
        "coordinator_reserve_pct": 3,
        "bookend_reserve_pct": 20,
        "lift": {"max_multiple": 3, "max_hours": 168},
    },
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repository and configuration home for one dispatch."""
    home = tmp_path / "config"
    home.mkdir()
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    scripts = root / "skills" / "reckon-build" / "scripts"
    plans.mkdir(parents=True)
    scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / source.name).write_text(source.read_text())
    (plans / "example.html").write_text(
        '<!doctype html><html><head><meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="example"></head>'
        '<body><h2 id="dispatch">Dispatch</h2></body></html>'
    )
    (root / "seed.txt").write_text("seed\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/example.html"],
        ["commit", "-q", "-m", "chore: seed repository"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"proj": str(root / "docs")}))

    real_home = Path.home() / ".config" / "reckon"
    before = traces_naming(real_home, (bl.LIFTS_LEAF,), (MASKED_GROUP,))
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv(bl.RUN_ID_ENV, raising=False)
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: CONFIG
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    yield root
    after = traces_naming(real_home, (bl.LIFTS_LEAF,), (MASKED_GROUP,))
    assert after == before, f"a test wrote the live budget-lifts.json: {after}"


def _window(
    *,
    period_minutes: int,
    utilisation_pct: float,
    resets_in: timedelta,
    credits_balance: float | None = None,
) -> dict:
    """One recorded quota window, optionally with a credit balance on the account."""
    block = _backends.unknown_budget("recorded by the backend's own report")
    block.update(
        {
            "headroom": "known",
            "utilisation_pct": utilisation_pct,
            "rate_limit_period_minutes": period_minutes,
            "resets_at": (datetime.now(UTC) + resets_in)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "threshold_status": "rate_limit_reached",
        }
    )
    if credits_balance is not None:
        block["credits"] = {
            "has_credits": True,
            "unlimited": False,
            "balance": credits_balance,
        }
    return block


def _record(
    project: str,
    root: Path,
    *,
    backend: str,
    budget_block: dict,
    run_id: str,
    completed_at: str,
    lane_receipt: dict | None = None,
) -> None:
    record = ledger.build_record(
        run_id=run_id,
        plan="example",
        gate="passed",
        agent={"backend": backend},
        completed_at=completed_at,
        budget=budget_block,
        lane_receipt=lane_receipt,
    )
    ledger.append_run(project, record, root=root)
    relative = Path("docs") / "state" / project / "crew.json"
    if not (root / relative).exists():
        return
    subprocess.run(
        ["git", "-C", str(root), "add", str(relative)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "chore(state): record a run"],
        check=True,
        capture_output=True,
    )


def _grant(
    *,
    group: str,
    credits: float,
    balance_at_grant: float,
    scope: str = bl.GLOBAL,
    session: str | None = None,
) -> str:
    """Grant one credit lift, expiring in a day so it is in force at dispatch."""
    ends = {
        "kind": "at",
        "at": (datetime.now(UTC) + timedelta(hours=24))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    lift = bl.grant(
        CONFIG,
        group=group,
        reason="carry the unspent credits forward",
        form=bl.CREDITS,
        credits=credits,
        balance_at_grant=balance_at_grant,
        scope=scope,
        session=session,
        ends=ends,
    )
    return str(lift["id"])


def _masked_fixture(root: Path, *, session_cap: float = SESSION_CAP) -> dict[str, str]:
    """Record the spent windows and grant the two lifts on the masked group."""
    now = datetime.now(UTC)
    _record(
        PROJECT,
        root,
        backend="alpha",
        run_id="reading-alpha",
        completed_at=(now - timedelta(minutes=1)).isoformat(timespec="seconds"),
        budget_block=_window(
            period_minutes=10080,
            utilisation_pct=100.0,
            resets_in=timedelta(days=3),
        ),
    )
    # The five-hour clock is what a pace judgement reads the fill against, and
    # the seven-day window is the one the credit lift admits past; both are
    # published so the dispatch reaches the budget judgement rather than
    # stopping at an unread clock. The seven-day reading is the newest, so it
    # is the one the credit-cap judgement reads.
    _record(
        PROJECT,
        root,
        backend="beta",
        run_id="reading-beta",
        completed_at=(now - timedelta(minutes=1)).isoformat(timespec="seconds"),
        budget_block=_window(
            period_minutes=10080,
            utilisation_pct=100.0,
            resets_in=timedelta(days=3),
            credits_balance=BALANCE_NOW,
        ),
        lane_receipt={
            "quota_state": "measured",
            "observed_at": (now - timedelta(minutes=1))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "quota_windows": [
                {
                    "window_minutes": 300,
                    "used_percent": 10.0,
                    "resets_at": (now + timedelta(hours=3)).isoformat(
                        timespec="seconds"
                    ),
                    "observed_at": (now - timedelta(minutes=1))
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z"),
                },
                {
                    "window_minutes": 10080,
                    "used_percent": 100.0,
                    "resets_at": (now + timedelta(days=3)).isoformat(
                        timespec="seconds"
                    ),
                    "observed_at": (now - timedelta(minutes=1))
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z"),
                },
            ],
        },
    )
    session_lift = _grant(
        group=MASKED_GROUP,
        credits=session_cap,
        balance_at_grant=SESSION_AT_GRANT,
        scope=f"{bl.SESSION_PREFIX}{SESSION}",
        session=SESSION,
    )
    # The global lift is granted last, so it is the newest on the group. A
    # judgement that reads the newest lift whatever its scope therefore reads
    # the spent global one and masks the session lift, which is the defect.
    system_lift = _grant(
        group=MASKED_GROUP, credits=GLOBAL_CAP, balance_at_grant=GLOBAL_AT_GRANT
    )
    return {"system": system_lift, "session": session_lift}


def _dispatch(repo: Path, *, backend: str, session: str, node: str):
    arguments = [
        "crew",
        "dispatch",
        "--project",
        PROJECT,
        "--plan",
        "example",
        "--section",
        "dispatch",
        "--role",
        "implement",
        "--spec-level",
        "exact",
        "--backend",
        backend,
        "--route",
        "deterministic",
        "--node",
        node,
        "--goal",
        "record the credit cap read for one session",
        "--done-when",
        "uv run pytest tests/test_a_spent_global_lift_does_not_mask_a_session_lift.py reports 0 failures",
        "--write-path",
        "result.json",
        "--session",
        session,
        "--repo",
        str(repo),
        "--checkout-path",
        str(repo),
        "--no-watch",
    ]
    result = CliRunner().invoke(cli.main, arguments)
    payload = json.loads(result.output.splitlines()[0])
    return result, payload


def test_a_live_session_lift_admits_its_own_session(repo):
    """The session lift under its cap admits its session past the spent global."""
    _masked_fixture(repo)
    result, payload = _dispatch(
        repo, backend="beta", session=SESSION, node="own-session"
    )
    assert result.exit_code == 0, result.output
    assert payload.get("error") != "budget-hold"


def test_the_spent_global_lift_still_holds_another_session(repo):
    """A session the global lift alone governs is still held on its spent cap."""
    lifts = _masked_fixture(repo)
    result, payload = _dispatch(repo, backend="beta", session=PEER, node="peer-session")
    assert result.exit_code == 3
    assert payload["error"] == "budget-hold"
    assert lifts["system"] in payload["detail"]


def test_a_session_lift_past_its_own_cap_holds_its_session(repo):
    """The session lift is judged on its own spend, not the global lift's cap."""
    lifts = _masked_fixture(repo, session_cap=4_000.0)
    result, payload = _dispatch(
        repo, backend="beta", session=SESSION, node="own-capped"
    )
    assert result.exit_code == 3
    assert payload["error"] == "budget-hold"
    assert lifts["session"] in payload["detail"]


def test_a_session_lift_judges_its_own_spend_on_the_fallback_path(repo):
    """A substitute in the masked group is re-judged with the same session lift."""
    _masked_fixture(repo)
    result, payload = _dispatch(repo, backend="alpha", session=SESSION, node="fallback")
    assert result.exit_code == 0, result.output
    assert payload.get("backend") == "beta"
