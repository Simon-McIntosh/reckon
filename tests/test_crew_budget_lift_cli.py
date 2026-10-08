"""The budget-lift and budget-lifts verbs: grant, clear and list a pace lift.

Every test runs through click's runner against the real command, resolving a
flight configuration and a published headroom document this file writes into a
throwaway home. Nothing here reaches the workstation's own ``budget-lifts.json``
or its published document: the home is a temporary directory, and the real lift
record is snapshotted before the run and compared afterwards, because a test
that writes the live record is a monitor for the machine rather than a test of
the code.

The projection is checked against the same arithmetic the lift records — the
clock's reset less the window, plus the window over the multiple — derived here
from the fixture's own reset rather than written down as a literal, so a fixture
date moving does not silently change what the assertion means.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _store
from reckon.cli import main as cli_main
from reckon.crew import budget_lift as bl
from reckon.crew import paid_lanes

GROUP = "codex-sub"
BACKEND = "codex"
WEEK_HOURS = bl.CLOCK_HOURS[bl.SEVEN_DAY]


# A host layer declaring one backend on one wallet. No budget block is written,
# so the shipped defaults supply the configured pace multiple and the lift
# ceilings the refusals are measured against.
HOST_LAYER = f"""
version: 1

backends:
  {BACKEND}:
    budget_group: {GROUP}
"""


def _reset_in(days: float) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


def _document(*, week_reset: datetime, five_reset: datetime, observed: datetime) -> dict:
    """A published headroom document carrying one observed reading for the lane."""
    return {
        "accounts": {
            BACKEND: {
                "windows": {
                    "five_hour": {
                        "state": paid_lanes.OBSERVED,
                        "utilisation": 0.05,
                        "observed_at": observed.isoformat(),
                        "resets_at": five_reset.isoformat(),
                    },
                    "seven_day": {
                        "state": paid_lanes.OBSERVED,
                        "utilisation": 0.21,
                        "observed_at": observed.isoformat(),
                        "resets_at": week_reset.isoformat(),
                    },
                }
            }
        }
    }


@pytest.fixture(autouse=True)
def _real_lifts_untouched():
    """Prove the workstation's own lift record is untouched by any test here."""
    real = Path.home() / ".config" / "reckon" / bl.LIFTS_LEAF
    before = real.read_bytes() if real.exists() else None
    yield
    after = real.read_bytes() if real.exists() else None
    assert after == before, "a test wrote the live budget-lifts.json"


class _Fleet:
    """A throwaway home carrying the host layer and its published document."""

    def __init__(self, home: Path, week_reset: datetime, five_reset: datetime):
        self.home = home
        self.week_reset = week_reset
        self.five_reset = five_reset

    def projection(self, multiple: float) -> datetime:
        """The instant the week window fills at ``multiple``, from its reset."""
        return (
            self.week_reset
            - timedelta(hours=WEEK_HOURS)
            + timedelta(hours=WEEK_HOURS / multiple)
        )


@pytest.fixture()
def fleet(isolated_reckon_home):
    """Write the host layer and the published document into the temp home."""
    home = isolated_reckon_home
    assert _store._config_home() == home
    (home / "flight.yaml").write_text(HOST_LAYER)
    observed = datetime.now(UTC)
    week_reset = _reset_in(5.0)
    five_reset = _reset_in(0.2)
    (home / paid_lanes.DOCUMENT_NAME).write_text(
        json.dumps(
            _document(week_reset=week_reset, five_reset=five_reset, observed=observed)
        ),
    )
    return _Fleet(home, week_reset, five_reset)


def _invoke(*args, env=None):
    return CliRunner().invoke(cli_main, ["crew", *args], env=env)


