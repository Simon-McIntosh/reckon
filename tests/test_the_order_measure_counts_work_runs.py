"""The order measure counts only the runs a section wait holds.

A review run carries its subject's section and is not held by that section's
waits, so its early launch is not a violation. A work run launched early is a
defect unless a record says why the wait was lifted. The measure reads landing
moments from the completion record, so two reads of one window agree.
"""

from reckon.crew import drain_measure

PLAN = "workers-spend-fewer-tokens-per-landed-node"


def _run(
    run_id,
    *,
    role="implement",
    project="reckon",
    plan=PLAN,
    section="s8",
    launched="2026-11-21T20:21:28Z",
    completed="2026-11-22T14:53:42Z",
    waiver=None,
    dispatched_waits=None,
):
    record = {
        "run_id": run_id,
        "role": role,
        "project": project,
        "plan": plan,
        "section": section,
        "worker_exit": {"launched_at": launched},
        "completed_at": completed,
    }
    if waiver is not None:
        record["section_wait_waiver"] = waiver
    if dispatched_waits is not None:
        record["dispatched_waits"] = dispatched_waits
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


def test_an_implement_run_under_a_recorded_waiver_is_reported_with_it():
    reason = "the lead lifted the wait on s17 so the pooled record could close"
    run = _run("r-waived", waiver=reason)

    result = drain_measure.measure_dag_order([run], DEPENDS_ON)

    assert result["violations"] == 0
    assert result["rows"] == []
    assert result["waived_launches"] == 1
    row = result["waived_rows"][0]
    assert row["run_id"] == "r-waived"
    assert row["waiver"] == reason
    assert "recorded waiver" in row["reason"]


def test_a_waiver_naming_one_edge_does_not_cover_another():
    """A mapping is read per waited-on ref; another edge's early launch stays a defect."""
    other = "some-other-plan#s3"
    run = _run(
        "r-mapped",
        waiver={f"{PLAN}#s17": "the lead lifted it"},
    )
    depends_on = {("reckon", PLAN): {"s8": [f"{PLAN}#s17", other]}}

    result = drain_measure.measure_dag_order([run], depends_on)

    assert result["waived_launches"] == 1
    assert result["violations"] == 1
    assert result["rows"][0]["waits_on"] == other


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


def test_an_edited_wait_explains_a_launch_the_plan_no_longer_holds():
    """A request queued without this edge was never held by it."""
    run = _run("r-edited", dispatched_waits=[f"{PLAN}#s4"])

    result = drain_measure.measure_dag_order([run], DEPENDS_ON)

    assert result["violations"] == 0
    assert result["edited_wait_launches"] == 1
    assert "edited wait" in result["edited_wait_rows"][0]["reason"]


def test_two_reads_of_one_window_return_identical_rows():
    window = [
        _run("r-b", launched="2026-11-21T20:21:28Z"),
        _run("r-a", launched="2026-11-21T18:00:00Z"),
        _run("r-review", role="review", launched="2026-11-21T21:34:11Z"),
        _run(
            "r-upstream",
            section="s17",
            launched="2026-11-20T10:00:00Z",
            completed="2026-11-20T12:00:00Z",
        ),
        _run("r-early", section="s8", launched="2026-11-21T19:00:00Z"),
    ]
    depends_on = {("reckon", PLAN): {"s8": [f"{PLAN}#s17"]}}
    later = [_run("r-later", section="s17")]

    first = drain_measure.measure_dag_order(window, depends_on)
    second = drain_measure.measure_dag_order(list(window), depends_on)
    an_hour_later = drain_measure.measure_dag_order(window, depends_on)
    with_later_history = drain_measure.measure_dag_order(
        window, depends_on, history=[*window, *later]
    )

    assert first["rows"] == second["rows"] == an_hour_later["rows"]
    assert first["violations"] == second["violations"]
    # A run added outside the window cannot move a row already read.
    assert first["rows"] == with_later_history["rows"]
    assert first["waived_rows"] == with_later_history["waived_rows"]
    # Rows are sorted, so the input order does not change what a read returns.
    assert [row["run_id"] for row in first["rows"]] == sorted(
        row["run_id"] for row in first["rows"]
    )
