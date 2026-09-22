"""The pre-flight names the pace before a wave opens, and never invents one.

A coordinator reading a pre-flight has to be able to see both metered clocks
with the age of each reading, the allowance the week allows and the bar a stated
ready set is judged against -- and to route on which of those nodes the bar
would admit, to which lane. The cases below pin the payload a caller reads.

Two properties are the reason the file exists rather than a shape check. A
group no reading reached reports unknown for its clocks and its allowance and
never a zero, because a zero utilisation reads as an empty window and admits
everything, which is the substitution this module refuses for a hold and must
refuse for a pace. And an age is reported even when the reading is seconds old,
so a reader never has to treat a missing age as evidence of a fresh one.

Every assertion below reads the emitted payload -- a returned mapping's fields --
and never a rendered summary string, because the summary is prose a reader parses
and the payload is what a caller routes on.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import budget, ledger
from reckon.cli import main as cli_main
from reckon.crew import bar as bar_module
from reckon.crew import pace as pace_module
from reckon.crew import window_reading

# A fixed instant, so every age and every reset stamp is arithmetic rather than
# a stroke of the clock. A test that read the wall clock here would pass on the
# day it was written and drift afterwards.
NOW = datetime(2026, 9, 21, 18, 0, 0, tzinfo=UTC)

# The keys the pre-flight reported before pace was added to it. Listed explicitly
# rather than sampled: the point of the assertion below is that this addition is
# an addition, so a field that quietly disappeared has to fail the test.
PREFLIGHT_FIELDS = {
    "project",
    "purpose",
    "checked_at",
    "policy",
    "held",
    "held_backends",
    "clear_backends",
    "backends",
    "unattributed_records",
    "resume_after_seconds",
    "resume_at",
    "summary",
}

# Four backends: two sharing one declared wallet, one on a wallet of its own, and
# one declaring no wallet at all -- so "one entry per declared group" is counted
# rather than named, and the ungrouped lane is present to be left out.
CONFIG = {
    "default_backend": "sol-a",
    "backends": {
        "sol-a": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "budget_group": "sol",
        },
        "sol-b": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "budget_group": "sol",
        },
        "other-a": {
            "launch": "cli",
            "command": "claude",
            "sandbox": "worktree-full",
            "budget_group": "other",
        },
        "orphan": {"launch": "cli", "command": "codex", "sandbox": "worktree-full"},
    },
    "roles": {"implement": {}, "review": {"backend": "other-a"}},
    "budget": {
        "utilisation_ceiling_pct": 100,
        "resume_placeholder": None,
        "resume_reserve_pct": 5,
        "exhausted_statuses": [],
    },
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _reading(
    five_hour: float,
    seven_day: float,
    *,
    age_seconds: float = 5.0,
    five_reset_in_hours: float = 1.0,
    week_reset_in_hours: float = 100.0,
) -> window_reading.WindowReading:
    """One backend's window report, aged exactly ``age_seconds`` against ``NOW``."""
    observed = NOW - timedelta(seconds=age_seconds)
    figures = (
        window_reading.WindowFigure(
            period="five_hour",
            utilisation=five_hour,
            observed_at=observed,
            age_seconds=age_seconds,
            resets_at=_iso(NOW + timedelta(hours=five_reset_in_hours)),
        ),
        window_reading.WindowFigure(
            period="seven_day",
            utilisation=seven_day,
            observed_at=observed,
            age_seconds=age_seconds,
            resets_at=_iso(NOW + timedelta(hours=week_reset_in_hours)),
        ),
    )
    return window_reading.WindowReading(
        figures=figures, observed_at=observed, age_seconds=age_seconds
    )


def _group(report: list[dict], name: str) -> dict:
    found = [entry for entry in report if entry["group"] == name]
    assert len(found) == 1, f"expected exactly one entry for {name!r}, got {found}"
    return found[0]


def _stamped(minutes_ago: float) -> dict:
    moment = NOW - timedelta(minutes=minutes_ago)
    return {"type": "assistant", "timestamp": moment.isoformat()}


