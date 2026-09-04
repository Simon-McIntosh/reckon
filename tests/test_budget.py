"""Budget-aware dispatch: what holds a wave, and — harder — what never does.

Every test is hermetic. ``RECKON_HOME`` moves the crew home into a temp tree,
ledgers are written under a throwaway repository, and no test spawns a harness or
reaches a network: the one exchange with a real account surface is replayed from
a recorded fixture.

The measures this file exists to demonstrate:

  - headroom is parsed wherever a backend publishes it, from a recorded run
    stream and from a recorded account-limit answer alike
  - a backend publishing no headroom reads ``unknown`` and a wave still opens on
    it — no test may show absence treated as exhaustion
  - a recorded exhaustion holds a wave with no worktree created and the node left
    ready rather than failed
  - holds are per-backend: one held backend leaves another dispatching
  - the reserve stops a dispatch that would leave nothing to answer a stuck
    worker with, while leaving that answer itself possible
  - only an opted-in backend with an undated exhaustion is probed, once per
    bounded cache interval rather than once per node
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _backends, budget, crew, ledger
from reckon.cli import main as cli_main

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "backends"

# Two backends, so every per-backend claim can be shown rather than asserted of a
# single one. Neither names a provider: both are config data a project supplies.
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
    moment = datetime.now(tz=timezone.utc) + timedelta(seconds=offset_seconds)
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


def _window_budget(
    utilisation: float,
    *,
    now: datetime,
    elapsed_fraction: float,
    period_minutes: float = 10080,
) -> dict:
    """A known reading at an exact point inside its quota window."""
    remaining_minutes = period_minutes * (1.0 - elapsed_fraction)
    block = _backends.unknown_budget("recorded by the backend's own report")
    block.update(
        {
            "headroom": "known",
            "utilisation_pct": utilisation,
            "rate_limit_period_minutes": period_minutes,
            "resets_at": (now + timedelta(minutes=remaining_minutes))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        }
    )
    return block


def _record(
    project: str,
    root: Path,
    *,
    backend: str,
    budget_block: dict,
    run_id: str,
    completed_at: str | None = None,
):
    """Promote one completed run carrying a budget block into the ledger.

    ``completed_at`` is the moment the reading was taken, which a test about the
    age of evidence has to own rather than inherit from the clock.
    """
    record = ledger.build_record(
        run_id=run_id,
        plan="plan-a",
        gate="passed",
        agent={"backend": backend},
        completed_at=completed_at or _stamp(-60),
        budget=budget_block,
    )
    ledger.append_run(project, record, root=root)
    return record


def _node(**overrides) -> crew.TaskNode:
    fields = {
        "id": "node-a",
        "goal": "record the account-limit read for one backend",
        "plan": "plan-a",
        "section": "§2",
        "done_when": "uv run pytest tests/test_budget.py reports 0 failures",
        "write_paths": ["reckon/budget.py"],
        "spec_level": "exact",
        "time_budget": "20m",
        "manifest_path": "/tmp/node-a-manifest.md",
    }
    fields.update(overrides)
    return crew.TaskNode(**fields)


# ── Headroom is parsed wherever a backend publishes it ──────────────────────


def test_a_recorded_run_stream_yields_utilisation_and_reset_time() -> None:
    """The backend that publishes headroom on its stream is read from a real run."""
    observation = _backends.observe_log(
        backend_name="beta",
        backend={"launch": "cli", "command": "claude"},
        log_path=FIXTURES / "claude-turn.jsonl",
    )
    block = observation.budget
    assert block["headroom"] == "known"
    assert isinstance(block["utilisation_pct"], (int, float))
    assert block["resets_at"] and block["resets_at"].endswith("Z")


def test_a_recorded_account_limit_answer_yields_utilisation_and_reset_time() -> None:
    """The other backend publishes headroom too — on its account surface."""
    lines = (FIXTURES / "codex-account-limits.jsonl").read_text().splitlines()
    answers = [json.loads(line) for line in lines if line.strip()]
    answer = next(item for item in answers if item.get("id") == 2)

    block = _backends.probe_budget(
        backend_name="alpha",
        backend={"launch": "cli", "command": "codex"},
        runner=lambda probe: answer,
    )
    assert block["headroom"] == "known"
    # The binding window is the one furthest through, not the first reported:
    # that is the window a wave would actually run into.
    assert block["utilisation_pct"] == 82.0
    assert block["rate_limit_type"] == "secondary"
    assert block["rate_limit_period_minutes"] == 10080
    assert block["resets_at"] == _backends._epoch_to_iso(1790600000)


def test_an_unreadable_probe_reports_unknown_rather_than_raising() -> None:
    """An instrument that fails must never become a hold."""
    block = _backends.probe_budget(
        backend_name="alpha",
        backend={"launch": "cli", "command": "codex"},
        runner=lambda probe: None,
    )
    assert block["headroom"] == "unknown"
    assert "no answer" in block["detail"]


# ── Unknown is honest, and never blocks ─────────────────────────────────────


def test_a_backend_with_no_headroom_signal_reads_unknown(home, repo) -> None:
    _record(
        "proj",
        repo,
        backend="alpha",
        budget_block=_backends.unknown_budget("backend reports no headroom"),
        run_id="r-1",
    )
    report = budget.preflight("proj", CONFIG, root=repo)
    state = next(item for item in report["backends"] if item["backend"] == "alpha")
    assert state["state"]["headroom"] == "unknown"


def test_burn_multiple_is_utilisation_divided_by_elapsed_window(home, repo) -> None:
    moment = datetime(2030, 1, 1, tzinfo=UTC)
    block = _window_budget(42.0, now=moment, elapsed_fraction=0.25)
    _record(
        "proj",
        repo,
        backend="alpha",
        budget_block=block,
        run_id="r-window-rate",
        completed_at=moment.isoformat(),
    )

    report = budget.preflight("proj", CONFIG, root=repo, backends=["alpha"], now=moment)
    state = report["backends"][0]["state"]

    assert state["burn_multiple"] == pytest.approx((42.0 / 100.0) / 0.25)


def test_high_burn_is_reported_without_holding_the_wave(home, repo) -> None:
    moment = datetime(2030, 1, 1, tzinfo=UTC)
    block = _window_budget(13.0, now=moment, elapsed_fraction=0.017)
    _record(
        "proj",
        repo,
        backend="alpha",
        budget_block=block,
        run_id="r-fast-burn",
        completed_at=moment.isoformat(),
    )

    report = budget.preflight("proj", CONFIG, root=repo, backends=["alpha"], now=moment)
    verdict = report["backends"][0]

    assert round(verdict["state"]["burn_multiple"], 1) == 7.6
    assert "utilisation 13% with burn multiple 7.6x" in verdict["reason"]
    assert "burn multiple 7.6x" in report["summary"]
    assert verdict["held"] is False
    assert report["held"] is False


def test_unknown_window_period_reports_unknown_burn(home, repo) -> None:
    _record(
        "proj",
        repo,
        backend="alpha",
        budget_block=_known(13.0),
        run_id="r-unknown-period",
    )

    report = budget.preflight("proj", CONFIG, root=repo, backends=["alpha"])
    state = report["backends"][0]["state"]

    assert state["burn_multiple"] is None
    assert "utilisation 13% with burn multiple unknown" in report["summary"]
    assert report["held"] is False


def test_crew_budget_view_exposes_the_burn_multiple(
    home, repo, monkeypatch, tmp_path
) -> None:
    from reckon import mcp

    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(tmp_path / "absent.yaml"))
    now = datetime.now(tz=UTC)
    block = _window_budget(13.0, now=now, elapsed_fraction=0.017)
    _record(
        "proj",
        repo,
        backend="native",
        budget_block=block,
        run_id="r-crew-budget",
    )

    report = mcp._crew("proj", view="budget", checkout_path=str(repo))
    state = report["backends"][0]["state"]

    assert report["ok"] is True
    assert round(state["burn_multiple"], 1) == 7.6


def test_a_wave_opens_on_a_backend_whose_headroom_is_unknown(home, repo) -> None:
    """Absence of a signal is not evidence of exhaustion, and never holds."""
    _record(
        "proj",
        repo,
        backend="alpha",
        budget_block=_backends.unknown_budget("backend reports no headroom"),
        run_id="r-1",
    )
    report = budget.preflight("proj", CONFIG, root=repo)
    assert report["held"] is False
    assert report["held_backends"] == []
    verdict = next(item for item in report["backends"] if item["backend"] == "alpha")
    assert "never read as exhaustion" in verdict["reason"]


def test_a_project_with_no_records_at_all_holds_nothing(home, repo) -> None:
    report = budget.preflight("proj", CONFIG, root=repo)
    assert report["held"] is False
    assert sorted(report["clear_backends"]) == ["alpha", "beta"]


def test_a_later_silence_does_not_erase_a_recorded_exhaustion(home, repo) -> None:
    """A silent run carries no information, so it must not outrank a measurement."""
    _record("proj", repo, backend="alpha", budget_block=_known(99.0), run_id="r-old")
    silent = ledger.build_record(
        run_id="r-new",
        plan="plan-a",
        gate="passed",
        agent={"backend": "alpha"},
        completed_at=_stamp(-1),
        budget=_backends.unknown_budget("no rate-limit event in the stream"),
    )
    ledger.append_run("proj", silent, root=repo)

    best = budget.latest_recorded("proj", root=repo)
    assert best["alpha"].budget["utilisation_pct"] == 99.0


def test_an_empty_rate_limit_mapping_cannot_displace_a_numeric_reading(
    home, repo
) -> None:
    _record("proj", repo, backend="beta", budget_block=_known(73.0), run_id="r-old")
    empty = _backends.dialect_for(CONFIG["backends"]["beta"])._budget({})
    silent = ledger.build_record(
        run_id="r-new",
        plan="plan-a",
        gate="passed",
        agent={"backend": "beta"},
        completed_at=_stamp(-1),
        budget=empty,
    )
    ledger.append_run("proj", silent, root=repo)

    best = budget.latest_recorded("proj", root=repo, config=CONFIG)

    assert empty["headroom"] == "unknown"
    assert best["beta"].budget["utilisation_pct"] == 73.0


def test_promotion_preserves_backend_when_the_agent_block_is_absent(home, repo) -> None:
    """The pointer's routing identity survives independently of agent metadata."""
    run_id = "r-promoted"
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "repo": str(repo),
            "worktree": str(repo),
            "node": {
                "id": "signal-reader",
                "plan": "plan-a",
                "section": "reading",
                "time_budget": "20m",
                "write_paths": ["reckon/budget.py"],
            },
            "role": "implement",
            "member": "",
            "backend": "beta",
            "agent": {},
            "created_at": _stamp(-60),
            "manifest_path": "/tmp/signal-reader-manifest.md",
            "session_id": None,
            "budget": _known(71.0),
        },
    )

    promoted = crew.complete(
        run_id,
        gate="passed",
        commits=[revision],
        changed_lines={"added": 0, "removed": 0, "files": 0},
    )
    readings = budget.latest_recorded("proj", root=repo)
    report = budget.preflight("proj", CONFIG, root=repo, backends=["beta"])
    state = report["backends"][0]["state"]

    assert promoted["record"]["agent"] == {}
    assert promoted["record"]["backend"] == "beta"
    assert promoted["record"]["commits"] == [revision]
    assert readings["beta"].budget["utilisation_pct"] == 71.0
    assert readings["beta"].attribution == "record"
    assert state["headroom"] == "known"
    assert state["utilisation_pct"] == 71.0


