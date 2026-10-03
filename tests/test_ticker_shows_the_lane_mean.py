"""A row shows its own generation rate with the lane's mean beside it.

A rate is a reading about one worker with no denominator on the row, and a
reader holding only that number attributes a lane-wide cause to whichever node
it owns: measured, two coordinators read the same figure as a slow worker and as
a slow lane. So the row carries the lane's mean in the same cell as the run's
own rate, taken from the document the lane publishes about itself — the fact a
watch event does not carry, which is why the cell reads it from the lane rather
than from the row's own record. The run's own rate is rendered either way: it is
what the run did, and withholding it because the lane could not be read would
cost the reader the row's measurement as well. When no mean can be read the cell
names that state beside the rate, because an unmeasured lane and a lane whose
figure was withheld must not read alike.

The rows are built by the watch path's own transition builder — the event a
follower renders, with its spend figures folded from a stream the way the live
fleet folds them — and the lane figure comes from a temp host layer's declared
lane document. Nothing here is a hand-built row, and no figure is asserted from
a field the watch event does not carry.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from reckon.crew import recovery
from reckon.crew import ticker as ticker_module

ESCAPES = re.compile(r"\x1b\[[0-9;]*m")

# The backend the temp host layer declares, and the stream a run's spend figures
# fold from: 600 output tokens over a 300 s generation span is the run's own
# rate of 2.00 tokens a second, distinct from the lane's mean so a cell that
# repeated one figure twice could not satisfy these assertions.
BACKEND = "stub-lane"
ROW_RATE = 2.0
LANE_MEAN = 1.44
RUN_ID = "r-mean-beside"
NODE = "n-mean-beside"

DETAIL = "the gate refused and no receipt was written"
# The clause's opening, which is all a row carrying the lane cell has room for
# inside the 180-column pane.
CLAUSE_HEAD = DETAIL.split(" and ", maxsplit=1)[0]


def plain(line: str) -> str:
    """The row as its own layout reads it, with any colour removed."""
    return ESCAPES.sub("", line)


def _stamp(age_seconds: int) -> str:
    """A document's own stamp, aged from now rather than written down."""
    moment = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return moment.isoformat().replace("+00:00", "Z")


def lane_document(mean: float | None = LANE_MEAN, *, age_seconds: int = 12) -> str:
    """One published lane document, as the lane's own writer composes it.

    The stamp is derived from the moment of writing rather than written down,
    so freshness is a measurement and not a date this file would have to be
    edited to keep true. ``mean=None`` publishes a lane that measured no rate:
    the document is there and its throughput block is not.
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
    """A temp host layer declaring the row's backend and its lane document.

    The layer is pointed at through the env var the flight resolution honours,
    so the cell resolves the document the way a dispatch through this
    configuration would and nothing reads the machine's real config or mount
    registry. The document itself is not written here: a test writes it, or
    does not, which is the difference between a lane that published and one
    that has nothing to say.
    """
    document = tmp_path / "lane.json"
    layer = tmp_path / "flight.yaml"
    layer.write_text(
        f"backends:\n  {BACKEND}:\n    lane_document: {document}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(layer))
    return document


@pytest.fixture
def row(tmp_path):
    """Build one event through the watch path's own transition builder.

    The spend figures are folded from a stream beside a throughput block, which
    is how the live path derives a run's own rate, so the figure the cell
    renders is the one a real row carries. Only the backend varies between
    cases: everything else is the watch path's own output.
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
            "session": "s-mean-beside",
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
        # so the pane under test never reaches the machine's mount registry.
        # The row moves from ``dispatched``: a stream event restating the state
        # the pane already shows is suppressed as a re-derivation, so a
        # transition is what a reader actually sees drawn.
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


def test_the_lane_mean_stands_beside_the_rows_own_rate(lane, row, grid):
    """The two figures stand in one cell, the run's own first.

    The pair is the whole change: the row already carried the run's own figure,
    and the lane's mean arriving beside it is what lets a reader tell a slow
    worker from a slow lane.
    """
    lane.write_text(lane_document(), encoding="utf-8")
    line = plain(grid.render(row(to_state="blocked")))
    pair = f"{ROW_RATE:.2f} lane {LANE_MEAN:.2f}"
    assert pair in line, line
    # Beside each other on the row, after the fleet counters and before the
    # clause, which is where the reading says the two figures stand.
    assert line.index(pair) > line.index("3w"), line
    assert line.index(pair) < line.index(CLAUSE_HEAD), line


def test_the_run_rate_survives_a_lane_that_published_nothing(lane, row, grid):
    """Without a document the row keeps the run's own figure and names the rest.

    This is the case a live pane meets whenever the lane has not published yet,
    and it is why the rate is rendered unconditionally: a row that dropped its
    own figure here would leave the reader with neither the rate nor the mean.
    The phrase names the cause — the lane has published no figure — rather than
    an absence a reader could take for an outage.
    """
    line = plain(grid.render(row()))
    assert f"{ROW_RATE:.2f}" in line, line
    assert ticker_module.LANE_LABEL in line, line
    assert ticker_module.LANE_MEAN_UNPUBLISHED in line, line
    assert f"{LANE_MEAN:.2f}" not in line, line