def _window_event(five_hour: float, seven_day: float) -> dict:
    return {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "allowed",
            "rate_limit_type": "five_hour",
            "unifiedWindows": {
                "five_hour": {"utilization": five_hour, "resetsAt": 1787751600},
                "seven_day": {"utilization": seven_day, "resetsAt": 1788206400},
            },
        },
    }


# ── Both clocks, each with the age of its own reading ───────────────────────


def test_a_declared_group_reports_both_clocks_with_the_age_of_each_reading() -> None:
    """The payload carries each clock's figure beside the age of its reading."""
    report = budget.group_pace(
        CONFIG,
        windows={"sol-b": _reading(0.64, 0.41, age_seconds=120.0)},
        now=NOW,
    )

    sol = _group(report, "sol")
    assert sol["state"] == budget.OBSERVED
    assert sol["member"] == "sol-b"
    for period, figure in (("five_hour", 0.64), ("seven_day", 0.41)):
        clock = sol["clocks"][period]
        assert clock["period"] == period
        assert clock["utilisation"] == figure
        assert clock["age_seconds"] == 120.0
        assert clock["observed_at"] == (NOW - timedelta(seconds=120)).isoformat()
        assert clock["state"] == budget.OBSERVED


def test_the_group_is_read_once_from_its_freshest_member() -> None:
    """One wallet, one reading: the newest dated reading speaks for the group."""
    report = budget.group_pace(
        CONFIG,
        windows={
            "sol-a": _reading(0.10, 0.10, age_seconds=900.0),
            "sol-b": _reading(0.77, 0.33, age_seconds=30.0),
        },
        now=NOW,
    )

    sol = _group(report, "sol")
    assert sol["members"] == ["sol-a", "sol-b"]
    assert sol["member"] == "sol-b"
    assert sol["clocks"]["five_hour"]["utilisation"] == 0.77
    assert sol["clocks"]["five_hour"]["age_seconds"] == 30.0


def test_an_undated_member_never_supplies_the_groups_clocks() -> None:
    """A reading that cannot be aged does not compete with one that can."""
    undated = window_reading.WindowReading(
        figures=(
            window_reading.WindowFigure(
                period="five_hour",
                utilisation=0.99,
                observed_at=NOW,
                age_seconds=0.0,
            ),
        ),
        observed_at=None,
        age_seconds=None,
    )
    report = budget.group_pace(
        CONFIG,
        windows={"sol-a": undated, "sol-b": _reading(0.21, 0.22, age_seconds=60.0)},
        now=NOW,
    )

    sol = _group(report, "sol")
    assert sol["member"] == "sol-b"
    assert sol["clocks"]["five_hour"]["utilisation"] == 0.21


def test_an_age_is_reported_even_when_the_reading_is_fresh() -> None:
    """A fresh age is a measurement; a missing one would have to be inferred."""
    report = budget.group_pace(
        CONFIG, windows={"sol-a": _reading(0.5, 0.5, age_seconds=3.0)}, now=NOW
    )

    clock = _group(report, "sol")["clocks"]["five_hour"]
    assert clock["age_seconds"] == 3.0
    assert clock["state"] == budget.OBSERVED


def test_the_reading_may_be_a_stream_and_the_reader_sets_its_age() -> None:
    """A stream source is resolved through the window reader, not read here."""
    events = [
        _stamped(90),
        _window_event(0.31, 0.44),
        _stamped(89),
        _stamped(1),
    ]
    report = budget.group_pace(CONFIG, windows={"sol-a": events}, now=NOW)

    clock = _group(report, "sol")["clocks"]["five_hour"]
    assert clock["utilisation"] == 0.31
    assert clock["age_seconds"] == pytest.approx(89 * 60.0)


# ── The allowance is the derivation over the week's own clocks ──────────────


