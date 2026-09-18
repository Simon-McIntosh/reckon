"""What counts as an arrival in a watch stream, and what counts as news.

Three fleets wrote three filters over the same watch logs and got three
answers. The two filters that look natural are both wrong, and the live
streams on this workstation show why:

* keeping only ``event == "transition"`` drops genuine arrivals. A dispatch a
  follower first observes has no prior state to transition from, so it is
  recorded as a baseline with a null ``from_state``. Across the six live
  streams at 2026-09-18T09:26Z (12446 rows) 2417 of 3031 nodes first appear as
  a baseline row, so a transition-only filter re-dates every one of those to a
  later movement, and 23 of the 3031 have no transition row anywhere, so it
  loses them outright.
* keeping every baseline row invents arrivals. A follower that re-attaches
  re-inventories the whole fleet in one instant, one baseline row per live run.
  The largest such burst on disk covers ten nodes in the same second, every one
  of them already carrying earlier rows, and 204 of the 2621 baseline rows on
  disk are members of such a burst.

The discriminator is first appearance per node, refined by a direct test for
the re-inventory burst, because a run dispatched before the recording began has
no earlier row and would otherwise read as an arrival. The range below is the
point of the file: the domain is every shape an arrival can take — first as
baseline, first as transition, a node seen across two follower sessions, a node
whose only row is a burst row, a wave arriving in one poll, an all-transition
stream — not the one fixture that happened to raise the defect.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon.crew.query import extract_watch_arrivals, parse_watch_row

TRANSITION_ARROW = "\N{RIGHTWARDS ARROW}"

# Every rendered fixture below is a verbatim line of a live watch stream, with
# the stream and line number named at the fixture. The corpus is the authority
# for what a rendered row looks like: 2314 rendered rows across the six streams,
# in exactly two shapes — an arrow with one token to its left (656 rows, a node
# appearing for the first time) and an arrow with two (1658 rows). A fixture in
# a shape the corpus never produces cannot fail on the defect the corpus has,
# which is how an earlier version of this file passed while the parser dropped
# the arrival it existed to find.


def _row(
    node: str,
    *,
    event: str,
    observed_at: str,
    from_state: str | None = None,
    to_state: str = "dispatched",
    session: str | None = "s1",
    project: str = "demo",
) -> str:
    record = {
        "agent": "gpt-5.6-sol/medium",
        "blocked": 0,
        "event": event,
        "from_state": from_state,
        "node": node,
        "observed_at": observed_at,
        "project": project,
        "run_id": "r-" + str(node),
        "session": session,
        "to_state": to_state,
        "unpromoted": 0,
        "working": 1,
    }
    return json.dumps(record, sort_keys=True) + "\n"


def _stream(tmp_path: Path, name: str, lines: list[str]) -> Path:
    path = tmp_path / name
    path.write_text("".join(lines), encoding="utf-8")
    return path


def _arrived(result: dict) -> list[str]:
    return sorted(row["node"] for row in result["arrivals"])


def test_follower_restart_counts_each_node_once(tmp_path: Path) -> None:
    """A re-inventory after a follower restart is not an arrival.

    Session one dispatches two nodes and records a movement. Session two
    begins with a re-inventory of the live fleet - including a node that was
    already running before the stream began and so has no earlier row at all -
    then records one genuine dispatch and one genuine movement.
    """
    path = _stream(
        tmp_path,
        "demo-abc123.events",
        [
            _row("alpha", event="baseline", observed_at="2026-09-17T10:00:00Z"),
            _row(
                "alpha",
                event="transition",
                from_state="dispatched",
                to_state="complete",
                observed_at="2026-09-17T10:05:00Z",
            ),
            _row("beta", event="baseline", observed_at="2026-09-17T10:01:00Z"),
            _row(
                "beta",
                event="baseline",
                observed_at="2026-09-17T11:00:00Z",
                session="s2",
            ),
            _row(
                "pre-existing",
                event="baseline",
                observed_at="2026-09-17T11:00:00Z",
                session="s2",
            ),
            _row(
                "gamma",
                event="baseline",
                observed_at="2026-09-17T11:00:00Z",
                session="s2",
            ),
            _row("delta", event="baseline", observed_at="2026-09-17T11:20:00Z"),
            _row(
                "beta",
                event="transition",
                from_state="dispatched",
                to_state="working",
                observed_at="2026-09-17T11:21:00Z",
            ),
        ],
    )

    result = extract_watch_arrivals([path])

    assert _arrived(result) == ["alpha", "beta", "delta"]
    assert sorted(row["node"] for row in result["re_inventory"]) == [
        "beta",
        "gamma",
        "pre-existing",
    ]
    # every node appears exactly once, so the re-inventory rows were not
    # double-counted as arrivals
    assert len(result["arrivals"]) == len(
        {row["project"] + row["node"] for row in result["arrivals"]}
    )
    assert [row["node"] for row in result["state_changes"]] == ["alpha", "beta"]


def test_a_dispatch_recorded_as_baseline_is_counted(tmp_path: Path) -> None:
    """The other half of the defect: this is the row the transition-only
    filter loses, and it must be reported as a real arrival."""
    path = _stream(
        tmp_path,
        "demo-abc123.events",
        [
            _row("alpha", event="baseline", observed_at="2026-09-17T10:00:00Z"),
            _row(
                "alpha",
                event="transition",
                from_state="dispatched",
                to_state="working",
                observed_at="2026-09-17T10:00:30Z",
            ),
        ],
    )
    result = extract_watch_arrivals([path])
    assert _arrived(result) == ["alpha"]
    assert result["arrivals"][0]["event"] == "baseline"
    assert result["arrivals"][0]["kind"] == "arrival"


def test_a_node_first_seen_as_a_transition_is_an_arrival(tmp_path: Path) -> None:
    path = _stream(
        tmp_path,
        "demo-abc123.events",
        [
            _row(
                "late",
                event="transition",
                from_state="working",
                to_state="complete",
                observed_at="2026-09-17T10:00:00Z",
            )
        ],
    )
    result = extract_watch_arrivals([path])
    assert _arrived(result) == ["late"]
    assert result["state_changes"] == []


def test_state_changes_after_the_first_row_keep_their_order(tmp_path: Path) -> None:
    path = _stream(
        tmp_path,
        "demo-abc123.events",
        [
            _row("alpha", event="baseline", observed_at="2026-09-17T10:00:00Z"),
            _row(
                "alpha",
                event="transition",
                from_state="dispatched",
                to_state="working",
                observed_at="2026-09-17T10:01:00Z",
            ),
            _row(
                "alpha",
                event="transition",
                from_state="working",
                to_state="complete",
                observed_at="2026-09-17T10:02:00Z",
            ),
            _row(
                "alpha",
                event="transition",
                from_state="complete",
                to_state="promoted",
                observed_at="2026-09-17T10:03:00Z",
            ),
        ],
    )
    result = extract_watch_arrivals([path])
    assert [row["to_state"] for row in result["state_changes"]] == [
        "working",
        "complete",
        "promoted",
    ]
    assert [row["from_state"] for row in result["state_changes"]] == [
        "dispatched",
        "working",
        "complete",
    ]


def test_a_wave_sharing_one_stamp_is_still_arrivals(tmp_path: Path) -> None:
    """A same-second group with no already-known member is a wave, not an
    inventory: the measured 8-node and 3-node waves on disk have this shape."""
    path = _stream(
        tmp_path,
        "demo-abc123.events",
        [
            _row("w1", event="baseline", observed_at="2026-09-17T10:00:00Z"),
            _row("w2", event="baseline", observed_at="2026-09-17T10:00:00Z"),
            _row("w3", event="baseline", observed_at="2026-09-17T10:00:00Z"),
        ],
    )
    result = extract_watch_arrivals([path])
    assert _arrived(result) == ["w1", "w2", "w3"]
    assert result["re_inventory"] == []


def test_a_stream_with_no_baseline_rows_keeps_its_arrivals(tmp_path: Path) -> None:
    """Positive control: the extraction does not depend on a baseline existing."""
    path = _stream(
        tmp_path,
        "demo-abc123.events",
        [
            _row(
                "alpha",
                event="transition",
                from_state="dispatched",
                to_state="working",
                observed_at="2026-09-17T10:00:00Z",
            ),
            _row(
                "alpha",
                event="transition",
                from_state="working",
                to_state="complete",
                observed_at="2026-09-17T10:01:00Z",
            ),
            _row(
                "beta",
                event="transition",
                from_state="dispatched",
                to_state="complete",
                observed_at="2026-09-17T10:02:00Z",
            ),
        ],
    )
    result = extract_watch_arrivals([path])
    assert _arrived(result) == ["alpha", "beta"]
    assert [row["node"] for row in result["state_changes"]] == ["alpha"]


def test_every_stamp_is_utc_or_explicitly_unknown(tmp_path: Path) -> None:
    """A stamp is never assumed: JSON rows are UTC, rendered rows are unknown.

    The rendered line carries a wall clock in the reader's LOCAL zone with no
    offset and no date, so treating a rendering as UTC is how a concurrent
    ascent becomes a descending limb in a merged view.

    Verbatim from the reckon stream, line 1 — the arrival that opens that
    stream, and the same arrival that opens the imas-ambix stream.
    """
    rendered = (
        "06:45:38  reading-mode-and-palette      → dispatched              "
        "1 live · 0 blocked · 0 unpromoted"
    )
    path = _stream(
        tmp_path,
        "reckon-18dc8a86709b.events",
        [
            _row("alpha", event="baseline", observed_at="2026-09-17T10:00:00Z"),
            rendered + "\n",
        ],
    )
    result = extract_watch_arrivals([path])

    for row in result["arrivals"] + result["state_changes"] + result["re_inventory"]:
        if row["observed_at_zone"] == "utc":
            assert row["observed_at_utc"] is not None
            assert row["observed_at_utc"].endswith("+00:00"), row["observed_at_utc"]
        else:
            assert row["observed_at_zone"] == "unknown"
            assert row["observed_at_utc"] is None

    assert result["counts"]["rendered_rows"] == 1
    assert result["counts"]["stamps_unknown"] == 1
    assert [row["line"] for row in result["stamps_unknown"]] == [2]


def test_a_rendered_transition_row_is_read_not_guessed() -> None:
    """Two tokens left of the arrow is a movement between two named states.

    Verbatim from the imas-ambix stream, line 4: the node holds the arrival at
    line 1 and this row is the movement that follows it, so the pair pins the
    one-token reading above to the same stream.
    """
    rendered = (
        "16:31:11  clive-reuse-map               dispatched → working      "
        "3 live · 0 blocked · 0 unpromoted"
    )
    row = parse_watch_row(
        rendered,
        path=Path("/w/imas-ambix-fd8fbc1b4563.events"),
        line_number=4,
    )
    assert row is not None
    assert row["event"] == "transition"
    assert row["node"] == "clive-reuse-map"
    assert row["from_state"] == "dispatched"
    assert row["to_state"] == "working"
    assert row["project"] == "imas-ambix"
    assert row["observed_at_zone"] == "unknown"


def test_a_rendered_row_without_a_source_state_is_still_an_arrival(
    tmp_path: Path,
) -> None:
    """An arrival rendered as an arrow row has no source state to print.

    Verbatim from the imas-ambix stream, line 1 - the node, then the arrow,
    with nothing between them but the empty source-state column's padding.
    A node the follower sees for the first time has nothing to transition from,
    so the pane prints the node alone. Re-derived over the six live streams at
    2026-09-18T09:26Z: 12446 rows, 3031 nodes, 2314 rendered rows, of which 656
    carry the one-token shape and 614 of those are a node's first row. A parser
    requiring two tokens left of the arrow discards the arrival it exists to
    find and reports the node's next row, a state change, in its place. The JSON
    row below stands for that next row, so the assertion is about which row is
    the arrival rather than about whether the node appears at all.
    """
    rendered = (
        "16:29:36  clive-reuse-map               "
        + TRANSITION_ARROW
        + " dispatched              1 live · 0 blocked · 0 unpromoted"
    )
    path = _stream(
        tmp_path,
        "imas-ambix-fd8fbc1b4563.events",
        [
            rendered + "\n",
            _row(
                "clive-reuse-map",
                event="transition",
                from_state="dispatched",
                to_state="working",
                observed_at="2026-09-17T10:05:00Z",
                project="imas-ambix",
            ),
        ],
    )
    result = extract_watch_arrivals([path])

    arrivals = [row for row in result["arrivals"] if row["node"] == "clive-reuse-map"]
    assert len(arrivals) == 1
    assert arrivals[0]["line"] == 1
    assert arrivals[0]["rendered"] is True
    assert arrivals[0]["to_state"] == "dispatched"
