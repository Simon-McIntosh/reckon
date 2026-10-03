"""The lane cell names why it has no mean, and never reads as an outage.

A follower row prints the run's own generation rate beside the lane's mean, and
when no mean can be read it says why. The one phrase that said only what was
missing — the same words whichever way the reading failed — read to an operator
as the lane being down, and was reported as one while the lane was serving
normally. So each state names its own cause beside the rate: a backend that
declares no lane document carries the run's own rate with no lane clause at all,
a document that has published no figure says ``mean unpublished``, and one past
its own shelf life says ``lane doc stale``. A document carrying a mean prints
the mean, unchanged.

The rows are built by the watch path's own transition builder — the event a
follower renders, with its spend figures folded from a stream the way the live
fleet folds them — and the lane figures come from a temp host layer's declared
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

# The backend whose lane document the temp host layer declares, and a second one
# it declares without a lane document at all — the codex-shaped row, which has
# no lane to read.
BACKEND = "stub-lane"
BACKEND_WITHOUT_DOCUMENT = "stub-nolane"
# The stream a run's spend figures fold from: 600 output tokens over a 300 s
# generation span is the run's own rate of 2.00 tokens a second, distinct from
# the lane's mean so a cell that repeated one figure twice could not satisfy
# these assertions.
ROW_RATE = 2.0
LANE_MEAN = 1.44
RUN_ID = "r-cause-beside"
NODE = "n-cause-beside"

DETAIL = "the gate refused and no receipt was written"


def plain(line: str) -> str:
    """The row as its own layout reads it, with any colour removed."""
    return ESCAPES.sub("", line)


def assert_no_outage_wording(line: str) -> None:
    """No rendered row may carry a word that reads as an outage.

    The phrase this cell used to print was read as the lane being down while it
    was serving, so the assertion is the whole point of the repair rather than a
    spelling check: a row may name an unpublished figure or a stale document,
    and never an unavailability.
    """
    assert "unavailable" not in line, line


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
    """A temp host layer declaring one backend with a lane document and one without.

    The layer is pointed at through the env var the flight resolution honours,
    so the cell resolves each backend's declaration the way a dispatch through
    this configuration would and nothing reads the machine's real config or
    mount registry. The document itself is not written here: a test writes it,
    or does not, which is the difference between a lane that published and one
    that has nothing to say.
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
            "session": "s-cause-beside",
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


def test_a_backend_without_a_lane_document_prints_no_lane_clause(lane, row, grid):
    """A row whose backend declares no document has no lane to speak about.

    The document is declared per backend, so a backend that declares none — a
    codex-shaped row — has no lane to read. It carries its own rate and no lane
    clause, rather than a phrase about a lane the row never used; there is no
    lane state to name, so nothing is named.
    """
    # The lane document exists and carries a mean, so a cell that printed one
    # would be reading a document this row's backend never declared.
    lane.write_text(lane_document(), encoding="utf-8")
    line = plain(grid.render(row(backend=BACKEND_WITHOUT_DOCUMENT)))
    assert ticker_module.LANE_LABEL not in line, line
    assert f"{LANE_MEAN:.2f}" not in line, line
    assert ticker_module.LANE_MEAN_UNPUBLISHED not in line, line
    assert ticker_module.LANE_DOC_STALE not in line, line
    assert_no_outage_wording(line)


def test_a_document_without_a_throughput_block_says_unpublished(lane, row, grid):
    """The lane published a document whose figure has not arrived.

    This is the live lane's shape at the moment of the report: the document is
    there, measured and fresh, and its throughput block is not. The cell says
    the figure is unpublished — a state a reader can wait out — rather than a
    word they read as the lane being down.
    """
    lane.write_text(lane_document(mean=None), encoding="utf-8")
    line = plain(grid.render(row()))
    assert ticker_module.LANE_MEAN_UNPUBLISHED in line, line
    assert ticker_module.LANE_DOC_STALE not in line, line
    assert f"{ROW_RATE:.2f}" in line, line
    assert_no_outage_wording(line)


def test_a_stale_document_says_so(lane, row, grid):
    """A document past its own shelf life is named, not numbered.

    The lane publishes its own shelf life, and a figure read past it describes a
    lane that may have changed since; the row says the document is stale rather
    than printing the old figure or an outage phrase.
    """
    lane.write_text(lane_document(LANE_MEAN, age_seconds=600), encoding="utf-8")
    line = plain(grid.render(row()))
    assert ticker_module.LANE_DOC_STALE in line, line
    assert ticker_module.LANE_MEAN_UNPUBLISHED not in line, line
    assert f"{LANE_MEAN:.2f}" not in line, line
    assert f"{ROW_RATE:.2f}" in line, line
    assert_no_outage_wording(line)


def test_a_document_carrying_a_mean_prints_the_mean(lane, row, grid):
    """The measured case is unchanged: the figure stands beside the rate."""
    lane.write_text(lane_document(), encoding="utf-8")
    line = plain(grid.render(row()))
    assert f"{ticker_module.LANE_LABEL} {LANE_MEAN:.2f}" in line, line
    assert f"{ROW_RATE:.2f}" in line, line
    assert_no_outage_wording(line)
