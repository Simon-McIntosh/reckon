"""The production pre-flight reads the published headroom document.

The production callers -- ``reckon crew preflight`` and the MCP ``budget`` view
-- always inject a window reading of their own from what earlier runs recorded,
and they also name the published headroom document. ``budget.preflight`` merges
the two per backend: the document speaks for a backend it carries a reading for
that is fresh at read time, and the caller's recorded reading speaks for every
other backend. The report names the source that spoke for each backend, so a
reader sees the split rather than inferring it.

Two properties of the merge are load-bearing and are pinned here. A reading's
age is derived from its observation time against the moment of the read, not
from the age the document baked at composition, so a document published long
ago does not read as fresh. And the document path the callers resolve lies
under the crew home, which every case's fixture isolates, so a pre-flight reads
its own temporary tree and never a document the workstation happens to hold.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import budget, cli, flight, mcp
from reckon.crew import paid_lanes, window_reading

# A fixed instant for the direct cases: every age and reset is arithmetic
# rather than a stroke of the clock, so a case cannot pass on the day it was
# written and drift after.
NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)

# Two accounts, one wallet each, so a group's figures are one member's and a
# per-backend fallback is visible as one group disagreeing rather than two.
CONFIG = {
    "backends": {
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

GROUP_OF = {"codex": "codex", "claude": "claude"}


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _reading(
    five_hour: float,
    seven_day: float,
    *,
    moment: datetime,
    age_seconds: float = 20.0,
) -> window_reading.WindowReading:
    """One account's window report, aged exactly ``age_seconds`` against ``moment``."""
    observed = moment - timedelta(seconds=age_seconds)
    figures = tuple(
        window_reading.WindowFigure(
            period=period,
            utilisation=value,
            observed_at=observed,
            age_seconds=age_seconds,
            resets_at=_iso(moment + timedelta(hours=reset_in_hours)),
        )
        for period, value, reset_in_hours in (
            ("five_hour", five_hour, 1.0),
            ("seven_day", seven_day, 100.0),
        )
    )
    return window_reading.WindowReading(
        figures=figures, observed_at=observed, age_seconds=age_seconds
    )


def _group(report: list[dict], name: str) -> dict:
    found = [entry for entry in report if entry["group"] == name]
    assert len(found) == 1, f"expected exactly one entry for {name!r}, got {found}"
    return found[0]


def _clock_utilisation(
    report: dict, group: str, period: str = "five_hour"
) -> float | None:
    return _group(report["groups"], group)["clocks"][period]["utilisation"]


def _write_document(tmp_path: Path, document: dict) -> Path:
    """Publish the document to a path a direct caller then names explicitly."""
    path = tmp_path / "paid-lanes.json"
    paid_lanes.write_document_atomically(document, path)
    return path


