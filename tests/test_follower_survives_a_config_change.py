"""A follower survives a configuration layer a merge left momentarily malformed.

A merge that lands a key the running image's schema does not yet know — or one
the image's schema has just retired — leaves every ``flight.resolve()`` call in
that process raising ``FlightConfigError`` until the layer is repaired. The
follower calls the quota-rate reader from its per-tick path: the baseline it
derives through ``watch_stream_cursor`` carries each run's notional cost, and
that figure is read from the resolved configuration. So a config file a merge
broke under a running follower used to end the pane, for a file the follower
does not own and cannot repair.

Two repairs carry the node. The rate reader returns an empty, all-unpriced
reading instead of raising, so both the follower and the producer that share it
keep their transitions and lose only the notional cost. And the follower's
per-tick loop catches a config error the reader does not cover, prints one dim
deferral line, and retries on the next tick rather than exiting — so the pane
lives through the merge window and resumes delivering on the tick after the
read stops raising.

Every test here is red at the base revision: the reader raises, and the raised
error ends the follower. The declared mutation restores the reader's raise, and
both arms go red — the reader arm because it raises again, the follower arm
because the per-tick read it depends on never stops raising, so the pane never
delivers.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from reckon import cli, crew
from reckon.crew import runs
from reckon.crew.quota_weight import (
    UnknownQuotaWeight,
    backend_rate_statuses,
    quota_weight,
)
from reckon.flight import FlightConfigError

PROJECT = "config-change-proj"
SESSION = "s1"
RUN_A = "r-config-change-a"

# Long enough that a loaded interpreter reaches its first read, short enough
# that a suite of them stays quick.
ARM_LIFETIME = 0.6

_FLEET_EVENTS = frozenset({"baseline", "transition", "manifest-rewritten"})


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep pointers, manifests and streams in temporary state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _break_the_config(home: Path, monkeypatch) -> Path:
    """Point the host layer at a key the running schema refuses.

    The value is irrelevant: the schema rejects the key itself, which is the
    shape a merge leaves behind when it lands a slot the running image's schema
    predates. Every resolve in this process then raises until the layer is
    repaired, exactly as in the merged checkout the node repairs.
    """
    path = home / "flight.yaml"
    path.write_text("an_unknown_flight_config_key: 7\n", encoding="utf-8")
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(path))
    return path


def _write_pointer(home: Path, run_id: str, node: str, *, phase: str) -> None:
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "m1",
                    "usage": {"input_tokens": 1200, "output_tokens": 300},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "node": {"id": node, "plan": "plan-a", "time_budget": "20m"},
            "phase": phase,
            "created_at": runs._utc_now(),
            "manifest_path": str(home / "manifests" / f"{run_id}.md"),
            "log_path": str(log),
            "process_alive": None,
        },
    )


def _arm(*, lifetime: float = ARM_LIFETIME, **kwargs) -> list[dict]:
    """Run one arming to its own lifetime and collect the fleet rows it drew."""
    generator = cli._follow_watch_lines(
        PROJECT,
        session=SESSION,
        poll_interval=0.001,
        sweep=None,
        lifetime=lifetime,
        **kwargs,
    )
    return [event for event in generator if event.get("event") in _FLEET_EVENTS]


# ── The shared reader degrades to an unknown reading ────────────────────────


def test_the_rate_reader_returns_an_unknown_reading_on_a_bad_config(
    home, monkeypatch
) -> None:
    """An unreadable layer leaves every backend unpriced, not the process dead.

    The reader runs in the per-tick path of long-lived processes, so a config
    error must cost a rate figure and never raise. The empty map is the explicit
    all-unpriced reading: no backend inherits a neighbour's rate because the
    configuration that would have named one cannot be read.
    """
    _break_the_config(home, monkeypatch)

    statuses = backend_rate_statuses(anchor=date(2026, 1, 1))
    assert statuses == {}, (
        "a config that fails validation yields the empty, all-unpriced reading; "
        f"got {statuses!r}"
    )

    # The consumer that folds these rates reports the model explicitly unpriced
    # rather than propagating the config error out of a metered call.
    weight = quota_weight("any-model", [])
    assert isinstance(weight, UnknownQuotaWeight), (
        f"an unreadable config leaves the weight unknown; got {weight!r}"
    )


# ── The follower's per-tick loop defers and keeps delivering ────────────────


def test_a_follower_defers_one_tick_and_keeps_delivering_across_a_config_error(
    home, monkeypatch, capfd
) -> None:
    """The pane survives a per-tick config error, says so once, and carries on.

    The follower's per-tick read meets a layer the schema refuses, which the
    reader's degradation does not by itself cover — so the loop's own catch is
    what carries the pane. At base that error escapes the loop and the arming
    ends with nothing delivered. Here the loop catches it, prints one dim
    deferral line — once, not once per tick — and retries: the next tick's read
    succeeds and the run's baseline row is delivered. A config a merge broke
    under a live follower then costs the pane a tick, not the stream.
    """
    _break_the_config(home, monkeypatch)
    _write_pointer(home, RUN_A, "node-a", phase="working")

    real_cursor = runs.watch_stream_cursor
    attempts = {"n": 0}

    def cursor_meeting_a_config_error(project, **kwargs):
        """The per-tick read: raises while the config is unreadable."""
        if attempts["n"] == 0:
            attempts["n"] += 1
            raise FlightConfigError(
                "<merged layer>",
                "backends",
                "an unknown key the running schema rejects",
            )
        return real_cursor(project, **kwargs)

    monkeypatch.setattr(runs, "watch_stream_cursor", cursor_meeting_a_config_error)

    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired, "the arming needs a live producer seat to reach a read"
        events = _arm()

    printed = capfd.readouterr().out

    assert attempts["n"] == 1, "the per-tick read met the config error exactly once"
    assert [event["run_id"] for event in events] == [RUN_A], (
        f"the follower resumed delivering once the read stopped raising; got {events!r}"
    )

    deferrals = [line for line in printed.splitlines() if "deferred a tick" in line]
    assert len(deferrals) == 1, (
        f"exactly one dim deferral line, printed once; got {deferrals!r} from {printed!r}"
    )
    assert deferrals[0].startswith(cli.HISTORY_DIM), (
        f"the deferral line is dimmed; got {deferrals[0]!r}"
    )
    assert "flight configuration" in deferrals[0], (
        f"the deferral line names what could not be read; got {deferrals[0]!r}"
    )
