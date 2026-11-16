"""A run dispatched with a brief is still returned by the plan-selected read.

A run's plan is read from the top level of its run record, but a dispatch that
carried a brief records that field as ``None`` even when the run names a plan
and section on its node definition: the brief is the run's authority for what
to do, and the record writer treated that as meaning the run had no plan. The
plan-selected read (``ledger.runs(..., plan=...)``) selects on the top-level
field, so every brief-carried run dropped out of its own plan's history, and a
plan whose work was all dispatched with briefs read as having none.

The plan a row belongs to now has one owner, ``ledger._row_plan``, which reads
the row's own field first and then the two places a dispatch records the same
fact: the impl move the run reported and the node definition it ran. The index
extracts that owner's answer into the column the plan-selected read filters on,
so the selection stays inside the index query and decodes no other plan's
payload.

These cases hold over a temporary store built through the store's own writer:
a promoted brief-carried run naming a plan, a stored record naming its plan
only on its node definition, a record whose definition a rotation has washed
out naming it through its impl move, and a brief-carried run naming no plan at
all. Every read is compared against the unfiltered read over the same store, so
a row that is missing from a plan's read is never a row that was never written.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import ledger

PROJECT = "demo"
#: The plan the brief-carried run names.
NAMED = "plan-named-by-a-brief"
#: A second plan, so a read for it proves the selection still separates plans.
OTHER = "plan-named-by-another-brief"
#: A brief's digest, in the shape the record writer stores.
BRIEF = {"path": "/briefs/brief.md", "sha256": "0" * 64}
#: A stored review, so the scored read has something to select on.
REVIEW = {"status": "parsed", "scores": {"evidence": 18}, "absent": [], "total": 18}
#: The index projection the extraction moved away from.
EARLIER_INDEX_VERSION = 6
#: The stored record whose index entry the earlier extraction left planless.
STORED_RUN = "r-20260102T000001000-alro-stored"


def _promoted_brief_run(
    run_id: str, plan: str, *, node_definition: dict[str, str]
) -> dict[str, object]:
    """One run composed the way promotion composes it for a brief-carried node."""
    row = ledger.build_record(
        run_id=run_id,
        plan=plan,
        gate="passed",
        project=PROJECT,
        role="implement",
        section="s5",
        node="a-run-naming-its-plan",
        node_definition=node_definition,
        brief=BRIEF,
        dispatched_at="2026-01-02T00:00:00Z",
        completed_at="2026-01-02T00:10:00Z",
        worker_seconds=600,
        completed_at_source="provided",
    )
    # A measured run carrying a stored review, so the effort rollup and the
    # scored read both have a row to place.
    row["review"] = dict(REVIEW)
    return row


def _store(tmp_path: Path) -> Path:
    """A temporary store holding every row these cases read.

    The index resolves through the isolated configuration home the suite's own
    fixtures point at a temporary directory; nothing here names a real one.
    """
    root = tmp_path
    (root / "docs" / "state" / PROJECT / "runs").mkdir(parents=True)
    rows = [
        # Composed by promotion: a brief-carried run that names plan and section
        # on its node definition.
        _promoted_brief_run(
            "r-20260102T000000000-alro-named",
            NAMED,
            node_definition={"id": "alro-named", "plan": NAMED, "section": "s5"},
        ),
        # The stored shape: no top-level plan, no impl move, the definition is
        # the only place the plan appears.
        {
            "run_id": "r-20260102T000001000-alro-stored",
            "project": PROJECT,
            "node": "alro-stored",
            "role": "implement",
            "gate": "passed",
            "node_definition": {"id": "alro-stored", "plan": NAMED, "section": "s5"},
            "dispatched_at": "2026-01-02T00:00:00Z",
            "completed_at": "2026-01-02T00:10:00Z",
        },
        # A definition washed out by rotation: the durable impl move is the only
        # place the plan remains.
        {
            "run_id": "r-20260102T000002000-alro-washed",
            "project": PROJECT,
            "node": "alro-washed",
            "role": "implement",
            "gate": "passed",
            "node_definition": None,
            "impl_move": {
                "plan": NAMED,
                "at_dispatch": 0.0,
                "at_complete": None,
                "verdict": "moved",
                "reason": "impl advanced",
            },
            "dispatched_at": "2026-01-02T00:00:00Z",
            "completed_at": "2026-01-02T00:10:00Z",
        },
        # A brief-carried run whose node names no plan anywhere.
        _promoted_brief_run(
            "r-20260102T000003000-alro-unplanned",
            "",
            node_definition={"id": "alro-unplanned", "section": "s5"},
        ),
        # Another plan's brief-carried run, so a read for NAMED is compared
        # against a store that holds a second plan's rows too.
        _promoted_brief_run(
            "r-20260102T000004000-alro-other",
            OTHER,
            node_definition={"id": "alro-other", "plan": OTHER, "section": "s1"},
        ),
    ]
    for row in rows:
        ledger.write_run_file(PROJECT, row, root, fsync=False)
    return root


def _ids(root: Path, plan: str | None = None) -> set[str]:
    rows = ledger.runs(PROJECT, root, plan=plan)
    return {str(row["run_id"]) for row in rows}


def test_a_promoted_brief_run_naming_a_plan_is_returned_for_it(
    tmp_path: Path,
) -> None:
    """The run promotion writes for a brief-carried node answers its own plan."""
    root = _store(tmp_path)

    returned = _ids(root, NAMED)

    assert "r-20260102T000000000-alro-named" in returned, (
        "a promoted brief-carried run naming a plan was not returned by the "
        f"plan-selected read: {sorted(returned)}"
    )
    assert "r-20260102T000004000-alro-other" not in returned, (
        "the read returned another plan's run for this plan"
    )


def test_a_stored_record_naming_its_plan_on_its_definition_is_returned(
    tmp_path: Path,
) -> None:
    """A row already in the store is found by the plan its definition names."""
    root = _store(tmp_path)

    returned = _ids(root, NAMED)

    assert "r-20260102T000001000-alro-stored" in returned, (
        "a stored record whose node definition names the plan was not returned: "
        f"{sorted(returned)}"
    )


def test_a_washed_out_definition_keeps_the_plan_its_impl_move_names(
    tmp_path: Path,
) -> None:
    """Rotation drops the definition, so the durable impl move answers instead."""
    root = _store(tmp_path)

    returned = _ids(root, NAMED)

    assert "r-20260102T000002000-alro-washed" in returned, (
        "a record whose definition was rotated away was not returned from the "
        f"plan its impl move names: {sorted(returned)}"
    )


def test_a_brief_run_naming_no_plan_is_returned_for_no_plan(tmp_path: Path) -> None:
    """A run that names no plan is not claimed by a read naming some plan."""
    root = _store(tmp_path)

    every = _ids(root)
    # The instrument sees the row before an absence is reported: the fallback
    # must not turn "no plan" into "some plan".
    assert "r-20260102T000003000-alro-unplanned" in every, sorted(every)
    assert "r-20260102T000003000-alro-unplanned" not in _ids(root, NAMED)
    assert "r-20260102T000003000-alro-unplanned" not in _ids(root, OTHER)
    assert len(_ids(root, NAMED)) == 3, sorted(_ids(root, NAMED))
    assert len(_ids(root, OTHER)) == 1, sorted(_ids(root, OTHER))


def test_a_brief_carried_run_is_scored_under_the_plan_it_names(
    tmp_path: Path,
) -> None:
    """The scored read selects on the resolver's answer, not the top-level field."""
    root = _store(tmp_path)
    rows = ledger.runs(PROJECT, root)

    scored = ledger.review_scores(rows, plan=NAMED)

    ids = {entry["run_id"] for entry in scored}
    assert "r-20260102T000000000-alro-named" in ids, (
        "a brief-carried run carrying a review was not scored under its plan: "
        "the scored read selected on the row's own empty plan field"
    )
    assert ids and all(entry["plan"] == NAMED for entry in scored), scored
    # The row the no-plan bucket owns is the one naming no plan anywhere.
    unplanned = ledger.review_scores(rows, plan="")
    assert [entry["run_id"] for entry in unplanned] == [
        "r-20260102T000003000-alro-unplanned"
    ], unplanned


