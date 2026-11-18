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
        live_row = next(row for row in report["entries"] if row["path"] == str(live))
        assert live_row["bytes"] is None  # An unmeasured live tree is not empty.
        rows = [
            json.loads(line)
            for line in (scratch.state / "scratch-removals.jsonl")
            .read_text()
            .splitlines()
        ]
        assert {row["path"] for row in rows} == {str(orphan), str(ended)}
        assert sum(row["bytes"] for row in rows if row["state"] == "complete") == len(
            orphan.name
        ) + len(ended.name)
        assert [row["state"] for row in rows] == [
            "intent",
            "complete",
            "intent",
            "complete",
        ]
        assert all(row["ctime_age_seconds"] >= 7200 and row["utc"] for row in rows)
        assert all("no live run or process" in row["reason"] for row in rows)
        assert supervisor.reclaim_unowned_scratch()["removed"] == []
        assert (
            len((scratch.state / "scratch-removals.jsonl").read_text().splitlines())
            == 4
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
    assert report["entries"][0]["withheld"] == "descendant refreshed after intent"
    assert path.exists()


def test_symlinks_never_remove_their_target(scratch, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload").write_bytes(b"protected")
    (scratch.root / "linked").symlink_to(outside, target_is_directory=True)
    assert supervisor.reclaim_unowned_scratch()["removed"] == []
    assert (outside / "payload").read_bytes() == b"protected"


def test_unwritable_removal_record_refuses_cleanup(scratch):
    path = entry(scratch, "landing-needs-a-record")
    scratch.state.mkdir()
    (scratch.state / "scratch-removals.jsonl").mkdir()
    report = supervisor.reclaim_unowned_scratch()
    assert report["error"]
    assert path.is_dir()


def test_sweep_refuses_a_symlink_root(scratch, tmp_path):
    scratch.root.rmdir()
    scratch.root.symlink_to(tmp_path, target_is_directory=True)
    report = supervisor.reclaim_unowned_scratch()
    assert "noncanonical root" in report["error"]
    assert scratch.root.is_symlink()


def test_disk_pressure_names_stale_entries_retained_for_a_live_runs_owner(
    scratch, monkeypatch, capsys
):
    now = scratch.now - 8000 + 11 * 3600
    monkeypatch.setattr(gc.time, "time", lambda: now)
    live = scratch.root / "live-run"
    live.mkdir()
    owner = pointer(scratch, live.name)
    owner.write_text(
        json.dumps(
            {"run_id": live.name, "project": "fixture", "session": "owning-session"}
        )
    )
    stale = live / "repository-copy"
    stale.mkdir()
    payload = stale / "repository-data"
    with payload.open("wb") as stream:
        stream.truncate(1_400_000_000)  # Sparse decoy: no large allocation or read.
    recent = live / "recent-copy"
    recent.mkdir()
    (recent / "payload").write_bytes(b"recent")
    os.utime(recent / "payload", (now - 60, now - 60))
    held = live / "held-copy"
    held.mkdir()
    (held / "payload").write_bytes(b"held")
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; print('ready',flush=True); sys.stdin.read()",
        ],
        cwd=held,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        assert holder.stdout.readline() == b"ready\n"
        monkeypatch.setenv(supervisor.NODE_DISK_ROOT_ENV, str(scratch.root))
        monkeypatch.setattr(supervisor, "node_disk_reserve_bytes", lambda: 10**18)
        assert supervisor.sample_node_disk() == 0
        sample = json.loads((scratch.state / supervisor.NODE_DISK_RECORD).read_text())
        retained = sample["crew_scratch"]["stale_live_entries"]
        assert len(retained) == 1
        row = retained[0]
        assert row["path"] == str(stale)
        assert row["bytes"] == 1_400_000_000
        assert 11 * 3600 - 2 <= row["inactivity_seconds"] <= 11 * 3600 + 2
        assert row["run_id"] == live.name
        assert row["project"] == "fixture"
        assert row["session"] == "owning-session"
        pressure = supervisor.node_disk_pressure(sample)
        assert pressure["stale_live_scratch"] == retained
        for expected in [str(stale), "1400000000 bytes", "live-run", "owning-session"]:
            assert expected in pressure["reason"]
        assert "seconds inactive" in pressure["reason"]
        assert payload.stat().st_size == 1_400_000_000
        assert (held / "payload").read_bytes() == b"held"
        assert (recent / "payload").read_bytes() == b"recent"
        assert sample["crew_scratch"]["removed"] == []
        assert not (scratch.state / "scratch-removals.jsonl").read_text()
        assert len(capsys.readouterr().out.split()) == 4
    finally:
        holder.communicate(timeout=5)


@pytest.mark.parametrize("failure", ["write", "flush", "fsync"])
def test_open_audit_sink_that_refuses_write_preserves_the_tree(
    scratch, monkeypatch, failure
):
    path = entry(scratch, "landing-needs-durable-intent")
    before = contents(path)
    original_open = Path.open
    original_fsync = os.fsync
    opened = []
    audit_fds = set()

    class RefusedWrite:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, data):
            if failure == "write":
                raise OSError("decoy audit write refused after successful open")
            return self.stream.write(data)

        def flush(self):
            if failure == "flush":
                raise OSError("decoy audit flush refused after successful open")
            return self.stream.flush()

    def open_sink(candidate, mode="r", *args, **kwargs):
        stream = original_open(candidate, mode, *args, **kwargs)
        if candidate.name == "scratch-removals.jsonl" and "a" in mode:
            opened.append(candidate)
            audit_fds.add(stream.fileno())
            return RefusedWrite(stream)
        return stream

    def fsync(descriptor):
        if failure == "fsync" and descriptor in audit_fds:
            raise OSError("decoy audit fsync refused after successful open")
        return original_fsync(descriptor)

    monkeypatch.setattr(Path, "open", open_sink)
    monkeypatch.setattr(supervisor.os, "fsync", fsync)
    report = supervisor.reclaim_unowned_scratch()
    assert opened
    assert f"{failure} refused" in report["error"]
    assert path.exists(), "a successful open must not license unaudited deletion"
    assert contents(path) == before