def test_stream_evidence_recovers_two_known_readings(home, repo) -> None:
    """Two durable measurements remain useful without rewriting their records."""
    stream_budget = _backends.observe_log(
        backend_name="beta",
        backend=CONFIG["backends"]["beta"],
        log_path=FIXTURES / "claude-turn.jsonl",
    ).budget
    for position, utilisation in enumerate((41.0, 42.0), start=1):
        reading = {**stream_budget, "utilisation_pct": utilisation}
        record = ledger.build_record(
            run_id=f"r-legacy-{position}",
            plan="plan-a",
            gate="passed",
            agent={},
            completed_at=_stamp(-120 + position),
            manifest_path=f"/tmp/worker/run-{position}/manifest.md",
            budget=reading,
        )
        ledger.append_run("proj", record, root=repo)

    all_readings = budget._readings("proj", root=repo, config=CONFIG)
    best = budget.latest_recorded("proj", root=repo, config=CONFIG)

    recovered = [reading for reading in all_readings if reading.backend == "beta"]
    assert len(recovered) == 2
    assert {reading.attribution for reading in recovered} == {"budget-evidence"}
    assert best["beta"].budget["utilisation_pct"] == 42.0


def test_stream_evidence_attribution_uses_the_numeric_signature(home, repo) -> None:
    stream_budget = _backends.observe_log(
        backend_name="beta",
        backend=CONFIG["backends"]["beta"],
        log_path=FIXTURES / "claude-turn.jsonl",
    ).budget
    record = ledger.build_record(
        run_id="r-unlabelled-stream",
        plan="plan-a",
        gate="passed",
        agent={},
        completed_at=_stamp(-60),
        budget=stream_budget,
    )
    ledger.append_run("proj", record, root=repo)

    reading = budget.latest_recorded("proj", root=repo, config=CONFIG)["beta"]

    assert reading.attribution == "budget-evidence"
    assert reading.budget["rate_limit_type"] == "overage"


def test_delivery_path_naming_another_harness_cannot_change_the_producer(
    home, repo
) -> None:
    stream_budget = _backends.observe_log(
        backend_name="beta",
        backend=CONFIG["backends"]["beta"],
        log_path=FIXTURES / "claude-turn.jsonl",
    ).budget
    record = ledger.build_record(
        run_id="r-misleading-path",
        plan="plan-a",
        gate="passed",
        agent={},
        completed_at=_stamp(-60),
        manifest_path="/tmp/alpha-39486/run/manifest.md",
        budget=stream_budget,
    )
    ledger.append_run("proj", record, root=repo)

    reading = budget.latest_recorded("proj", root=repo, config=CONFIG)["beta"]

    assert reading.record_id == "r-misleading-path"
    assert reading.attribution == "budget-evidence"