def test_the_allowance_is_the_delta_the_week_derives_from_those_clocks() -> None:
    """Recomputed from the emitted clocks, so drift from the module would show."""
    report = budget.group_pace(
        CONFIG,
        windows={"sol-a": _reading(0.2, 0.3, week_reset_in_hours=100.0)},
        now=NOW,
    )

    clock = _group(report, "sol")["clocks"]["seven_day"]
    resets_at = datetime.fromisoformat(clock["resets_at"])
    elapsed_hours = pace_module.WEEK_HOURS - (resets_at - NOW).total_seconds() / 3600.0
    expected = pace_module.allowance_for_group(
        pace_module.GroupReading(
            group="sol",
            utilisation=clock["utilisation"],
            elapsed_hours=elapsed_hours,
        ),
        config=CONFIG,
    )

    allowance = _group(report, "sol")["allowance"]
    assert allowance["elapsed_hours"] == pytest.approx(elapsed_hours)
    assert allowance["derived"] == pytest.approx(expected.derived)
    assert allowance["remaining_windows"] == pytest.approx(expected.remaining_windows)
    assert allowance["effective_limit"] == pytest.approx(expected.effective_limit)
    assert allowance["pace_multiple"] == pace_module.DEFAULT_PACE_MULTIPLE
    assert allowance["limited_by"] == expected.limited_by


def test_a_weekly_clock_with_no_readable_reset_yields_an_unknown_allowance() -> None:
    """A clock that cannot place itself in the week cannot divide it."""
    report = budget.group_pace(
        CONFIG,
        windows={
            "sol-a": window_reading.WindowReading(
                figures=(
                    window_reading.WindowFigure(
                        period="seven_day",
                        utilisation=0.3,
                        observed_at=NOW - timedelta(seconds=5),
                        age_seconds=5.0,
                    ),
                ),
                observed_at=NOW - timedelta(seconds=5),
                age_seconds=5.0,
            )
        },
        now=NOW,
    )

    allowance = _group(report, "sol")["allowance"]
    assert allowance["state"] == budget.UNKNOWN
    assert allowance["utilisation"] == 0.3
    assert allowance["derived"] is None
    assert allowance["effective_limit"] is None
    assert allowance["reason"]


# ── The bar, drawn against the window that fills ────────────────────────────


def test_the_bar_is_drawn_against_the_five_hour_window_not_the_week() -> None:
    """The two clocks disagree here on purpose: only one of them is the fill."""
    report = budget.group_pace(CONFIG, windows={"sol-a": _reading(0.1, 0.9)}, now=NOW)

    bar = _group(report, "sol")["bar"]
    assert bar["window_fill"] == 0.1
    assert bar["state"] == budget.OBSERVED


def test_the_bar_value_is_the_one_the_bar_module_returns_at_that_fill() -> None:
    """The figure is the module's, not a curve restated in this payload."""
    report = budget.group_pace(
        CONFIG,
        windows={"sol-a": _reading(0.2, 0.3)},
        ready=[{"name": "mid", "group": "sol", "score": 0.6}],
        now=NOW,
    )

    bar = _group(report, "sol")["bar"]
    entry = bar["recommendations"][0]
    expected = bar_module.recommend(bar["window_fill"], 0.6)
    assert entry["verdict"] == expected.verdict
    assert entry["bar"] == pytest.approx(expected.bar)
    assert entry["margin"] == pytest.approx(expected.margin)
    assert entry["decided_by"] == "window"


def test_a_stated_ready_set_is_named_by_verdict_and_bucketed_by_lane() -> None:
    """Which nodes the bar admits, and to which lane, in the four outcomes.

    The fill is pinned at the middle of the domain, where the bar is 0.5, so
    each score below lands in a different band and the four verdicts appear in
    one payload rather than being asserted one at a time.
    """
    report = budget.group_pace(
        CONFIG,
        windows={"sol-a": _reading(0.5, 0.5)},
        ready=[
            {"name": "prescribed", "group": "sol", "score": 0.1},
            {"name": "clears", "group": "sol", "score": 0.5},
            {"name": "close-call", "group": "sol", "score": 0.4},
            {"name": "too-open", "group": "sol", "score": 0.3},
        ],
        now=NOW,
    )

    bar = _group(report, "sol")["bar"]
    verdicts = {entry["name"]: entry["verdict"] for entry in bar["recommendations"]}
    assert verdicts == {
        "prescribed": bar_module.SEND_LOCAL,
        "clears": bar_module.SEND_METERED,
        "close-call": bar_module.SPLIT,
        "too-open": bar_module.HOLD,
    }
    assert bar["admitted"] == [
        {"name": "prescribed", "verdict": bar_module.SEND_LOCAL},
        {"name": "clears", "verdict": bar_module.SEND_METERED},
    ]
    assert bar["split"] == ["close-call"]
    assert bar["held"] == ["too-open"]