def test_slow_first_tree_cannot_starve_a_small_eligible_tree(scratch, monkeypatch):
    slow = entry(scratch, "a-slow-tree")
    small = entry(scratch, "z-small-tree")
    clock = [0.0]
    original_activity = gc._scratch_activity
    monkeypatch.setattr(gc.time, "monotonic", lambda: clock[0])

    def activity(path, deadline):
        if path == slow:
            clock[0] += 6
            raise TimeoutError("decoy slow tree exhausted its walk budget")
        return original_activity(path, deadline)

    monkeypatch.setattr(gc, "_scratch_activity", activity)
    reports = [supervisor.reclaim_unowned_scratch() for _ in range(2)]
    assert slow.exists()
    assert not small.exists(), "a slow first tree must not starve later eligible work"
    assert any(str(small) in report["removed"] for report in reports)


def test_final_activity_walk_receives_a_fresh_candidate_budget(scratch, monkeypatch):
    path = entry(scratch, "landing-slow-readable")
    clock = [0.0]
    deadlines = []
    original_activity = gc._scratch_activity
    monkeypatch.setattr(gc.time, "monotonic", lambda: clock[0])

    def activity(candidate, deadline):
        deadlines.append(deadline)
        clock[0] += 4
        return original_activity(candidate, deadline)

    monkeypatch.setattr(gc, "_scratch_activity", activity)
    report = supervisor.reclaim_unowned_scratch()
    assert report["removed"] == [str(path)]
    assert deadlines[1] > deadlines[0]


def test_intent_is_durable_before_the_tree_is_removed(scratch, monkeypatch):
    path = entry(scratch, "landing-durable-record")
    original_remove = gc.shutil.rmtree
    original_fsync = os.fsync
    synced = []

    def fsync(descriptor):
        synced.append(os.readlink(f"/proc/self/fd/{descriptor}"))
        return original_fsync(descriptor)

    def remove(target, *args, **kwargs):
        if Path(target) == path:
            log = scratch.state / "scratch-removals.jsonl"
            row = json.loads(log.read_text().splitlines()[-1])
            assert row["state"] == "intent" and not row["removed"]
            assert row["path"] == str(path)
            assert row["bytes"] == len(path.name)
            assert row["ctime_age_seconds"] >= 7200 and row["reason"]
            assert str(log) in synced
            receipts = list((scratch.state / "scratch-removal-intents").rglob("*.json"))
            assert len(receipts) == 1
            assert (
                json.loads(receipts[0].read_text())["removal_id"] == row["removal_id"]
            )
        return original_remove(target, *args, **kwargs)

    monkeypatch.setattr(supervisor.os, "fsync", fsync)
    monkeypatch.setattr(gc.shutil, "rmtree", remove)
    report = supervisor.reclaim_unowned_scratch()
    assert report["removed"] == [str(path)]
    rows = [
        json.loads(line)
        for line in (scratch.state / "scratch-removals.jsonl").read_text().splitlines()
    ]
    assert [row["state"] for row in rows] == ["intent", "complete"]
    assert not list((scratch.state / "scratch-removal-intents").rglob("*.json"))


