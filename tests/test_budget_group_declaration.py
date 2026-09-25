"""Wallet declarations resolve from a configuration home, once per wallet.

Every assertion here is drawn from a flight configuration this test synthesises
in a throwaway home and resolves through the real resolver, so what is under
test is the declaration path a host config reaches and never a mapping a fixture
built by hand.  The workstation's own configuration home is snapshotted before
the run and compared afterwards: a test that read it would pass or fail with
whatever that host happens to declare, and one that wrote it would make a peer
session wrong.  Only the home's own files are hashed, because live sessions
write the caches beneath it while this runs.

The window readings are streams, not hand-written mappings: each lane's fixture
is the provider's own event carrying ``unifiedWindows``, and the reading under
test is produced from it by the crew's reader, exactly as production produces
one.  A consumer reading a key nothing produces therefore resolves no figure
here and fails, which is the failure this fixture is shaped to catch.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import budget, flight
from reckon._store import _config_home
from reckon.crew import budget_group as bg
from reckon.crew import window_reading
from tests.conftest import test_temp_config_home as _temp_config_home

SOL_FAMILY_LANES = ("codex", "codex-astra", "codex-terra", "codex-luna")
SEPARATE_LANE = "codex-spark"
UNDECLARED_LANE = "codex-orphan"
SOL_WALLET = "codex-sub"
SPARK_WALLET = "spark-sub"

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)

# How far the week clock's reset stands from the observation, so the wallet's
# placement in its week is arithmetic rather than a stroke of the clock.
WEEK_RESET_IN_HOURS = 124.0
FIVE_HOUR_RESET_IN_HOURS = 1.0

# The declarations a host layer carries: four lanes on one subscription, one on
# a second, and a lane declaring none.  No lane name reaches the assertions
# through a shortcut — the resolver reads them out of this file.
HOST_LAYER = """
version: 1

backends:
  codex:
    budget_group: codex-sub
  codex-astra:
    budget_group: codex-sub
  codex-terra:
    budget_group: codex-sub
  codex-luna:
    budget_group: codex-sub
  codex-spark:
    budget_group: spark-sub
  codex-orphan:
    command: codex
"""


def _home_manifest(home: Path) -> dict[str, str]:
    """The home's own files, by name and content, with its file census."""
    files = {"<census>": json.dumps(sorted(p.name for p in home.iterdir()))}
    for entry in sorted(home.iterdir()):
        if entry.is_file():
            files[entry.name] = hashlib.sha256(entry.read_bytes()).hexdigest()
    return files


@pytest.fixture()
def declared_home(monkeypatch):
    """A throwaway home carrying the declarations, with the real one watched."""
    real = _config_home()
    before = _home_manifest(real)
    home = _temp_config_home("reckon-wallet-declaration-")
    (home / "flight.yaml").write_text(HOST_LAYER)
    (home / "mounts.json").write_text("{}")
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    monkeypatch.delenv("RECKON_FLIGHT_CONFIG", raising=False)
    yield home
    assert _home_manifest(real) == before, (
        "the run changed the workstation's own configuration home"
    )


@pytest.fixture()
def resolved(declared_home):
    """The resolved flight config of the synthesised home."""
    return flight.resolve()


def _report(
    five_hour: float,
    seven_day: float,
    *,
    observed_at: datetime,
    dated: bool = True,
    week_reset: bool = True,
) -> dict:
    """One stream's window-carrying report, in the provider's own vocabulary.

    Built as a stream carries it: a ``rate_limit_event`` whose
    ``unifiedWindows`` holds each period with its ``utilization`` as a fraction
    of that window, beside the ``resetsAt`` the window itself publishes.  The
    reading every assertion runs on is produced from this by the reader, so a
    fixture cannot supply a key the stream never carries.
    """
    seven: dict = {"utilization": seven_day}
    if week_reset:
        seven["resetsAt"] = (
            observed_at + timedelta(hours=WEEK_RESET_IN_HOURS)
        ).isoformat()
    event: dict = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "unifiedWindows": {
                "five_hour": {
                    "utilization": five_hour,
                    "resetsAt": (
                        observed_at + timedelta(hours=FIVE_HOUR_RESET_IN_HOURS)
                    ).isoformat(),
                },
                "seven_day": seven,
            }
        },
    }
    if dated:
        event["timestamp"] = observed_at.isoformat()
    return event


