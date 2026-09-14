"""The binding quota window is inferred from stamped refusals, or left undetermined.

A lane that reports no quota reading is constrained by whichever window keeps
refusing it, and only its own refusals can say which. Two refusals about one
short window's period apart show the weekly window binding (the short window
reset in between and the lane refused anyway); two far apart show the short
window binding; a single refusal cannot say and reports undetermined rather
than guessing. Stamps are written and read back through a real
``RunStore.refusal_stamps`` so the assertions run against the durable records a
later surface would read, not against literals handed to the inference.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckon.crew.lane_evidence import infer_binding_window
from reckon.run_store import RunStore

ANCHOR = datetime(2026, 9, 1, tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _refusals(
    store: Path, entries: list[tuple[str, datetime, datetime | None]]
) -> list[dict]:
    with RunStore(store) as sqlite_store:
        for lane, refused_at, returns_at in entries:
            sqlite_store.stamp_refusal(
                lane,
                _iso(refused_at),
                _iso(
                    returns_at
                    if returns_at is not None
                    else refused_at + timedelta(hours=1)
                ),
            )
        return sqlite_store.refusal_stamps()


def test_two_refusals_five_hours_apart_report_the_weekly_window(tmp_path: Path) -> None:
    stamps = _refusals(
        tmp_path / "run_store.db",
        [
            ("codex-spark", ANCHOR, None),
            ("codex-spark", ANCHOR + timedelta(hours=5), None),
        ],
    )
    verdict = infer_binding_window(stamps, "codex-spark")

    assert verdict["binding_window"] == "weekly"
    assert verdict["refusal_count"] == 2
    assert verdict["pairs"][0]["reset_crossed"] is True


def test_two_refusals_far_apart_report_the_short_window(tmp_path: Path) -> None:
    stamps = _refusals(
        tmp_path / "run_store.db",
        [
            ("codex-spark", ANCHOR, None),
            ("codex-spark", ANCHOR + timedelta(hours=30), None),
        ],
    )
    verdict = infer_binding_window(stamps, "codex-spark")

    assert verdict["binding_window"] == "short"
    assert verdict["refusal_count"] == 2
    assert verdict["pairs"][0]["reset_crossed"] is False


def test_a_single_refusal_reports_undetermined_rather_than_guessing(
    tmp_path: Path,
) -> None:
    stamps = _refusals(tmp_path / "run_store.db", [("codex-spark", ANCHOR, None)])
    verdict = infer_binding_window(stamps, "codex-spark")

    assert verdict["binding_window"] == "undetermined"
    assert verdict["refusal_count"] == 1


def test_a_lane_does_not_borrow_another_lanes_refusals(tmp_path: Path) -> None:
    stamps = _refusals(
        tmp_path / "run_store.db",
        [
            ("codex-sol", ANCHOR, None),
            ("codex-sol", ANCHOR + timedelta(hours=5), None),
            ("codex-spark", ANCHOR + timedelta(hours=20), None),
        ],
    )

    assert (
        infer_binding_window(stamps, "codex-spark")["binding_window"] == "undetermined"
    )
    assert infer_binding_window(stamps, "codex-sol")["binding_window"] == "weekly"


def test_a_refusal_without_a_readable_time_cannot_place_the_window(
    tmp_path: Path,
) -> None:
    stamps = _refusals(
        tmp_path / "run_store.db",
        [
            ("codex-spark", ANCHOR, None),
            ("codex-spark", ANCHOR + timedelta(hours=5), None),
        ],
    )
    stamps.append({"lane": "codex-spark", "refused_at": "not-a-timestamp"})

    verdict = infer_binding_window(stamps, "codex-spark")

    assert verdict["binding_window"] == "weekly"
    assert verdict["refusal_count"] == 2