def _install_default_document(
    tmp_path: Path, document: dict, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Install the document where the production callers resolve it.

    The callers resolve the published path through the crew home rather than the
    reader's ``HOME``, so the path a pre-flight reads is the fixture's own tree
    and never the operator's real home. ``HOME`` is not consulted here at all --
    that omission is the property these cases exist to hold. The path is proven
    rather than assumed by asserting the resolver returns the file just written.
    """
    monkeypatch.delenv(paid_lanes.DOCUMENT_ENV, raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("RECKON_HOME", str(home))
    path = home / "paid-lanes.json"
    paid_lanes.write_document_atomically(document, path)
    # The default path the callers name is this file, proven rather than assumed.
    assert budget.published_document_path() == path
    return path


# ── A fresh document reading wins over the recorded window ──────────────────


def test_a_fresh_document_reading_wins_over_the_recorded_window(
    tmp_path: Path,
) -> None:
    """The backend the document covers freshly is paced from the document.

    The caller injects a recorded window of its own -- exactly as the
    production callers do -- so the case fails if the merge never consults the
    document once a reading is present.
    """
    document = paid_lanes.compose_document(
        ["codex"],
        sources={
            "codex": [paid_lanes.Candidate("stream", _reading(0.77, 0.44, moment=NOW))]
        },
        moment=NOW,
    )
    path = _write_document(tmp_path, document)
    recorded = {"codex": _reading(0.11, 0.09, moment=NOW)}

    report = budget.preflight(
        "demo",
        CONFIG,
        now=NOW,
        windows=recorded,
        document_path=path,
    )

    assert _clock_utilisation(report, "codex") == pytest.approx(0.77)
    assert report["window_sources"]["codex"] == budget.WINDOW_SOURCE_DOCUMENT


# ── An absent document leaves every figure as the recorded-window result ────


def test_an_absent_document_leaves_every_figure_as_recorded(tmp_path: Path) -> None:
    """A document that is not there changes nothing: the merge falls back whole.

    The baseline is the same call with no document named at all, so the
    comparison is against today's recorded-window result rather than against a
    number written down here. The declared negative control -- removing the
    per-backend fallback so an absent document yields no figures -- turns the
    document-named call's figures to ``None``, which this case then reports as
    a disagreement.
    """
    recorded = {
        "codex": _reading(0.11, 0.09, moment=NOW),
        "claude": _reading(0.22, 0.18, moment=NOW),
    }
    missing = tmp_path / "not-published" / "paid-lanes.json"
    assert not missing.exists()

    baseline = budget.preflight("demo", CONFIG, now=NOW, windows=recorded)
    with_absent = budget.preflight(
        "demo",
        CONFIG,
        now=NOW,
        windows=recorded,
        document_path=missing,
    )

    for name, group in GROUP_OF.items():
        assert _clock_utilisation(with_absent, group) is not None, name
        assert _clock_utilisation(with_absent, group) == pytest.approx(
            _clock_utilisation(baseline, group)
        ), name
        assert with_absent["window_sources"][name] == budget.WINDOW_SOURCE_RECORDED


# ── A stale reading falls back for that backend alone ───────────────────────


def test_a_stale_reading_falls_back_for_that_backend_alone(tmp_path: Path) -> None:
    """One backend's stale figure does not drag its fresh sibling down with it.

    The document carries a fresh ``codex`` figure and a ``claude`` figure two
    hours old, past the staleness horizon. ``codex`` is paced from the document
    and ``claude`` from the caller's recorded window, and the report names each
    source, so a reader can see the split rather than infer it.
    """
    document = paid_lanes.compose_document(
        ["codex", "claude"],
        sources={
            "codex": [paid_lanes.Candidate("stream", _reading(0.77, 0.44, moment=NOW))],
            "claude": [
                paid_lanes.Candidate(
                    "stream",
                    _reading(0.66, 0.55, moment=NOW, age_seconds=7200.0),
                )
            ],
        },
        moment=NOW,
    )
    path = _write_document(tmp_path, document)
    recorded = {
        "codex": _reading(0.11, 0.09, moment=NOW),
        "claude": _reading(0.22, 0.18, moment=NOW),
    }

    report = budget.preflight(
        "demo",
        CONFIG,
        now=NOW,
        windows=recorded,
        document_path=path,
    )

    assert _clock_utilisation(report, "codex") == pytest.approx(0.77)
    assert _clock_utilisation(report, "claude") == pytest.approx(0.22)
    assert report["window_sources"]["codex"] == budget.WINDOW_SOURCE_DOCUMENT
    assert report["window_sources"]["claude"] == budget.WINDOW_SOURCE_RECORDED


# ── The CLI and MCP paths report the same per-backend figures ───────────────


def test_the_cli_and_mcp_paths_agree_on_every_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both production callers name the default document and read it the same.

    Each caller resolves its own flow -- the CLI through ``crew preflight`` and
    the MCP through the ``budget`` view -- against one installed document and
    one stubbed flight config. Agreement is asserted on the per-backend source
    map and each group's five-hour clock, which is what a reader of either
    surface reads. Both callers inject recorded windows and find none, so every
    figure here comes from the document; a path that failed to name it would
    report unknown clocks instead.
    """
    moment = datetime.now(UTC)
    document = paid_lanes.compose_document(
        ["codex", "claude"],
        sources={
            "codex": [
                paid_lanes.Candidate("stream", _reading(0.31, 0.12, moment=moment))
            ],
            "claude": [
                paid_lanes.Candidate("stream", _reading(0.62, 0.28, moment=moment))
            ],
        },
        moment=moment,
    )
    _install_default_document(tmp_path, document, monkeypatch)
    monkeypatch.setattr(
        flight, "resolve", lambda *args, **kwargs: SimpleNamespace(config=CONFIG)
    )

    invoked = CliRunner().invoke(cli.main, ["crew", "preflight", "--project", "demo"])
    assert invoked.exit_code == 0, invoked.output
    from_cli = json.loads(invoked.output)

    from_mcp = mcp._crew("demo", view="budget")

    assert from_cli["window_sources"] == from_mcp["window_sources"]
    assert from_cli["window_sources"] == {
        "codex": budget.WINDOW_SOURCE_DOCUMENT,
        "claude": budget.WINDOW_SOURCE_DOCUMENT,
    }
    for group in GROUP_OF.values():
        assert _clock_utilisation(from_cli, group) == pytest.approx(
            _clock_utilisation(from_mcp, group)
        ), group


# ── A document's age is the read-time age, not the baked one ────────────────


def test_a_document_read_later_reports_its_read_time_age(tmp_path: Path) -> None:
    """The age a report carries is derived at read time, not copied from the file.

    The document is composed with a twenty-second baked age and read fifteen
    minutes later, still inside the staleness horizon. The figure is taken from
    the document -- so the case is not passing by falling back -- and the age it
    reports is the fifteen minutes plus the baked twenty seconds, which is the
    age the reading actually has. A report echoing the baked figure would say
    twenty seconds.
    """
    composed_at = NOW - timedelta(minutes=15)
    document = paid_lanes.compose_document(
        ["codex"],
        sources={
            "codex": [
                paid_lanes.Candidate("stream", _reading(0.77, 0.44, moment=composed_at))
            ]
        },
        moment=composed_at,
    )
    recorded = {"codex": _reading(0.11, 0.09, moment=NOW)}

    report = budget.preflight(
        "demo",
        CONFIG,
        now=NOW,
        windows=recorded,
        document_path=_write_document(tmp_path, document),
    )

    assert report["window_sources"]["codex"] == budget.WINDOW_SOURCE_DOCUMENT
    assert _clock_utilisation(report, "codex") == pytest.approx(0.77)
    reported_age = _group(report["groups"], "codex")["clocks"]["five_hour"][
        "age_seconds"
    ]
    assert reported_age == pytest.approx(15 * 60 + 20)


def test_a_document_composed_long_ago_reads_stale_and_falls_back(
    tmp_path: Path,
) -> None:
    """A small baked age does not keep a stale document fresh.

    The document was composed past the staleness horizon with a baked
    twenty-second age, so a reader trusting that figure would pace codex from a
    reading almost an hour and a quarter old while nothing had observed it
    since. The merge judges each backend on the age recomputed at read time, so
    this backend is stale and the caller's recorded reading speaks for it.
    """
    composed_at = NOW - timedelta(seconds=3700)
    document = paid_lanes.compose_document(
        ["codex"],
        sources={
            "codex": [
                paid_lanes.Candidate("stream", _reading(0.77, 0.44, moment=composed_at))
            ]
        },
        moment=composed_at,
    )
    recorded = {"codex": _reading(0.11, 0.09, moment=NOW)}

    report = budget.preflight(
        "demo",
        CONFIG,
        now=NOW,
        windows=recorded,
        document_path=_write_document(tmp_path, document),
    )

    assert report["window_sources"]["codex"] == budget.WINDOW_SOURCE_RECORDED
    assert _clock_utilisation(report, "codex") == pytest.approx(0.11)


# ── The callers read the crew home, never the operator's real home ──────────


def test_a_document_under_the_operators_home_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A published document outside the crew home does not reach a pre-flight.

    A document is planted exactly where ``paid_lanes``' own default resolves it
    -- under the reader's ``HOME``, the operator's real-home location -- and the
    pre-flight resolves its path through the crew home instead. The positive
    control is the same document named explicitly: it does speak, which is what
    makes the isolation claim mean something rather than passing because the
    document was unreadable.
    """
    document = paid_lanes.compose_document(
        ["codex"],
        sources={
            "codex": [paid_lanes.Candidate("stream", _reading(0.77, 0.44, moment=NOW))]
        },
        moment=NOW,
    )
    stand_in_home = tmp_path / "operator-home"
    monkeypatch.setenv("HOME", str(stand_in_home))
    planted = stand_in_home / "public" / "reckon" / "paid-lanes.json"
    paid_lanes.write_document_atomically(document, planted)
    assert paid_lanes.document_path() == planted
    recorded = {"codex": _reading(0.11, 0.09, moment=NOW)}

    # The control: named explicitly, the planted document does speak.
    controlled = budget.preflight(
        "demo", CONFIG, now=NOW, windows=recorded, document_path=planted
    )
    assert controlled["window_sources"]["codex"] == budget.WINDOW_SOURCE_DOCUMENT
    assert _clock_utilisation(controlled, "codex") == pytest.approx(0.77)

    # The isolation: the path the callers resolve is not the planted one.
    resolved = budget.published_document_path()
    assert resolved != planted
    isolated = budget.preflight(
        "demo", CONFIG, now=NOW, windows=recorded, document_path=resolved
    )
    assert isolated["window_sources"]["codex"] == budget.WINDOW_SOURCE_RECORDED
    assert _clock_utilisation(isolated, "codex") == pytest.approx(0.11)
