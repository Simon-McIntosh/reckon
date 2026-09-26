"""The lanes view prefers attributable live quota probes over old receipts.

A receipt is a position only while it is inside its shelf life. Past that, the
lane re-queries the probe it already read and reports the fresh figure; a probe
that cannot answer leaves the old figure with the age that disqualifies it and
a serving state of unknown, so the row reads as neither headroom nor exhaustion.
The probe is read once for the whole composition, so a lane that re-queries
costs no second probe invocation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import _backends, _store, mcp_views
from reckon.crew import staleness

SHORT_WINDOW_MINUTES = 5 * 60
LONG_WINDOW_MINUTES = 7 * 24 * 60
SHARED_SHORT_RESET = 1_893_484_800
SHARED_LONG_RESET = 1_894_089_600
SEPARATE_LONG_RESET = SHARED_LONG_RESET + 4 * 60 * 60
_WRITE_REACHABLE_TREES = ("crew/lane-probes", "crew/lanes")


@dataclass(frozen=True)
class _Reading:
    window_minutes: int
    used_percent: int
    resets_at: int


@dataclass(frozen=True)
class _Receipt:
    model_context_window: int
    quota_readings: dict[int, _Reading]


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _receipt(short_used: int, long_used: int, long_reset: int) -> _Receipt:
    return _Receipt(
        model_context_window=128_000,
        quota_readings={
            SHORT_WINDOW_MINUTES: _Reading(
                SHORT_WINDOW_MINUTES, short_used, SHARED_SHORT_RESET
            ),
            LONG_WINDOW_MINUTES: _Reading(LONG_WINDOW_MINUTES, long_used, long_reset),
        },
    )


def _probe_block(short_used: int = 95, long_used: int = 37) -> dict[str, Any]:
    return {
        "headroom": "known",
        "quota_windows": {
            SHORT_WINDOW_MINUTES: {
                "window_minutes": SHORT_WINDOW_MINUTES,
                "used_percent": short_used,
                "resets_at": _backends._epoch_to_iso(SHARED_SHORT_RESET),
            },
            LONG_WINDOW_MINUTES: {
                "window_minutes": LONG_WINDOW_MINUTES,
                "used_percent": long_used,
                "resets_at": _backends._epoch_to_iso(SHARED_LONG_RESET),
            },
        },
        "detail": "account quota probe answered",
    }


def _config(*, shelf_life_minutes: int = 1) -> dict[str, Any]:
    return {
        "budget": {"evidence_shelf_life_minutes": shelf_life_minutes},
        "backends": {
            "shared": {"launch": "cli", "command": "codex"},
            "separate": {"launch": "cli", "command": "codex"},
        },
    }


def _pooled_config(*, shelf_life_minutes: int = 1) -> dict[str, Any]:
    """Two declared pools, one command: the host's own codex arrangement.

    Every codex-family lane on this workstation runs the same ``codex`` CLI
    while declaring one of two budget groups, so the command's probe is read
    once for lanes whose declared groups differ.
    """
    return {
        "budget": {"evidence_shelf_life_minutes": shelf_life_minutes},
        "backends": {
            "shared": {
                "launch": "cli",
                "command": "codex",
                "budget_group": "codex-sub",
            },
            "separate": {
                "launch": "cli",
                "command": "codex",
                "budget_group": "spark-sub",
            },
        },
    }


def _real_config_home() -> Path:
    """The configuration home resolution lands in, before any test redirects it."""
    configured = os.environ.get("RECKON_HOME")
    if configured:
        return Path(configured).expanduser()
    xdg = Path.home() / ".config" / "reckon"
    return xdg if xdg.exists() else Path.home() / "docs-server"


_REAL_CONFIG_HOME = _real_config_home()


def _inventory(root: Path) -> list[tuple[str, int, int]]:
    """The composition-writable trees under ``root``, walked whole.

    Only the two subtrees a composition writes a probe cache or a lane document
    into are inventoried; a tree that does not exist yet contributes nothing,
    so a write that creates one is caught as a mismatch as well as a modified
    file inside one that already exists.  The rest of the configuration home is
    deliberately out of scope: a fleet dispatch creates and removes run
    directories and live pointers every few seconds, so an inventory reaching
    ``crew/runs`` or ``crew/live`` would report a peer's dispatch as this
    test's write.
    """
    findings: list[tuple[str, int, int]] = []
    for subtree in _WRITE_REACHABLE_TREES:
        whole = root / subtree
        if not whole.exists():
            continue
        for path in sorted(whole.rglob("*")):
            try:
                path_stat = path.stat()
            except OSError:
                continue
            findings.append(
                (
                    str(path.relative_to(root)),
                    path_stat.st_size,
                    int(path_stat.st_mtime),
                )
            )
    return findings


@pytest.fixture(autouse=True)
def real_config_home_is_untouched(isolated_reckon_home: Path) -> None:
    """Every case here runs against a temporary home; the real one is left alone.

    Pointing ``RECKON_HOME`` at a temporary tree proves an isolated read.  The
    write is the direction that can make another session wrong, so the real
    home's composition-writable subtrees are inventoried before and after every
    case and must come back identical -- an isolated read does not prove an
    isolated write.  Only those subtrees are walked: a fleet dispatch churns
    run directories and live pointers constantly, so inventorying the whole
    home would fail on a peer's dispatch, which no composition writes, and
    accuse this test's own code of it.
    """
    assert _store._config_home() == isolated_reckon_home.resolve()
    before = _inventory(_REAL_CONFIG_HOME)
    yield
    assert _inventory(_REAL_CONFIG_HOME) == before, (
        "a lanes-view case wrote into the real configuration home "
        f"{_REAL_CONFIG_HOME} rather than its temporary one"
    )


def _view(
    probe_reader,
    *,
    composed_at: datetime | None = None,
    config: dict[str, Any] | None = None,
    receipt_age: timedelta = timedelta(hours=4),
) -> dict[str, Any]:
    now = composed_at or datetime(2030, 1, 1, tzinfo=UTC)
    ancient = now - receipt_age
    selected_config = config if config is not None else _config()
    receipts = {
        "shared-session": _receipt(11, 22, SHARED_LONG_RESET),
        "separate-session": _receipt(73, 43, SEPARATE_LONG_RESET),
        "plain-session": _receipt(13, 23, SHARED_LONG_RESET),
    }
    sessions = {
        "shared": "shared-session",
        "separate": "separate-session",
        "plain": "plain-session",
    }
    runs = [
        {
            "backend": backend,
            "session_id": sessions[backend],
            "completed_at": _stamp(ancient),
        }
        for backend in selected_config["backends"]
    ]
    return mcp_views.crew_lanes_view(
        selected_config,
        runs,
        receipt_reader=receipts.__getitem__,
        probe_reader=probe_reader,
        composed_at=_stamp(now),
    )


def _lanes(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {lane["backend"]: lane for lane in view["lanes"]}


def _windows(lane: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {window["window_minutes"]: window for window in lane["quota_windows"]}


def test_an_ancient_receipt_yields_to_the_probe_for_every_lane_on_that_account():
    """A receipt past its shelf life is re-queried, and the fresh figure wins.

    Both lanes draw on the one account the probe describes, so both report the
    probe's figure: the lane whose window signature the probe matches adopts it
    directly, and the lane whose own receipt is four hours past the declared
    one-minute shelf life is re-queried and reports the same fresh number
    rather than its own old one.  The probe is read once for the whole
    composition, so the re-query costs no second probe invocation.
    """
    composed_at = datetime(2030, 1, 1, tzinfo=UTC)
    invocations = 0

    def probe_reader(backend, settings):
        nonlocal invocations
        invocations += 1
        return _probe_block()

    lanes = _lanes(_view(probe_reader, composed_at=composed_at))

    shared = _windows(lanes["shared"])
    separate = _windows(lanes["separate"])

    assert lanes["shared"]["probe_status"] == "answered"
    assert lanes["shared"]["quota_source"] == "probe"
    assert lanes["shared"]["requeried"] is False
    assert {window["source"] for window in shared.values()} == {"probe"}
    assert {window["observed_at"] for window in shared.values()} == {
        _stamp(composed_at)
    }
    assert {window["age_seconds"] for window in shared.values()} == {0}
    assert all(
        window["serving_state"] != mcp_views.STALE_SERVING_STATE
        for window in shared.values()
    )

    # The lane's rows are the probe's rows now, so its probe fields agree with
    # its source and its re-query: a row showing the probe's fresh figure while
    # still reporting the probe unmatched would contradict itself, and the
    # status must not describe the lane's replaced receipt either.
    assert (
        lanes["separate"]["probe_status"],
        lanes["separate"]["quota_source"],
        lanes["separate"]["requeried"],
    ) == ("answered", "probe", True)
    # The re-queried lane reports the figure from the one read the probe gave
    # this composition: the pair is asserted together, because a re-query that
    # asked the probe again would show it here as a second invocation, and a
    # re-query that never happened would show the lane's own old receipt.
    assert (invocations, lanes["separate"]["quota_source"]) == (1, "probe")
    assert lanes["separate"]["requeried"] is True
    assert {window["source"] for window in separate.values()} == {"probe"}
    assert {window["observed_at"] for window in separate.values()} == {
        _stamp(composed_at)
    }
    assert {window["age_seconds"] for window in separate.values()} == {0}
    assert separate[LONG_WINDOW_MINUTES]["used_percent"] == 37
    assert separate[LONG_WINDOW_MINUTES]["used_percent"] != 43
    assert all(
        window["serving_state"] != mcp_views.STALE_SERVING_STATE
        for window in separate.values()
    )


def test_a_command_declared_by_two_pools_still_answers_its_lanes_requery() -> None:
    """A shared command answers its lanes' re-query; a second pool does not bar it.

    This is the host's own arrangement: every codex-family lane runs the same
    ``codex`` command while declaring one of two budget groups.  The ownership
    test that governs adopting a matching probe cannot govern the re-query --
    applied here it refuses the very probe the lane's command was read from, so
    no stale lane of a shared command ever shows a fresh figure and every row
    falls back to its old receipt.
    """
    composed_at = datetime(2030, 1, 1, tzinfo=UTC)
    invocations = 0

    def probe_reader(backend, settings):
        nonlocal invocations
        invocations += 1
        return _probe_block()

    lanes = _lanes(
        _view(probe_reader, composed_at=composed_at, config=_pooled_config())
    )

    assert invocations == 1
    assert (lanes["shared"]["budget_group"], lanes["separate"]["budget_group"]) == (
        "codex-sub",
        "spark-sub",
    )
    for lane in (lanes["shared"], lanes["separate"]):
        assert (
            lane["probe_status"],
            lane["quota_source"],
            lane["requeried"],
        ) == ("answered", "probe", True)
    shared_windows = _windows(lanes["shared"])
    separate_windows = _windows(lanes["separate"])
    assert shared_windows[LONG_WINDOW_MINUTES]["used_percent"] == 37
    assert shared_windows[LONG_WINDOW_MINUTES]["used_percent"] != 22
    assert separate_windows[LONG_WINDOW_MINUTES]["used_percent"] == 37
    assert separate_windows[LONG_WINDOW_MINUTES]["used_percent"] != 43


@pytest.mark.parametrize(
    "probe_reader",
    [
        pytest.param(
            lambda backend, settings: (_ for _ in ()).throw(RuntimeError("boom")),
            id="raises",
        ),
        pytest.param(lambda backend, settings: None, id="returns-nothing"),
    ],
)
def test_a_requery_that_cannot_answer_keeps_the_old_figure_and_its_age(
    probe_reader,
) -> None:
    """A failed re-query reports the age that disqualifies the figure, and why.

    The lane has nothing fresh to show, so it shows what it has -- the
    four-hour-old receipt -- with the age that marks it a record of the past and
    a serving state of unknown, which is neither headroom nor exhaustion.  The
    reason travels as a field beside the state, because a row that renders
    unknown without saying which refusal produced it leaves the caller to guess
    whether the probe was silent or absent.
    """
    composed_at = datetime(2030, 1, 1, tzinfo=UTC)
    lane = _lanes(_view(probe_reader, composed_at=composed_at))["shared"]
    windows = _windows(lane)

    assert lane["requeried"] is True
    assert lane["quota_source"] == "receipt"
    assert lane["observed_at"] == _stamp(composed_at - timedelta(hours=4))
    assert {window["source"] for window in windows.values()} == {"receipt"}
    assert {window["age_seconds"] for window in windows.values()} == {4 * 60 * 60}
    assert {window["serving_state"] for window in windows.values()} == {
        staleness.SERVING_STATE_UNKNOWN
    }
    assert all(
        window["unmeasured"]["serving_state"] == "requery_did_not_answer"
        for window in windows.values()
    )
    assert windows[LONG_WINDOW_MINUTES]["used_percent"] == 22


def test_a_receipt_inside_its_shelf_life_is_reported_without_a_requery() -> None:
    """A live reading is reported as it stands, and a probe is not its successor.

    The receipt is half an hour old against a one-hour shelf life, so it is a
    position and the lane reports it.  The probe's windows deliberately differ
    from the receipt's, and the probe answers: an implementation that re-queried
    unconditionally, or that adopted every answering probe, would report the
    probe's figure here.  The probe is read -- once, as always -- and its figure
    is still not what the lane shows.
    """
    composed_at = datetime(2030, 1, 1, tzinfo=UTC)
    invocations = 0

    def probe_reader(backend, settings):
        nonlocal invocations
        invocations += 1
        return _probe_block()

    lane = _lanes(
        _view(
            probe_reader,
            composed_at=composed_at,
            config=_config(shelf_life_minutes=60),
            receipt_age=timedelta(minutes=30),
        )
    )["separate"]
    windows = _windows(lane)

    assert invocations == 1
    assert lane["requeried"] is False
    assert lane["quota_source"] == "receipt"
    assert {window["source"] for window in windows.values()} == {"receipt"}
    assert {window["age_seconds"] for window in windows.values()} == {30 * 60}
    assert windows[LONG_WINDOW_MINUTES]["used_percent"] == 43
    assert windows[LONG_WINDOW_MINUTES]["used_percent"] != 37
    assert windows[LONG_WINDOW_MINUTES]["serving_state"] not in {
        mcp_views.STALE_SERVING_STATE,
        staleness.SERVING_STATE_UNKNOWN,
    }


def test_probe_windows_keep_their_lengths_and_values() -> None:
    short_used = 96
    long_used = 38
    shared = _windows(
        _lanes(_view(lambda backend, settings: _probe_block(short_used, long_used)))[
            "shared"
        ]
    )

    assert set(shared) == {SHORT_WINDOW_MINUTES, LONG_WINDOW_MINUTES}
    assert shared[SHORT_WINDOW_MINUTES]["used_percent"] == short_used
    assert shared[LONG_WINDOW_MINUTES]["used_percent"] == long_used
    assert shared[LONG_WINDOW_MINUTES]["used_percent"] != short_used


@pytest.mark.parametrize(
    "probe_reader",
    [
        pytest.param(
            lambda backend, settings: (_ for _ in ()).throw(RuntimeError("boom")),
            id="raises",
        ),
        pytest.param(lambda backend, settings: None, id="returns-nothing"),
    ],
)
def test_probe_failure_falls_back_to_the_receipt_and_says_it_did_not_answer(
    probe_reader,
) -> None:
    lane = _lanes(_view(probe_reader))["shared"]

    assert lane["probe_status"] == "unavailable"
    assert "probe did not answer" in lane["probe_detail"]
    assert lane["quota_source"] == "receipt"
    assert {window["source"] for window in lane["quota_windows"]} == {"receipt"}


def test_two_reads_inside_the_declared_cache_lifetime_probe_once() -> None:
    invocations = 0

    def probe_reader(backend, settings):
        nonlocal invocations
        invocations += 1
        return _probe_block()

    first = datetime(2030, 1, 1, tzinfo=UTC)
    _view(probe_reader, composed_at=first)
    second = _view(probe_reader, composed_at=first + timedelta(seconds=30))

    assert invocations == 1
    assert _lanes(second)["shared"]["probe_cached"] is True


def test_a_dialect_without_a_probe_never_calls_the_probe_reader() -> None:
    """A command with no readable surface leaves the reader uninvoked.

    The command is deliberately synthetic and names no harness at all, because a
    real harness name is exactly what failed this case before: it once used
    ``claude`` as its stand-in for a probe-less dialect, and that stopped being
    true when the claude dialect took over its own account-surface read, so the
    fixture silently became a probe carrier.  Any name a dialect is a candidate
    for carries the same fuse, since the composer resolves the dialect from the
    command; a token no harness would ever take cannot acquire one.
    """
    invocations = 0

    def probe_reader(backend, settings):
        nonlocal invocations
        invocations += 1
        return _probe_block()

    lane = _lanes(
        _view(
            probe_reader,
            config={
                "backends": {
                    "plain": {
                        "launch": "cli",
                        "command": "synthetic-no-dialect-harness",
                    },
                }
            },
        )
    )["plain"]

    assert invocations == 0
    assert lane["probe_status"] == "not_declared"
    assert lane["quota_source"] == "receipt"


@pytest.mark.parametrize(
    ("used_percent", "expected"),
    [
        (mcp_views.AT_RISK_USED_PERCENT, mcp_views.AT_RISK_SERVING_STATE),
        (100, mcp_views.EXHAUSTED_SERVING_STATE),
    ],
)
def test_live_probe_keeps_the_existing_risk_and_exhaustion_states(
    used_percent: int, expected: str
) -> None:
    lane = _lanes(
        _view(lambda backend, settings: _probe_block(short_used=used_percent))
    )["shared"]

    assert _windows(lane)[SHORT_WINDOW_MINUTES]["serving_state"] == expected


def test_codex_probe_parser_retains_every_window_keyed_by_length() -> None:
    answer = {
        "result": {
            "rateLimits": {
                "primary": {
                    "usedPercent": 29,
                    "windowDurationMins": SHORT_WINDOW_MINUTES,
                    "resetsAt": SHARED_SHORT_RESET,
                },
                "secondary": {
                    "usedPercent": 71,
                    "windowDurationMins": LONG_WINDOW_MINUTES,
                    "resetsAt": SHARED_LONG_RESET,
                },
            }
        }
    }

    block = _backends.probe_budget(
        backend_name="metered",
        backend={"launch": "cli", "command": "codex"},
        runner=lambda probe: answer,
    )

    assert set(block["quota_windows"]) == {
        SHORT_WINDOW_MINUTES,
        LONG_WINDOW_MINUTES,
    }
    assert block["quota_windows"][SHORT_WINDOW_MINUTES]["used_percent"] == 29
    assert block["quota_windows"][LONG_WINDOW_MINUTES]["used_percent"] == 71


def _lane_for_run(run: dict[str, Any], *, composed_at: datetime) -> dict[str, Any]:
    """Compose the lanes view over one run, entering where a caller enters.

    The entry point is the composer rather than the dating helper, because a
    direct call proves the function and not the wiring: a lane can only be shown
    to report a stamp if the view that publishes it is the thing asked.
    """
    view = mcp_views.crew_lanes_view(
        {"backends": {"solo": {"launch": "cli", "command": "codex"}}},
        [run],
        receipt_reader=lambda session_id: _receipt(11, 22, SHARED_LONG_RESET),
        probe_reader=lambda backend, settings: None,
        composed_at=_stamp(composed_at),
    )
    return _lanes(view)["solo"]


def test_the_lane_dates_a_run_whose_only_stamp_is_created_at() -> None:
    """A record dated only by created_at is datable, and the lane publishes it.

    The field list this view kept used to stop before created_at, so a record
    whose only stamp is that one rendered unmeasured here while the pace reader
    dated the same record. The same stamp decides which of a backend's sessions
    is the newest, so the two surfaces could disagree about which run a figure
    came from.
    """
    composed = datetime(2030, 1, 1, tzinfo=UTC)
    created = composed - timedelta(hours=2)

    lane = _lane_for_run(
        {
            "backend": "solo",
            "session_id": "sess-created-at",
            "created_at": _stamp(created),
        },
        composed_at=composed,
    )

    assert lane["receipt_state"] == "readable"
    assert lane["observed_at"] == _stamp(created)
    # A lane with no date carries the refusal under this key; a lane that
    # resolved one carries no refusal at all, which is the distinction asserted.
    assert lane.get("unmeasured", {}).get("observed_at") is None


def test_the_lane_passes_over_a_budget_stamp_it_cannot_parse() -> None:
    """A malformed budget stamp loses to the readable stamp behind it.

    The shared helper parses before it returns, so a value that is present but
    unreadable is passed over rather than published: the lane dates the run by
    its next readable stamp. A helper that returned the first truthy field would
    hand the malformed text back as a date instead.
    """
    composed = datetime(2030, 1, 1, tzinfo=UTC)
    completed = composed - timedelta(hours=3)

    lane = _lane_for_run(
        {
            "backend": "solo",
            "session_id": "sess-malformed-stamp",
            "budget": {"observed_at": "yesterday afternoon"},
            "completed_at": _stamp(completed),
        },
        composed_at=composed,
    )

    assert lane["observed_at"] == _stamp(completed)
    assert lane["observed_at"] != "yesterday afternoon"
    assert lane.get("unmeasured", {}).get("observed_at") is None
