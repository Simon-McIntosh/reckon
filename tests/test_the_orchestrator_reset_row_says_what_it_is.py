"""The reset row says what it is, instead of drawing a bare transition.

The lead's announcement of a usage-limit reset is recorded under the budget
group ``claude-orchestrators`` and reaches every pane as one broadcast row.
That row is a fact about a budget group, not a state change of a run, but the
pane mapped it onto the ``working`` word like any other row and drew
``limit claude-orchestrators new → working``. Nothing in that line says it is a
limit reset or that it asks a stopped session to continue, so each reader had to
work it out for themselves.

The row is now drawn as its own clause — what it is, the group, the clock it
resets, and whose work may continue — with the announcer's reason after it when
the record carries one. The closing clause is chosen by the group: a wallet no
configured backend draws on is the orchestrator wallet, so its reset frees
orchestrator sessions; a group a backend draws on frees that worker lane and
claims nothing above it; a config that declares no wallet names no lane at all.
These tests drive the real
producer path (``budget_reset.mark_used`` into ``lift_watch.sweep_resets``,
through the real pane claim) so the measurement counts the line a pane prints
rather than a fixture's opinion of it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import budget_reset, lift_watch, ticker
from reckon.crew.recovery_watch import format_watch_transition
from reckon.crew_follow_commands import follower_row_path, lift_claim_dir

SESSION = "s-orchestrator"
GROUP = "claude-orchestrators"
CLOCK = "five_hour"
REASON = "the lead reports the usage limits have reset"
PROJECTS = ("alpha", "beta")
NOW = datetime(2026, 10, 14, 18, 30, tzinfo=UTC)
WIDE = 200

# A host layer declaring one worker lane's wallet. Its purpose here is to make
# the pane's reading of the config a declared fact rather than an accident of
# which machine runs the tests: with a wallet declared, a group the config does
# not name is the orchestrator wallet.
LANE_GROUP = "claude-sub"
HOST_LAYER = f"version: 1\n\nbackends:\n  claude:\n    budget_group: {LANE_GROUP}\n"


def _listing(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return sorted(str(path) for path in directory.rglob("*"))


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the crew home at a temp dir; prove the real home is untouched."""
    real_claims = lift_claim_dir("")
    before = _listing(real_claims)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    (tmp_path / "flight.yaml").write_text(HOST_LAYER)
    yield tmp_path
    assert _listing(real_claims) == before, "a test wrote the real crew home"


# ── the producer's own path ─────────────────────────────────────────────────


def _announce(*, reason: str | None = REASON) -> dict:
    """One recorded reset announcement, as ``crew budget --reset`` writes it."""
    return budget_reset.mark_used(
        GROUP,
        by="lead",
        moment=NOW,
        reason=reason,
        clock=CLOCK,
        session=None,
    )


def _rows(stream: Path) -> list[dict]:
    if not stream.exists():
        return []
    return [
        json.loads(line) for line in stream.read_text().splitlines() if line.strip()
    ]


def _reset_rows(rows: Sequence[dict]) -> list[dict]:
    return [row for row in rows if row.get("to_state") == lift_watch.LIMIT_RESET]


def _drawn(streams_root: Path, *, start: float = 1000.0) -> list[dict]:
    """Sweep every project's stream into one pane and return the rows it draws.

    One follower stands for one pane, so a row appearing twice in the result is
    the defect the once-per-pane claim exists to prevent.
    """
    follower = follower_row_path(session=SESSION)
    printed: list[dict] = []
    now = start
    for project in PROJECTS:
        stream = streams_root / f"{project}.events"
        lift_watch.sweep_resets(project, seen=None, now=NOW, stream_path=stream)
        for row in _reset_rows(_rows(stream)):
            printed.extend(follower.feed(row, now=now))
            now += 1.0
    return printed


def _rendered(row: dict) -> list[str]:
    """A row as the pane prints it: the watch formatter and the grid."""
    return [
        format_watch_transition(row, ticker=ticker.Ticker(width=WIDE)),
        ticker.Ticker(width=WIDE).render(row),
    ]


# ── the row says what it is ─────────────────────────────────────────────────


def test_the_recorded_orchestrator_reset_row_says_what_it_is(tmp_path: Path):
    """The line a pane prints names the reset, the group, the clock and the duty."""
    _announce()
    drawn = _reset_rows(_drawn(tmp_path))
    assert len(drawn) == 1, f"one pane drew the reset {len(drawn)} times"
    for rendered in _rendered(drawn[0]):
        assert "limit reset" in rendered, rendered
        assert GROUP in rendered, rendered
        assert CLOCK in rendered, rendered
        assert ticker.RESET_ORCHESTRATOR_INSTRUCTION in rendered, rendered
        assert ticker.RESET_LANE_INSTRUCTION not in rendered, rendered
        assert "new → working" not in rendered, rendered
        assert "→" not in rendered, f"a reset row carries no transition: {rendered}"


