"""A plan-selected store read asks the row owner, not the recorded column.

The store keeps a ``plan`` column; a later rotation can wash the detail a
brief-carried row's plan is read from out of the row, so the column outlives
the evidence it was written from. The selection therefore asks
:func:`reckon.ledger.row_plan` about the fields it reads — the row's own plan,
then the implementation move it reported, then the node definition it ran.

This test writes one row whose ``plan`` column is empty while its implementation
move names ``selected``, and asks the store for that plan by name, by identity,
and for a plan the row does not own.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import ledger, run_store

PROJECT = "owner"
OWNED = "selected"


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A crew home holding one row whose column and owner disagree."""
    crew_home = tmp_path / "crew-home"
    (crew_home / "crew").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(crew_home))
    location = str(crew_home / "crew" / "run_store.db")
    monkeypatch.setenv("RECKON_RUN_STORE", location)

    record = ledger.build_record(
        run_id="r-20260101T000000000000-owner",
        plan="",
        section="s5",
        node="owner-node",
        role="implement",
        gate="passed",
        commits=["0" * 40],
        completed_at="2026-01-01T00:00:10Z",
    )
    record["impl_move"] = {"plan": OWNED}
    identity, durable, detail = run_store._split_durable(record)
    with run_store.RunStore() as handle, handle._conn:
        handle._conn.execute(
            run_store._RUNS_INSERT,
            (
                identity,
                *run_store._query_values(durable, detail, project=PROJECT),
                None,
                json.dumps(durable),
            ),
        )
        handle._conn.execute(
            "INSERT INTO run_details (run_id, detail) VALUES (?, ?)",
            (identity, json.dumps(detail)),
        )
        # The column is the owner's answer at write time; a rotation can leave
        # the detail a brief-carried row's plan is read from as the only copy,
        # so the row keeps its detail and loses the recorded column.
        handle._conn.execute("UPDATE runs SET plan = '' WHERE run_id = ?", (identity,))
    return location


def test_a_plan_read_returns_a_row_its_owner_names(store: str) -> None:
    """The recorded column is empty, and the plan still reads its own row."""
    rows = run_store.stored_run_rows(PROJECT, path=store, plan=OWNED)
    assert rows is not None
    assert [row["run_id"] for row in rows] == ["r-20260101T000000000000-owner"]


def test_a_named_run_read_is_narrowed_by_the_owner(store: str) -> None:
    """The composition the landing audit uses: a named identity and a plan."""
    rows = run_store.stored_run_rows(
        PROJECT, path=store, plan=OWNED, run_ids=["r-20260101T000000000000-owner"]
    )
    assert rows is not None
    assert [row["run_id"] for row in rows] == ["r-20260101T000000000000-owner"]


def test_another_plan_does_not_read_an_owned_row(store: str) -> None:
    """A row whose owner answers another plan is not returned for this one."""
    rows = run_store.stored_run_rows(PROJECT, path=store, plan="other")
    assert rows == []


#: Every way a row can name a plan across the three fields the owner reads,
#: beside the plan the owner answers for it. The narrowing expression the store
#: uses to reach candidates without decoding every row restates that precedence
#: in SQL, so each combination is a case where the two statements must agree:
#: a narrowing that drops a row the owner names is a wrong answer.
OWNER_CASES = [
    ("", "", "", ""),
    ("", "", "node-def", "node-def"),
    ("", "impl-move", "node-def", "impl-move"),
    ("own", "impl-move", "node-def", "own"),
    ("  spaced  ", "", "", "spaced"),
    ("", "  spaced  ", "", "spaced"),
    ("", "", "  spaced  ", "spaced"),
    ("", "other", "node-def", "other"),
    ("other", "", "", "other"),
]


@pytest.fixture()
def owner_domain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """One row per combination of the three fields the owner reads."""
    crew_home = tmp_path_name(tmp_path)
    monkeypatch.setenv("RECKON_HOME", str(crew_home))
    location = str(crew_home / "crew" / "run_store.db")
    monkeypatch.setenv("RECKON_RUN_STORE", location)

    with run_store.RunStore() as handle, handle._conn:
        for index, (own, impl, node, _) in enumerate(OWNER_CASES):
            identity = f"r-20260101T0000{index:02d}00-owner"
            payload: dict[str, object] = {}
            detail: dict[str, object] = {}
            if own:
                payload["plan"] = own
            if impl:
                detail["impl_move"] = {"plan": impl}
            if node:
                detail["node_definition"] = {"plan": node}
            record = {
                "run_id": identity,
                "project": PROJECT,
                "role": "implement",
                "gate": "passed",
                "completed_at": "2026-01-01T00:00:10Z",
                **payload,
            }
            with_detail = {**record, **detail}
            _, durable, wide = run_store._split_durable(with_detail)
            handle._conn.execute(
                run_store._RUNS_INSERT,
                (
                    identity,
                    *run_store._query_values(durable, wide, project=PROJECT),
                    None,
                    json.dumps(durable),
                ),
            )
            handle._conn.execute(
                "INSERT INTO run_details (run_id, detail) VALUES (?, ?)",
                (identity, json.dumps(wide)),
            )
    return location


def tmp_path_name(tmp_path: Path) -> Path:
    crew_home = tmp_path / "crew-home"
    (crew_home / "crew").mkdir(parents=True, exist_ok=True)
    return crew_home


@pytest.mark.parametrize("plan", sorted({case[3] for case in OWNER_CASES} - {""}))
def test_the_narrowing_agrees_with_the_owner(owner_domain: str, plan: str) -> None:
    """Every plan read answers exactly the rows whose owner names that plan."""
    expected = [
        f"r-20260101T0000{index:02d}00-owner"
        for index, case in enumerate(OWNER_CASES)
        if case[3] == plan
    ]
    rows = run_store.stored_run_rows(
        PROJECT, path=owner_domain, plan=plan, fields=("run_id",)
    )
    assert rows is not None
    assert sorted(row["run_id"] for row in rows) == expected