def test_duplicate_stream_interpreters_leave_the_producer_unattributed(
    home, repo
) -> None:
    stream_budget = _backends.observe_log(
        backend_name="beta",
        backend=CONFIG["backends"]["beta"],
        log_path=FIXTURES / "claude-turn.jsonl",
    ).budget
    record = ledger.build_record(
        run_id="r-ambiguous-producer",
        plan="plan-a",
        gate="passed",
        agent={},
        completed_at=_stamp(-60),
        budget=stream_budget,
    )
    ledger.append_run("proj", record, root=repo)
    ambiguous = {
        **CONFIG,
        "backends": {
            **CONFIG["backends"],
            "beta-peer": dict(CONFIG["backends"]["beta"]),
        },
    }

    readings = budget.latest_recorded("proj", root=repo, config=ambiguous)

    assert "beta" not in readings
    assert len(readings.unattributed) == 1
    assert readings.unattributed[0].record_id == "r-ambiguous-producer"


def test_unattributable_known_reading_reports_recorded_not_silent(home, repo) -> None:
    record = ledger.build_record(
        run_id="r-unmatched",
        plan="plan-a",
        gate="passed",
        agent={},
        completed_at=_stamp(-60),
        manifest_path="/tmp/worker/manifest.md",
        budget=_known(88.0),
    )
    ledger.append_run("proj", record, root=repo)

    report = budget.preflight("proj", CONFIG, root=repo, backends=["alpha"])
    verdict = report["backends"][0]

    assert verdict["state"]["headroom"] == "unknown"
    assert verdict["state"]["source"] == "unattributed-ledger"
    assert "recorded but could not be attributed" in verdict["reason"]
    assert report["unattributed_records"] == [
        {
            "observed_at": record["completed_at"],
            "record_id": "r-unmatched",
            "source": "ledger",
        }
    ]


def test_nothing_recorded_keeps_its_own_reason(home, repo) -> None:
    report = budget.preflight("empty-project", CONFIG, root=repo, backends=["alpha"])
    verdict = report["backends"][0]

    assert verdict["state"]["source"] == "none"
    assert "nothing recorded for this backend" in verdict["reason"]
    assert "could not be attributed" not in verdict["reason"]
    assert report["unattributed_records"] == []


# ── Exhaustion holds the wave, and creates nothing ──────────────────────────