def test_the_row_carries_the_announcers_reason(tmp_path: Path):
    """The reason the lead's announcement recorded reaches the line."""
    _announce()
    drawn = _reset_rows(_drawn(tmp_path))
    assert len(drawn) == 1, f"one pane drew the reset {len(drawn)} times"
    for rendered in _rendered(drawn[0]):
        assert REASON in rendered, rendered


def test_a_reset_with_no_reason_still_says_what_it_asks(tmp_path: Path):
    """A detected reset records no announcer, so the row carries no reason.

    The instruction is the row's own words rather than part of a clause the
    producer composes, so it survives a record that carries no reason at all.
    """
    detected = {
        "id": "reset-detected-1",
        "group": GROUP,
        "clock": CLOCK,
        "kind": "consumed",
        "detected_at": NOW.isoformat(),
        "session": "",
    }
    row = lift_watch._reset_row(PROJECTS[0], detected, moment=NOW)
    for rendered in _rendered(row):
        assert "limit reset" in rendered, rendered
        assert ticker.RESET_ORCHESTRATOR_INSTRUCTION in rendered, rendered
        assert "announced by" not in rendered, rendered


def test_a_second_sweep_draws_nothing_more_but_a_second_pane_does(tmp_path: Path):
    """One row per pane per event: the sweep id is the pane's own memory."""
    _announce()
    stream = tmp_path / f"{PROJECTS[0]}.events"
    seen = lift_watch.sweep_resets(PROJECTS[0], seen=None, now=NOW, stream_path=stream)
    again = lift_watch.sweep_resets(PROJECTS[0], seen=seen, now=NOW, stream_path=stream)
    assert again <= seen, "a second pass announced the same event again"
    assert len(_reset_rows(_rows(stream))) == 1, "a second pass appended a row"

    second_pane = follower_row_path(session="s-other")
    printed = [
        row
        for row in _reset_rows(_rows(stream))
        for row in second_pane.feed(row, now=2000.0)
    ]
    assert len(printed) == 1, "the other session's pane never drew the reset"


def _reset(group: str) -> dict:
    """One recorded reset for ``group``, built by the producer's own builder."""
    return lift_watch._reset_row(
        PROJECTS[0],
        {
            "id": f"reset-{group}",
            "group": group,
            "clock": CLOCK,
            "kind": "used",
            "by": "lead",
            "reason": REASON,
            "detected_at": NOW.isoformat(),
            "session": "",
        },
        moment=NOW,
    )


def test_a_worker_lanes_reset_claims_nothing_about_orchestrator_sessions():
    """A group a backend draws on is that lane's wallet, so its reset says so.

    Only a group no backend declares belongs to the orchestrator wallet, and
    the wording follows that fact rather than the row: the same renderer prints
    the lane clause for a lane's group and the orchestrator clause for the
    wallet above it, so no row promises orchestrator sessions a lane reset did
    not free.
    """
    assert ticker.group_is_paced(LANE_GROUP) is True
    for rendered in _rendered(_reset(LANE_GROUP)):
        assert "limit reset" in rendered, rendered
        assert LANE_GROUP in rendered, rendered
        assert ticker.RESET_LANE_INSTRUCTION in rendered, rendered
        assert "orchestrator" not in rendered, rendered


def test_a_config_declaring_no_group_names_no_lane(tmp_path: Path, monkeypatch):
    """With no wallet declared, the row claims no lane and no orchestrator.

    A host that declares no budget group has said nothing about which sessions
    a reset frees, and the pane must not turn that silence into the orchestrator
    clause the lead's own wallet gets.
    """
    empty = tmp_path / "unconfigured"
    empty.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(empty))
    assert ticker.group_is_paced(LANE_GROUP) is None
    assert ticker.group_is_paced(GROUP) is None
    for rendered in _rendered(_reset(GROUP)):
        assert ticker.RESET_NEUTRAL_INSTRUCTION in rendered, rendered
        assert ticker.RESET_ORCHESTRATOR_INSTRUCTION not in rendered, rendered
        assert ticker.RESET_LANE_INSTRUCTION not in rendered, rendered


def test_the_row_stays_on_one_line_at_the_panes_minimum_width():
    """A long reason is cut to the pane rather than wrapping the row."""
    row = lift_watch._reset_row(
        PROJECTS[0],
        {
            "id": "reset-wide-1",
            "group": GROUP,
            "clock": CLOCK,
            "kind": "used",
            "by": "lead",
            "reason": "the lead reports the usage limits have reset after a long wait",
            "detected_at": NOW.isoformat(),
            "session": "",
        },
        moment=NOW,
    )
    rendered = ticker.Ticker(width=ticker.MIN_WIDTH).render(row)
    assert "\n" not in rendered, rendered
    assert "limit reset" in rendered, rendered
    assert len(rendered) <= ticker.MIN_WIDTH, (
        f"{len(rendered)} columns for a {ticker.MIN_WIDTH}-column pane"
    )
