"""A grant replaces the lift already standing on its group and scope.

A one-second wobble in a window's reset stamp ended a lift before the jitter
tolerance landed, so the lead granted a second lift for the same group and
scope. Both records carried the same group and scope, and ``grant`` appended
the second beside the first rather than superseding it, so the workstation read
two lifts in force where one was meant. These tests pin the replacement:

* two grants on one group and scope leave exactly one lift in force, the first
  cleared with ``cleared_by`` naming the granting user and ``replaced_by``
  naming the second, and the second carrying ``replaces`` naming the first;
* a lift on a second scope stays in force, so replacement is scoped to the
  group and scope it was granted on;
* a ``CliRunner`` run of ``crew budget-lift`` for the second grant shows
  ``replaces`` in its reply's ``lift`` field;
* a ``lift_watch`` sweep over the result writes one ended row and one granted
  row, and nothing on the next sweep.

Every test points the config home at a temporary directory and asserts the
workstation's own ``budget-lifts.json`` is byte-identical afterwards, because a
test that writes the real lift record is a monitor for the machine rather than
the code.
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
from reckon.crew import lift_watch, runs

WEEK_HOURS = bl.CLOCK_HOURS[bl.SEVEN_DAY]
GROUP = "codex-sub"
BACKEND = "codex"
PROJECT = "reckon"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
ENV = {"USER": "lead"}

HOST_LAYER = f"""
version: 1

backends:
  {BACKEND}:
    budget_group: {GROUP}
"""


def _week_reset(*, elapsed_hours: float = 20.0) -> datetime:
    """The seven-day reset the group shows after ``elapsed_hours`` of its week."""
    return NOW + timedelta(hours=WEEK_HOURS - elapsed_hours)


def _reading(*, week_utilisation: float = 0.21) -> dict:
    """A window reading in the shape the lift resolver reads."""
    return {
        "observed_at": NOW.isoformat(),
        "five_hour": {
            "utilisation": 0.05,
            "resets_at": (NOW + timedelta(hours=4)).isoformat(),
        },
        "seven_day": {
            "utilisation": week_utilisation,
            "resets_at": _week_reset().isoformat(),
        },
    }


def _config() -> dict:
    return {
        "backends": {BACKEND: {"budget_group": GROUP}},
        "budget": {
            "pace_multiple": 1.1,
            "utilisation_ceiling_pct": 100.0,
            "resume_reserve_pct": 5.0,
            "coordinator_reserve_pct": 3.0,
        },
    }


def _in_force(config: dict, lift: dict) -> bool:
    return bl._in_force(lift, readings=None, now=NOW, bound=bl.ceilings(config))


@pytest.fixture(autouse=True)
def _real_record_untouched():
    """Prove the workstation's own lift record is untouched by any test here."""
    real = Path.home() / ".config" / "reckon" / bl.LIFTS_LEAF
    before = real.read_bytes() if real.exists() else None
    yield
    after = real.read_bytes() if real.exists() else None
    assert after == before, "a test wrote the live budget-lifts.json"


def _grant(
    config: dict,
    *,
    multiple: float = 1.8,
    scope: str = bl.GLOBAL,
    session: str | None = None,
) -> dict:
    return bl.grant(
        config,
        group=GROUP,
        reason="spend the window",
        multiple=multiple,
        scope=scope,
        session=session,
        readings=[_reading()],
        now=NOW,
        environ=ENV,
    )


# ── the supersession ────────────────────────────────────────────────────────


def test_a_grant_replaces_the_standing_lift_on_the_same_group_and_scope():
    config = _config()
    first = _grant(config)
    second = _grant(config, multiple=2.0)

    rows = {lift["id"]: lift for lift in bl.list_lifts(group=GROUP)}
    assert set(rows) == {first["id"], second["id"]}

    cleared = rows[first["id"]]
    assert cleared["cleared_by"] == "lead", "cleared_by names the granting user"
    assert cleared["cleared_at"], "the superseded lift is stamped as cleared"
    assert cleared["replaced_by"] == second["id"], "replaced_by names the new lift"

    standing = rows[second["id"]]
    assert standing["replaces"] == first["id"], "replaces names the cleared lift"
    assert standing["cleared_at"] is None

    in_force = [lift for lift in rows.values() if _in_force(config, lift)]
    assert [lift["id"] for lift in in_force] == [second["id"]]

    block = bl.effective_budget(config, group=GROUP, readings=None, now=NOW)
    assert block["lift_id"] == second["id"]


