"""Copied control state admits writes without permitting a real-home write."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "control_profile", ROOT / "scripts/profile_control_pass.py"
)
profile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profile)


@pytest.fixture()
def copied(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "crew").mkdir(parents=True)
    (source / "mounts.json").write_text("{}")
    (source / "crew" / "known.json").write_text(
        json.dumps({"path": str(source / "crew" / "known.json")})
    )
    monkeypatch.setenv("RECKON_PROFILE_REAL_HOME", str(source))
    monkeypatch.setenv("RECKON_PROFILE_SCRATCH", str(tmp_path))
    destination = tmp_path / "copy"
    receipt = profile.prepare(source, destination)
    return source, destination, receipt


@pytest.mark.parametrize("alias", ["same", "child", "parent", "symlink"])
def test_a_real_home_is_refused_before_its_first_write(tmp_path, monkeypatch, alias):
    source = tmp_path / "source"
    source.mkdir()
    (source / "known").write_text("unchanged")
    monkeypatch.setenv("RECKON_PROFILE_REAL_HOME", str(source))
    destination = {"same": source, "child": source / "child", "parent": tmp_path}.get(
        alias
    )
    if alias == "symlink":
        destination = tmp_path / "alias"
        destination.symlink_to(source, target_is_directory=True)
    before = profile.inventory(source)
    with pytest.raises(profile.UnsafeProfileError, match="real crew home"):
        profile.require_copy(destination, source)
    assert profile.inventory(source) == before
    assert before["known"]["bytes"] > 0


def test_the_cli_refuses_the_real_home_before_calling_a_writer(tmp_path, monkeypatch):
    source = tmp_path / "real"
    source.mkdir()
    monkeypatch.setenv("RECKON_PROFILE_REAL_HOME", str(source))
    calls = []
    monkeypatch.setattr(profile, "prepare", lambda *args: calls.append(args) or {})
    assert (
        profile.main(["prepare", "--source-home", str(source), "--home", str(source)])
        == 2
    )
    assert calls == []


def test_a_copy_has_a_digest_and_remaps_its_own_paths(copied):
    source, destination, receipt = copied
    assert receipt["source_unchanged_during_copy"] is True
    assert receipt["input_digest"] == profile.input_digest(receipt["input_listing"])
    assert receipt["source_before"] == profile.inventory(source)
    assert json.loads((destination / "crew" / "known.json").read_text())["path"] == str(
        destination / "crew" / "known.json"
    )
    assert (destination / ".profile-copy.json").is_file()


def test_external_symlinks_are_preserved_and_never_followed(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("real bytes")
    (source / "link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("RECKON_PROFILE_REAL_HOME", str(source))
    monkeypatch.setenv("RECKON_PROFILE_SCRATCH", str(tmp_path))
    destination = tmp_path / "copy"
    profile.prepare(source, destination)
    assert (destination / "link").is_symlink()
    assert profile.inventory(source) == {"link": {"link": str(outside)}}


def test_a_store_counter_can_see_known_rows_and_bytes(tmp_path):
    database = tmp_path / "known.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE items (payload TEXT)")
        connection.executemany("INSERT INTO items VALUES (?)", [("alpha",), ("beta",)])
    counter = profile.StoreReads()
    with counter.connect(database) as connection:
        assert connection.execute("SELECT payload FROM items").fetchall() == [
            ("alpha",),
            ("beta",),
        ]
        assert list(connection.execute("SELECT payload FROM items")) == [
            ("alpha",),
            ("beta",),
        ]
        cursor = connection.execute("SELECT payload FROM items")
        assert cursor.fetchone() == ("alpha",)
        assert cursor.fetchmany(10) == [("beta",)]
        assert cursor.fetchone() is None
    assert counter.stores[str(database)]["rows_returned"] == 6
    assert counter.stores[str(database)]["returned_bytes"] == 27


def test_decoded_rows_are_counted_in_their_own_store(tmp_path):
    from reckon import run_store

    database = tmp_path / "runs.db"
    with run_store.RunStore(database) as store:
        store.append(
            "fixture", {"run_id": "known", "node": "known", "status": "complete"}
        )
    counter = profile.StoreReads()
    with counter.measure():
        rows = run_store.stored_run_rows("fixture", path=database)
    assert rows[0]["run_id"] == "known"
    assert counter.stores[str(database)]["rows_decoded"] == 1
    assert counter.stores[str(database)]["decoded_bytes"] > 0


def test_a_pass_requires_the_filesystem_fence(copied, monkeypatch):
    _, destination, _ = copied
    monkeypatch.delenv("RECKON_PROFILE_FENCED", raising=False)
    with pytest.raises(profile.UnsafeProfileError, match="filesystem fence"):
        profile.profile(
            "admission", home=destination, batch=None, entries=1, repository=ROOT
        )


def test_profile_records_memory_allocations_and_failure_receipts(copied, monkeypatch):
    _, destination, receipt = copied
    monkeypatch.setenv("RECKON_PROFILE_FENCED", "1")
    held = []

    def action(*args, **kwargs):
        held.append(bytearray(200_000))
        return {"known": True}

    monkeypatch.setattr(profile, "_pass", action)
    record = profile.profile(
        "admission", home=destination, batch=None, entries=1, repository=ROOT
    )
    assert record["exit_status"] == 0
    assert record["input_digest"] == receipt["input_digest"]
    assert record["peak_resident_bytes"] > 0
    assert record["traced_peak_bytes"] >= 200_000
    assert 1 <= len(record["largest_allocation_sites"]) <= 10
    assert record["largest_allocation_sites"][0]["bytes"] > 0

    def refuse(*args, **kwargs):
        raise PermissionError("blocked write")

    monkeypatch.setattr(profile, "_pass", refuse)
    record = profile.profile(
        "station", home=destination, batch=None, entries=1, repository=ROOT
    )
    assert record["exit_status"] == 1
    assert "blocked write" in record["error"]


@pytest.mark.parametrize("command", ["admission", "station"])
def test_actual_single_pass_finishes_and_keeps_source_unchanged(
    copied, monkeypatch, command
):
    from reckon.crew import admission

    source, destination, receipt = copied
    monkeypatch.setenv("RECKON_HOME", str(destination))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(destination / "mounts.json"))
    monkeypatch.setenv("RECKON_RUN_STORE", str(destination / "crew" / "run_store.db"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(destination / ".profile-runtime"))
    # No queued requests: a real selection pass still takes its copy's lock.
    result = profile._pass(
        command, home=destination, batch=None, entries=1, repository=ROOT
    )
    assert isinstance(result, dict)
    assert profile.inventory(source) == receipt["source_before"]
    if command == "station":
        assert (destination / ".profile-runtime" / "station.sqlite3").exists()
    else:
        assert admission is not None
        assert list((destination / "crew" / "locks").iterdir())


@pytest.mark.parametrize("entries", [1, 8])
def test_dispatch_stores_every_entry(copied, tmp_path, monkeypatch, entries):
    from reckon import flight
    from tests.test_a_batch_dispatch_reads_each_plan_once import (
        _checkout,
        _config,
        _entries,
    )

    source, destination, receipt = copied
    plans = ("alpha", "beta")
    sections = ("s1", "s2", "s3", "s4")
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    repository = _checkout(fixture, monkeypatch, plans, sections, {})
    monkeypatch.setenv("RECKON_HOME", str(destination))
    monkeypatch.setenv("RECKON_RUN_STORE", str(destination / "crew" / "run_store.db"))
    monkeypatch.setattr(
        flight, "resolve", lambda *args, **kwargs: SimpleNamespace(config=_config())
    )
    batch = tmp_path / "batch.json"
    batch.write_text(json.dumps(_entries(plans, sections)))
    result = profile._pass(
        "dispatch",
        home=destination,
        batch=batch,
        entries=entries,
        repository=repository,
    )
    assert result["queued"] == entries
    assert result["refused"] == 0
    assert profile.inventory(source) == receipt["source_before"]


def test_the_filesystem_fence_refuses_an_external_write(copied, tmp_path):
    source, destination, receipt = copied
    code = (
        "from pathlib import Path; Path("
        + repr(str(source / "forbidden"))
        + ").write_text('no')"
    )
    command = profile.fenced_command(destination, [])
    command[command.index("--") + 1 :] = [sys.executable, "-c", code]
    result = subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=15
    )
    assert result.returncode != 0
    assert "Read-only file system" in result.stderr
    assert profile.inventory(source) == receipt["source_before"]
    # Positive control: the same fence permits the copied state to be written.
    command[-1] = (
        "from pathlib import Path; Path("
        + repr(str(destination / "allowed"))
        + ").write_text('yes')"
    )
    result = subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=15
    )
    assert result.returncode == 0, result.stderr
    assert (destination / "allowed").read_text() == "yes"


@pytest.mark.parametrize("command", ["admission", "station"])
def test_the_cli_completes_a_fenced_pass_and_reuses_the_snapshot(copied, command):
    source, destination, receipt = copied
    initial = profile.inventory(destination)
    arguments = [
        sys.executable,
        str(ROOT / "scripts/profile_control_pass.py"),
        command,
        "--source-home",
        str(source),
        "--home",
        str(destination),
    ]
    result = subprocess.run(
        arguments, capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stderr + result.stdout
    record = json.loads((destination / f"profile-{command}-1.json").read_text())
    assert record["exit_status"] == 0
    assert record["input_digest"] == receipt["input_digest"]
    assert record["peak_resident_bytes"] > 0
    assert profile.inventory(source) == receipt["source_before"]
    for name, value in initial.items():
        assert profile.inventory(destination)[name] == value
    assert not (destination / ".profile-runtime").exists()


def test_a_missing_source_is_refused_before_starting_a_pass(copied, monkeypatch):
    source, destination, _ = copied
    source.rename(source.with_name("moved-source"))
    calls = []
    monkeypatch.setattr(
        profile.subprocess,
        "run",
        lambda *args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0),
    )
    assert (
        profile.main(
            ["admission", "--source-home", str(source), "--home", str(destination)]
        )
        == 2
    )
    assert calls == []


def test_a_changed_snapshot_is_refused_before_starting_a_pass(copied, monkeypatch):
    source, destination, _ = copied
    (destination / "crew" / "known.json").write_text('{"changed": true}')
    calls = []
    monkeypatch.setattr(
        profile.subprocess,
        "run",
        lambda *args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0),
    )
    assert (
        profile.main(
            ["admission", "--source-home", str(source), "--home", str(destination)]
        )
        == 2
    )
    assert calls == []


def test_sqlite_can_spill_to_private_disk_inside_the_fence(copied):
    source, destination, receipt = copied
    code = f"""
import importlib.util, json, sqlite3, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('control_profile', {str(ROOT / "scripts/profile_control_pass.py")!r})
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)
def spill(*args, **kwargs):
    with sqlite3.connect(':memory:') as connection:
        connection.execute('PRAGMA temp_store=FILE')
        connection.execute('CREATE TEMP TABLE items(payload BLOB)')
        connection.executemany('INSERT INTO items VALUES (zeroblob(1048576))', [()] * 32)
        return {{'bytes': connection.execute('SELECT SUM(length(payload)) FROM items').fetchone()[0]}}
profile._pass = spill
record = profile.profile('admission', home=Path({str(destination)!r}), batch=None, entries=1, repository=Path({str(ROOT)!r}))
print(json.dumps(record))
sys.exit(record['exit_status'])
"""  # noqa: S608 - only Python path literals interpolate; SQL is static
    command = profile.fenced_command(destination, [])
    command[command.index("--") + 1 :] = [sys.executable, "-c", code]
    result = subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stderr + result.stdout
    record = json.loads(result.stdout)
    assert record["result"]["bytes"] == 32 * 1024 * 1024
    assert profile.inventory(source) == receipt["source_before"]
