"""The after window counts only the review attempts that started inside it.

The whole-history block pools every attempt the corpus holds. The after window
is the one figure that answers whether the review reflex's configuration since
it moved off xhigh kills less often, so the window's boundary carries that
claim: a filter that silently counted the earlier attempts too would pool two
populations and state the pooled share under the window's label.

This gate builds a synthetic runs root with review attempts on both sides of a
fixed cutoff — the earlier ones carrying deaths deliberately — and asserts the
window's own count and death count, its Wilson interval, the state below the
plan's 60-attempt floor, and the fixed control the block is aimed with. Remove
the window boundary from the scan and the count case here reads the earlier
attempts too and fails.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCANNER = REPO / "docs" / "research" / "scripts" / "scan_local_lane_deaths.py"

SINCE = "2026-10-01T00:00:00Z"
UNTIL = "2026-10-01T12:00:00Z"
# A second window that opens inside the first and holds fewer attempts than the
# plan's floor, so the not-measurable state is exercised on the same corpus.
NARROW_SINCE = "2026-10-01T10:00:00Z"

CONTROL_RUN = "r-synth-control-dead-review"

# (started, dead) for attempts that began before the window opens. Their two
# deaths exist to be counted by a scanner whose filter has been removed.
BEFORE_WINDOW = [
    ("2026-09-30T20:00:00Z", False),
    ("2026-09-30T20:01:00Z", False),
    ("2026-09-30T20:02:00Z", False),
    ("2026-09-30T20:03:00Z", False),
    ("2026-09-30T20:04:00Z", True),
    ("2026-09-30T20:05:00Z", True),
]


def _stamp(base: str, minutes: int) -> str:
    return (datetime.fromisoformat(base) + timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def in_window_attempts() -> list[tuple[str, bool]]:
    """63 completed + 3 dead inside [SINCE, UNTIL); 8 of them after NARROW_SINCE."""
    rows: list[tuple[str, bool]] = [
        (_stamp(SINCE, minute), False) for minute in range(55)
    ]
    rows.append((_stamp(SINCE, 55), True))
    rows.append((_stamp(SINCE, 56), True))
    rows.append((_stamp(SINCE, 630), True))
    rows.extend((_stamp(SINCE, minute), False) for minute in range(631, 638))
    return rows


def _write_attempt(run_dir: Path, started: str, *, dead: bool) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.txt").write_text(
        "NODE synthesised-window-attempt\nROLE review\n", encoding="utf-8"
    )
    # Compact separators matter: the scanner reads a stream's own timestamps by
    # their byte shape, and a spaced dump would fall back to the file mtime and
    # date every attempt by when the test happened to run.
    lines = [
        json.dumps({"timestamp": started, "type": "assistant"}, separators=(",", ":")),
        json.dumps(
            {"timestamp": _stamp(started, 1), "type": "user"}, separators=(",", ":")
        ),
    ]
    if not dead:
        lines.append(
            json.dumps(
                {"duration_api_ms": 1, "type": "result", "subtype": "success"},
                separators=(",", ":"),
            )
        )
    (run_dir / "stream.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_corpus(root: Path) -> tuple[Path, Path, Path]:
    """A runs root, an empty live dir and a ledger store for the synthesised runs."""
    runs = root / "runs"
    runs.mkdir(parents=True)
    live = root / "live"
    live.mkdir()
    store = root / "store.sqlite3"
    connection = sqlite3.connect(store)
    connection.execute(
        'CREATE TABLE "runs" ("run_id" TEXT PRIMARY KEY, "payload" TEXT)'
    )
    rows = BEFORE_WINDOW + in_window_attempts()
    for index, (started, dead) in enumerate(rows):
        run_id = f"r-synth-{index:03d}-review-window"
        _write_attempt(runs / run_id, started, dead=dead)
        payload = json.dumps(
            {"agent": {"backend": "clive", "effort": "high"}, "role": "review"}
        )
        connection.execute(
            'INSERT INTO "runs" ("run_id", "payload") VALUES (?, ?)',
            (run_id, payload),
        )
    connection.commit()
    connection.close()
    _write_attempt(runs / CONTROL_RUN, "2026-09-29T12:00:00Z", dead=True)
    (runs / CONTROL_RUN / "attempt-1-exit.json").write_text(
        json.dumps({"signal_name": "SIGTERM", "ended_during": "working"}),
        encoding="utf-8",
    )
    # A later attempt of the same run that completed, so the control covers both
    # directions on one run the way the committed control does.
    (runs / CONTROL_RUN / "resume-1.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {"timestamp": "2026-09-29T13:00:00Z", "type": "assistant"},
                    separators=(",", ":"),
                ),
                json.dumps(
                    {"duration_api_ms": 1, "type": "result", "subtype": "success"},
                    separators=(",", ":"),
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return runs, live, store


def load_scanner():
    spec = importlib.util.spec_from_file_location("scan_local_lane_deaths", SCANNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scan(module, root: Path, out: Path, *, since: str) -> dict:
    runs, live, store = build_corpus(root)
    code = module.main(
        [
            "--out",
            str(out),
            "--runs-root",
            str(runs),
            "--live-dir",
            str(live),
            "--store",
            str(store),
            "--control-run",
            CONTROL_RUN,
            "--since",
            since,
            "--until",
            UNTIL,
        ]
    )
    assert code == 0
    return json.loads(out.read_text(encoding="utf-8"))


def test_the_after_window_counts_only_attempts_started_inside_it(
    tmp_path: Path,
) -> None:
    stated = scan(load_scanner(), tmp_path, tmp_path / "after.json", since=SINCE)
    after = stated["after"]
    before = stated["before"]["deaths"]["review_role_all_efforts"]

    # The same scan holds 71 review attempts in total and five of their deaths:
    # the window's own figures below are a filter of this population, and the
    # count case fails if the boundary stops excluding the six earlier attempts.
    assert before["attempts"] == 71
    assert before["count"] == 5

    assert stated["after_state"] == "measured"
    assert after["reviews"] == 65
    assert after["deaths"]["headline"]["count"] == 3
    assert after["deaths"]["headline"]["attempts"] == 65
    assert after["rate"] == 0.0462
    assert after["rate_ci95"]["low"] == 0.0158
    assert after["rate_ci95"]["high"] == 0.1271
    assert after["measurable"] is True
    assert after["time_source"] == {"stream": 65}, (
        "an attempt dated by its file mtime would land in the window by the "
        "clock of the run rather than of its own records"
    )
    assert after["discrimination"]["this_effort_started_before_the_window"] == 6
    assert after["discrimination"]["other_efforts_in_the_window"] == 0


def test_a_window_below_the_floor_is_stated_as_not_yet_measurable(
    tmp_path: Path,
) -> None:
    stated = scan(
        load_scanner(), tmp_path, tmp_path / "narrow.json", since=NARROW_SINCE
    )
    after = stated["after"]

    assert after["reviews"] == 8
    assert after["deaths"]["headline"]["count"] == 1
    assert after["measurable"] is False
    assert after["rate"] is None
    assert after["rate_ci95"] is None
    assert after["deaths"]["headline"]["rate"] is None
    assert stated["after_state"] == "not_measurable"
    assert "8" in after["not_measurable_reason"]
    assert "60" in after["not_measurable_reason"]


def test_the_after_block_carries_the_control_that_aims_the_classifier(
    tmp_path: Path,
) -> None:
    stated = scan(load_scanner(), tmp_path, tmp_path / "control.json", since=SINCE)
    after = stated["after"]

    control = after["positive_control"]
    assert control is not None, "an after death count with no control is unaimed"
    assert control["run_id"] == CONTROL_RUN
    assert control["classification"] == "dead"
    assert control["has_result_record"] is False
    assert control["signal_exit_record"] is True
    assert after["completion_control"]["classification"] == "completed"
    assert after["controls_note"], "the out-of-window control must say what it is"