def test_one_fixed_score_travels_toward_hold_as_the_window_fills() -> None:
    """The bar becomes stricter as the window fills: the fill is read, not fixed."""
    ready = [{"name": "mid", "group": "sol", "score": 0.6}]
    early = budget.group_pace(
        CONFIG, windows={"sol-a": _reading(0.1, 0.1)}, ready=ready, now=NOW
    )
    late = budget.group_pace(
        CONFIG, windows={"sol-a": _reading(0.98, 0.1)}, ready=ready, now=NOW
    )

    early_verdict = _group(early, "sol")["bar"]["recommendations"][0]["verdict"]
    late_verdict = _group(late, "sol")["bar"]["recommendations"][0]["verdict"]
    assert early_verdict == bar_module.SEND_METERED
    assert late_verdict == bar_module.HOLD
    assert early_verdict != late_verdict


def test_the_buckets_partition_the_stated_ready_set_exactly() -> None:
    """Every stated node lands in exactly one bucket, and none is dropped."""
    names = ["prescribed", "clears", "close-call", "too-open"]
    report = budget.group_pace(
        CONFIG,
        windows={"sol-a": _reading(0.5, 0.5)},
        ready=[
            {"name": "prescribed", "group": "sol", "score": 0.1},
            {"name": "clears", "group": "sol", "score": 0.5},
            {"name": "close-call", "group": "sol", "score": 0.4},
            {"name": "too-open", "group": "sol", "score": 0.3},
        ],
        now=NOW,
    )

    bar = _group(report, "sol")["bar"]
    placed = (
        [entry["name"] for entry in bar["admitted"]]
        + bar["split"]
        + bar["held"]
        + bar["undecided"]
    )
    assert sorted(placed) == sorted(names)
    assert sorted(entry["name"] for entry in bar["recommendations"]) == sorted(names)


def test_a_ready_node_keeps_its_own_groups_bar() -> None:
    """A wave's nodes are judged against the wallet they will spend.

    Each group is asserted to see only its own nodes. A bar built over every
    group's nodes at once would still return a verdict for the first entry of
    each bucket -- it would just be judging work against a wallet it will never
    spend, which is why the membership is named rather than sampled.
    """
    report = budget.group_pace(
        CONFIG,
        windows={"sol-a": _reading(0.5, 0.5), "other-a": _reading(0.02, 0.02)},
        ready=[
            {"name": "on-sol", "group": "sol", "score": 0.4},
            {"name": "on-other", "group": "other", "score": 0.4},
        ],
        now=NOW,
    )

    sol_bar = _group(report, "sol")["bar"]
    other_bar = _group(report, "other")["bar"]
    assert [entry["name"] for entry in sol_bar["recommendations"]] == ["on-sol"]
    assert [entry["name"] for entry in other_bar["recommendations"]] == ["on-other"]
    assert sol_bar["recommendations"][0]["verdict"] == bar_module.SPLIT
    assert other_bar["recommendations"][0]["verdict"] == bar_module.SEND_METERED


# ── Unknown is unknown, and never a zero ───────────────────────────────────