def _family_readings() -> dict[str, list[dict]]:
    """Four lanes' streams: three reporting alike, and one stale lane of its own."""
    readings = {
        lane: [_report(0.14, 0.31, observed_at=NOW - timedelta(minutes=6))]
        for lane in SOL_FAMILY_LANES[1:]
    }
    readings[SOL_FAMILY_LANES[0]] = [
        _report(0.99, 0.31, observed_at=NOW - timedelta(hours=2.0))
    ]
    return readings


def test_the_four_sol_lanes_resolve_to_one_group(resolved):
    """One wallet holds the four lanes, by count and by name."""
    groups = bg.declared_groups(resolved.config)

    assert sorted(groups) == [SOL_WALLET, SPARK_WALLET]
    assert len(groups[SOL_WALLET]) == len(SOL_FAMILY_LANES) == 4
    assert sorted(groups[SOL_WALLET]) == sorted(SOL_FAMILY_LANES)
    assert groups[SPARK_WALLET] == [SEPARATE_LANE]


def test_a_lane_declaring_no_wallet_is_left_ungrouped(resolved):
    """The undeclared lane joins no wallet, least of all its neighbour's."""
    groups = bg.declared_groups(resolved.config)

    assert UNDECLARED_LANE in bg.ungrouped(resolved.config)
    assert UNDECLARED_LANE not in groups[SOL_WALLET]
    assert UNDECLARED_LANE not in groups[SPARK_WALLET]
    assert UNDECLARED_LANE not in [
        member for members in groups.values() for member in members
    ]


def test_the_declarations_come_from_the_synthesised_home(resolved, declared_home):
    """The throwaway home is the layer the declarations were read from."""
    host = next(layer for layer in resolved.layers if layer.name == "host")

    assert host.path == str(declared_home / "flight.yaml")
    assert UNDECLARED_LANE in resolved.config["backends"]
    assert resolved.origin(f"backends.{SOL_FAMILY_LANES[0]}.budget_group") == "host"


def test_a_wallets_figures_resolve_once_for_the_wallet(resolved):
    """The wallet's fill is its freshest member's, never a stale lane's own."""
    figures = bg.group_figures(SOL_WALLET, resolved.config, _family_readings(), now=NOW)

    assert figures.group == SOL_WALLET
    assert sorted(figures.members) == sorted(SOL_FAMILY_LANES)
    assert not set(figures.members) & set(bg.ungrouped(resolved.config))
    assert figures.state == bg.OBSERVED
    # The stale lane reports 0.99 of its own window two hours ago; the wallet
    # reads the lanes that observed six minutes ago, so the fresh 0.14 is the
    # fill and the spent 0.99 is not.
    assert figures.member in SOL_FAMILY_LANES[1:]
    assert figures.fill == pytest.approx(0.14)
    assert figures.bar == pytest.approx(0.053312)
    assert figures.pace is not None
    assert figures.pace["group"] == SOL_WALLET
    assert figures.pace["utilisation"] == pytest.approx(0.31)
    assert figures.reserve_pct == 20.0


def test_a_wallet_reads_the_same_clocks_the_preflight_publishes(resolved):
    """The wallet's figures come from the reports the preflight publishes.

    One vocabulary serves both surfaces: the same streams are handed to
    :func:`reckon.budget.group_pace`, whose group entry is the published
    reading, and to the wallet's own figures.  A consumer reading a key the
    producer never writes resolves no figure here and fails.
    """
    streams = _family_readings()
    published = next(
        entry
        for entry in budget.group_pace(resolved.config, windows=streams, now=NOW)
        if entry["group"] == SOL_WALLET
    )
    figures = bg.group_figures(SOL_WALLET, resolved.config, streams, now=NOW)
    clocks = published["clocks"]

    assert published["state"] == budget.OBSERVED
    assert clocks["five_hour"]["state"] == budget.OBSERVED
    assert figures.member == published["member"]
    assert figures.fill == pytest.approx(clocks["five_hour"]["utilisation"])
    assert figures.pace is not None
    assert figures.pace["utilisation"] == pytest.approx(
        clocks["seven_day"]["utilisation"]
    )