def test_failed_completion_is_reconciled_from_the_durable_intent(scratch, monkeypatch):
    path = entry(scratch, "landing-needs-reconciliation")
    original_open = Path.open
    writes = [0]

    class RefusedCompletion:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, data):
            writes[0] += 1
            if writes[0] == 2:
                raise OSError("decoy completion write refused")
            return self.stream.write(data)

    def open_sink(candidate, mode="r", *args, **kwargs):
        stream = original_open(candidate, mode, *args, **kwargs)
        if candidate.name == "scratch-removals.jsonl" and "a" in mode:
            return RefusedCompletion(stream)
        return stream

    monkeypatch.setattr(Path, "open", open_sink)
    report = supervisor.reclaim_unowned_scratch()
    assert "completion write refused" in report["error"]
    assert not path.exists()
    pending = list((scratch.state / "scratch-removal-intents").rglob("*.json"))
    assert len(pending) == 1
    intent = json.loads(pending[0].read_text())
    assert intent["path"] == str(path) and intent["state"] == "intent"
    assert intent["bytes"] == len(path.name) and intent["reason"]
    monkeypatch.setattr(Path, "open", original_open)
    recovered = supervisor.reclaim_unowned_scratch()
    assert len(recovered["reconciled_removals"]) == 1
    row = recovered["reconciled_removals"][0]
    assert row["removed"] and row["state"] == "complete"
    assert row["removal_id"] == intent["removal_id"]
    assert not list((scratch.state / "scratch-removal-intents").rglob("*.json"))


def test_pointer_appearing_during_intent_publication_prevents_deletion(
    scratch, monkeypatch
):
    path = entry(scratch, "landing-reclaimed-by-live-run")
    original_fsync = os.fsync

    def fsync(descriptor):
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        result = original_fsync(descriptor)
        if target.endswith("scratch-removals.jsonl"):
            pointer(scratch, path.name)
        return result

    monkeypatch.setattr(supervisor.os, "fsync", fsync)
    report = supervisor.reclaim_unowned_scratch()
    assert path.exists()
    assert report["removed"] == []
    rows = [
        json.loads(line)
        for line in (scratch.state / "scratch-removals.jsonl").read_text().splitlines()
    ]
    assert [row["state"] for row in rows] == ["intent", "retained"]
    assert "live pointer" in rows[-1]["withheld"]


@pytest.mark.parametrize("refresh_time", [False, True])
def test_closed_descendant_write_after_intent_is_retained_and_recorded(
    scratch, monkeypatch, refresh_time
):
    path = entry(scratch, "landing-written-after-intent")
    payload = path / "payload"
    root_ctime = path.stat().st_ctime_ns
    original_sweep = gc.garbage_collect_orphan_scratch
    published = []

    def sweep(**kwargs):
        durable_record = kwargs["record_removal"]

        def publish(row):
            durable_record(row)
            if row["state"] == "intent":
                published.append(row["removal_id"])
                with payload.open("wb") as stream:
                    assert not stream.closed
                    stream.write(b"written and closed during intent publication")
                assert stream.closed
                if refresh_time:
                    os.utime(payload, (scratch.now, scratch.now))
                assert path.stat().st_ctime_ns == root_ctime
                assert not gc.pointer_path(path.name).exists()

        kwargs["record_removal"] = publish
        return original_sweep(**kwargs)

    monkeypatch.setattr(gc, "garbage_collect_orphan_scratch", sweep)
    report = supervisor.reclaim_unowned_scratch()
    assert published
    assert report["removed"] == []
    assert payload.read_bytes() == b"written and closed during intent publication"
    assert path.stat().st_ctime_ns == root_ctime
    rows = [
        json.loads(line)
        for line in (scratch.state / "scratch-removals.jsonl").read_text().splitlines()
    ]
    assert [row["state"] for row in rows] == ["intent", "retained"]
    assert rows[-1]["removal_id"] == published[0]
    assert "descendant" in rows[-1]["withheld"]
    assert not list((scratch.state / "scratch-removal-intents").rglob("*.json"))


@pytest.mark.parametrize("failure", [OSError, TimeoutError])
def test_unreadable_post_intent_activity_is_retained_and_recorded(
    scratch, monkeypatch, failure
):
    path = entry(scratch, "landing-unreadable-after-intent")
    before = contents(path)
    original_sweep = gc.garbage_collect_orphan_scratch
    original_activity = gc._scratch_activity
    acknowledged = []

    def sweep(**kwargs):
        durable_record = kwargs["record_removal"]

        def publish(row):
            durable_record(row)
            if row["state"] == "intent":
                acknowledged.append(row["removal_id"])

        kwargs["record_removal"] = publish
        return original_sweep(**kwargs)

    def activity(candidate, deadline):
        if acknowledged:
            raise failure("decoy post-intent activity unavailable")
        return original_activity(candidate, deadline)

    monkeypatch.setattr(gc, "garbage_collect_orphan_scratch", sweep)
    monkeypatch.setattr(gc, "_scratch_activity", activity)
    report = supervisor.reclaim_unowned_scratch()
    assert acknowledged and report["removed"] == []
    assert contents(path) == before
    rows = [
        json.loads(line)
        for line in (scratch.state / "scratch-removals.jsonl").read_text().splitlines()
    ]
    assert [row["state"] for row in rows] == ["intent", "retained"]
    assert "activity unreadable after intent" in rows[-1]["withheld"]
    assert rows[-1]["reason"] == rows[-1]["withheld"]
    assert not list((scratch.state / "scratch-removal-intents").rglob("*.json"))
