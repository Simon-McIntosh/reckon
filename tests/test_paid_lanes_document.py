"""The published headroom document, and the pre-flight that reads it.

Headroom for a metered account is observed continuously and published as one
document, so a headroom reading is available between dispatches rather than
only at a refusal. These cases pin the payload the document carries for each
account, the reconciliation of its sources per window, and the agreement
between the per-account figures and the per-group pace the pre-flight reports.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import budget
from reckon.crew import paid_lanes, window_reading

# A fixed instant: every age and every reset is arithmetic rather than a stroke
# of the clock, so a case cannot pass on the day it was written and drift after.
NOW = datetime(2026, 9, 24, 18, 0, 0, tzinfo=UTC)

# Three accounts, one wallet each, so a group's figures are one member's and the
# per-account/per-group agreement is a comparison rather than a coincidence of
# which member happened to be freshest.
CONFIG = {
    "backends": {
        "clive": {
            "launch": "cli",
            "command": "claude",
            "sandbox": "worktree-full",
            "budget_group": "local",
        },
        "codex": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "budget_group": "codex",
        },
        "claude": {
            "launch": "cli",
            "command": "claude",
            "sandbox": "worktree-full",
            "budget_group": "claude",
        },
    },
    "roles": {},
    "budget": {"exhausted_statuses": []},
}


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _reading(
    five_hour: float | None,
    seven_day: float | None,
    *,
    age_seconds: float = 30.0,
    five_reset_in_hours: float = 1.0,
    week_reset_in_hours: float = 100.0,
) -> window_reading.WindowReading:
    """One account's window report, aged exactly ``age_seconds`` against ``NOW``.

    A ``None`` figure is omitted rather than passed as ``0.0``: an unread window
    is an absence, and a zero would read as an empty one.
    """
    observed = NOW - timedelta(seconds=age_seconds)
    figures = []
    if five_hour is not None:
        figures.append(
            window_reading.WindowFigure(
                period="five_hour",
                utilisation=five_hour,
                observed_at=observed,
                age_seconds=age_seconds,
                resets_at=_iso(NOW + timedelta(hours=five_reset_in_hours)),
            )
        )
    if seven_day is not None:
        return _with_week(
            figures, observed, age_seconds, seven_day, week_reset_in_hours
        )
    return window_reading.WindowReading(
        figures=tuple(figures), observed_at=observed, age_seconds=age_seconds
    )


def _with_week(
    figures: list,
    observed: datetime,
    age_seconds: float,
    seven_day: float,
    week_reset_in_hours: float,
) -> window_reading.WindowReading:
    figures.append(
        window_reading.WindowFigure(
            period="seven_day",
            utilisation=seven_day,
            observed_at=observed,
            age_seconds=age_seconds,
            resets_at=_iso(NOW + timedelta(hours=week_reset_in_hours)),
        )
    )
    return window_reading.WindowReading(
        figures=tuple(figures), observed_at=observed, age_seconds=age_seconds
    )


def _three_accounts() -> dict[str, list[paid_lanes.Candidate]]:
    return {
        "clive": [paid_lanes.Candidate("stream", _reading(0.10, 0.05))],
        "codex": [paid_lanes.Candidate("receipt", _reading(0.42, 0.21))],
        "claude": [paid_lanes.Candidate("stream", _reading(0.77, 0.44))],
    }


def _group(report: list[dict], name: str) -> dict:
    found = [entry for entry in report if entry["group"] == name]
    assert len(found) == 1, f"expected exactly one entry for {name!r}, got {found}"
    return found[0]


# ── One reading for each account, with the figures behind it ────────────────


def test_a_reading_is_published_for_each_of_the_three_backends() -> None:
    """Every account carries both metered windows with its source and its age."""
    document = paid_lanes.compose_document(
        ["clive", "codex", "claude"], sources=_three_accounts(), moment=NOW
    )

    assert sorted(document["accounts"]) == ["claude", "clive", "codex"]
    expected = {"clive": 0.10, "codex": 0.42, "claude": 0.77}
    for account, figure in expected.items():
        entry = document["accounts"][account]
        assert entry["state"] == paid_lanes.OBSERVED
        assert entry["source"]
        assert entry["observed_at"] == (NOW - timedelta(seconds=30)).isoformat()
        five = entry["windows"]["five_hour"]
        assert five["state"] == paid_lanes.OBSERVED
        assert five["utilisation"] == pytest.approx(figure)
        # The derived fields are carried beside the figure, not left for the
        # reader to recompute: a window draining faster than it refills is the
        # early warning the position-only reading cannot give.
        assert five["burn_multiple"] is not None
        assert five["projected_exhaustion"] is not None
        assert five["resets_at"] is not None


def test_each_window_carries_its_own_source_and_observation() -> None:
    """The account entry names the freshest source per window, not per account."""
    older = paid_lanes.Candidate(
        "receipt",
        _reading(0.90, 0.21, age_seconds=1800.0),
    )
    newer = paid_lanes.Candidate("stream", _reading(0.40, None, age_seconds=5.0))
    document = paid_lanes.compose_document(
        ["codex"], sources={"codex": [older, newer]}, moment=NOW
    )

    windows = document["accounts"]["codex"]["windows"]
    # The stream is fresher and carries the five-hour window, so it speaks for it
    # while the older receipt holds the week the stream never reported.
    assert windows["five_hour"]["source"] == "stream"
    assert windows["five_hour"]["utilisation"] == pytest.approx(0.40)
    assert windows["seven_day"]["source"] == "receipt"
    assert windows["seven_day"]["utilisation"] == pytest.approx(0.21)


# ── One failed precondition leaves only its own field unknown ───────────────


def test_one_failed_precondition_leaves_only_that_field_unknown() -> None:
    """A reading that carried the week alone leaves the five-hour clock unknown."""
    document = paid_lanes.compose_document(
        ["codex"],
        sources={"codex": [paid_lanes.Candidate("rollout", _reading(None, 0.44))]},
        moment=NOW,
    )

    windows = document["accounts"]["codex"]["windows"]
    assert windows["seven_day"]["state"] == paid_lanes.OBSERVED
    assert windows["seven_day"]["utilisation"] == pytest.approx(0.44)
    # The five-hour clock is an explicit absence and never a zero, which cannot
    # be told apart from a measured empty window.
    assert windows["five_hour"]["state"] == paid_lanes.UNKNOWN
    assert windows["five_hour"]["utilisation"] is None
    assert windows["five_hour"]["utilisation"] != 0.0


# ── A too-old field is stale alone ──────────────────────────────────────────


def test_a_too_old_field_is_reported_stale_alone() -> None:
    """A stale week does not mark a fresh five-hour clock stale with it."""
    fresh = NOW - timedelta(seconds=30)
    old = NOW - timedelta(seconds=7200)
    stale_reading = window_reading.WindowReading(
        figures=(
            window_reading.WindowFigure(
                period="five_hour",
                utilisation=0.30,
                observed_at=old,
                age_seconds=7200.0,
                resets_at=_iso(NOW + timedelta(hours=1)),
            ),
            window_reading.WindowFigure(
                period="seven_day",
                utilisation=0.21,
                observed_at=fresh,
                age_seconds=30.0,
                resets_at=_iso(NOW + timedelta(hours=100)),
            ),
        ),
        observed_at=fresh,
        age_seconds=30.0,
    )
    document = paid_lanes.compose_document(
        ["codex"],
        sources={"codex": [paid_lanes.Candidate("stream", stale_reading)]},
        moment=NOW,
        stale_seconds=3600.0,
    )

    windows = document["accounts"]["codex"]["windows"]
    assert windows["seven_day"]["stale"] is False
    assert windows["five_hour"]["stale"] is True
    # Stale alone: the stale window still carries the figure it measured.
    assert windows["five_hour"]["utilisation"] == pytest.approx(0.30)


# ── The pre-flight paces from the document, and the figures agree ───────────


def _write_and_point(tmp_path: Path, document: dict, monkeypatch) -> Path:
    path = tmp_path / "paid-lanes.json"
    paid_lanes.write_document_atomically(document, path)
    monkeypatch.setenv(paid_lanes.DOCUMENT_ENV, str(path))
    return path


def test_preflight_paces_from_the_document_and_the_five_hour_figures_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-account figures and the per-group pace are one reading, not two.

    The document is written to disk and reached through the environment, so the
    path the pre-flight takes is the document's own reader. Each group here has
    one member, so a group whose clock disagreed with its account would be a
    second, divergent implementation of the same figure.
    """
    document = paid_lanes.compose_document(
        ["clive", "codex", "claude"], sources=_three_accounts(), moment=NOW
    )
    _write_and_point(tmp_path, document, monkeypatch)

    report = budget.preflight("demo", CONFIG, now=NOW)

    group_name = {"clive": "local", "codex": "codex", "claude": "claude"}
    for account, group in group_name.items():
        account_five = document["accounts"][account]["windows"]["five_hour"][
            "utilisation"
        ]
        group_five = _group(report["groups"], group)["clocks"]["five_hour"][
            "utilisation"
        ]
        assert group_five == pytest.approx(account_five), account
        account_week = document["accounts"][account]["windows"]["seven_day"][
            "utilisation"
        ]
        group_week = _group(report["groups"], group)["clocks"]["seven_day"][
            "utilisation"
        ]
        assert group_week == pytest.approx(account_week), account


# ── The command publishes the document it composes ──────────────────────────


def test_the_command_writes_the_document_to_the_named_path(tmp_path: Path) -> None:
    """``--once`` writes the composed document, parseable and whole."""
    path = tmp_path / "out" / "paid-lanes.json"

    exit_code = paid_lanes.main(["--once", "--path", str(path)])

    assert exit_code == 0
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["document"] == "paid-lanes"
    assert isinstance(written["accounts"], dict)