def test_an_unread_group_reports_unknown_clocks_and_allowance_and_never_zero() -> None:
    """Absence of a reading is not an empty window and not a free allowance.

    The assertion is deliberately two-sided: the fields are ``None`` *and* they
    are not ``0.0``. A zero utilisation would read as a wide-open window and
    admit everything, which is the substitution this module refuses a hold.
    """
    report = budget.group_pace(CONFIG, now=NOW)

    other = _group(report, "other")
    assert other["state"] == budget.UNKNOWN
    assert other["member"] is None
    for period in ("five_hour", "seven_day"):
        clock = other["clocks"][period]
        assert clock["state"] == budget.UNKNOWN
        assert clock["utilisation"] is None
        assert clock["age_seconds"] is None
        assert clock["utilisation"] not in (0.0, 1.0)

    allowance = other["allowance"]
    assert allowance["state"] == budget.UNKNOWN
    for key in (
        "utilisation",
        "elapsed_hours",
        "drain_hours",
        "remaining_budget",
        "remaining_windows",
        "pace_multiple",
        "derived",
        "provider_ceiling",
        "effective_limit",
    ):
        assert allowance[key] is None, key
    assert allowance["reason"]

    bar = other["bar"]
    assert bar["window_fill"] is None
    assert bar["state"] == budget.UNKNOWN


def test_an_ungrouped_backend_is_reported_under_no_group_at_all() -> None:
    """One entry per declared group: a lane declaring none is not in the report."""
    report = budget.group_pace(CONFIG, windows={"orphan": _reading(0.9, 0.9)}, now=NOW)

    assert sorted(entry["group"] for entry in report) == ["other", "sol"]
    assert all("orphan" not in entry["members"] for entry in report)


def test_a_prescribed_node_is_decided_without_a_window_and_an_open_one_is_not() -> None:
    """Prescription is judged before the window, so it needs no fill to decide.

    A node whose shape is read off its own record goes to the free lane at any
    fill, so its outcome is decidable with no window read at all. A node with
    open-endedness left to invent is not: which lane it needs depends on the
    fill, and asserting one without a reading would be inventing the very
    figure the pre-flight exists to report.
    """
    report = budget.group_pace(
        CONFIG,
        ready=[
            {"name": "prescribed", "group": "other", "score": 0.0},
            {"name": "open", "group": "other", "score": 0.9},
        ],
        now=NOW,
    )

    bar = _group(report, "other")["bar"]
    assert bar["window_fill"] is None
    assert bar["state"] == budget.UNKNOWN
    by_name = {entry["name"]: entry for entry in bar["recommendations"]}
    assert by_name["prescribed"]["verdict"] == bar_module.SEND_LOCAL
    assert by_name["prescribed"]["decided_by"] == "prescription"
    assert by_name["prescribed"]["window_fill"] is None
    assert by_name["prescribed"]["bar"] is None
    assert by_name["open"]["verdict"] is None
    assert by_name["open"]["state"] == budget.UNKNOWN
    assert bar["undecided"] == ["open"]
    assert bar["admitted"] == [{"name": "prescribed", "verdict": bar_module.SEND_LOCAL}]


def test_a_ready_node_naming_an_undeclared_group_is_refused() -> None:
    """A node silently dropped from the admitted set is the failure to prevent."""
    with pytest.raises(ValueError, match="sol"):
        budget.group_pace(
            CONFIG,
            ready=[{"name": "stray", "group": "nowhere", "score": 0.5}],
            now=NOW,
        )


def test_a_ready_node_without_a_numeric_score_is_refused() -> None:
    """The score is what the bar reads; a missing one is not a zero score."""
    with pytest.raises(ValueError, match="score"):
        budget.group_pace(
            CONFIG,
            ready=[{"name": "unscored", "group": "sol", "score": None}],
            now=NOW,
        )


# ── The addition is an addition ─────────────────────────────────────────────


def test_the_preflight_report_keeps_every_field_and_adds_only_the_groups() -> None:
    """The pace is added to the pre-flight, not substituted for any part of it."""
    report = budget.preflight(
        "demo",
        CONFIG,
        windows={"sol-a": _reading(0.3, 0.2)},
        ready=[{"name": "mid", "group": "sol", "score": 0.6}],
        now=NOW,
    )

    assert set(report) == PREFLIGHT_FIELDS | {"groups"}
    assert sorted(entry["group"] for entry in report["groups"]) == ["other", "sol"]


