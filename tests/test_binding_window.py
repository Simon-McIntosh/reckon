"""The binding quota window is inferred from the refusals a lane stamped.

A lane that publishes no quota reading is constrained by whichever window keeps
refusing it, and only the refusals it could not avoid emitting can say which.
Two refusals about one short window's period apart show the weekly window
binding, two far apart show the short window binding, and a single refusal
distinguishes neither and reports *undetermined* rather than naming a window.

Every case writes its stamps through the same durable writer production uses and
reads them back through the budget reader, so the assertions run against the
store a later surface would read rather than against literals handed to an
inference. The suite-wide home fixture already points the configuration home at
a temporary tree, and the first test pins that the reader and the writer resolve
to it, so no assertion here depends on the fleet's own store.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckon import budget, run_store

ANCHOR = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
WINDOW_NAMES = {"short", "weekly"}


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _stamp(lane: str, refused_at: datetime, hours_to_reset: int = 1) -> None:
    """Write one refusal through the production writer, and require it to land.

    A shadow write is best-effort and reports its failure rather than raising,
    so an unasserted stamp would let a broken write read back as a lane that
    never refused — which the single-refusal case reports as undetermined, the
    answer the test is least able to distinguish from a pass.
    """
    outcome = budget._stamp_refusal(
        lane=lane,
        refused_at=_iso(refused_at),
        returns_at=_iso(refused_at + timedelta(hours=hours_to_reset)),
    )
    assert outcome["status"] == "written", outcome


def _rows() -> list[dict]:
    with run_store.RunStore() as store:
        return store.refusal_stamps()


def test_the_writer_and_the_reader_resolve_the_same_temporary_store(
    isolated_reckon_home: Path,
) -> None:
    resolved = run_store.store_path()

    assert resolved.is_relative_to(isolated_reckon_home)
    _stamp("codex-spark", ANCHOR)
    assert [row["lane"] for row in _rows()] == ["codex-spark"]
    assert budget.binding_window("codex-spark")["refusal_count"] == 1


def test_two_refusals_one_short_window_apart_report_the_weekly_window() -> None:
    _stamp("codex-spark", ANCHOR)
    _stamp("codex-spark", ANCHOR + timedelta(hours=5))

    verdict = budget.binding_window("codex-spark")

    assert verdict["binding_window"] == "weekly"
    assert verdict["refusal_count"] == 2


def test_two_refusals_far_apart_report_the_short_window() -> None:
    _stamp("codex-spark", ANCHOR)
    _stamp("codex-spark", ANCHOR + timedelta(hours=30))

    verdict = budget.binding_window("codex-spark")

    assert verdict["binding_window"] == "short"
    assert verdict["refusal_count"] == 2


def test_a_single_refusal_reports_undetermined_and_names_no_window() -> None:
    _stamp("codex-spark", ANCHOR)

    verdict = budget.binding_window("codex-spark")

    assert verdict["binding_window"] == "undetermined"
    assert verdict["refusal_count"] == 1
    # The undetermined answer must not be readable as a verdict: not as a
    # window name under another spelling, and not as a boolean a caller keying
    # on truth could take for "the weekly window is binding".
    assert verdict["binding_window"] not in WINDOW_NAMES
    assert not any(isinstance(value, bool) for value in verdict.values())


def test_a_second_lane_s_refusals_never_place_this_lane_s_window() -> None:
    # codex-sol's two refusals sit one short window apart and would report the
    # weekly window on their own; codex-spark's single refusal is stamped inside
    # that same span, so a reader that pooled the table would pair it into a
    # crossing and answer weekly instead of undetermined.
    other_lane = "codex-sol"
    _stamp(other_lane, ANCHOR)
    _stamp(other_lane, ANCHOR + timedelta(hours=5))
    _stamp("codex-spark", ANCHOR + timedelta(hours=1))

    assert budget.binding_window(other_lane)["binding_window"] == "weekly"
    spark = budget.binding_window("codex-spark")
    assert spark["binding_window"] == "undetermined"
    assert spark["refusal_count"] == 1
