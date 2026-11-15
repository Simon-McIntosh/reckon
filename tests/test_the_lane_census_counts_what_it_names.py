"""The lane census reports the figures it names, over a store built for it.

Every figure here is computed by the census script from a fixture the case
builds, so a moved span boundary, a widened cohort filter, a hold wording the
path parser stops recognising or an identity sweep that counts too much reddens
a test rather than leaving a plausible number behind.

Each case writes only inside its own temporary directory. The crew home, the
admission log and the claims directory the script reads are replaced by the
fixture, so a case that counts identity files reports on its own claims
directory rather than on the workstation's.

The retirement case is the one that matters beyond arithmetic: a run the store
has moved out of its hot table still has to contribute to an older window, and
that is the whole reason the ledger read goes through the store's own readers.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.run_store import RunStore

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "lane_window_census.py"


def _load_census():
    """Load the census script by path; ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location("lane_window_census", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


census = _load_census()

START = datetime(2026, 11, 14, 21, 0, tzinfo=UTC)
END = datetime(2026, 11, 14, 22, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def _run(
    run_id: str,
    role: str,
    completed: datetime | str,
    *,
    launched: datetime | None = None,
    exited: datetime | None = None,
) -> dict:
    return {
        "run_id": run_id,
        "project": "sample",
        "node": run_id,
        "role": role,
        "dispatched_at": None,
        "completed_at": completed.isoformat()
        if isinstance(completed, datetime)
        else completed,
        "launched_at": launched.isoformat() if launched else None,
        "exited_at": exited.isoformat() if exited else None,
    }


def _stamp(path: Path, when: datetime) -> None:
    """Stamp a file at one instant, so a case does not depend on the clock."""
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))


def test_concurrency_reports_a_time_weighted_mean_and_a_peak():
    """Two overlapping spans inside a one-hour window average one worker."""
    spans = [
        (START - 10 * MINUTE, START + 20 * MINUTE, {}),
        (START + 10 * MINUTE, START + 50 * MINUTE, {}),
        (START + 2 * 60 * MINUTE, START + 3 * 60 * MINUTE, {}),
    ]

    figure = census.concurrency(spans, START, END)

    # 1200 s from the first span and 2400 s from the second over 3600 s.
    assert figure == {"mean": 1.0, "peak": 2, "workers": 2}


def test_concurrency_over_a_window_no_span_touches_reads_zero():
    spans = [(START - 60 * MINUTE, START - 10 * MINUTE, {})]

    figure = census.concurrency(spans, START, END)

    assert figure == {"mean": 0.0, "peak": 0, "workers": 0}


def test_promotion_latency_counts_implement_runs_completed_in_the_window():
    """A review run and a run completed earlier stay out of the cohort."""
    runs = [
        _run("r-fast", "implement", START + 10 * MINUTE),
        _run("r-unlanded", "implement", START + 40 * MINUTE),
        _run("r-review", "review", START + 15 * MINUTE),
        _run("r-earlier", "implement", START - 60 * MINUTE),
    ]
    stamps = {
        "r-fast": START + 20 * MINUTE,
        "r-review": START + 16 * MINUTE,
        "r-earlier": START,
    }

    figure = census.promotion_latency(runs, stamps, START, END)

    assert figure == {
        "cohort": 2,
        "n": 1,
        "no_landing_record": 1,
        "median_seconds": 600.0,
        "p90_seconds": 600.0,
        "clamped_negative": 0,
    }


def test_promotion_latency_clamps_a_landing_stamped_before_completion():
    """A landing recorded before its run completed counts as no wait, not a negative one."""
    runs = [_run("r-odd", "implement", START + 10 * MINUTE)]
    stamps = {"r-odd": START}

    figure = census.promotion_latency(runs, stamps, START, END)

    assert figure["clamped_negative"] == 1
    assert figure["median_seconds"] == 0.0