def test_naming_a_window_and_a_ready_set_does_not_move_the_hold_decision() -> None:
    """The hold is read from recorded exhaustion, so a pace cannot change it."""
    bare = budget.preflight("demo", CONFIG, now=NOW)
    with_pace = budget.preflight(
        "demo",
        CONFIG,
        windows={"sol-a": _reading(0.99, 0.99)},
        ready=[{"name": "mid", "group": "sol", "score": 0.6}],
        now=NOW,
    )

    for field in (
        "held",
        "held_backends",
        "clear_backends",
        "backends",
        "unattributed_records",
        "resume_after_seconds",
        "resume_at",
    ):
        assert with_pace[field] == bare[field], field


# ── The command surface carries the block ──────────────────────────────────


def _flight_yaml(tmp_path: Path) -> Path:
    """A minimal flight config declaring the two wallets the tests use."""
    config = tmp_path / "flight.yaml"
    config.write_text(
        "default_backend: sol-a\n"
        "backends:\n"
        "  sol-a:\n"
        "    launch: cli\n"
        "    command: codex\n"
        "    sandbox: worktree-full\n"
        "    budget_group: sol\n"
        "  sol-b:\n"
        "    launch: cli\n"
        "    command: codex\n"
        "    sandbox: worktree-full\n"
        "    budget_group: sol\n"
        "  other-a:\n"
        "    launch: cli\n"
        "    command: claude\n"
        "    sandbox: worktree-full\n"
        "    budget_group: other\n"
        "roles:\n"
        "  implement: {}\n"
        "budget:\n"
        "  utilisation_ceiling_pct: 100\n"
        "  exhausted_statuses: []\n",
        encoding="utf-8",
    )
    return config


