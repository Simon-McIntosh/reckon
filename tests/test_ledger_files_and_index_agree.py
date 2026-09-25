"""Committed rows answer identically across placements and cache lifetimes."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from reckon import ledger, run_store
from reckon.crew import carryover_census, death_census

PROJECT = "example"
READERS = (
    "load",
    "runs",
    "records",
    "get_run",
    "get_detail",
    "durable_rows",
    "death",
    "carryover",
)


@pytest.fixture()
def corpus(tmp_path, monkeypatch):
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv("RECKON_STATE_ROOT", raising=False)
    monkeypatch.delenv("RECKON_MOUNTS_PATH", raising=False)
    monkeypatch.setenv("RECKON_RUN_STORE", str(home / "elsewhere" / "index.db"))
    rows = [
        {
            "run_id": f"r-{name}",
            "member": f"worker-{name}",
            "role": "implement",
            "agent": {
                "backend": "local",
                "effort": "high",
                "sandbox": "workspace-write",
            },
            "node_definition": {"lane_declaration": {"backend": "local"}},
            "outcome": f"completed {name}",
            "store_write": {"status": "written"},
        }
        for name in ("alpha", "beta", "gamma")
    ]
    return home, rows


def place_rows(rows, placement):
    path = ledger.ledger_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = {"aggregate": len(rows), "files": 0, "split": 1}[placement]
    path.write_text(
        json.dumps(
            {
                "data": {
                    "_version": 7,
                    "members": [{"id": "member"}],
                    "runs": rows[:count],
                    "holds": [{"id": "hold"}],
                }
            }
        )
    )
    directory = path.parent / "runs"
    directory.mkdir()
    for row in rows[count:]:
        (directory / f"{row['run_id']}.json").write_text(
            json.dumps(row, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        )
    return path


def prepare_index(rows, state):
    if state == "rebuilt":
        run_store.import_ledger(PROJECT)
        return
    path = run_store.store_path()
    with run_store.RunStore(path) as store:
        for row in rows:
            store.append(PROJECT, row)
    if state == "deleted":
        path.unlink()


def answer(reader, home, rows):
    if reader == "load":
        data, version = ledger.load(PROJECT)
        assert version == 7
        assert data["members"] == [{"id": "member"}]
        assert data["holds"] == [{"id": "hold"}]
        return data["runs"]
    if reader == "runs":
        return ledger.runs(PROJECT)
    if reader == "records":
        return ledger.read_records(PROJECT, with_figures=False)[0]
    if reader == "death":
        return death_census.recorded_runs(run_store.store_path())
    if reader == "carryover":
        return carryover_census.recorded_dispatch_metadata(home / "crew")
    with run_store.RunStore() as store:
        if reader == "get_run":
            return {row["run_id"]: store.get_run(row["run_id"]) for row in rows}
        if reader == "get_detail":
            return {row["run_id"]: store.get_detail(row["run_id"]) for row in rows}
        return store.durable_rows(PROJECT)


def expected(reader, rows):
    if reader in {"load", "runs", "records"}:
        return rows
    if reader == "death":
        return {row["run_id"]: {**row["agent"], "role": row["role"]} for row in rows}
    if reader == "carryover":
        return (
            {row["run_id"]: "local" for row in rows},
            {row["run_id"]: row["member"] for row in rows},
        )
    if reader == "get_detail":
        return {
            row["run_id"]: {"node_definition": row["node_definition"]} for row in rows
        }
    return {
        row["run_id"]: {
            **{
                key: value
                for key, value in row.items()
                if key not in (*run_store.DETAIL_FIELDS, "store_write")
            },
            "project": PROJECT,
        }
        for row in rows
    }


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("placement", ["aggregate", "files", "split"])
@pytest.mark.parametrize("index_state", ["present", "deleted", "rebuilt"])
def test_readers_agree(corpus, reader, placement, index_state):
    home, rows = corpus
    place_rows(rows, placement)
    prepare_index(rows, index_state)
    assert answer(reader, home, rows) == expected(reader, rows)
    if reader not in {"load", "runs", "records"}:
        with sqlite3.connect(run_store.store_path()) as connection:
            assert {
                row[0] for row in connection.execute("SELECT run_id FROM runs")
            } == {row["run_id"] for row in rows}


def test_identical_duplicate_is_read_once(corpus, monkeypatch):
    _, rows = corpus
    path = place_rows(rows, "aggregate")
    (path.parent / "runs" / f"{rows[0]['run_id']}.json").write_text(
        json.dumps(dict(reversed(list(rows[0].items()))))
    )
    original_read = Path.read_text
    reads = []

    def read_once(source, *args, **kwargs):
        if source.parent.name == "runs":
            reads.append(source)
        return original_read(source, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_once)
    assert ledger.load(PROJECT)[0]["runs"] == rows
    assert reads == [path.parent / "runs" / f"{rows[0]['run_id']}.json"]


def test_differing_duplicate_refuses_the_whole_read(corpus):
    _, rows = corpus
    path = place_rows(rows, "aggregate")
    other = path.parent / "runs" / f"{rows[0]['run_id']}.json"
    other.write_text(json.dumps({**rows[0], "outcome": "different"}))
    with pytest.raises(ledger.LedgerError) as caught:
        ledger.load(PROJECT)
    message = str(caught.value)
    assert rows[0]["run_id"] in message
    assert str(path) in message
    assert str(other) in message
    print(message)


@pytest.mark.parametrize(
    "reader", ["get_run", "get_detail", "durable_rows", "death", "carryover"]
)
@pytest.mark.parametrize("disagreement", ["missing", "extra", "same_count"])
def test_index_membership_is_rebuilt(corpus, reader, disagreement):
    home, rows = corpus
    place_rows(rows, "files")
    indexed = rows[1:] if disagreement in {"missing", "same_count"} else rows
    with run_store.RunStore() as store:
        for row in indexed:
            store.append(PROJECT, row)
        if disagreement in {"extra", "same_count"}:
            store.append(PROJECT, {"run_id": "r-ghost"})
        store.append("unrelated", {"run_id": "r-elsewhere"})
        store.stamp_refusal("lane", "2020-01-01T00:00:00Z", "2020-01-01T01:00:00Z")
    wanted = expected(reader, rows)
    if reader == "death":
        wanted["r-elsewhere"] = dict.fromkeys(("backend", "effort", "sandbox", "role"))
    assert answer(reader, home, rows) == wanted
    with run_store.RunStore() as store:
        assert store.durable_rows(PROJECT, refresh=False) == expected(
            "durable_rows", rows
        )
        assert store.get_run("r-elsewhere")["project"] == "unrelated"
        assert store.get_run("r-ghost") is None
        assert len(store.refusal_stamps()) == 1


@pytest.mark.parametrize("aggregate_state", ["absent", "no_runs_key"])
def test_run_files_survive_without_an_aggregate_run_list(corpus, aggregate_state):
    _, rows = corpus
    path = place_rows(rows, "files")
    if aggregate_state == "absent":
        path.unlink()
    else:
        envelope = json.loads(path.read_text())
        del envelope["data"]["runs"]
        path.write_text(json.dumps(envelope))
    data, version = ledger.load(PROJECT)
    assert data["runs"] == rows
    assert version == (0 if aggregate_state == "absent" else 7)
    with run_store.RunStore() as store:
        assert (
            store.get_run(rows[0]["run_id"])
            == expected("get_run", rows)[rows[0]["run_id"]]
        )


@pytest.mark.parametrize("payload", ["not-json", "[]", '{"run_id": "r-wrong"}'])
def test_bad_run_file_is_not_silently_omitted(corpus, payload):
    _, rows = corpus
    path = place_rows(rows, "files").parent / "runs" / f"{rows[0]['run_id']}.json"
    path.write_text(payload)
    with pytest.raises(ledger.LedgerError, match=rows[0]["run_id"]):
        ledger.load(PROJECT)


def test_serialisation_is_stable_and_keeps_every_field():
    record = {
        "z": {"detail": [None, True, 3.5]},
        "a": "é",
        "store_write": {"status": "failed"},
    }
    assert (
        ledger.serialize_run(record)
        == json.dumps(record, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    )
    assert ledger.serialize_run(record) == ledger.serialize_run(
        dict(reversed(list(record.items())))
    )


def test_matching_index_uses_the_cached_payload(corpus, monkeypatch):
    _, rows = corpus
    place_rows(rows, "files")
    prepare_index(rows, "present")

    def no_rebuild(*args, **kwargs):
        pytest.fail("matching membership must not decode every run file")

    monkeypatch.setattr(ledger, "load", no_rebuild)
    with run_store.RunStore() as store:
        assert (
            store.get_run(rows[0]["run_id"])
            == expected("get_run", rows)[rows[0]["run_id"]]
        )


def test_rebuild_failure_still_answers_from_files(corpus, caplog):
    _, rows = corpus
    place_rows(rows, "files")
    with run_store.RunStore() as store:
        store._conn.execute(
            "CREATE TRIGGER refuse_cache BEFORE INSERT ON runs BEGIN SELECT RAISE(ABORT, 'cache refused'); END"
        )
        assert (
            store.get_run(rows[0]["run_id"])
            == expected("get_run", rows)[rows[0]["run_id"]]
        )
        assert (
            store.get_detail(rows[1]["run_id"])
            == expected("get_detail", rows)[rows[1]["run_id"]]
        )
        assert store.durable_rows(PROJECT) == expected("durable_rows", rows)
        assert store.durable_rows(PROJECT, refresh=False) == {}
    assert "cache refused" in caplog.text


def test_carryover_live_pointer_wins(corpus):
    home, rows = corpus
    place_rows(rows, "files")
    pointer = home / "crew" / "live" / f"{rows[0]['run_id']}.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(
        json.dumps(
            {
                "run_id": rows[0]["run_id"],
                "backend": "live-lane",
                "member": "live-member",
            }
        )
    )
    backends, members = carryover_census.recorded_dispatch_metadata(home / "crew")
    assert backends[rows[0]["run_id"]] == "live-lane"
    assert members[rows[0]["run_id"]] == "live-member"
    assert len(backends) == len(members) == len(rows)


@pytest.mark.parametrize("routing", ["state_symlink", "mount", "explicit_root"])
def test_index_resolves_the_owning_checkout(corpus, tmp_path, routing):
    home, rows = corpus
    path = place_rows(rows, "files")
    checkout = tmp_path / "checkout"
    state = checkout / "docs" / "state" / PROJECT
    state.parent.mkdir(parents=True)
    path.parent.rename(state)
    root = None
    if routing == "state_symlink":
        path.parent.symlink_to(state, target_is_directory=True)
    elif routing == "mount":
        (home / "mounts.json").write_text(
            json.dumps({"mounts": {PROJECT: str(checkout / "docs")}})
        )
    else:
        root = checkout
    with run_store.RunStore(root=root) as store:
        assert (
            store.get_run(rows[0]["run_id"])
            == expected("get_run", rows)[rows[0]["run_id"]]
        )
    assert (
        ledger.run_path(PROJECT, rows[0]["run_id"], checkout)
        == state / "runs" / f"{rows[0]['run_id']}.json"
    )


@pytest.mark.parametrize("placement", ["aggregate", "files", "split"])
def test_promotion_order_survives_a_split(corpus, placement):
    _, rows = corpus
    for day, row in enumerate(reversed(rows), 1):
        row["completed_at"] = f"2020-01-{day:02d}T00:00:00Z"
    rows.reverse()
    place_rows(rows, placement)
    assert ledger.runs(PROJECT) == rows
    assert ledger.runs(PROJECT, limit=1) == rows[-1:]
