"""The order measure counts only the runs a section wait holds.

A review run carries its subject's section and is not held by that section's
waits, so its early launch is not a violation. A work run launched early is
reported with the record that released it, or as a defect when no record does.
The measure reads landing moments from the completion record and sorts its
rows, so two reads of one window agree and the input order cannot change them.

Every record used here is one the dispatcher writes: ``node_definition`` and
its ``waits_only_after`` flag ride the promoted row, and a request record
carries the ``waits`` it was queued under.
"""

from reckon.crew import drain_measure

PLAN = "workers-spend-fewer-tokens-per-landed-node"
UNSET = object()


def _run(
    run_id,
    *,
    role="implement",
    project="reckon",
    plan=PLAN,
    section="s8",
    launched="2026-11-21T20:21:28Z",
    completed="2026-11-22T14:53:42Z",
    after=(),
    waits_only_after=False,
    waits=UNSET,
):
    record = {
        "run_id": run_id,
        "role": role,
        "project": project,
        "worker_exit": {"launched_at": launched},
        "completed_at": completed,
        "node_definition": {
            "plan": plan,
            "section": section,
            "role": role,
            "after": list(after),
            "waits_only_after": bool(waits_only_after),
        },
    }
    if waits is not UNSET:
        record["waits"] = waits
    return record


#: The plan's own declaration: s8 waits on s17, which never lands in these
#: fixtures unless a test supplies it.
DEPENDS_ON = {("reckon", PLAN): {"s8": [f"{PLAN}#s17"]}}


def test_a_review_run_launched_before_its_upstream_landed_is_not_counted():
    """The exclusion acts: the same launch is a violation only as a work run."""
    review = _run("r-review", role="review")
    work = _run("r-work", role="implement")

    with_review_only = drain_measure.measure_dag_order([review], DEPENDS_ON)
    with_work_only = drain_measure.measure_dag_order([work], DEPENDS_ON)

    assert with_review_only["violations"] == 0
    assert with_review_only["rows"] == []
    assert with_review_only["review_runs_not_counted"] == 1
    assert with_review_only["launches_with_declared_waits"] == 0

    # Positive control: the instrument sees the launch, and a work run at the
    # same instant, in the same plan and section, is the defect.
    assert with_work_only["launches_with_declared_waits"] == 1
    assert with_work_only["violations"] == 1
    assert with_work_only["rows"][0]["run_id"] == "r-work"
    assert with_work_only["rows"][0]["role"] == "implement"


def test_a_request_dispatched_to_wait_only_on_its_own_refs_is_reported_with_that_flag():
    """The dispatch flag is what changes the verdict, not any echoed value."""
    released = _run("r-only-after", after=["r-another-request"], waits_only_after=True)

    allowed = drain_measure.measure_dag_order([released], DEPENDS_ON)

    assert allowed["violations"] == 0
    assert allowed["rows"] == []
    assert allowed["waived_launches"] == 1
    assert allowed["allowed_early_launches"] == 1
    row = allowed["waived_rows"][0]
    assert row["run_id"] == "r-only-after"
    assert row[drain_measure.WAITS_ONLY_AFTER_FIELD] is True
    assert "declared waits did not apply" in row["reason"]


def test_the_dispatch_flag_is_what_changes_the_verdict():
    """The same row without the flag is a defect."""
    held = _run("r-only-after", after=["r-another-request"])

    defect = drain_measure.measure_dag_order([held], DEPENDS_ON)

    assert defect["violations"] == 1
    assert defect["waived_launches"] == 0
    assert defect["allowed_early_launches"] == 0


def test_a_queued_wait_set_that_omits_the_edge_is_reported_as_an_edited_wait():
    """The pointer is read through the owner, and the same launch twice."""
    run = _run("r-pointer")  # the row itself carries no wait set
    pointer_holding_it = {"run_id": "r-pointer", "waits": [f"{PLAN}#s17"]}
    pointer_without_it = {"run_id": "r-pointer", "waits": [f"{PLAN}#s4"]}

    defect = drain_measure.measure_dag_order(
        [run], DEPENDS_ON, live_pointers=[pointer_holding_it]
    )
    allowed = drain_measure.measure_dag_order(
        [run], DEPENDS_ON, live_pointers=[pointer_without_it]
    )

    assert defect["violations"] == 1
    assert defect["edited_wait_launches"] == 0

    assert allowed["violations"] == 0
    assert allowed["edited_wait_launches"] == 1
    row = allowed["edited_wait_rows"][0]
    assert row["run_id"] == "r-pointer"
    assert row["queued_waits"] == [f"{PLAN}#s4"]


def test_a_pointer_shaped_run_is_read_for_its_waits():
    """A caller measuring the queue may pass the record that carries them."""
    run = _run("r-shaped", waits=[f"{PLAN}#s4"])

    result = drain_measure.measure_dag_order([run], DEPENDS_ON)

    assert result["violations"] == 0
    assert result["edited_wait_launches"] == 1


def test_an_implement_run_launched_early_with_no_record_is_a_defect():
    run = _run("r-early")

    result = drain_measure.measure_dag_order([run], DEPENDS_ON)

    assert result["status"] == "measured"
    assert result["launches_with_declared_waits"] == 1
    assert result["violations"] == 1
    assert result["runs_in_violation"] == 1
    assert result["allowed_early_launches"] == 0
    row = result["rows"][0]
    assert row["run_id"] == "r-early"
    assert row["reason"] == "the waited-on section never landed"
    # The result names where a released launch would have to be recorded, so a
    # defect row is not mistaken for proof that the wait was violated.
    assert "promotion removes the pointer" in result["reason_evidence"]


def test_two_reads_of_one_window_return_identical_sorted_rows():
    """Rows are non-empty, so the sort and the stability are both exercised."""
    window = [
        _run("r-zz-early", launched="2026-11-21T20:21:28Z"),
        _run("r-aa-early", launched="2026-11-21T18:00:00Z"),
        _run("r-review", role="review", launched="2026-11-21T21:34:11Z"),
        _run(
            "r-upstream",
            section="s17",
            launched="2026-11-20T10:00:00Z",
            completed="2026-11-21T23:00:00Z",
        ),
    ]
    later = [
        _run(
            "r-later",
            section="s17",
            launched="2026-11-22T09:00:00Z",
            completed="2026-11-22T14:00:00Z",
        )
    ]

    first = drain_measure.measure_dag_order(window, DEPENDS_ON)
    second = drain_measure.measure_dag_order(list(window), DEPENDS_ON)
    reversed_input = drain_measure.measure_dag_order(list(reversed(window)), DEPENDS_ON)
    with_later_history = drain_measure.measure_dag_order(
        window, DEPENDS_ON, history=[*window, *later]
    )

    assert first["violations"] == 2
    assert first["rows"], "the window must report rows or the sort guard is vacuous"
    assert first["rows"] == second["rows"] == reversed_input["rows"]
    assert first["violations"] == second["violations"] == reversed_input["violations"]
    # A later landing outside the window does not move a row already read: the
    # landing moment is the earliest completion, which the window already holds.
    assert first["rows"] == with_later_history["rows"]
    # Rows are sorted, so a read does not depend on the order rows arrive in.
    assert [row["run_id"] for row in first["rows"]] == sorted(
        row["run_id"] for row in first["rows"]
    )