def test_the_preflight_command_emits_one_group_block_per_declared_wallet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader of the command's JSON payload sees the pace without a flag."""
    home = tmp_path / "home"
    (home / "crew").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(_flight_yaml(tmp_path)))

    result = CliRunner().invoke(
        cli_main,
        ["crew", "preflight", "--project", "demo", "--checkout-path", str(tmp_path)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == PREFLIGHT_FIELDS | {"ok", "groups", "hold_history"}
    assert sorted(entry["group"] for entry in payload["groups"]) == ["other", "sol"]
    for entry in payload["groups"]:
        assert set(entry) == {
            "group",
            "members",
            "member",
            "state",
            "clocks",
            "allowance",
            "bar",
        }
        assert entry["state"] == budget.UNKNOWN
        assert entry["clocks"]["five_hour"]["utilisation"] is None
        assert entry["allowance"]["derived"] is None
        assert entry["allowance"]["reason"]


# ── The windows come from what runs recorded ──────────────────────────────


def _reset_in(hours: float) -> int:
    """An epoch-second reset this many hours ahead of the moment of asking.

    Derived from the clock rather than written down, because the command being
    driven takes no injectable "now": a literal reset would sit in the past on
    some future day and move the allowance derivation under the test.
    """
    return int((datetime.now(UTC) + timedelta(hours=hours)).timestamp())


def _write_ledger(root: Path, project: str, rows: list[dict]) -> None:
    """Write the project's ledger directly, so the test owns the recorded rows."""
    ledger.write(project, {"members": [], "runs": rows, "holds": []}, 0, root=root)


def _receipt_record(
    backend: str,
    *,
    windows: list[tuple[int, float]],
    observed_at: str,
    run_id: str = "r-receipt",
) -> dict:
    """One completed run whose lane receipt records ``windows``.

    Each window is ``(window_minutes, used_percent)``. One observation stamp
    covers the whole receipt, which is how the harness writes it: the receipt
    is the reading, and its rows are the clocks that reading carried.
    """
    stamp = observed_at
    return ledger.build_record(
        run_id=run_id,
        plan="a-plan",
        gate="passed",
        backend=backend,
        completed_at=stamp,
        lane_receipt={
            "quota_state": "measured",
            "observed_at": stamp,
            "quota_windows": [
                {
                    "window_minutes": minutes,
                    "used_percent": used,
                    "resets_at": _reset_in(100.0),
                    "observed_at": stamp,
                }
                for minutes, used in windows
            ],
        },
    )


def _stream_record(backend: str, *, run_id: str) -> dict:
    """One completed run that records no receipt, so only its stream can speak."""
    return ledger.build_record(
        run_id=run_id,
        plan="a-plan",
        gate="passed",
        backend=backend,
        completed_at=_iso(NOW - timedelta(minutes=2)),
    )


def _write_stream(path: Path, events: list[dict]) -> None:
    """Write a run's stream as JSON lines, the shape the reader parses."""
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )


def _preflight_command(tmp_path: Path, *extra: str):
    """Invoke the command against the tmp flight config, keeping its JSON payload."""
    return CliRunner().invoke(
        cli_main,
        [
            "crew",
            "preflight",
            "--project",
            "demo",
            "--checkout-path",
            str(tmp_path),
            *extra,
        ],
        catch_exceptions=False,
    )


def test_the_command_reads_a_groups_clocks_from_a_runs_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded receipt is the codex dialect's window source, read at the command.

    The receipt names both metered windows by length in minutes and stamps the
    reading it recorded, so both clocks come back observed with an age. The
    ready node is judged against its own group's bar, which is the second half
    of what a pre-flight is for: the admitted subset, not just the pace.
    """
    home = tmp_path / "home"
    (home / "crew").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(_flight_yaml(tmp_path)))
    _write_ledger(
        tmp_path,
        "demo",
        [
            _receipt_record(
                "sol-a",
                windows=[(300, 42.0), (10080, 21.0)],
                observed_at=_iso(NOW - timedelta(minutes=7)),
            )
        ],
    )

    result = _preflight_command(tmp_path, "--ready", "mid=sol:0.4")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    sol = _group(payload["groups"], "sol")
    assert sol["member"] == "sol-a"
    assert sol["state"] == budget.OBSERVED
    assert sol["clocks"]["five_hour"]["utilisation"] == pytest.approx(0.42)
    assert sol["clocks"]["five_hour"]["age_seconds"] is not None
    assert sol["clocks"]["seven_day"]["utilisation"] == pytest.approx(0.21)
    assert sol["clocks"]["seven_day"]["age_seconds"] is not None
    entry = next(
        item for item in sol["bar"]["recommendations"] if item["name"] == "mid"
    )
    assert entry["verdict"] is not None


def test_a_groups_clock_is_read_from_the_stream_a_run_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claude dialect reports its windows on the run's stream, not its receipt.

    A run recording no measured receipt is the case the stream source exists
    for, so the observed state here comes from the ``unifiedWindows`` event and
    nothing else.
    """
    home = tmp_path / "home"
    stream_dir = home / "crew" / "runs" / "r-stream"
    stream_dir.mkdir(parents=True)
    _write_stream(
        stream_dir / "stream.jsonl",
        [_stamped(90), _window_event(0.31, 0.44), _stamped(1)],
    )
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(_flight_yaml(tmp_path)))
    _write_ledger(tmp_path, "demo", [_stream_record("other-a", run_id="r-stream")])

    result = _preflight_command(tmp_path)

    assert result.exit_code == 0, result.output
    other = _group(json.loads(result.output)["groups"], "other")
    assert other["member"] == "other-a"
    assert other["state"] == budget.OBSERVED
    assert other["clocks"]["five_hour"]["utilisation"] == pytest.approx(0.31)
    assert other["clocks"]["five_hour"]["age_seconds"] is not None