def test_an_exhausted_backend_holds_the_wave_without_creating_a_worktree(
    home, repo
) -> None:
    _record("proj", repo, backend="alpha", budget_block=_known(100.0), run_id="r-1")

    with pytest.raises(crew.BudgetHold) as excinfo:
        crew.dispatch(
            node=_node(),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="sess",
            launcher=lambda *a, **k: 1,
        )

    verdict = excinfo.value.verdict
    assert verdict["held"] is True
    assert verdict["backend"] == "alpha"
    assert verdict["state"]["utilisation_pct"] == 100.0
    assert verdict["state"]["resets_at"]
    # Held, not failed: nothing exists to unwind and the node is still ready.
    assert crew.list_live() == []
    listed = subprocess.run(
        ["git", "worktree", "list"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "node-a" not in listed.stdout
    assert crew.validate_node(_node(), budget_ceiling="25m").ok


def test_a_recorded_quota_refusal_holds_the_next_wave(home, repo) -> None:
    """The refusal a backend wrote on its stream must reach the pre-flight.

    This is the loop the signal exists to close. The block under test is what a
    real recorded refusal parses to, and the failure it prevents is a pre-flight
    reporting a clear backend while the account behind it is spent — which is not
    hypothetical: it ran that way for six days.
    """
    refusal = _backends.refusal_budget(
        "You've hit your usage limit. Visit https://example.invalid/usage to "
        "purchase more credits or try again at Sep 8th, 2036 7:38 AM."
    )
    assert refusal is not None
    _record("proj", repo, backend="alpha", budget_block=refusal, run_id="r-refused")

    with pytest.raises(crew.BudgetHold) as excinfo:
        crew.dispatch(
            node=_node(),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="sess",
            launcher=lambda *a, **k: 1,
        )

    verdict = excinfo.value.verdict
    assert verdict["held"] is True
    assert verdict["state"]["resets_at"]
    assert verdict["state"]["seconds_until_reset"] > 0


def test_a_refused_window_that_has_reset_no_longer_holds(home, repo) -> None:
    """A spent window describes nothing once it has rolled over."""
    refusal = _backends.refusal_budget(
        "You've hit your usage limit. Try again at Jan 2nd, 2020 7:38 AM."
    )
    assert refusal is not None
    _record("proj", repo, backend="alpha", budget_block=refusal, run_id="r-stale")
    recorded = budget.latest_recorded("proj", root=repo, config=CONFIG)

    state = budget.state_for("alpha", recorded=recorded.get("alpha"))

    assert state.expired is True
    assert state.headroom == "unknown"
    assert budget.decide(state, budget.policy(CONFIG))["held"] is False


def test_a_declared_exhausted_status_holds_whatever_the_utilisation_reads(
    home, repo
) -> None:
    """The overage question is answered as data, so reckon enumerates nothing."""
    config = {**CONFIG, "budget": {**CONFIG["budget"], "exhausted_statuses": ["spent"]}}
    _record(
        "proj",
        repo,
        backend="alpha",
        budget_block=_known(12.0, status="spent"),
        run_id="r-1",
    )
    report = budget.preflight("proj", config, root=repo)
    assert report["held_backends"] == ["alpha"]
    assert "counts as exhausted" in report["backends"][0]["reason"]


def test_a_window_that_has_already_reset_stops_holding(home, repo) -> None:
    """One exhausted record must not hold a project forever."""
    _record(
        "proj",
        repo,
        backend="alpha",
        budget_block=_known(100.0, resets_in=-60),
        run_id="r-1",
    )
    report = budget.preflight("proj", CONFIG, root=repo)
    state = next(item for item in report["backends"] if item["backend"] == "alpha")
    assert state["state"]["expired"] is True
    assert state["state"]["headroom"] == "unknown"
    assert report["held"] is False


# ── A held lane hands over to its declared fallback ─────────────────────────


def test_a_held_backend_with_a_declared_fallback_dispatches_on_it(home, repo) -> None:
    """A held lane hands over rather than refuse, and the run says so.

    Measured motivation: an account spend limit killed seven workers across
    two waves while a locally served lane sat idle with headroom, and the
    substitution was done by hand. The run must record the lane asked for,
    the lane used and the hold that caused it, so a calibration slice never
    attributes a fallback run to the backend the caller named.
    """
    config = {
        **CONFIG,
        "backends": {
            **CONFIG["backends"],
            "alpha": {**CONFIG["backends"]["alpha"], "fallback": "beta"},
        },
    }
    _record("proj", repo, backend="alpha", budget_block=_known(100.0), run_id="r-1")

    record = crew.dispatch(
        node=_node(),
        project="proj",
        repo=repo,
        config=config,
        session="sess",
        launcher=lambda *a, **k: 1,
    )

    assert record["backend"] == "beta"
    assert record["agent"]["backend"] == "beta"
    fallback = record["budget_fallback"]
    assert fallback["requested_backend"] == "alpha"
    assert fallback["used_backend"] == "beta"
    assert fallback["hold"]["held"] is True
    assert fallback["hold"]["backend"] == "alpha"
    assert fallback["hold"]["state"]["utilisation_pct"] == 100.0
    assert [item["run_id"] for item in crew.list_live()] == [record["run_id"]]


def test_a_held_backend_with_no_declared_fallback_still_refuses(home, repo) -> None:
    """Absence of a declared fallback must never be filled in by guessing one.

    Both alpha and beta are defined in this config, but alpha names no
    fallback, so a hold on alpha still refuses exactly as before this
    mechanism existed — it must not silently land on beta merely because
    beta happens to be defined.
    """
    _record("proj", repo, backend="alpha", budget_block=_known(100.0), run_id="r-1")

    with pytest.raises(crew.BudgetHold) as excinfo:
        crew.dispatch(
            node=_node(),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="sess",
            launcher=lambda *a, **k: 1,
        )

    assert excinfo.value.verdict["backend"] == "alpha"
    assert crew.list_live() == []


def test_unknown_headroom_on_a_fallback_declaring_backend_neither_holds_nor_falls_back(
    home, repo
) -> None:
    """Absence of a signal is not evidence of exhaustion, fallback or not.

    A declared fallback must fire only on a genuine hold; a backend merely
    reporting unknown headroom dispatches on itself, unsubstituted, the same
    as a backend declaring no fallback at all.
    """
    config = {
        **CONFIG,
        "backends": {
            **CONFIG["backends"],
            "alpha": {**CONFIG["backends"]["alpha"], "fallback": "beta"},
        },
    }
    _record(
        "proj",
        repo,
        backend="alpha",
        budget_block=_backends.unknown_budget("backend reports no headroom"),
        run_id="r-1",
    )

    record = crew.dispatch(
        node=_node(),
        project="proj",
        repo=repo,
        config=config,
        session="sess",
        launcher=lambda *a, **k: 1,
    )

    assert record["backend"] == "alpha"
    assert record["budget_fallback"] is None


def test_a_fallback_that_is_itself_held_still_refuses(home, repo) -> None:
    """A held fallback must not chain into guessing a third lane.

    A declared fallback is a single named substitute, not a search: when the
    fallback is itself held, dispatch refuses on the fallback's own verdict.
    """
    config = {
        **CONFIG,
        "backends": {
            **CONFIG["backends"],
            "alpha": {**CONFIG["backends"]["alpha"], "fallback": "beta"},
        },
    }
    _record("proj", repo, backend="alpha", budget_block=_known(100.0), run_id="r-1")
    _record("proj", repo, backend="beta", budget_block=_known(100.0), run_id="r-2")

    with pytest.raises(crew.BudgetHold) as excinfo:
        crew.dispatch(
            node=_node(),
            project="proj",
            repo=repo,
            config=config,
            session="sess",
            launcher=lambda *a, **k: 1,
        )

    assert excinfo.value.verdict["backend"] == "beta"
    assert crew.list_live() == []


def test_a_fallback_naming_an_undefined_backend_is_refused(home, repo) -> None:
    """A fallback is declared data, so a broken declaration fails loud."""
    config = {
        **CONFIG,
        "backends": {
            **CONFIG["backends"],
            "alpha": {**CONFIG["backends"]["alpha"], "fallback": "ghost"},
        },
    }
    _record("proj", repo, backend="alpha", budget_block=_known(100.0), run_id="r-1")

    with pytest.raises(crew.CrewError, match="ghost"):
        crew.dispatch(
            node=_node(),
            project="proj",
            repo=repo,
            config=config,
            session="sess",
            launcher=lambda *a, **k: 1,
        )


# ── Holds are per-backend ───────────────────────────────────────────────────


def test_one_backend_held_leaves_ready_nodes_on_another_dispatching(home, repo) -> None:
    _record("proj", repo, backend="alpha", budget_block=_known(100.0), run_id="r-1")
    _record("proj", repo, backend="beta", budget_block=_known(10.0), run_id="r-2")

    report = budget.preflight("proj", CONFIG, root=repo)
    assert report["held_backends"] == ["alpha"]
    assert report["clear_backends"] == ["beta"]

    # And the node routed to the clear backend really does dispatch.
    record = crew.dispatch(
        node=_node(id="node-b", role="review", write_paths=["reckon/ledger.py"]),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
        launcher=lambda plan, *, log_path, stderr_path, prompt_path: (
            log_path.write_text("") or 4242
        ),
    )
    assert record["backend"] == "beta"
    assert Path(record["worktree"]).is_dir()


# ── The reserve protects the escape hatch ───────────────────────────────────


def test_the_reserve_holds_a_dispatch_but_still_allows_the_resume(home, repo) -> None:
    """Spending the last of a quota on a new node strands the wave it starts."""
    _record("proj", repo, backend="alpha", budget_block=_known(97.0), run_id="r-1")

    dispatching = budget.preflight("proj", CONFIG, root=repo, purpose="dispatch")
    resuming = budget.preflight("proj", CONFIG, root=repo, purpose="resume")

    assert dispatching["held_backends"] == ["alpha"]
    assert resuming["held_backends"] == []
    held = next(item for item in dispatching["backends"] if item["backend"] == "alpha")
    assert held["effective_ceiling_pct"] == 95.0
    assert held["ceiling_pct"] == 100.0


def test_a_resume_is_still_held_at_a_genuinely_spent_quota() -> None:
    state = budget.BudgetState(backend="alpha", headroom="known", utilisation_pct=100.0)
    verdict = budget.decide(state, budget.policy(CONFIG), purpose="resume")
    assert verdict["held"] is True
    assert verdict["effective_ceiling_pct"] == 100.0


# ── The pre-flight spends nothing ───────────────────────────────────────────


def test_the_preflight_spawns_no_process(home, repo, monkeypatch) -> None:
    """Free means free: any spawned process here would be a token cost."""

    def refuse(*args, **kwargs):
        raise AssertionError("the pre-flight spawned a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)

    _record("proj", repo, backend="alpha", budget_block=_known(100.0), run_id="r-1")
    report = budget.preflight("proj", CONFIG, root=repo)
    assert report["held_backends"] == ["alpha"]


def test_role_preflight_includes_every_specification_routing_backend(
    home, repo
) -> None:
    config = {
        **CONFIG,
        "roles": {
            **CONFIG["roles"],
            "implement": {
                "by_spec_level": {
                    "exact": {"backend": "beta", "time_budget": "3m"},
                    "guided": {"effort": "medium"},
                }
            },
        },
    }

    assert budget.backends_for_roles(config, ["implement"]) == ["alpha", "beta"]
    report = budget.preflight("proj", config, root=repo, roles=["implement"])
    assert [item["backend"] for item in report["backends"]] == ["alpha", "beta"]


def test_an_account_surface_is_read_only_when_the_backend_asks_for_it(
    home, repo
) -> None:
    """A read that has to be configured cannot happen by accident."""
    probed: list[str] = []

    def runner(probe):
        probed.append(probe.argv[0])
        return None

    budget.preflight("proj", CONFIG, root=repo, probe_runner=runner)
    assert probed == []

    asking = {
        **CONFIG,
        "backends": {
            **CONFIG["backends"],
            "alpha": {**CONFIG["backends"]["alpha"], "budget_check": True},
        },
    }
    budget.preflight("proj", asking, root=repo, probe_runner=runner)
    assert probed == ["codex"]


def test_a_known_account_reading_outranks_an_older_record(home, repo) -> None:
    """The surface describes now; a finished run describes whenever it ended."""
    _record("proj", repo, backend="alpha", budget_block=_known(10.0), run_id="r-1")
    answer = json.loads(
        next(
            line
            for line in (FIXTURES / "codex-account-limits.jsonl")
            .read_text()
            .splitlines()
            if '"id":2' in line
        )
    )
    asking = {
        **CONFIG,
        "backends": {
            **CONFIG["backends"],
            "alpha": {**CONFIG["backends"]["alpha"], "budget_check": True},
        },
    }
    report = budget.preflight(
        "proj", asking, root=repo, probe_runner=lambda probe: answer
    )
    state = next(item for item in report["backends"] if item["backend"] == "alpha")
    assert state["state"]["source"] == "account-surface"
    assert state["state"]["utilisation_pct"] == 82.0


def test_one_preflight_state_is_reused_by_three_dispatch_checks(home, repo) -> None:
    answers = [
        json.loads(line)
        for line in (FIXTURES / "codex-account-limits.jsonl").read_text().splitlines()
    ]
    answer = next(item for item in answers if item.get("id") == 2)
    asking = {
        **CONFIG,
        "backends": {
            **CONFIG["backends"],
            "alpha": {**CONFIG["backends"]["alpha"], "budget_check": True},
        },
    }
    probes = 0

    def runner(probe):
        nonlocal probes
        probes += 1
        return answer

    report = budget.preflight(
        "proj", asking, root=repo, backends=["alpha"], probe_runner=runner
    )
    shared_state = report["backends"][0]["state"]
    verdicts = [
        crew._budget_verdict(
            project="proj",
            root=repo,
            config=asking,
            backend_name="alpha",
            backend=asking["backends"]["alpha"],
            purpose="dispatch",
            budget_state=shared_state,
        )
        for _ in range(3)
    ]

    assert probes == 1
    assert all(verdict["state"]["utilisation_pct"] == 82.0 for verdict in verdicts)


def test_budget_history_failure_warns_without_aborting_dispatch(
    home, repo, monkeypatch
) -> None:
    monkeypatch.setattr(
        budget,
        "record_checks",
        lambda *args, **kwargs: (_ for _ in ()).throw(ledger.LedgerError("read-only")),
    )

    record = crew.dispatch(
        node=_node(),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="sess",
        launcher=lambda *args, **kwargs: 4242,
    )

    assert record["phase"] == "starting"
    assert record["warnings"] == [
        "budget check passed but its ledger history was not recorded: read-only"
    ]


# ── A hold reports like a dispatch ──────────────────────────────────────────


def test_a_hold_reports_on_all_four_axes_with_a_figure(home, repo) -> None:
    """A hold that looks like silence is indistinguishable from a crash."""
    _record("proj", repo, backend="alpha", budget_block=_known(100.0), run_id="r-1")
    _record("proj", repo, backend="beta", budget_block=_known(3.0), run_id="r-2")

    report = budget.preflight("proj", CONFIG, root=repo)
    verdict = crew.validate_summary(report["summary"], occasion="hold")
    assert verdict["ok"], verdict["findings"]
    assert "beta" in report["summary"]
    assert report["resume_after_seconds"] > 0
    assert report["resume_at"] == report["backends"][0]["state"]["resets_at"]


def test_a_clear_preflight_summary_reports_that_the_wave_can_open(home, repo) -> None:
    report = budget.preflight("proj", CONFIG, root=repo)
    assert "0 backend(s) held" in report["summary"]
    assert "the wave may open now" in report["summary"]


# ── Holds are committed measurements ───────────────────────────────────────


def test_recording_a_held_preflight_increments_the_ledger(home, repo) -> None:
    _record("proj", repo, backend="alpha", budget_block=_known(100.0), run_id="r-1")
    _data, version_before = ledger.load("proj", repo)

    report = budget.preflight("proj", CONFIG, root=repo, backends=["alpha"])
    budget.record_checks("proj", report["backends"], root=repo)

    history = ledger.holds("proj", repo)
    _data, version_after = ledger.load("proj", repo)
    assert version_after == version_before + 1
    assert len(history) == 1
    assert history[0]["backend"] == "alpha"
    assert history[0]["utilisation_pct"] == 100.0
    assert history[0]["resets_at"] == report["resume_at"]
    assert history[0]["purpose"] == "dispatch"


def test_a_hold_does_not_change_run_or_effort_measurements(home, repo) -> None:
    _record("proj", repo, backend="alpha", budget_block=_known(100.0), run_id="r-1")
    runs_before = ledger.runs("proj", repo)
    effort_before = ledger.effort_report("proj", root=repo, declared={"plan-a": "M"})

    report = budget.preflight("proj", CONFIG, root=repo, backends=["alpha"])
    budget.record_checks("proj", report["backends"], root=repo)

    assert ledger.runs("proj", repo) == runs_before
    assert (
        ledger.effort_report("proj", root=repo, declared={"plan-a": "M"})
        == effort_before
    )


def test_repeated_preflights_in_one_hold_window_write_one_record(home, repo) -> None:
    _record("proj", repo, backend="alpha", budget_block=_known(100.0), run_id="r-1")
    moment = datetime.now(tz=timezone.utc).replace(microsecond=0)

    first = budget.preflight("proj", CONFIG, root=repo, backends=["alpha"], now=moment)
    first_history = budget.record_checks(
        "proj", first["backends"], root=repo, now=moment
    )
    second = budget.preflight(
        "proj",
        CONFIG,
        root=repo,
        backends=["alpha"],
        now=moment + timedelta(seconds=90),
    )
    second_history = budget.record_checks(
        "proj",
        second["backends"],
        root=repo,
        now=moment + timedelta(seconds=90),
    )

    assert len(ledger.holds("proj", repo)) == 1
    assert first_history["version"] == second_history["version"]
    assert second_history["outcomes"][0]["action"] == "unchanged"


def test_a_clear_resume_closes_the_hold_with_actual_elapsed_time(
    home, repo, monkeypatch
) -> None:
    opened = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    reset = opened + timedelta(seconds=600)
    states = iter(
        [
            budget.BudgetState(
                backend="alpha",
                headroom="known",
                utilisation_pct=100.0,
                resets_at=reset.isoformat().replace("+00:00", "Z"),
            ),
            budget.BudgetState(
                backend="alpha",
                headroom="known",
                utilisation_pct=10.0,
                resets_at=reset.isoformat().replace("+00:00", "Z"),
            ),
        ]
    )
    monkeypatch.setattr(budget, "state_for", lambda *args, **kwargs: next(states))

    held = budget.preflight("proj", CONFIG, root=repo, backends=["alpha"], now=opened)
    budget.record_checks("proj", held["backends"], root=repo, now=opened)
    clear = budget.preflight(
        "proj",
        CONFIG,
        root=repo,
        backends=["alpha"],
        purpose="resume",
        now=opened + timedelta(seconds=137),
    )
    budget.record_checks(
        "proj",
        clear["backends"],
        root=repo,
        now=opened + timedelta(seconds=137),
        resumption_fired=True,
    )

    record = ledger.holds("proj", repo)[0]
    assert record["held_seconds"] == 137
    assert record["held_seconds"] != 600
    assert record["closed_by_purpose"] == "resume"
    assert record["resumption_fired"] is True


def test_a_stuck_worker_check_does_not_claim_a_scheduled_resumption(home, repo) -> None:
    opened = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    held = {
        "backend": "alpha",
        "purpose": "dispatch",
        "held": True,
        "effective_ceiling_pct": 95.0,
        "reason": "utilisation is at the effective ceiling",
        "state": {
            "utilisation_pct": 100.0,
            "resets_at": (opened + timedelta(seconds=600)).isoformat(),
        },
    }
    budget.record_checks("proj", [held], root=repo, now=opened)
    _record("proj", repo, backend="alpha", budget_block=_known(10.0), run_id="r-1")
    crew._write_json(
        crew.pointer_path("r-stuck"),
        {
            "run_id": "r-stuck",
            "launch": "cli",
            "session_id": "session-one",
            "backend": "alpha",
            "project": "proj",
            "repo": str(repo),
            "worktree": str(repo),
            "argv": ["codex"],
            "sandbox": "worktree-full",
        },
    )

    crew.resume_plan("r-stuck", "continue", config=CONFIG)

    record = ledger.holds("proj", repo)[0]
    assert record["closed_by_purpose"] == "resume"
    assert record["resumption_fired"] is False


def test_a_dispatch_path_records_its_hold_before_creating_any_worktree(
    home, repo
) -> None:
    _record("proj", repo, backend="alpha", budget_block=_known(100.0), run_id="r-1")

    with pytest.raises(crew.BudgetHold):
        crew.dispatch(
            node=_node(),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="sess",
            launcher=lambda *args, **kwargs: 1,
        )

    assert len(ledger.holds("proj", repo)) == 1
    assert ledger.holds("proj", repo)[0]["backend"] == "alpha"


# ── The command surface ─────────────────────────────────────────────────────


def test_cli_preflight_exits_three_when_a_backend_is_held(
    home, repo, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(tmp_path / "absent.yaml"))
    _record("proj", repo, backend="native", budget_block=_known(100.0), run_id="r-1")

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "preflight",
            "--project",
            "proj",
            "--checkout-path",
            str(repo),
        ],
    )
    payload = json.loads(result.output)
    assert result.exit_code == 3
    assert payload["held"] is True
    assert payload["held_backends"] == ["native"]
    assert payload["resume_at"]


def test_cli_preflight_exits_zero_when_every_backend_is_clear(
    home, repo, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(tmp_path / "absent.yaml"))
    result = CliRunner().invoke(
        cli_main,
        ["crew", "preflight", "--project", "proj", "--checkout-path", str(repo)],
    )
    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["held"] is False


def test_cli_dispatch_reports_a_hold_on_its_own_exit_code(home, repo) -> None:
    """A held node is retried later; a malformed one is rewritten. Different codes."""
    _record("proj", repo, backend="native", budget_block=_known(100.0), run_id="r-1")

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "dispatch",
            "--project",
            "proj",
            "--plan",
            "plan-a",
            "--node",
            "node-a",
            "--goal",
            "record the account-limit read for one backend",
            "--done-when",
            "uv run pytest tests/test_budget.py reports 0 failures",
            "--write-path",
            "reckon/budget.py",
            "--spec-level",
            "exact",
            "--session",
            "sess",
            "--repo",
            str(repo),
            "--checkout-path",
            str(repo),
        ],
    )
    payload = json.loads(result.output)
    assert result.exit_code == 3
    assert payload["error"] == "budget-hold"
    assert payload["hold"]["state"]["utilisation_pct"] == 100.0