@pytest.mark.parametrize(
    ("mean", "rendered"),
    [
        (0.4, "0.40"),
        (1.44, "1.44"),
        (20.0, "20.0"),
    ],
)
def test_the_lane_figure_follows_the_document(lane, row, grid, mean, rendered):
    """The cell renders the document rather than a constant.

    A figure that did not follow its input would make the row a decoration
    rather than a reading, so the same row is rendered against three published
    means and the cell must carry each of them beside the run's own rate. Two
    decimals below ten keep figures a reader compares at the resolution that
    separates them; above it one decimal is all the column needs.
    """
    lane.write_text(lane_document(mean), encoding="utf-8")
    line = plain(grid.render(row()))
    assert f"lane {rendered}" in line, (mean, line)
    # The run's own figure is unchanged by the lane's, so the pair is a
    # comparison rather than one number printed twice.
    assert f"{ROW_RATE:.2f}" in line, (mean, line)


def test_a_stale_document_is_named_not_numbered(lane, row, grid):
    """A document past its own shelf life contributes no figure.

    The lane publishes its own shelf life and a figure read past it describes a
    lane that may have changed since; the row says so in words rather than
    printing a number a reader would take for the present state.
    """
    lane.write_text(lane_document(LANE_MEAN, age_seconds=600), encoding="utf-8")
    line = plain(grid.render(row()))
    assert ticker_module.LANE_DOC_STALE in line, line
    assert f"{LANE_MEAN:.2f}" not in line, line
    assert f"{ROW_RATE:.2f}" in line, line


def test_a_document_without_a_mean_is_named(lane, row, grid):
    """A lane that published no rate is named rather than read as zero."""
    lane.write_text(lane_document(mean=None), encoding="utf-8")
    line = plain(grid.render(row()))
    assert ticker_module.LANE_MEAN_UNPUBLISHED in line, line
    assert "0.00" not in line, line
    assert f"{ROW_RATE:.2f}" in line, line


def test_a_document_that_is_not_json_is_named(lane, row, grid):
    """An unparsable document is a named absence, not a raise.

    A pane that stopped rendering because the lane's half-written file was
    caught mid-write would lose the row entirely, so the cell degrades to the
    phrase and the row renders.
    """
    lane.write_text('{"throughput": ', encoding="utf-8")
    line = plain(grid.render(row()))
    assert ticker_module.LANE_MEAN_UNPUBLISHED in line, line
    assert f"{ROW_RATE:.2f}" in line, line


def test_a_row_naming_no_backend_adds_no_cell(lane, row, grid):
    """A row with no backend has no lane to read and keeps its base shape.

    The lane document is declared per backend, so a row that names none — a
    synthetic probe, or a legacy line — cannot be given a lane figure, and it
    renders exactly as it did before this cell existed rather than carrying a
    phrase about a lane it never used.
    """
    lane.write_text(lane_document(), encoding="utf-8")
    line = plain(grid.render(row(backend="")))
    assert ticker_module.LANE_LABEL not in line, line
    assert f"{ROW_RATE:.2f}" not in line, line


def test_the_counters_keep_their_column_whether_the_lane_kept_quiet(lane, row):
    """The lane cell is added after the counters, so the fleet figures hold.

    The counters are the pane's fixed frame; a cell that displaced them would
    make a reader re-find the reader's own fleet numbers on every row that
    carried a reading. One row has a document and one does not, and the count
    must land on the same column in both. Each row is drawn by its own pane,
    because a pane holds the lane's figure for a reuse window rather than
    reading the lane again per row.
    """
    lane.write_text(lane_document(), encoding="utf-8")
    pane = ticker_module.Ticker(width=180, theme="light", color=False, model_aliases=())
    published = plain(pane.render(row()))
    lane.unlink()
    quiet = plain(
        ticker_module.Ticker(
            width=180, theme="light", color=False, model_aliases=()
        ).render(row())
    )
    assert published.index("3w") == quiet.index("3w"), (published, quiet)


def test_the_row_stays_exactly_the_pane_width(lane, row, grid):
    """The lane cell spends the clause's margin, never the pane's edge."""
    lane.write_text(lane_document(), encoding="utf-8")
    for width in (180, ticker_module.MIN_WIDTH):
        pane = ticker_module.Ticker(
            width=width, theme="light", color=False, model_aliases=()
        )
        line = plain(pane.render(row()))
        assert len(line) == pane.width, (width, len(line), line)


def test_the_clause_survives_beside_the_lane_cell(lane, row, grid):
    """The reason is still the row's payload, cut to the room the cell leaves.

    A lane cell that overflowed or wrapped would cost the row the clause a
    reader came for, and a wrapped row costs a quarter of the pane's history.
    The cell does spend margin, so what is asserted is that the reason still
    starts after the figures and that the row is still one line of the pane's
    width.
    """
    lane.write_text(lane_document(), encoding="utf-8")
    line = plain(grid.render(row(to_state="blocked")))
    assert CLAUSE_HEAD in line, line
    assert line.index(CLAUSE_HEAD) > line.index(ticker_module.LANE_LABEL), line
    assert len(line) == grid.width, line