def test_a_brief_carried_run_is_rolled_up_under_the_plan_it_names(
    tmp_path: Path,
) -> None:
    """The effort rollup buckets each run by the plan the resolver answers."""
    root = _store(tmp_path)

    report = ledger.effort_report(PROJECT, root=root, declared={NAMED: "M"})

    rows = {row["plan"]: row for row in report["plans"]}
    assert rows[NAMED]["runs"] == 1, rows
    assert rows[NAMED]["declared_effort"] == "M", rows
    assert rows[""]["runs"] == 1, f"the run naming no plan left the bucket: {rows}"
    assert rows[OTHER]["runs"] == 1, rows


def test_a_brief_carried_run_appears_in_the_plans_index(tmp_path: Path) -> None:
    """The plans index names every plan the runs resolve to, and the empty one."""
    root = _store(tmp_path)

    report = ledger.summary(PROJECT, root=root, declared={NAMED: "M"})

    assert report["plans"] == sorted({"", NAMED, OTHER}), (
        f"the plans index did not name the brief-carried runs' plans: {report['plans']}"
    )


def _earlier_extraction(record: object) -> str:
    """The extraction before the resolver: the row's own field alone."""
    plan = record.get("plan") if isinstance(record, dict) else None
    return str(plan or "")


def test_a_row_indexed_under_the_earlier_extraction_is_read_for_its_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored row is re-extracted, so its plan reaches the index's query.

    An index built by the earlier extraction holds no plan for a row that names
    one only on its node definition. A warm read does not revisit that row, so
    nothing corrects the planless entry unless the projection version has moved
    and the stale index is dropped. The generation is pinned so the reuse
    decision turns on the version this case varies rather than on a counter
    another writer moves.
    """
    root = _store(tmp_path)
    current_plan = ledger._row_plan
    current_version = ledger._RUN_INDEX_VERSION
    monkeypatch.setattr(ledger, "ledger_generation", lambda project: "pinned")
    # Build the index the earlier extraction left behind: the row's own field
    # alone, under that extraction's own version.
    monkeypatch.setattr(ledger, "_row_plan", _earlier_extraction)
    monkeypatch.setattr(ledger, "_RUN_INDEX_VERSION", EARLIER_INDEX_VERSION)
    stale = _ids(root, NAMED)
    # The instrument sees the defect before the case reports its absence: the
    # stale entry carries no plan, so the row is missing from its plan's read.
    assert STORED_RUN not in stale, sorted(stale)

    monkeypatch.setattr(ledger, "_row_plan", current_plan)
    # The version is restored as it was found, so a tree that reverted it
    # reuses the planless entry and this case fails.
    monkeypatch.setattr(ledger, "_RUN_INDEX_VERSION", current_version)

    assert STORED_RUN in _ids(root, NAMED), (
        "a row indexed under the earlier extraction was not re-read for its plan"
    )