# ── A judgement with no stated reset has a shelf life ───────────────────────
#
# The measured defect these close: a hold written from a provider refusal that
# named no reset time refused its lane for as long as the record survived. It
# could not clear itself — only a served run writes a budget record, and the
# hold refuses every run — and it could not be overridden, because a refusal
# records full utilisation while the ceiling is capped at the same figure. Every
# moment below is supplied by the test and every age derived from that pair, so
# no assertion here can rot into a date.


def _spent_with_no_stated_reset() -> dict:
    """What a refusal that named no reset time parses to: spent, undated."""
    block = _known(100.0)
    block.update({"resets_at": None, "threshold_status": "exhausted"})
    return block


def _aged_state(
    repo: Path,
    block: dict,
    *,
    observed: datetime,
    now: datetime,
    run_id: str = "r-aged",
):
    """One backend's state, read back the way the pre-flight reads it."""
    _record(
        "proj",
        repo,
        backend="alpha",
        budget_block=block,
        run_id=run_id,
        completed_at=observed.isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
    recorded = budget.latest_recorded("proj", root=repo, config=CONFIG)
    return budget.state_for("alpha", recorded=recorded.get("alpha"), now=now)


def test_an_undated_exhaustion_stops_holding_once_it_outlives_its_shelf_life(
    home, repo
) -> None:
    """A refusal naming no reset describes the past, and says how far past.

    The reason has to carry both figures: a reader who sees a lane released
    needs to know the reading's age and the bound it outran, or the release is
    as unarguable as the indefinite hold it replaces.
    """
    bound = budget.policy(CONFIG)["evidence_shelf_life_minutes"]
    observed = datetime.now(tz=UTC) - timedelta(minutes=bound * 4)
    now = observed + timedelta(minutes=bound * 3)
    state = _aged_state(repo, _spent_with_no_stated_reset(), observed=observed, now=now)

    assert state.headroom == "known"
    assert state.resets_at is None
    verdict = budget.decide(state, budget.policy(CONFIG), now=now)

    assert verdict["held"] is False
    assert verdict["state"]["headroom"] == "unknown"
    age_minutes = int((now - observed).total_seconds() // 60)
    assert f"{age_minutes} minutes old" in verdict["reason"]
    assert f"{bound:g} minute shelf life" in verdict["reason"]


def test_the_same_undated_exhaustion_still_holds_inside_its_shelf_life(
    home, repo
) -> None:
    """Fresh evidence of a spent lane is exactly what a hold is for."""
    bound = budget.policy(CONFIG)["evidence_shelf_life_minutes"]
    observed = datetime.now(tz=UTC) - timedelta(minutes=bound)
    now = observed + timedelta(minutes=bound / 2)
    state = _aged_state(repo, _spent_with_no_stated_reset(), observed=observed, now=now)

    verdict = budget.decide(state, budget.policy(CONFIG), now=now)

    assert verdict["held"] is True
    assert verdict["state"]["headroom"] == "known"
    assert "shelf life" not in verdict["reason"]


def test_a_stated_reset_governs_its_own_expiry_at_both_ages(home, repo) -> None:
    """The bound reaches only judgements that state no reset.

    A reading that names its window's end already decays through that, and a
    second mechanism aging it would release a hold whose window is demonstrably
    still open — the opposite failure, and the expensive one.
    """
    bound = budget.policy(CONFIG)["evidence_shelf_life_minutes"]
    observed = datetime.now(tz=UTC) - timedelta(minutes=bound * 4)
    inside = observed + timedelta(minutes=bound / 2)
    outside = observed + timedelta(minutes=bound * 3)
    # The window outlives both comparison moments, so only the shelf life could
    # release this hold — and it must not.
    block = _known(100.0)
    block["resets_at"] = (
        (outside + timedelta(minutes=bound))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    for index, moment in enumerate((inside, outside)):
        state = _aged_state(
            repo, block, observed=observed, now=moment, run_id=f"r-window-{index}"
        )
        verdict = budget.decide(state, budget.policy(CONFIG), now=moment)
        assert verdict["held"] is True, moment
        assert verdict["state"]["expired"] is False, moment
        assert "shelf life" not in verdict["reason"]


def test_a_shelf_life_of_zero_keeps_the_indefinite_hold(home, repo) -> None:
    """An indefinite hold is available, but only to a project that asks for one."""
    config = {
        **CONFIG,
        "budget": {**CONFIG["budget"], "evidence_shelf_life_minutes": 0},
    }
    bound = budget.policy(CONFIG)["evidence_shelf_life_minutes"]
    observed = datetime.now(tz=UTC) - timedelta(minutes=bound * 10)
    now = observed + timedelta(minutes=bound * 9)
    state = _aged_state(repo, _spent_with_no_stated_reset(), observed=observed, now=now)

    assert budget.decide(state, budget.policy(config), now=now)["held"] is True


def test_an_unset_shelf_life_still_ages_an_undated_hold(home, repo) -> None:
    """A project that declared no bound must not inherit the indefinite hold.

    This is the one place the module's usual `unset means permissive` reading
    inverts: the permissive reading of an absent bound is a hold nothing can
    clear, which is the failure rather than the safe default.
    """
    config = {**CONFIG, "budget": {"utilisation_ceiling_pct": 100}}
    bound = budget.policy(config)["evidence_shelf_life_minutes"]
    assert bound == budget.DEFAULT_SHELF_LIFE_MINUTES
    observed = datetime.now(tz=UTC) - timedelta(minutes=bound * 4)
    now = observed + timedelta(minutes=bound * 3)
    state = _aged_state(repo, _spent_with_no_stated_reset(), observed=observed, now=now)

    assert budget.decide(state, budget.policy(config), now=now)["held"] is False


# ── Observing a run cannot renew a hold observing did not create ───────────
#
# Measured defect: the ageing rule took its age from the live pointer's
# observed_at, and `observe()` rewrites that stamp on every call while the
# stream it reads still carries the original rejection — so a coordinator
# investigating why its lane was held extended the hold by looking, one
# re-read at a time, forever.


def _live_pointer(
    project: str,
    run_id: str,
    *,
    backend: str,
    budget_block: dict,
    created_at: str,
) -> dict:
    """A minimal live pointer ``observe()`` can fold without a backend log.

    ``launch`` is deliberately not ``"cli"``: that is the one field
    ``observe()`` checks before reaching for a log file, and any other value
    takes the branch that touches nothing but the pointer's own bookkeeping —
    exactly what re-reading an already-refused run does against the real
    dialects too, since the refusal already lives in ``budget`` rather than
    needing to be re-parsed.
    """
    record = {
        "run_id": run_id,
        "project": project,
        "backend": backend,
        "launch": "in-harness",
        "phase": "working",
        "budget": budget_block,
        "created_at": created_at,
        "observed_at": created_at,
    }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def test_observing_a_refused_run_does_not_renew_its_hold(home, repo) -> None:
    """Re-reading a refused run's stream must not restamp an old refusal as new.

    Asserted through the same ``decide`` call preflight itself makes, over a
    state the ledger reader built from a live pointer carrying a real
    rejection — as one case, observe-then-decide, rather than deciding once
    and reasoning separately about what a later observe would do.
    """
    bound = budget.policy(CONFIG)["evidence_shelf_life_minutes"]
    refused_at = datetime.now(tz=UTC) - timedelta(minutes=bound * 3)
    stamp = refused_at.isoformat(timespec="seconds").replace("+00:00", "Z")
    record = _live_pointer(
        "proj",
        "r-observed-refusal",
        backend="alpha",
        budget_block=_spent_with_no_stated_reset(),
        created_at=stamp,
    )

    before = budget.preflight("proj", CONFIG, backends=["alpha"], root=repo)
    verdict_before = before["backends"][0]
    assert verdict_before["held"] is False
    assert verdict_before["state"]["headroom"] == "unknown"

    for _ in range(10):
        crew.observe(record["run_id"], config=CONFIG)

    refreshed = crew.read_pointer(record["run_id"])
    # observe() really did rewrite the mutable stamp, so this assertion would
    # fail to distinguish the fix from the defect if it had not.
    assert refreshed["observed_at"] != stamp
    assert refreshed["created_at"] == stamp

    after = budget.preflight("proj", CONFIG, backends=["alpha"], root=repo)
    verdict_after = after["backends"][0]

    assert verdict_after["held"] == verdict_before["held"] is False
    assert verdict_after["state"]["headroom"] == verdict_before["state"]["headroom"]
    assert verdict_after["reason"] == verdict_before["reason"]


def test_observing_a_fresh_refusal_still_holds(home, repo) -> None:
    """A genuinely fresh refusal keeps holding across a re-read, not just an old one."""
    refused_at = datetime.now(tz=UTC) - timedelta(seconds=5)
    stamp = refused_at.isoformat(timespec="seconds").replace("+00:00", "Z")
    record = _live_pointer(
        "proj",
        "r-fresh-refusal",
        backend="alpha",
        budget_block=_spent_with_no_stated_reset(),
        created_at=stamp,
    )

    before = budget.preflight("proj", CONFIG, backends=["alpha"], root=repo)
    assert before["backends"][0]["held"] is True

    crew.observe(record["run_id"], config=CONFIG)

    after = budget.preflight("proj", CONFIG, backends=["alpha"], root=repo)
    assert after["backends"][0]["held"] is True
    assert "shelf life" not in after["backends"][0]["reason"]


def test_observing_a_stated_reset_is_not_aged_by_the_observe(home, repo) -> None:
    """A hold whose refusal names its own reset time is governed by that time,
    not by the shelf life the observe-then-decide path exists to protect
    against — asserted over the same live-pointer-plus-observe path as the
    undated case above, so both paths stay reachable.
    """
    bound = budget.policy(CONFIG)["evidence_shelf_life_minutes"]
    refused_at = datetime.now(tz=UTC) - timedelta(minutes=bound * 4)
    stamp = refused_at.isoformat(timespec="seconds").replace("+00:00", "Z")
    block = _known(100.0, resets_in=int(timedelta(minutes=bound * 5).total_seconds()))
    record = _live_pointer(
        "proj",
        "r-dated-refusal",
        backend="alpha",
        budget_block=block,
        created_at=stamp,
    )

    before = budget.preflight("proj", CONFIG, backends=["alpha"], root=repo)
    assert before["backends"][0]["held"] is True

    crew.observe(record["run_id"], config=CONFIG)

    after = budget.preflight("proj", CONFIG, backends=["alpha"], root=repo)
    assert after["backends"][0]["held"] is True
    assert "shelf life" not in after["backends"][0]["reason"]


# ── An undated hold is decided by a lane probe ─────────────────────────────


def _probing_config(*backend_names: str, cache_seconds: float = 30) -> dict:
    """Enable the serving probe only on the named configured backends."""
    return {
        **CONFIG,
        "backends": {
            name: {
                **CONFIG["backends"][name],
                "budget_check": True,
            }
            for name in backend_names
        },
        "budget": {
            **CONFIG["budget"],
            "evidence_shelf_life_minutes": cache_seconds / 60,
        },
    }


def test_a_served_lane_probe_opens_an_undated_hold_and_records_the_observation(
    home, repo
) -> None:
    checked_at = datetime(2026, 9, 4, 4, 0, tzinfo=UTC)
    _record(
        "proj",
        repo,
        backend="alpha",
        budget_block=_spent_with_no_stated_reset(),
        run_id="r-undated",
        completed_at=(checked_at - timedelta(minutes=5)).isoformat(),
    )
    probes: list[str] = []

    def served(backend_name, _backend):
        probes.append(backend_name)
        return {"status": "served", "detail": "minimal request was served"}

    report = budget.preflight(
        "proj",
        _probing_config("alpha"),
        backends=["alpha"],
        root=repo,
        now=checked_at,
        probe_runner=lambda _probe: None,
        lane_probe_runner=served,
    )
    cached_report = budget.preflight(
        "proj",
        _probing_config("alpha"),
        backends=["alpha"],
        root=repo,
        now=checked_at + timedelta(seconds=10),
        probe_runner=lambda _probe: None,
        lane_probe_runner=served,
    )

    verdict = report["backends"][0]
    assert probes == ["alpha"]
    assert verdict["held"] is False
    assert verdict["state"]["availability"] == "served"
    assert verdict["state"]["availability_observed_at"] == "2026-09-04T04:00:00Z"
    assert verdict["state"]["source"] == "lane-probe"
    assert report["policy"]["availability_probe_cache_seconds"] == 30
    assert "served" in verdict["reason"]
    assert cached_report["backends"][0]["held"] is False
    assert cached_report["backends"][0]["state"]["availability_cached"] is True


def test_a_refused_lane_probe_rearms_the_hold_and_is_reused_only_within_its_cache(
    home, repo
) -> None:
    first_check = datetime(2026, 9, 4, 4, 0, tzinfo=UTC)
    _record(
        "proj",
        repo,
        backend="alpha",
        budget_block=_spent_with_no_stated_reset(),
        run_id="r-undated",
        completed_at=(first_check - timedelta(hours=4)).isoformat(),
    )
    answers = iter(
        [
            {
                "status": "refused",
                "detail": "minimal request was refused",
                "budget": _spent_with_no_stated_reset(),
            },
            {"status": "served", "detail": "minimal request was served"},
        ]
    )
    probes = 0

    def probe(_backend_name, _backend):
        nonlocal probes
        probes += 1
        return next(answers)

    config = _probing_config("alpha", cache_seconds=30)
    refused = budget.preflight(
        "proj",
        config,
        backends=["alpha"],
        root=repo,
        now=first_check,
        probe_runner=lambda _probe: None,
        lane_probe_runner=probe,
    )["backends"][0]
    cached = budget.preflight(
        "proj",
        config,
        backends=["alpha"],
        root=repo,
        now=first_check + timedelta(seconds=20),
        probe_runner=lambda _probe: None,
        lane_probe_runner=probe,
    )["backends"][0]
    served = budget.preflight(
        "proj",
        config,
        backends=["alpha"],
        root=repo,
        now=first_check + timedelta(seconds=31),
        probe_runner=lambda _probe: None,
        lane_probe_runner=probe,
    )["backends"][0]

    assert refused["held"] is True
    assert refused["state"]["availability"] == "refused"
    assert refused["state"]["observed_at"] == "2026-09-04T04:00:00Z"
    assert cached["held"] is True
    assert cached["state"]["availability_cached"] is True
    assert served["held"] is False
    assert probes == 2


def test_lane_probes_are_once_per_backend_not_once_per_record(home, repo) -> None:
    checked_at = datetime(2026, 9, 4, 4, 0, tzinfo=UTC)
    for index in range(10):
        _record(
            "proj",
            repo,
            backend="alpha",
            budget_block=_spent_with_no_stated_reset(),
            run_id=f"r-alpha-{index}",
            completed_at=(checked_at - timedelta(seconds=index + 1)).isoformat(),
        )
    _record(
        "proj",
        repo,
        backend="beta",
        budget_block=_spent_with_no_stated_reset(),
        run_id="r-beta",
        completed_at=(checked_at - timedelta(seconds=1)).isoformat(),
    )
    probes: list[str] = []

    def served(backend_name, _backend):
        probes.append(backend_name)
        return {"status": "served"}

    report = budget.preflight(
        "proj",
        _probing_config("alpha", "beta"),
        backends=["alpha", "beta"],
        root=repo,
        now=checked_at,
        probe_runner=lambda _probe: None,
        lane_probe_runner=served,
    )

    assert probes == ["alpha", "beta"]
    assert report["held"] is False


def test_a_reset_bearing_hold_and_an_unreported_backend_are_not_probed(
    home, repo
) -> None:
    checked_at = datetime.now(tz=UTC).replace(microsecond=0)
    _record(
        "proj",
        repo,
        backend="alpha",
        budget_block=_known(100.0, resets_in=600),
        run_id="r-dated",
        completed_at=checked_at.isoformat(),
    )

    def forbidden(_backend_name, _backend):
        raise AssertionError("this backend has no undated exhaustion to probe")

    report = budget.preflight(
        "proj",
        _probing_config("alpha", "beta"),
        backends=["alpha", "beta"],
        root=repo,
        now=checked_at,
        probe_runner=lambda _probe: None,
        lane_probe_runner=forbidden,
    )

    alpha, beta = report["backends"]
    assert alpha["held"] is True
    assert alpha["state"]["resets_at"] is not None
    assert beta["held"] is False
    assert beta["state"]["headroom"] == "unknown"