def test_grant_projects_the_multiple_against_the_reset_and_admits(fleet):
    multiple = 1.8
    result = _invoke(
        "budget-lift",
        "--group", GROUP,
        "--multiple", str(multiple),
        "--reason", "spend the week on the release",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["action"] == "grant"
    assert payload["admitted"] is True
    assert payload["multiple"] == pytest.approx(multiple)
    assert payload["scope"] == bl.GLOBAL

    projected = datetime.fromisoformat(payload["projected_exhaustion"])
    assert projected == fleet.projection(multiple)
    # The projection is anchored to the group's own reset, not an invented one.
    assert payload["ends"]["resets_at"] == fleet.week_reset.isoformat()

    document = json.loads((fleet.home / bl.LIFTS_LEAF).read_text())
    assert len(document["lifts"]) == 1
    landed = document["lifts"][0]
    assert landed["group"] == GROUP
    assert landed["pace_multiple"] == pytest.approx(multiple)
    assert landed["reason"] == "spend the week on the release"


def test_grant_is_refused_while_a_run_id_is_set(fleet):
    result = _invoke(
        "budget-lift",
        "--group", GROUP,
        "--multiple", "1.8",
        "--reason", "lift from inside a run",
        env={bl.RUN_ID_ENV: "r-20261008T150314744504-test"},
    )
    assert result.exit_code != 0
    assert "run" in result.output.lower()
    assert not (fleet.home / bl.LIFTS_LEAF).exists()


def test_grant_is_refused_for_an_undeclared_group(fleet):
    result = _invoke(
        "budget-lift",
        "--group", "not-a-wallet",
        "--multiple", "1.8",
        "--reason", "lift an undeclared group",
    )
    assert result.exit_code != 0
    assert "not-a-wallet" in result.output
    assert not (fleet.home / bl.LIFTS_LEAF).exists()


def test_grant_is_refused_above_the_declared_max_multiple(fleet):
    ceilings = bl.ceilings(None)
    overtaking = ceilings.max_multiple + 0.5
    result = _invoke(
        "budget-lift",
        "--group", GROUP,
        "--multiple", str(overtaking),
        "--reason", "lift past the ceiling",
    )
    assert result.exit_code != 0
    assert "max_multiple" in result.output
    assert not (fleet.home / bl.LIFTS_LEAF).exists()


def test_grant_requires_exactly_one_form_and_a_reason(fleet):
    none = _invoke("budget-lift", "--group", GROUP, "--reason", "no form")
    assert none.exit_code != 0
    both = _invoke(
        "budget-lift",
        "--group", GROUP,
        "--multiple", "1.8",
        "--uncapped",
        "--reason", "two forms",
    )
    assert both.exit_code != 0
    no_reason = _invoke("budget-lift", "--group", GROUP, "--multiple", "1.8")
    assert no_reason.exit_code != 0

    assert not (fleet.home / bl.LIFTS_LEAF).exists()


def test_drain_by_and_uncapped_are_accepted_forms(fleet):
    by = _invoke(
        "budget-lift",
        "--group", GROUP,
        "--drain-by", "12h",
        "--reason", "drain by noon",
    )
    assert by.exit_code == 0, by.output
    assert json.loads(by.output)["form"] == bl.DRAIN_BY

    uncapped = _invoke(
        "budget-lift",
        "--group", GROUP,
        "--uncapped",
        "--reason", "release the hold",
    )
    assert uncapped.exit_code == 0, uncapped.output
    assert json.loads(uncapped.output)["form"] == bl.UNCAPPED


def test_clear_revokes_the_governing_lift_and_none_is_a_no_op(fleet):
    granted = _invoke(
        "budget-lift", "--group", GROUP, "--multiple", "1.8", "--reason", "first"
    )
    assert granted.exit_code == 0, granted.output
    lift_id = json.loads(granted.output)["lift_id"]

    cleared = _invoke("budget-lift", "--clear", "--group", GROUP)
    assert cleared.exit_code == 0, cleared.output
    payload = json.loads(cleared.output)
    assert payload["action"] == "clear"
    assert payload["lift_id"] == lift_id
    assert payload["cleared"]["cleared_at"]

    again = _invoke("budget-lift", "--clear", "--group", GROUP)
    assert again.exit_code == 0, again.output
    assert json.loads(again.output)["cleared"] is None


def test_clear_by_id_refuses_an_id_of_another_group(fleet):
    other = _invoke(
        "budget-lift", "--group", GROUP, "--multiple", "1.8", "--reason", "theirs"
    )
    lift_id = json.loads(other.output)["lift_id"]

    refused = _invoke(
        "budget-lift", "--clear", "--group", "someone-else", "--id", lift_id
    )
    assert refused.exit_code != 0
    assert lift_id in refused.output


def test_budget_lifts_lists_active_and_recent_newest_first(fleet):
    first = json.loads(
        _invoke(
            "budget-lift", "--group", GROUP, "--multiple", "1.5", "--reason", "older"
        ).output
    )["lift_id"]
    second = json.loads(
        _invoke(
            "budget-lift", "--group", GROUP, "--multiple", "2.0", "--reason", "newer"
        ).output
    )["lift_id"]

    listed = _invoke("budget-lifts", "--group", GROUP)
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert payload["count"] == 2
    ids = [row["id"] for row in payload["lifts"]]
    assert set(ids) == {first, second}
    # Newest first: the second grant is at the head.
    assert ids[0] == second

    all_rows = json.loads(_invoke("budget-lifts").output)
    assert all_rows["count"] >= 2


def _config() -> dict:
    """A minimal resolved-flight backend map for the group the host layer declares."""
    return {"budget": {}, "backends": {BACKEND: {"budget_group": GROUP}}}


def test_grant_from_the_future_is_recorded_inert_until_then(fleet):
    starts = datetime.now(UTC) + timedelta(days=1)
    result = _invoke(
        "budget-lift",
        "--group", GROUP,
        "--multiple", "1.8",
        "--from", starts.isoformat(),
        "--reason", "start tomorrow",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    landed = payload["lift"]
    assert datetime.fromisoformat(landed["starts_at"]) == starts

    config = _config()
    readings = bl.published_readings(config, group=GROUP)
    before = bl.effective_budget(
        config, group=GROUP, readings=readings, now=datetime.now(UTC)
    )
    assert "lift_id" not in before, "a lift from the future is inert before it starts"
    after = bl.effective_budget(
        config, group=GROUP, readings=readings, now=starts + timedelta(minutes=1)
    )
    assert after["lift_id"] == payload["lift_id"]
    assert after["pace_multiple"] == pytest.approx(1.8)
    assert after["bookend_reserve_pct"] == 0.0


def test_until_an_iso_time_pins_the_lift_end(fleet):
    end = datetime.now(UTC) + timedelta(hours=3)
    result = _invoke(
        "budget-lift",
        "--group", GROUP,
        "--multiple", "1.8",
        "--until", end.isoformat(),
        "--reason", "end at three",
    )
    assert result.exit_code == 0, result.output
    ends = json.loads(result.output)["lift"]["ends"]
    assert ends["kind"] == "at"
    assert datetime.fromisoformat(ends["at"]) == end


def test_until_a_duration_pins_the_lift_end(fleet):
    before = datetime.now(UTC)
    result = _invoke(
        "budget-lift",
        "--group", GROUP,
        "--multiple", "1.8",
        "--until", "12h",
        "--reason", "twelve hours from grant",
    )
    assert result.exit_code == 0, result.output
    ends = json.loads(result.output)["lift"]["ends"]
    assert ends["kind"] == "at"
    landed = datetime.fromisoformat(ends["at"])
    assert before + timedelta(hours=12) <= landed <= datetime.now(UTC) + timedelta(
        hours=12
    )


def test_clock_five_hour_anchors_the_lift_to_that_window(fleet):
    result = _invoke(
        "budget-lift",
        "--group", GROUP,
        "--multiple", "1.8",
        "--clock", "five_hour",
        "--reason", "ride the five-hour window",
    )
    assert result.exit_code == 0, result.output
    ends = json.loads(result.output)["lift"]["ends"]
    assert ends["kind"] == "reset"
    assert ends["clock"] == "five_hour"
    assert ends["resets_at"] == fleet.five_reset.isoformat()
    assert ends["utilisation"] == pytest.approx(0.05)


def test_session_grant_is_scoped_to_that_session(fleet):
    session = "sess-abc123"
    result = _invoke(
        "budget-lift",
        "--group", GROUP,
        "--multiple", "1.8",
        "--session", session,
        "--reason", "only this session may spend",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scope"] == f"session:{session}"
    assert payload["lift"]["scope"] == f"session:{session}"


def test_global_and_session_together_are_refused(fleet):
    result = _invoke(
        "budget-lift",
        "--group", GROUP,
        "--multiple", "1.8",
        "--global",
        "--session", "sess-abc123",
        "--reason", "both scopes at once",
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output
    assert not (fleet.home / bl.LIFTS_LEAF).exists()