def test_a_grant_that_replaces_nothing_records_no_replaces():
    config = _config()
    only = _grant(config)
    assert only["replaces"] is None


def test_a_lift_on_another_scope_stays_in_force():
    config = _config()
    global_lift = _grant(config)
    session_lift = _grant(config, scope="session", session="sess-abc123")

    rows = {lift["id"]: lift for lift in bl.list_lifts(group=GROUP)}
    assert rows[global_lift["id"]]["cleared_at"] is None
    assert rows[global_lift["id"]]["replaced_by"] is None
    assert session_lift["replaces"] is None
    assert _in_force(config, rows[global_lift["id"]]), "the other scope is untouched"
    assert _in_force(config, rows[session_lift["id"]])


def test_a_second_grant_on_a_scope_replaces_only_that_scope():
    config = _config()
    global_lift = _grant(config)
    first = _grant(config, scope="session", session="sess-abc123")
    second = _grant(config, multiple=2.0, scope="session", session="sess-abc123")

    assert second["replaces"] == first["id"]
    rows = {lift["id"]: lift for lift in bl.list_lifts(group=GROUP)}
    assert rows[global_lift["id"]]["cleared_at"] is None
    assert rows[global_lift["id"]]["replaced_by"] is None


# ── the CLI reply names the replaced lift ───────────────────────────────────


def _invoke(*args, env=None):
    return CliRunner().invoke(cli_main, ["crew", *args], env=env)


def test_the_cli_reply_carries_replaces_in_its_lift_field(home: Path):
    (home / "flight.yaml").write_text(HOST_LAYER)
    assert _store._config_home() == home

    first = json.loads(
        _invoke(
            "budget-lift",
            "--group",
            GROUP,
            "--multiple",
            "1.8",
            "--reason",
            "first",
        ).output
    )
    second_result = _invoke(
        "budget-lift",
        "--group",
        GROUP,
        "--multiple",
        "2.0",
        "--reason",
        "second",
    )
    assert second_result.exit_code == 0, second_result.output
    payload = json.loads(second_result.output)
    assert payload["lift"]["replaces"] == first["lift_id"]


# ── the watch stream shows the pair ─────────────────────────────────────────


class _Readings:
    """A group's reading, in the shape the sweep's resolver reads."""

    def __call__(self, group: str, config: object, now: datetime) -> list[dict]:
        return [_reading()]


def _rows(stream: Path) -> list[dict]:
    if not stream.exists():
        return []
    return [
        json.loads(line) for line in stream.read_text().splitlines() if line.strip()
    ]


def _sweep(stream: Path, config: dict, *, seen: dict | None) -> dict:
    return lift_watch.sweep(
        PROJECT,
        seen=seen,
        now=NOW,
        config=config,
        readings_for=_Readings(),
        stream_path=stream,
    )


def test_a_sweep_over_the_replacement_writes_one_ended_and_one_granted_row(tmp_path):
    config = _config()
    stream = tmp_path / "proj.events"

    _grant(config)
    seen = _sweep(stream, config, seen={})
    assert [row["to_state"] for row in _rows(stream)] == [lift_watch.LIFT_GRANTED]

    _grant(config, multiple=2.0)
    seen = _sweep(stream, config, seen=seen)
    after = _rows(stream)[1:]
    assert len(after) == 2, "one ended row and one granted row"
    assert sorted(row["to_state"] for row in after) == sorted(
        [lift_watch.LIFT_ENDED, lift_watch.LIFT_GRANTED]
    )

    _sweep(stream, config, seen=seen)
    assert len(_rows(stream)) == 3, "an unchanged document costs no stream write"


def test_the_sweep_does_not_reach_the_live_watch_stream(tmp_path):
    real_stream = runs.watch_stream_path(PROJECT)
    before = real_stream.read_bytes() if real_stream.exists() else None
    config = _config()
    stream = tmp_path / "proj.events"
    _grant(config)
    _sweep(stream, config, seen={})
    _grant(config, multiple=2.0)
    _sweep(stream, config, seen={})
    after = real_stream.read_bytes() if real_stream.exists() else None
    assert after == before