def test_a_receipt_carrying_only_the_week_reports_one_clock_observed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The weekly clock divides while the five-hour clock stays unknown.

    Neither clock is inferred from the other: a receipt that recorded only the
    week leaves the fill unread, so the allowance derives and the bar stays
    undecided for every node whose lane the window could have changed.
    """
    home = tmp_path / "home"
    (home / "crew").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(_flight_yaml(tmp_path)))
    _write_ledger(
        tmp_path,
        "demo",
        [
            _receipt_record(
                "sol-a",
                windows=[(10080, 21.0)],
                observed_at=_iso(NOW - timedelta(hours=2)),
            )
        ],
    )

    result = _preflight_command(tmp_path, "--ready", "mid=sol:0.4")

    assert result.exit_code == 0, result.output
    sol = _group(json.loads(result.output)["groups"], "sol")
    assert sol["state"] == budget.OBSERVED
    assert sol["clocks"]["seven_day"]["state"] == budget.OBSERVED
    assert sol["clocks"]["seven_day"]["utilisation"] == pytest.approx(0.21)
    assert sol["clocks"]["seven_day"]["age_seconds"] is not None
    assert sol["clocks"]["five_hour"]["state"] == budget.UNKNOWN
    assert sol["clocks"]["five_hour"]["utilisation"] is None
    assert sol["allowance"]["derived"] is not None
    assert sol["allowance"]["utilisation"] == pytest.approx(0.21)
    assert sol["bar"]["window_fill"] is None
    assert sol["bar"]["recommendations"][0]["verdict"] is None


def test_the_budget_view_reads_the_same_recorded_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP budget view supplies the same source and reads its ready set.

    Its ready set arrives through the tool's existing ``candidates`` parameter,
    so the view and the command judge a wave from one payload rather than two
    implementations of the pace.
    """
    from reckon import mcp

    home = tmp_path / "home"
    (home / "crew").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(_flight_yaml(tmp_path)))
    _write_ledger(
        tmp_path,
        "demo",
        [
            _receipt_record(
                "sol-a",
                windows=[(300, 42.0), (10080, 21.0)],
                observed_at=_iso(NOW - timedelta(minutes=7)),
            )
        ],
    )

    report = mcp._crew(
        "demo",
        view="budget",
        checkout_path=str(tmp_path),
        candidates=[{"name": "mid", "group": "sol", "score": 0.4}],
    )

    sol = _group(report["groups"], "sol")
    assert sol["state"] == budget.OBSERVED
    assert sol["clocks"]["five_hour"]["utilisation"] == pytest.approx(0.42)
    assert sol["clocks"]["five_hour"]["age_seconds"] is not None
    assert sol["clocks"]["seven_day"]["utilisation"] == pytest.approx(0.21)
    entry = next(
        item for item in sol["bar"]["recommendations"] if item["name"] == "mid"
    )
    assert entry["verdict"] is not None


def test_the_recorded_windows_read_no_lane_declaring_no_wallet() -> None:
    """A lane outside every declared group is not read, and reaches no group."""
    rows = [
        _receipt_record(
            "orphan",
            windows=[(300, 99.0), (10080, 99.0)],
            observed_at=_iso(NOW - timedelta(minutes=1)),
        )
    ]

    windows = budget.recorded_windows("demo", CONFIG, records=rows)

    assert windows == {}


def test_an_unreadable_receipt_window_leaves_the_group_unknown() -> None:
    """A receipt naming no window this reader knows is not a zero reading.

    The two-sided assertion is the point: the group reports unknown *and* the
    clocks are not ``0.0``, because a zero utilisation reads as an empty window
    and admits everything.
    """
    rows = [
        ledger.build_record(
            run_id="r-odd",
            plan="a-plan",
            gate="passed",
            backend="sol-a",
            completed_at=_iso(NOW - timedelta(minutes=1)),
            lane_receipt={
                "quota_state": "measured",
                "observed_at": _iso(NOW - timedelta(minutes=1)),
                "quota_windows": [
                    {
                        "window_minutes": 1440,
                        "used_percent": 80.0,
                        "resets_at": _reset_in(10.0),
                        "observed_at": _iso(NOW - timedelta(minutes=1)),
                    }
                ],
            },
        )
    ]

    windows = budget.recorded_windows("demo", CONFIG, records=rows)

    assert "sol-a" not in windows
    sol = _group(budget.group_pace(CONFIG, windows=windows, now=NOW), "sol")
    assert sol["state"] == budget.UNKNOWN
    for period in ("five_hour", "seven_day"):
        assert sol["clocks"][period]["state"] == budget.UNKNOWN
        assert sol["clocks"][period]["utilisation"] not in (0.0, 1.0)
        assert sol["clocks"][period]["utilisation"] is None
