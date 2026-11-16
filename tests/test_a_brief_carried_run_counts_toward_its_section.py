"""A brief-carried run counts toward its plan in the readers outside the ledger.

A run dispatched with a brief records no top-level ``plan``: it names the plan
on the impl move it reported or on the node definition it ran. The ledger's own
readers take a row's plan from one resolver, ``ledger.row_plan``, which falls
back to both places. Readers outside the ledger took the flat field directly,
so a section whose only landed run carried a brief read as never landed, a
replayed dispatch named no plan, and a plan's counts missed its own runs.

Each case below runs one reader over a temporary store built through the
store's own writer, holds it to a brief-carried run naming a plan, and checks
that a run naming no plan is not claimed by a named plan. The repair is one
resolver; the cases stay separate because what each reader derives is its own.

The reported symptom — two queued runs left parked on a section declared done,
after three brief-carried runs landed it — is *not* decided by the section-hold
read. A section's declare-it-done release is decided by
``reckon.crew.admission.readiness._section_run_unlanded``, which asks git about
the promoted rows of the section; the promoted-only case is answered from the
section's declaration and the store measured here, so that read releases, not
holds. What the section-hold read decides is whether a section's nodes are
still work to dispatch, and the brief-carried landing below was invisible to
it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from reckon import capabilities, ledger
from reckon.crew import section_holds
from reckon.crew.node import TaskNode
from reckon.crew.picker import outcomes as outcomes_module
from reckon.crew.picker import replay as replay_module
from reckon.crew.picker import snapshot as snapshot_module
from reckon.crew.picker.types import PickRequest
from reckon.schedule import derive_schedule

PROJECT = "demo"
#: The plan the brief-carried run names.
NAMED = "plan-named-by-a-brief"
#: A second plan, so a count for one is proved not to be the other's.
OTHER = "plan-named-elsewhere"
#: The section the brief-carried run names.
SECTION = "s5"
#: A brief digest in the shape the record writer stores; a record carrying one
#: writes no top-level plan, which is the shape these cases read.
BRIEF = {"path": "/briefs/brief.md", "sha256": "0" * 64}
NOW = datetime(2026, 1, 3, tzinfo=UTC)
LANDED = "r-20260102T000000000-brief-landed"
UNPLANNED = "r-20260102T000001000-brief-unplanned"
OTHER_RUN = "r-20260102T000002000-brief-other"


def _record(
    run_id: str,
    *,
    definition_plan: str | None = None,
    node: str = "a-node",
    phase: str = "",
    write_paths: list[str] | None = None,
    worker_seconds: int | None = None,
) -> dict:
    """One record shaped the way a brief-carried dispatch is stored.

    The top-level plan is left empty and the plan appears only on the node
    definition, which is the shape a brief-carried promotion writes.
    """

    definition: dict[str, object] = {"id": node, "section": SECTION}
    if definition_plan is not None:
        definition["plan"] = definition_plan
    if write_paths is not None:
        definition["write_paths"] = list(write_paths)
    record = ledger.build_record(
        run_id=run_id,
        plan="",
        gate="passed",
        project=PROJECT,
        node=node,
        section=SECTION,
        role="implement",
        spec_level="exact",
        backend="worker",
        agent={"backend": "worker", "model": "concrete", "effort": "high"},
        node_definition=definition,
        dispatched_at="2026-01-02T00:00:00Z",
        completed_at="2026-01-02T00:10:00Z",
        completed_at_source="stream_mtime",
        brief=BRIEF,
        worker_seconds=worker_seconds,
    )
    if phase:
        record["phase"] = phase
    return record


def _store(root: Path) -> Path:
    """A temporary store holding a brief-carried landing and an unplanned run."""

    (root / "docs" / "state" / PROJECT / "runs").mkdir(parents=True)
    rows = [
        _record(LANDED, definition_plan=NAMED, node="brief-landed"),
        _record(UNPLANNED, node="brief-unplanned"),
        _record(OTHER_RUN, definition_plan=OTHER, node="brief-other"),
    ]
    for row in rows:
        ledger.write_run_file(PROJECT, row, root, fsync=False)
    return root


def _rows(root: Path) -> list[dict]:
    return list(ledger.runs(PROJECT, root))


def test_a_section_whose_only_landed_run_carried_a_brief_reads_as_landed(
    tmp_path: Path,
) -> None:
    """The section-hold read names the section its brief-carried run delivered."""

    store = _store(tmp_path)

    held = section_holds.held_section_blockers(PROJECT, store, (), plans={NAMED, OTHER})

    assert (NAMED, SECTION) in held, (
        "a section whose only landed run was dispatched with a brief did not "
        f"read as landed to the section-hold read: {sorted(held)}"
    )
    assert [str(row["run_id"]) for row in held[(NAMED, SECTION)]] == [LANDED]
    # The instrument sees the other plan's row before the split is trusted.
    assert [str(row["run_id"]) for row in held[(OTHER, SECTION)]] == [OTHER_RUN]


def test_a_run_naming_no_plan_is_not_claimed_by_a_named_plan(tmp_path: Path) -> None:
    """A brief-carried run naming no plan is not counted under any plan."""

    store = _store(tmp_path)
    assert UNPLANNED in {str(row["run_id"]) for row in _rows(store)}

    held = section_holds.held_section_blockers(PROJECT, store, ())

    claimed = {str(row["run_id"]) for rows in held.values() for row in rows}
    assert LANDED in claimed, claimed
    assert UNPLANNED not in claimed, (
        f"a run naming no plan was claimed by a section's hold: {sorted(held)}"
    )


def test_the_waiter_release_read_does_not_hold_a_brief_carried_landing(
    tmp_path: Path,
) -> None:
    """The read that decides a waiter's release releases, rather than parks.

    This is the measured answer to the reported symptom: the section's
    promoted-only landing is answered from the store, so that read does not
    park a waiter. The reader lives outside this node's files, so it is
    recorded as a measurement rather than repaired here.
    """

    from reckon.crew.admission import readiness

    store = _store(tmp_path)
    assert _rows(store), "the instrument must see the store before a read is trusted"

    unlanded = readiness._section_run_unlanded(
        PROJECT, NAMED, SECTION, live=(), cache={}
    )

    assert unlanded is False, (
        "the waiter-release read held a hope on a store whose only row of the "
        "plot landed a brief: it reported the section unlanded"
    )


def _request(node_id: str, plan: str) -> PickRequest:
    node = TaskNode(
        id=node_id,
        goal="a goal",
        plan=plan,
        section=SECTION,
        role="implement",
        spec_level="exact",
        done_when="a condition",
        estimated_hours=1.0,
    )
    return PickRequest(project=PROJECT, node=node)


def test_the_candidate_scan_counts_a_brief_carried_attempt(tmp_path: Path) -> None:
    """A run naming its plan only on its definition counts as an attempt."""

    store = _store(tmp_path)
    rows = _rows(store)
    assert len(rows) == 3, sorted(row.get("run_id") for row in rows)

    counted: dict[str, int] = {}
    snapshot_module._candidates_scan(
        rows, _request("brief-landed", NAMED), now=NOW, attempts_out=counted
    )
    assert counted.get("attempts") == 1, counted

    uncounted: dict[str, int] = {}
    snapshot_module._candidates_scan(
        rows, _request("brief-unplanned", NAMED), now=NOW, attempts_out=uncounted
    )
    assert not uncounted.get("attempts"), uncounted


def test_the_outcome_hold_names_the_run_that_followed_it(tmp_path: Path) -> None:
    """A hold is matched to the brief-carried run that followed the hold."""

    store = _store(tmp_path)
    rows = _rows(store)
    holds = [
        {"node": "brief-landed", "plan": NAMED, "held_at": "2026-01-01T00:00:00Z"},
        {"node": "brief-unplanned", "plan": NAMED, "held_at": "2026-01-01T00:00:00Z"},
    ]

    summary = outcomes_module.summarize({PROJECT: rows}, {PROJECT: holds})

    by_node = {row["node"]: row for row in summary["holds"]["records"]}
    assert by_node["brief-landed"]["later_run_id"] == LANDED
    assert by_node["brief-unplanned"]["later_run_id"] is None


def test_the_replay_rebuilds_the_plan_the_run_named(
    tmp_path: Path, monkeypatch
) -> None:
    """A replayed request for a brief-carried run carries the plan it named."""

    store = _store(tmp_path)
    records = _rows(store)
    captured: list[PickRequest] = []

    class _Selection:
        """The shape ``replay`` reads out of the pick it makes."""

        def as_dict(self) -> dict:
            return {
                "backend": "worker",
                "model": "concrete",
                "jev_latency_ms": 0.0,
                "fallback_reason": None,
                "latency_ms": 0.0,
                "usage": {},
            }

    def fake_pick(request, config, **_kwargs):
        captured.append(request)
        return _Selection()

    monkeypatch.setattr(replay_module, "pick", fake_pick)
    monkeypatch.setattr(replay_module.ledger, "runs", lambda *a, **k: records)

    replay_module.replay(PROJECT, len(records), {"backends": {}}, repo=store)

    by_node = {request.node.id: request.node.plan for request in captured}
    assert by_node["brief-landed"] == NAMED
    assert by_node["brief-unplanned"] == ""


def test_a_held_run_holds_the_bar_of_the_plan_it_names(tmp_path: Path) -> None:
    """A run row bucketed by plan reaches the bar of the plan it names."""

    store = _store(tmp_path)
    rows = _rows(store)
    held_row = next(row for row in rows if row["run_id"] == LANDED)
    held_row["phase"] = "unpromoted"
    plans = [
        {
            "slug": NAMED,
            "title": NAMED,
            "type": "plan",
            "status": "active",
            "effort_hours": 2.0,
            "depends_on": [],
        }
    ]

    schedule = derive_schedule(plans, [held_row], PROJECT, NOW)

    bar = next(item for item in schedule["items"] if item["slug"] == NAMED)
    assert bar["state"] == "held", (
        "a run row naming its plan on its definition did not reach its plan's "
        f"bar: {bar}"
    )


def test_the_measurement_observation_is_counted_under_its_plan(
    tmp_path: Path,
) -> None:
    """A brief-carried run's hours are measured against its own plan's estimate."""

    root = tmp_path
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "plans" / f"{NAMED}.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{NAMED}">'
        '<meta name="plan-effort-hours" content="2.0">'
        f"<title>{NAMED}</title></head><body></body></html>",
        encoding="utf-8",
    )
    (root / "docs" / "state" / PROJECT / "runs").mkdir(parents=True)
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)
    ledger.write_run_file(
        PROJECT,
        _record(LANDED, definition_plan=NAMED, worker_seconds=3600),
        root,
        fsync=False,
    )

    report = capabilities.derive_capabilities({PROJECT: root / "docs"})

    observations = {
        item["run_id"]: item
        for configuration in report["configurations"]
        for item in configuration["observations"]
    }
    assert set(observations) == {LANDED}, observations
    assert observations[LANDED]["plan"] == NAMED


def test_the_routing_rework_count_reads_a_brief_carried_run(tmp_path: Path) -> None:
    """A later run of the same plan counts its brief-carried predecessor."""

    root = tmp_path
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "state" / PROJECT / "runs").mkdir(parents=True)
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)
    rows = [
        _record(
            "r-early", definition_plan=NAMED, node="exact", write_paths=["src/a.py"]
        ),
        _record(
            "r-late",
            definition_plan=NAMED,
            node="exact",
            write_paths=["src/a.py"],
        ),
    ]
    for row in rows:
        ledger.write_run_file(PROJECT, row, root, fsync=False)

    report = capabilities.derive_routing({PROJECT: root / "docs"})

    assert report["rows"], report
    reworked = sum(int(item["reworked"]) for item in report["rows"])
    assert reworked == 1, report["rows"]


def test_the_waiter_release_read_holds_a_brief_carried_run_not_landed(
    tmp_path: Path, monkeypatch
) -> None:
    """A section whose brief-carried run is promoted and not landed holds its waiters.

    The read identifies that run as the section's, so it reaches the landing
    question instead of answering "no run here" and releasing the waiter.
    """

    from reckon.crew.admission import readiness

    store = _store(tmp_path)
    extra = _record(LANDED + "-promoted", definition_plan=NAMED, node="brief-landed")
    extra["promoted_revision"] = "0" * 40
    extra["run_id"] = LANDED
    ledger.write_run_file(PROJECT, extra, store, fsync=False)
    assert _rows(store), "the instrument must see the store before a read is trusted"

    rows = list(ledger.load(PROJECT, root=store)[0]["runs"])
    monkeypatch.setattr(ledger, "load", lambda project, *a, **k: ({"runs": rows}, 0))
    held = readiness._section_run_unlanded(
        PROJECT,
        NAMED,
        SECTION,
        live=(),
        cache={},
    )

    assert held is True, (
        "the waiter-release read released a section whose brief-carried run is "
        "promoted and not landed"
    )


def test_the_lane_advisory_charges_a_brief_carried_rework(tmp_path: Path) -> None:
    """A later run on the same plan reworks its brief-carried predecessor."""

    from reckon.crew import dispatch_admission

    rows = [
        _record(
            "r-early", definition_plan=NAMED, node="exact", write_paths=["src/a.py"]
        ),
        _record(
            "r-late", definition_plan=NAMED, node="exact", write_paths=["src/a.py"]
        ),
    ]

    evidence = dispatch_admission._lane_advisory_costs(
        rows, role="implement", spec_level="exact"
    )

    assert evidence, evidence
    assert evidence["worker"]["rework_rate"] == 0.5, evidence


def test_a_pick_counts_a_brief_carried_attempt(tmp_path: Path, monkeypatch) -> None:
    """The state a pick renders counts an attempt that named its plan on its definition."""

    from reckon.crew import picker as picker_module
    from reckon.crew.picker.types import Candidate

    store = _store(tmp_path)
    rows = _rows(store)
    assert rows, "the instrument must see the store before a count is trusted"
    seen: dict[str, object] = {}

    real_render = picker_module.prompts.render

    def render(template, **kwargs):
        seen["attempts"] = kwargs.get("attempts")
        return real_render(template, **kwargs)

    def snapshotter(*_args, **_kwargs):
        return [
            Candidate(
                backend="worker",
                family="local",
                model="concrete",
                effort="high",
                local=True,
                availability="served",
                utilisation_pct=None,
                burn_multiple=None,
                pace_allowance=None,
                resets_at=None,
                worker_slots=None,
                congestion=None,
                outcomes={"passed": 0, "failed": 0, "not-run": 0, "unknown": 0},
            )
        ]

    def caller(_state, _questions, **_kwargs):
        return {
            "model": "stub",
            "usage": {},
            "answers": {
                "route": {
                    "choice": "local:concrete:worker",
                    "confidence": 0.9,
                    "probabilities": {
                        "local:concrete:worker": 0.9,
                        "worker:concrete:worker": 0.1,
                    },
                }
            },
        }

    monkeypatch.setattr(picker_module.prompts, "render", render)
    monkeypatch.setattr(
        picker_module, "_declared_difficulty", lambda *_a, **_k: ({}, None, "")
    )

    picker_module.pick(
        _request("brief-landed", NAMED),
        {"backends": {}, "roles": {"implement": {}}},
        repo=store,
        snapshotter=snapshotter,
        caller=caller,
        records=rows,
        budget_snapshot={},
        authority={},
    )

    assert seen.get("attempts") == 1, seen