def test_a_claimed_path_holding_an_apostrophe_still_reads_as_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """repr() double-quotes a path holding an apostrophe; the hold still counts."""
    crew_home = tmp_path / "crew"
    log = tmp_path / "admission-verdicts.jsonl"
    monkeypatch.setattr(census, "CREW_HOME", crew_home)
    monkeypatch.setattr(census, "ADMISSION_LOG", log)

    shared = crew_home / "claims" / "plans" / "alpha"
    apostrophe = crew_home / "claims" / "docs" / "o'brien"
    foreign = tmp_path / "elsewhere" / "work-tree"
    rows = [
        {
            "at": "2026-11-14T21:05:00+00:00",
            "pointers": [
                {
                    "verdict": "held-on-claim",
                    "detail": f"run 'r-1' holds {str(shared)!r}: no worker process",
                }
            ],
        },
        {
            "at": "2026-11-14T21:10:00+00:00",
            "pointers": [
                {
                    "verdict": "held-on-claim",
                    "detail": (
                        "write path 'w' claims a directory overlapping the live "
                        f"claim {str(apostrophe)!r} held by run 'r-2' (node 'n')"
                    ),
                }
            ],
        },
        {
            "at": "2026-11-14T21:15:00+00:00",
            "pointers": [
                {
                    "verdict": "held-on-claim",
                    "detail": f"run 'r-3' holds {str(foreign)!r}: no worker process",
                }
            ],
        },
        {
            "at": "2026-11-14T21:20:00+00:00",
            "pointers": [{"verdict": "launched"}],
        },
        {
            "at": "2026-11-14T22:30:00+00:00",
            "pointers": [
                {
                    "verdict": "held-on-claim",
                    "detail": f"run 'r-4' holds {str(shared)!r}: no worker process",
                }
            ],
        },
    ]
    log.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    figure = census.admission_holds(START, END, census.admission_span())

    # The path holding an apostrophe is the second shared hold; a parser that
    # split on the quote character would silently report one.
    assert figure == {
        "passes": 4,
        "passes_holding_shared_claim": 2,
        "shared_claim_pointers": 2,
    }
    assert census.claimed_path(rows[1]["pointers"][0]["detail"]) == str(apostrophe)


def test_a_window_the_admission_log_does_not_cover_reads_as_unmeasured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An uncovered window reports nulls and says why, never a zero."""
    log = tmp_path / "admission-verdicts.jsonl"
    monkeypatch.setattr(census, "ADMISSION_LOG", log)
    log.write_text(
        json.dumps({"at": "2026-11-14T21:05:00+00:00", "pointers": []}) + "\n"
    )

    earlier = (START - 3 * 24 * 60 * MINUTE, START - 2 * 24 * 60 * MINUTE)
    figure = census.admission_holds(*earlier, census.admission_span())

    assert figure["passes"] is None
    assert figure["passes_holding_shared_claim"] is None
    assert "outside" in figure["note"]


def test_identity_files_counts_the_directory_as_it_stood_at_the_window_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A file written after the window's end is not counted, and one written before it is."""
    claims = tmp_path / "claims"
    claims.mkdir()
    monkeypatch.setattr(census, "CLAIMS_DIR", claims)

    ended = END
    present = claims / "exec-100.identity"
    later = claims / "exec-200.identity"
    other = claims / "claim-abc.json"
    present.write_text("{}")
    later.write_text("{}")
    other.write_text("{}")
    _stamp(present, ended - 30 * MINUTE)
    _stamp(other, ended - 20 * MINUTE)
    _stamp(later, ended + 30 * MINUTE)

    figure = census.identity_files(ended)

    assert figure["identity_files"] == 1
    assert figure["claims_entries"] == 2
    assert figure["as_of"] == ended.isoformat()


def test_load_runs_reads_a_retired_run_from_the_archive(tmp_path: Path):
    """A run retirement moved out of the hot table still reaches the census."""
    db = tmp_path / "run_store.db"
    hot = {
        "run_id": "r-hot",
        "project": "sample",
        "node": "n-hot",
        "role": "implement",
        "completed_at": "2026-11-15T00:10:00+00:00",
        "worker_exit": {
            "launched_at": "2026-11-15T00:00:00+00:00",
            "exited_at": "2026-11-15T00:20:00+00:00",
        },
    }
    old = {
        "run_id": "r-old",
        "project": "sample",
        "node": "n-old",
        "role": "implement",
        "completed_at": "2026-11-01T00:10:00+00:00",
        "worker_exit": {
            "launched_at": "2026-11-01T00:00:00+00:00",
            "exited_at": "2026-11-01T00:20:00+00:00",
        },
    }
    with RunStore(db) as store:
        store.append("sample", hot)
        store.append("sample", old)
        swept = store.retire(
            before=datetime(2026, 11, 10, tzinfo=UTC),
            protected=set(),
            projects={"sample"},
            apply=True,
        )
    assert swept["retired"] == ["r-old"]

    runs, counts = census.load_runs(db, projects={"sample"})
    by_id = {run["run_id"]: run for run in runs}

    assert set(by_id) == {"r-hot", "r-old"}
    assert counts["run_store"] == str(db)
    assert counts["runs_in_ledger"] == 1
    assert counts["runs_in_archive"] == 1
    assert by_id["r-old"]["launched_at"] == "2026-11-01T00:00:00+00:00"
    assert by_id["r-old"]["role"] == "implement"
    assert by_id["r-hot"]["exited_at"] == "2026-11-15T00:20:00+00:00"
