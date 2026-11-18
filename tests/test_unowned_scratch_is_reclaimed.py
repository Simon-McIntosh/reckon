"""Unowned scratch ages out while run ownership and process holders protect it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from reckon import flight
from reckon.crew import fleet_supervisor as supervisor
from reckon.crew import gc, landing_measure


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    root.mkdir()
    home = tmp_path / "home"
    state = tmp_path / "fleet"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(root))
    monkeypatch.setenv("FLEET_STATE_DIR", str(state))
    # Move the sweep clock forward so real ctimes age without sleeps or
    # metadata mocks. A recent descendant receives a matching future mtime.
    now = time.time() + 8000
    monkeypatch.setattr(gc.time, "time", lambda: now)
    monkeypatch.setattr(
        flight,
        "resolve",
        lambda: SimpleNamespace(
            config={"session_scratch": {"orphan_grace_seconds": 7200}}
        ),
    )
    return SimpleNamespace(root=root, home=home, state=state, now=now)


def entry(scratch, name):
    path = scratch.root / name
    path.mkdir()
    (path / "payload").write_bytes(name.encode())
    return path


def pointer(scratch, name):
    path = scratch.home / "crew" / "live" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": name}))
    return path


def contents(path):
    return {
        str(file.relative_to(path)): file.read_bytes()
        for file in path.rglob("*")
        if file.is_file()
    }


def test_pass_removes_only_two_old_unowned_entries_and_records_each(scratch):
    orphan = entry(scratch, "landing-abandoned")
    held = entry(scratch, "landing-held")
    young = entry(scratch, "landing-recent")
    live = entry(scratch, "live-run")
    ended = entry(scratch, "ended-run")
    pointer(scratch, live.name)
    pointer(scratch, ended.name).unlink()
    os.utime(young, (scratch.now - 60, scratch.now - 60))
    survivor_bytes = {path: contents(path) for path in (held, young, live)}
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time; print('ready',flush=True); sys.stdin.read()",
        ],
        cwd=held,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        assert holder.stdout.readline() == b"ready\n"
        report = supervisor.reclaim_unowned_scratch()
        assert set(report["removed"]) == {str(orphan), str(ended)}
        assert not orphan.exists() and not ended.exists()
        assert all(contents(path) == before for path, before in survivor_bytes.items())
        rows = [
            json.loads(line)
            for line in (scratch.state / "scratch-removals.jsonl")
            .read_text()
            .splitlines()
        ]
        assert {row["path"] for row in rows} == {str(orphan), str(ended)}
        assert sum(row["bytes"] for row in rows) == len(orphan.name) + len(ended.name)
        assert all(row["ctime_age_seconds"] >= 7200 and row["utc"] for row in rows)
        assert all("no live run or process" in row["reason"] for row in rows)
        assert supervisor.reclaim_unowned_scratch()["removed"] == []
        assert (
            len((scratch.state / "scratch-removals.jsonl").read_text().splitlines())
            == 2
        )
    finally:
        holder.communicate(timeout=5)


def test_recent_descendant_write_protects_an_old_tree(scratch):
    path = entry(scratch, "landing-nested")
    payload = path / "payload"
    os.utime(payload, (scratch.now - 10, scratch.now - 10))
    before = contents(path)
    report = supervisor.reclaim_unowned_scratch()
    assert report["removed"] == []
    assert report["entries"][0]["withheld"] == "descendant touched within grace"
    assert contents(path) == before


def test_open_measurement_descriptor_protects_a_parent_waiting_elsewhere(
    scratch, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        landing_measure, "_git", lambda *args, **kwargs: SimpleNamespace(returncode=0)
    )
    monkeypatch.setattr(landing_measure, "_link_environment", lambda *args: None)
    # The measuring process's cwd stays outside the base tree throughout.
    with landing_measure.base_checkout(tmp_path, "a" * 40) as tree:
        path = tree.parent
        assert Path.cwd() != tree
        report = supervisor.reclaim_unowned_scratch()
        assert report["removed"] == []
        assert "held by process" in report["entries"][0]["withheld"]
        assert (path / ".measurement-owner").is_file()
    assert not path.exists()


def test_grace_setting_changes_the_retirement_time(scratch, monkeypatch):
    path = entry(scratch, "landing-younger-than-configured-grace")
    monkeypatch.setattr(
        flight,
        "resolve",
        lambda: SimpleNamespace(
            config={"session_scratch": {"orphan_grace_seconds": 9000}}
        ),
    )
    assert supervisor.reclaim_unowned_scratch()["removed"] == []
    assert path.exists()


def test_shipped_grace_and_generated_schema_declare_two_hours():
    root = Path(__file__).resolve().parents[1]
    defaults = yaml.safe_load((root / "reckon/schema/flight-defaults.yaml").read_text())
    assert defaults["session_scratch"]["orphan_grace_seconds"] == 2 * 60 * 60
    from reckon._flight_schema import SessionScratchConfig

    assert "orphan_grace_seconds" in SessionScratchConfig.model_fields
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        SessionScratchConfig(orphan_grace_seconds=0)


def test_disk_sampler_runs_sweep_and_pressure_separates_live_and_unowned(
    scratch, monkeypatch, capsys
):
    orphan = entry(scratch, "landing-orphan")
    young = entry(scratch, "landing-recent")
    live = entry(scratch, "live-run")
    pointer(scratch, live.name)
    os.utime(young, (scratch.now - 60, scratch.now - 60))
    monkeypatch.setenv(supervisor.NODE_DISK_ROOT_ENV, str(scratch.root))
    monkeypatch.setattr(supervisor, "node_disk_reserve_bytes", lambda: 10**18)
    assert supervisor.sample_node_disk() == 0
    assert not orphan.exists()
    sample = json.loads((scratch.state / supervisor.NODE_DISK_RECORD).read_text())
    reason = supervisor.node_disk_pressure(sample)["reason"]
    assert f"live crew scratch: {live}" in reason
    assert f"unowned crew scratch: {young}" in reason
    assert str(orphan) not in reason
    assert len(capsys.readouterr().out.split()) == 4


def test_unreadable_activity_is_withheld_and_reported(scratch, monkeypatch):
    path = entry(scratch, "landing-unreadable")

    def unreadable(*args):
        raise PermissionError("decoy unreadable descendant")

    monkeypatch.setattr(gc, "_scratch_activity", unreadable)
    report = supervisor.reclaim_unowned_scratch()
    assert report["removed"] == []
    assert "activity unreadable" in report["entries"][0]["withheld"]
    assert path.is_dir()


def test_recent_write_during_sizing_prevents_removal(scratch, monkeypatch):
    path = entry(scratch, "landing-written-during-census")
    real_activity = gc._scratch_activity
    calls = 0

    def activity(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            os.utime(path / "payload", (scratch.now, scratch.now))
        return real_activity(*args)

    monkeypatch.setattr(gc, "_scratch_activity", activity)
    report = supervisor.reclaim_unowned_scratch()
    assert report["removed"] == []
    assert report["entries"][0]["withheld"] == "descendant refreshed during sweep"
    assert path.exists()


def test_symlinks_never_remove_their_target(scratch, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload").write_bytes(b"protected")
    (scratch.root / "linked").symlink_to(outside, target_is_directory=True)
    assert supervisor.reclaim_unowned_scratch()["removed"] == []
    assert (outside / "payload").read_bytes() == b"protected"
