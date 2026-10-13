"""A row shows its lane's mean generation rate in one fixed cell.

A rate is a reading about a lane rather than about the row's own process: a
reader holding only a node's own figure attributes a lane-wide cause to whichever
node it owns. So the row carries the mean the lane publishes about itself, taken
from the document the lane declares — the fact a watch event does not carry,
which is why the cell reads it from the lane rather than from the row's own
record. The figure stands right-aligned to a fixed width with its unit, so the
model and effort cells after it hold their columns.

Three states a reader must tell apart: a lane mean renders as a number with its
unit, a row whose backend declares no lane document at all leaves the cell
blank, and a declared document that yielded no figure prints the absence marker
rather than a zero. A zero would assert a measurement that was never taken.

The rows are built by the watch path's own transition builder — the event a
follower renders, with its spend figures folded from a stream the way the live
fleet folds them — and the lane figure comes from a temp host layer's declared
lane document. Nothing here is a hand-built row.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from reckon.crew import recovery
from reckon.crew import ticker as ticker_module

ESCAPES = re.compile(r"\x1b\[[0-9;]*m")

# The backend whose lane document the temp layer declares, a second one it
# declares with no lane document at all — the shape of a backend that has no
# lane to speak about — and the mean a published document carries.
BACKEND = "stub-lane"
BACKEND_WITHOUT_DOCUMENT = "stub-nolane"
LANE_MEAN = 51.2
RUN_ID = "r-mean-cell"
NODE = "n-mean-cell"

DETAIL = "the gate refused and no receipt was written"


def plain(line: str) -> str:
    """The row as its own layout reads it, with any colour removed."""
    return ESCAPES.sub("", line)


def _rate_column() -> int:
    """The column the rate cell starts on, from the module's own geometry.

    Every cell ahead of the rate is fixed-width, so the cell's column is the sum
    of those widths, read from the constants the renderer sizes them by rather
    than from a column written down: a cell added or widened without the rate
    moving with it fails here rather than agreeing with the arithmetic that
    placed it.
    """
    return (
        ticker_module.CLOCK
        + ticker_module.GAP
        + ticker_module.ROLE
        + ticker_module.GAP
        + ticker_module.NODE
        + ticker_module.GAP
        + ticker_module.STATE
        + ticker_module.GAP
        + ticker_module.SPEND_GAP
        + ticker_module.WALL
        + ticker_module.GAP
        + ticker_module.STATS
        + ticker_module.GAP
    )


def _rate_cell(line: str) -> str:
    """The rate cell as the row carries it, at its own fixed column."""
    start = _rate_column()
    return line[start : start + ticker_module.RATE]


def _stamp(age_seconds: int) -> str:
    """A document's own stamp, aged from now rather than written down."""
    moment = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return moment.isoformat().replace("+00:00", "Z")


def lane_document(mean: float | None = LANE_MEAN, *, age_seconds: int = 12) -> str:
    """One published lane document, as the lane's own writer composes it.

    The stamp is derived from the moment of writing rather than written down, so
    freshness is a measurement and not a date this file would have to be edited
    to keep true. ``mean=None`` publishes a lane that measured no rate: the
    document is there and its throughput block is not.
    """
    payload = {
        "observed_at": _stamp(age_seconds),
        "suggested_shelf_life_seconds": 90,
        "mean_context": 136000,
    }
    if mean is not None:
        payload["throughput"] = {
            "mean_tokens_per_second": mean,
            "aggregate_tokens_per_second": mean * 3,
            "runs": 3,
            "observed_at": _stamp(age_seconds),
        }
    return json.dumps(payload)