def test_the_figures_read_the_readers_own_period_names(resolved):
    """Both clocks are the reader's own periods, not keys this module invented."""
    reading = window_reading.read_windows(
        [_report(0.14, 0.31, observed_at=NOW - timedelta(minutes=6))], now=NOW
    )
    figures = bg.group_figures(
        SOL_WALLET, resolved.config, {SOL_FAMILY_LANES[0]: reading}, now=NOW
    )

    assert bg.FILL_CLOCK in window_reading.PERIODS
    assert bg.WEEK_CLOCK in window_reading.PERIODS
    assert figures.fill == pytest.approx(reading.utilisation(bg.FILL_CLOCK))
    assert figures.pace is not None
    assert figures.pace["utilisation"] == pytest.approx(
        reading.utilisation(bg.WEEK_CLOCK)
    )


def test_a_wallet_never_paces_on_another_wallets_reading(resolved):
    """One reading supplies the wallet that holds its lane, and no other."""
    readings = {
        SEPARATE_LANE: [_report(0.88, 0.54, observed_at=NOW - timedelta(minutes=1))]
    }

    spark = bg.group_figures(SPARK_WALLET, resolved.config, readings, now=NOW)
    sol = bg.group_figures(SOL_WALLET, resolved.config, readings, now=NOW)

    assert spark.members == (SEPARATE_LANE,)
    assert spark.fill == pytest.approx(0.88)
    assert spark.pace is not None
    assert spark.pace["utilisation"] == pytest.approx(0.54)
    assert sol.state == bg.UNOBSERVED
    assert sol.fill is None and sol.bar is None and sol.pace is None


def test_a_reading_that_cannot_be_aged_does_not_supply_the_wallet(resolved):
    """A window report nothing can date never speaks for the wallet.

    The undated lane reports a full window, but no stamp places the report in
    time, so it cannot be told from a current reading and does not compete with
    the lane that can be aged.
    """
    readings = {
        SOL_FAMILY_LANES[0]: [_report(1.0, 1.0, observed_at=NOW, dated=False)],
        SOL_FAMILY_LANES[1]: [
            _report(0.2, 0.4, observed_at=NOW - timedelta(minutes=3))
        ],
    }

    figures = bg.group_figures(SOL_WALLET, resolved.config, readings, now=NOW)

    assert figures.member == SOL_FAMILY_LANES[1]
    assert figures.fill == pytest.approx(0.2)


def test_a_week_clock_with_no_reset_reports_no_pace(resolved):
    """A week that cannot be placed in the week yields no allowance, not a guess."""
    readings = {
        SOL_FAMILY_LANES[0]: [
            _report(0.6, 0.4, observed_at=NOW - timedelta(minutes=2), week_reset=False)
        ]
    }

    figures = bg.group_figures(SOL_WALLET, resolved.config, readings, now=NOW)

    assert figures.fill == pytest.approx(0.6)
    assert figures.bar is not None
    assert figures.pace is None


def test_an_unobserved_wallet_reports_no_figure_rather_than_a_zero(resolved):
    """Absence of a reading is not a fill of zero, which would admit everything."""
    figures = bg.group_figures(SOL_WALLET, resolved.config, {}, now=NOW)

    assert figures.state == bg.UNOBSERVED
    assert figures.fill is None
    assert figures.bar is None
    assert figures.pace is None
    # The reserve is withheld from the wallet's window before any work is
    # dispatched, so it holds whatever the reading says.
    assert figures.reserve_pct == 20.0


def test_a_lane_name_is_not_a_wallet_and_paces_nothing(resolved):
    """A lane identifier is refused, so no per-lane figure can be asked for."""
    with pytest.raises(ValueError, match="not a declared budget group"):
        bg.group_figures(
            SOL_FAMILY_LANES[0], resolved.config, _family_readings(), now=NOW
        )


def test_the_module_exposes_no_per_lane_figures_entry_point():
    """The figures' only entry point takes a declared group identifier."""
    figure_names = [
        name
        for name, value in vars(bg).items()
        if not name.startswith("_") and callable(value) and "figure" in name
    ]

    assert figure_names == ["group_figures"]
    assert not hasattr(bg, "backend_figures")
    assert not hasattr(bg, "lane_figures")
