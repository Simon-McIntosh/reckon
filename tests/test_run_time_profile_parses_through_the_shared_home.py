"""The completion-stamp reader routes through the shared timestamp parser.

The module reads a row's ``completed_at`` stamp to bound the profile window at
its upper end. That stamp arrives in the shapes the repository writes: a ``Z``
suffix, a numeric offset, a naive instant, and occasionally a value that will
not parse at all. The reader must resolve them the way every other caller in
the repository does -- through ``reckon._timestamps`` -- so a stamp read here
and the same stamp read in the ledger cannot disagree about the moment it
names.

The behaviour table below was recorded from the base revision before the
reader was routed through the shared parser, and every row is asserted to hold
at both that base and the head. The table is the contract: routing a caller
onto a shared parser is only correct when it preserves what the caller did.

The malformed row is the load-bearing one. An unreadable stamp is not evidence
the row falls outside the window, so the row is left in rather than dropped;
the ledger's own ``since`` filter has already refused a row with no usable
stamp. The negative control for this node makes the reader drop an unreadable
row instead, and this row must then fail.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from reckon.crew import run_time_profile as profile_module
from reckon.crew.run_time_profile import _completed_after

#: The window's upper bound every row in the table is read against.
REFERENCE = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        pytest.param("2026-10-02T13:00:00Z", True, id="z-suffix-after"),
        pytest.param("2026-10-02T11:00:00Z", False, id="z-suffix-before"),
        pytest.param("2026-10-02T15:00:00+02:00", True, id="offset-after"),
        pytest.param("2026-10-02T11:00:00+02:00", False, id="offset-before"),
        pytest.param("2026-10-02T13:00:00", True, id="naive-after"),
        pytest.param("2026-10-02T00:00:00", False, id="naive-before"),
        pytest.param("not-a-timestamp", False, id="malformed"),
        pytest.param("   ", False, id="whitespace"),
        pytest.param(None, False, id="non-string"),
    ],
)
def test_the_completion_stamp_behaviour_table_holds(stamp, expected):
    """Each recorded stamp resolves to the boolean the base revision produced.

    A naive stamp is read as UTC, an offset is authoritative, and an unreadable
    stamp resolves to ``False`` -- the value that leaves the row in the
    selection rather than dropping it.
    """

    assert _completed_after({"completed_at": stamp}, REFERENCE) is expected


def test_an_unreadable_stamp_leaves_the_row_in_the_profile(install_rows):
    """A row whose stamp will not parse is kept, not dropped.

    ``run_time_profile`` keeps a row unless ``_completed_after`` confirms it
    completed after the bound, so an unreadable stamp must resolve to ``False``
    for the row to survive. This is the end-to-end form of the malformed row
    above: the row appears in the profile's count rather than vanishing from it.
    """

    install_rows(
        [
            {
                "backend": "clive",
                "agent": {"backend": "clive", "effort": "high"},
                "role": "implement",
                "spec_level": "exact",
                "gate": "passed",
                "completed_at": "not-a-timestamp",
            }
        ]
    )

    report = profile_module.run_time_profile("reckon", days=14, now=REFERENCE)

    assert report["rows"] == 1
    assert report["groups"][0]["runs"] == 1


@pytest.fixture()
def install_rows(tmp_path, monkeypatch):
    """Serve synthetic rows as ``ledger.runs``, never the operator's ledger."""

    import json

    from reckon import ledger

    store = tmp_path / "synthetic_runs.json"

    def install(rows: list[dict]) -> None:
        store.write_text(json.dumps(rows), encoding="utf-8")

        def fake_runs(project: str, **kwargs: object) -> list[dict]:
            return json.loads(store.read_text(encoding="utf-8"))

        monkeypatch.setattr(ledger, "runs", fake_runs)

    return install
