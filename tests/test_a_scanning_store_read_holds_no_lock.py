"""Selection work on stored history must leave a concurrent writer free."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime

import pytest

from reckon import ledger, run_store


@pytest.fixture(scope="module")
def population(tmp_path_factory):
    path = tmp_path_factory.mktemp("scanning-store") / "runs.db"
    records = []
    for index in range(20_000):
        plan = f"plan-{index % 100}"
        row = {
            "run_id": f"run-{index:05d}",
            "project": "example",
            "outcome": f"full-payload-{index}",
            "launched_at": "2020-01-01T00:00:00Z",
            "created_at": "malformed",
            "dispatched_at": None,
            "completed_at": "2025-01-01T00:00:00Z" if index % 100 == 0 else None,
        }
        if index % 3 == 0:
            row["plan"] = f" {plan} "
            row["impl_move"] = {"plan": "ignored"}
        elif index % 3 == 1:
            row["impl_move"] = {"plan": plan}
            row["node_definition"] = {"plan": "ignored"}
        else:
            row["node_definition"] = {"plan": plan}
        records.append(row)
    with run_store.RunStore(path) as store:
        with store._conn:
            for row in records:
                identity, durable, detail = run_store._split_durable("example", row)
                store._conn.execute(
                    run_store._RUNS_INSERT,
                    (
                        identity,
                        *(durable.get(key) for key in run_store.QUERY_KEYS),
                        None,
                        json.dumps(durable),
                    ),
                )
                store._conn.execute(
                    "INSERT INTO run_details VALUES (?, ?)",
                    (identity, json.dumps(detail)),
                )
        assert store._conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 20_000
        assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    return path, records


def _selection(kind):
    if kind == "plan":
        return {"plan": "plan-0"}
    return {"activity_since": datetime(2024, 1, 1, tzinfo=UTC).timestamp()}


@pytest.mark.parametrize("kind", ["activity", "plan"])
def test_a_record_write_completes_while_selection_is_paused(
    tmp_path, monkeypatch, population, kind
):
    source, _ = population
    path = tmp_path / "runs.db"
    shutil.copyfile(source, path)
    kwargs = _selection(kind)
    expected = run_store.stored_run_rows("example", path=path, **kwargs)
    assert len(expected) == 200
    selecting = threading.Event()
    release = threading.Event()
    original = ledger.row_plan if kind == "plan" else ledger.parse_utc

    def paused(value):
        if not selecting.is_set():
            selecting.set()
            assert release.wait(15), "selection was not released"
        return original(value)

    if kind == "plan":
        monkeypatch.setattr(ledger, "row_plan", paused)
    else:
        monkeypatch.setattr(run_store, "parse_utc", paused, raising=False)
        monkeypatch.setattr(ledger, "parse_utc", paused)
    answer, errors = [], []

    def read():
        try:
            answer.extend(run_store.stored_run_rows("example", path=path, **kwargs))
        except Exception as error:  # noqa: BLE001 - thread errors asserted by caller
            errors.append(error)

    thread = threading.Thread(target=read)
    thread.start()
    try:
        assert selecting.wait(10), "selection instrument did not observe a row"
        started = time.monotonic()
        write = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json, sys, time; from reckon import run_store; "
                    "start = time.monotonic(); "
                    "store = run_store.RunStore(sys.argv[1], busy_timeout=1.1); "
                    "store.append('example', {'run_id': 'concurrent-write'}); "
                    "row = store.get_run('concurrent-write'); store.close(); "
                    "print(json.dumps({'row': row, 'seconds': time.monotonic()-start}))"
                ),
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        elapsed = time.monotonic() - started
        release.wait(max(0, 3.0 - elapsed))
        assert thread.is_alive(), "selection finished before the writer receipt"
        assert write.returncode == 0, (
            f"write waiting past one second during {kind} selection: {write.stderr}"
        )
        receipt = json.loads(write.stdout)
        assert receipt["row"]["run_id"] == "concurrent-write"
        assert receipt["seconds"] < 1.0, receipt
        assert elapsed < 1.0, elapsed
        print(f"{kind}: 20000 rows, write {elapsed:.3f}s, selection held >=3s")
    finally:
        release.set()
        thread.join(timeout=15)
    assert not thread.is_alive()
    assert not errors
    assert answer == expected
    with run_store.RunStore(path) as store:
        assert store.get_run("concurrent-write")["run_id"] == "concurrent-write"
        assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


@pytest.mark.parametrize("fields", [None, ("run_id",), ("run_id", "outcome")])
@pytest.mark.parametrize("kind", ["activity", "plan", "both"])
def test_scoped_reads_keep_fallbacks_and_the_last_activity_stamp(
    population, fields, kind
):
    path, records = population
    kwargs = _selection("activity" if kind == "both" else kind)
    if kind == "both":
        kwargs.update(_selection("plan"))
    expected = [row for row in records if int(row["run_id"].split("-")[1]) % 100 == 0]
    assert len(expected) == 200
    assert {index % 3 for index in range(0, 20_000, 100)} == {0, 1, 2}
    if fields is not None:
        expected = [{key: row[key] for key in fields if key in row} for row in expected]
    assert (
        run_store.stored_run_rows("example", path=path, fields=fields, **kwargs)
        == expected
    )


def test_selection_keeps_scan_order_across_identity_batches_and_archives(
    tmp_path, population
):
    source, _ = population
    path = tmp_path / "runs.db"
    shutil.copyfile(source, path)
    with run_store.RunStore(path) as store:
        for identity in ("z-last", "a-last"):
            store.append(
                "example",
                {
                    "run_id": identity,
                    "plan": "plan-0",
                    "completed_at": "2025-01-01T00:00:00Z",
                },
            )
        retired = store.retire(
            before=datetime(2026, 1, 1, tzinfo=UTC),
            protected={"z-last", "a-last"},
            projects={"example"},
            limit=3,
            apply=True,
        )
        assert len(retired["retired"]) == 3
    unfiltered = run_store.stored_run_rows("example", path=path)
    for selection in (
        {"activity_since": datetime(2020, 1, 1, tzinfo=UTC).timestamp()},
        {"plan": "plan-0"},
    ):
        expected = [
            row
            for row in unfiltered
            if not selection.get("plan") or ledger.row_plan(row) == selection["plan"]
        ]
        assert run_store.stored_run_rows("example", path=path, **selection) == expected
        assert [row["run_id"] for row in expected[-3:]] == retired["retired"]
    bounded = {row["run_id"] for row in unfiltered if ledger.row_plan(row) == "plan-0"}
    assert run_store.stored_run_rows(
        "example", path=path, run_ids=bounded, plan="plan-0", fields=("run_id",)
    ) == run_store.stored_run_rows(
        "example", path=path, run_ids=bounded, fields=("run_id",)
    )
    assert (
        run_store.stored_run_rows(
            "example", path=path, plan="absent", fields=("run_id",)
        )
        == []
    )
