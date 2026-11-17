"""The pane emits only real transitions, in observation order, with a live age.

Four defects reached coordinators as rows they could not use:

* a row whose two halves name the same word — ``waiting -> waiting``,
  ``delivered -> delivered`` — woke a reader for a move between one state and
  itself. The row may still carry news (a duty, a launch, a wait whose
  declaration changed), so the row is kept and the transition it never made is
  dropped: it names the state the run stands in and draws no arrow.
* a row stamped before one already emitted for its run, drawn when a sweep
  rewrote a stream mid-read or a re-armed follower picked up older rows. The
  pane draws a run as of the newest observation it has shown, so the late row
  is dropped rather than drawn over a state the reader has already passed.
* an obligation's age frozen at the figure it had when it was written down, so
  an hour-old duty read as freshly observed. The age is measured again at every
  read, from the instant the row records for its own observation.
* a count of duties that named no run ("run-stalled 1"), which a coordinator
  cannot act on. Every counted obligation names at least one run id, and the
  summary names them beside the count.

Each case drives the module's own reader rather than a restatement of it: the
renderer for the drawn row, the follower's row path for the emitted one, and the
duties' own builders for the age and the count.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reckon.crew import obligations, ticker

HORIZON = 3600


def _row(state: str, at: str, **extra):
    """One streamed run row, as a producer writes it."""
    row = {
        "project": "pane-transitions-proj",
        "event": "stream",
        "run_id": "r-pane-transitions",
        "node": "pane-transitions-node",
        "role": "implement",
        "from_state": "working",
        "to_state": state,
        "working": 1,
        "blocked": 0,
        "unpromoted": 0,
        "waiting": 0,
        "observed_at": at,
    }
    row.update(extra)
    return row


def _wait(at: str, verdict: str):
    """A run standing in a wait, with the probe's own verdict beside the word.

    A wait is the case a same-word row is legitimately news for: the pane's word
    is the same either way, and what changed is the declaration the coordinator
    must be able to read.
    """
    return _row(
        "waiting",
        at,
        from_state="waiting",
        wait_condition_state=verdict,
        wait_overdue=False,
        expected_horizon_seconds=HORIZON,
    )


def test_a_row_repeating_the_pane_word_depicts_no_transition():
    """A same-word row carrying news names its state and draws no arrow."""
    pane = ticker.Ticker(width=180, color=False, model_aliases=())
    arrival = pane.render(_wait("2026-09-24T12:00:00+00:00", "pending"))
    assert ticker.ARROW in arrival, arrival
    # The probe answers differently: the row is news, and its word is the word
    # the pane already shows for the run.
    repeated = pane.render(_wait("2026-09-24T12:01:00+00:00", "live"))
    assert "waiting" in repeated, repeated
    assert ticker.ARROW not in repeated, repeated
    # A row that names a different left word still draws its transition.
    moved = pane.render(_row("blocked", "2026-09-24T12:02:00+00:00"))
    assert ticker.ARROW in moved, moved


def test_a_row_older_than_one_already_emitted_is_dropped():
    """Each run's rows are emitted in order, and a late row is dropped."""
    path = ticker.PaneRowPath(show_observer=True)
    later = _row("needs-help", "2026-09-24T12:05:00+00:00")
    older = _row("waiting", "2026-09-24T12:01:00+00:00")
    emitted = []
    emitted += path.feed(later, now=ticker.row_moment(later))
    emitted += path.feed(older, now=ticker.row_moment(older))
    assert [row["to_state"] for row in emitted] == ["needs-help"]

    # The same two rows in their own order both reach the pane, so the drop is
    # the ordering and not the row.
    in_order = ticker.PaneRowPath(show_observer=True)
    emitted = []
    emitted += in_order.feed(older, now=ticker.row_moment(older))
    emitted += in_order.feed(later, now=ticker.row_moment(later))
    assert [row["to_state"] for row in emitted] == ["waiting", "needs-help"]


def test_an_obligations_age_is_measured_at_every_read():
    """A duty's age is measured from its own observation at each read."""
    ended = datetime(2026, 9, 24, 9, 12, 4, tzinfo=UTC)
    settled = "2026-09-24T12:00:00Z"
    aged = obligations._live_item(
        {"run_id": "r-aged", "node": "n-aged", "terminal_at": settled},
        kind="run-stalled",
        now=ended,
    )
    assert aged["observed_at"] == settled

    # A reader holding the row measures the age from the row's own instant, so
    # the figure grows with the reader's clock instead of staying frozen.
    observed = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    half_hour_later = observed + timedelta(seconds=1800)
    assert obligations.refresh_age(aged, now=half_hour_later) == 1800

    # Two rows observed at different instants read differently at one clock,
    # which is what a batch stamp on the pass would hide.
    later_evidence = obligations._live_item(
        {"run_id": "r-other", "terminal_at": "2026-09-24T12:20:00Z"},
        kind="run-stalled",
        now=ended,
    )
    read_at = observed + timedelta(seconds=3600)
    assert obligations.refresh_age(aged, now=read_at) == 3600
    assert obligations.refresh_age(later_evidence, now=read_at) == 2400

    # A row whose evidence records no instant states no anchor, so a row cannot
    # pass the pass's own clock off as the duty's observation time.
    undated = obligations._live_item(
        {"run_id": "r-undated"}, kind="run-stalled", now=ended
    )
    assert "observed_at" not in undated
    assert obligations.refresh_age(undated, now=half_hour_later) is None


def test_a_counted_obligation_names_a_run():
    """Every duty row names the runs a reader can trace it to."""
    named = obligations._with_action_form(
        {
            "kind": "held-by-finished-run",
            "run_id": "r-waiting",
            "holder_run_id": "r-holder",
            "age_seconds": 120,
            "next_command": "",
        }
    )
    assert named["run_ids"] == ["r-waiting", "r-holder"]

    # A delivered run awaiting its review names the review beside the run, so a
    # duty about a review can be followed to the review's own run.
    reviewed = obligations._with_action_form(
        {
            "kind": "review-missing",
            "run_id": "r-delivered",
            "review_run_id": "r-review",
            "age_seconds": 30,
            "next_command": "",
        }
    )
    assert reviewed["run_ids"] == ["r-delivered", "r-review"]

    # A row whose builder already knows the runs it concerns keeps them, which
    # is how a project-scoped notice names the landings it unblocks.
    declared = obligations._with_action_form(
        {"kind": "index-lags-head", "run_id": "", "run_ids": ["r-a", "r-b"]}
    )
    assert declared["run_ids"] == ["r-a", "r-b"]

    # A duty naming no run at all is the shape this contract forbids.
    departed = obligations._with_action_form({"kind": "run-stalled", "run_id": ""})
    assert "run_ids" not in departed
