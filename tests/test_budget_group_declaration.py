"""Wallet declarations resolve from a configuration home, once per wallet.

Every assertion here is drawn from a flight configuration this test synthesises
in a throwaway home and resolves through the real resolver, so what is under
test is the declaration path a host config reaches and never a mapping a fixture
built by hand.  The workstation's own configuration home is snapshotted before
the run and compared afterwards: a test that read it would pass or fail with
whatever that host happens to declare, and one that wrote it would make a peer
session wrong.  Only the home's own files are hashed, because live sessions
write the caches beneath it while this runs.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import flight
from reckon._store import _config_home
from reckon.crew import budget_group as bg
from tests.conftest import test_temp_config_home as _temp_config_home

SOL_FAMILY_LANES = ("codex", "codex-astra", "codex-terra", "codex-luna")
SEPARATE_LANE = "codex-spark"
UNDECLARED_LANE = "codex-orphan"
SOL_WALLET = "codex-sub"
SPARK_WALLET = "spark-sub"

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)

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


def _reading(*, age: timedelta, five_hour: float, seven_day: float) -> dict:
    return {
        "observed_at": (NOW - age).isoformat(),
        bg.FIVE_HOUR_PERCENT_KEY: five_hour,
        bg.SEVEN_DAY_PERCENT_KEY: seven_day,
        bg.SEVEN_DAY_RESET_KEY: (NOW + timedelta(hours=124.0)).isoformat(),
    }


def _family_readings() -> dict[str, dict]:
    """Four lanes reporting near-identical clocks, and one stale lane of its own."""
    readings = {
        lane: _reading(age=timedelta(minutes=6), five_hour=14.0, seven_day=31.0)
        for lane in SOL_FAMILY_LANES[1:]
    }
    readings[SOL_FAMILY_LANES[0]] = _reading(
        age=timedelta(hours=2), five_hour=99.0, seven_day=31.0
    )
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
    assert figures.fill == pytest.approx(0.14)
    assert figures.bar == pytest.approx(0.14 * 0.14 * (3.0 - 2.0 * 0.14))
    assert figures.pace is not None
    assert figures.pace["group"] == SOL_WALLET
    assert figures.pace["utilisation"] == pytest.approx(0.31)
    assert figures.reserve_pct == 20.0


def test_a_wallet_never_paces_on_another_wallets_reading(resolved):
    """One reading supplies the wallet that holds its lane, and no other."""
    readings = {
        SEPARATE_LANE: _reading(
            age=timedelta(minutes=1), five_hour=88.0, seven_day=54.0
        )
    }

    spark = bg.group_figures(SPARK_WALLET, resolved.config, readings, now=NOW)
    sol = bg.group_figures(SOL_WALLET, resolved.config, readings, now=NOW)

    assert spark.members == (SEPARATE_LANE,)
    assert spark.fill == pytest.approx(0.88)
    assert spark.pace is not None
    assert spark.pace["utilisation"] == pytest.approx(0.54)
    assert sol.state == bg.UNOBSERVED
    assert sol.fill is None and sol.bar is None and sol.pace is None


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