@pytest.fixture
def lane(tmp_path, monkeypatch):
    """A temp host layer declaring one backend with a document and one without.

    The layer is pointed at through the env var the flight resolution honours, so
    the cell resolves each backend's declaration the way a dispatch through this
    configuration would and nothing reads the machine's real config or mount
    registry. The document itself is not written here: a test writes it, or does
    not, which is the difference between a lane that published and one with
    nothing to say.
    """
    document = tmp_path / "lane.json"
    layer = tmp_path / "flight.yaml"
    layer.write_text(
        f"backends:\n"
        f"  {BACKEND}:\n"
        f"    lane_document: {document}\n"
        f"  {BACKEND_WITHOUT_DOCUMENT}:\n"
        f"    launch: in-harness\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(layer))
    return document


@pytest.fixture
def row(tmp_path):
    """Build one event through the watch path's own transition builder.

    The spend figures are folded from a stream beside a throughput block, which
    is how the live path derives the row's elapsed figure, so the row carries
    what a real one carries. Only the backend varies between cases: everything
    else is the watch path's own output.
    """

    def build(*, backend: str = BACKEND, to_state: str = "working"):
        run_dir = tmp_path / "runs" / RUN_ID
        run_dir.mkdir(parents=True, exist_ok=True)
        stream = run_dir / "stream.jsonl"
        stream.write_text(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 3000,
                        "cached_input_tokens": 1000,
                        "output_tokens": 600,
                        "reasoning_output_tokens": 0,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        rows = [
            {
                "run_id": RUN_ID,
                "log_path": str(stream),
                "throughput": {"elapsed_seconds": 3421.0, "generation_seconds": 300.0},
            }
        ]
        snapshot = {
            "run_id": RUN_ID,
            "node": NODE,
            "session": "s-mean-cell",
            "role": "implement",
            "lineage": None,
            "backend": backend,
            "model": "dsv4.1-flash",
            "effort": "xhigh",
            "alias": "",
            "classification": "working",
            "process_alive": True,
            "liveness_proven": True,
            "detail": DETAIL,
            "recovery_classification": "working",
            "recovery": "",
        }
        # No project layer: the row resolves the shipped and host layers only,
        # so the pane under test never reaches the machine's mount registry. The
        # row moves from ``dispatched``: a stream event restating the state the
        # pane already shows is suppressed as a re-derivation, so a transition is
        # what a reader actually sees drawn.
        return recovery._watch_transition(
            "",
            kind="transition",
            snapshot=snapshot,
            previous="dispatched",
            current=to_state,
            counts={"working": 3, "blocked": 1, "unpromoted": 7},
            spend_runs=rows,
            rate_statuses={},
            streams_root=tmp_path / "runs",
        )

    return build


@pytest.fixture
def grid():
    """A grid whose model cell is pinned, so the row's columns are its own."""
    return ticker_module.Ticker(width=180, theme="light", color=False, model_aliases=())


def test_the_lane_mean_stands_at_one_fixed_column(lane, row, grid):
    """The published mean reads as a figure with its unit, at its own column.

    The cell is a reading rather than a decoration, so the figure stands where a
    reader scanning the pane finds every row's rate on one column, right-aligned
    so the unit holds still as the figure changes width.
    """
    lane.write_text(lane_document(LANE_MEAN), encoding="utf-8")
    line = plain(grid.render(row(to_state="blocked")))
    cell = _rate_cell(line)
    # The reading and its unit are written out rather than built from the
    # renderer's own constants: a comparison against the same constant the cell
    # prints would agree with the arithmetic that placed it, so a renamed unit
    # or a displaced figure would pass here.
    assert cell.strip() == "51.2 tok/s", line
    assert len(cell) == ticker_module.RATE, line
    # The cell sits after the fleet counters, past the two-column gutter.
    assert _rate_column() > line.index("3w"), line


@pytest.mark.parametrize(
    ("mean", "rendered"),
    [
        (0.4, "0.40"),
        (1.44, "1.44"),
        (20.0, "20.0"),
        (51.2, "51.2"),
    ],
)
def test_the_lane_figure_follows_the_document(lane, row, grid, mean, rendered):
    """The cell renders the document rather than a constant.

    A figure that did not follow its input would make the row a decoration rather
    than a reading, so the same row is rendered against four published means and
    the cell must carry each of them at the same column. Two decimals below ten
    keep figures a reader compares at the resolution that separates them; above
    it one decimal is all the column needs.
    """
    lane.write_text(lane_document(mean), encoding="utf-8")
    line = plain(grid.render(row()))
    assert _rate_cell(line) == (
        f"{rendered:>{ticker_module.LANE_RATE}} {ticker_module.RATE_UNIT}"
    ), (mean, line)


def test_a_backend_without_a_lane_document_leaves_the_cell_blank(lane, row, grid):
    """A row whose backend declares no document has no lane to read.

    The document is declared per backend, so a backend that declares none has no
    lane to read. The cell stays blank rather than naming the absence of a lane
    the row never used, and the model and effort after it hold their columns.
    """
    # The lane document exists and carries a mean, so a cell that rendered one
    # would be reading a document this row's backend never declared.
    lane.write_text(lane_document(LANE_MEAN), encoding="utf-8")
    line = plain(grid.render(row(backend=BACKEND_WITHOUT_DOCUMENT)))
    assert _rate_cell(line).strip() == "", line
    assert ticker_module.RATE_UNIT not in line, line
    assert ticker_module.DIM_MARKER not in line, line


def test_a_declared_document_with_no_figure_shows_the_absence_marker(lane, row, grid):
    """A declared document that yielded no figure marks the cell, never zero.

    A lane that published no rate has not measured nothing, so the cell carries
    the absence marker. This is the case a live pane meets whenever the lane has
    not published yet, and it must not read as the lane being down.
    """
    lane.write_text(lane_document(mean=None), encoding="utf-8")
    line = plain(grid.render(row()))
    expected = (
        f"{ticker_module.DIM_MARKER:>{ticker_module.LANE_RATE}} "
        f"{ticker_module.RATE_UNIT}"
    )
    assert _rate_cell(line) == expected, line
    assert "0.00" not in line, line


def test_a_stale_document_shows_the_absence_marker(lane, row, grid):
    """A document past its own shelf life contributes no figure.

    The lane publishes its own shelf life and a figure read past it describes a
    lane that may have changed since, so the cell marks the absence rather than
    printing a number a reader would take for the present state.
    """
    lane.write_text(lane_document(LANE_MEAN, age_seconds=600), encoding="utf-8")
    line = plain(grid.render(row()))
    assert ticker_module.DIM_MARKER in _rate_cell(line), line
    assert f"{LANE_MEAN:.1f}" not in line, line


def test_a_document_that_is_not_json_shows_the_absence_marker(lane, row, grid):
    """An unparsable document is a marked absence, not a raise.

    A pane that stopped rendering because the lane's half-written file was caught
    mid-write would lose the row entirely, so the cell degrades to the marker and
    the row renders.
    """
    lane.write_text('{"throughput": ', encoding="utf-8")
    line = plain(grid.render(row()))
    assert ticker_module.DIM_MARKER in _rate_cell(line), line
    assert ticker_module.RATE_UNIT in line, line


def test_the_counters_keep_their_column_whether_the_lane_kept_quiet(lane, row):
    """The rate cell is added after the counters, so the fleet figures hold.

    The counters are the pane's fixed frame; a cell that displaced them would
    make a reader re-find the reader's own fleet numbers on every row that
    carried a reading. One row has a document and one does not, and the count
    must land on the same column in both. Each row is drawn by its own pane,
    because a pane holds the lane's figure for a reuse window rather than reading
    the lane again per row.
    """
    lane.write_text(lane_document(LANE_MEAN), encoding="utf-8")
    pane = ticker_module.Ticker(width=180, theme="light", color=False, model_aliases=())
    published = plain(pane.render(row()))
    lane.unlink()
    quiet = plain(
        ticker_module.Ticker(
            width=180, theme="light", color=False, model_aliases=()
        ).render(row())
    )
    assert published.index("3w") == quiet.index("3w"), (published, quiet)


def test_the_row_stays_within_the_pane_width(lane, row, grid):
    """The rate cell spends the pane's margin, never its edge."""
    lane.write_text(lane_document(LANE_MEAN), encoding="utf-8")
    for width in (180, ticker_module.MIN_WIDTH):
        pane = ticker_module.Ticker(
            width=width, theme="light", color=False, model_aliases=()
        )
        line = plain(pane.render(row()))
        assert len(line) <= pane.width, (width, len(line), line)
