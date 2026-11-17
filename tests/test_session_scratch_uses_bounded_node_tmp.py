"""Interactive scratch stays on disk and excess use names its owning session."""

from __future__ import annotations

import csv
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import flight
from reckon.crew import fleet_supervisor as supervisor


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    environ = {
        "SLURM_JOB_ID": "decoy",
        "FLEET_SESSION_TMP_ROOT": str(tmp_path / "disk"),
        "FLEET_STATE_DIR": str(tmp_path / "state"),
    }
    monkeypatch.setattr(supervisor, "session_scratch_limits", lambda: (4096, 0.5))
    return environ


def test_two_session_starts_receive_private_local_disk_scratch(
    scratch, monkeypatch, tmp_path
):
    monkeypatch.delenv(supervisor.SESSION_TMP_ROOT_ENV)
    assert supervisor.session_tmp_root({}) == Path("/tmp")  # noqa: S108 - asserting the local disk contract
    assert supervisor._scratch_disk(supervisor.SESSION_TMP_ROOT).is_relative_to("/tmp")  # noqa: S108 - asserting the local disk contract
    captured = []

    def run(argv, **kwargs):
        directory = Path(kwargs["env"]["TMPDIR"]).resolve()
        assert directory.is_relative_to(
            Path(scratch["FLEET_SESSION_TMP_ROOT"]).resolve()
        )
        assert not directory.is_relative_to("/dev/shm")  # noqa: S108 - forbidden root
        captured.append(directory)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(supervisor, "session_running", lambda *_: False)
    monkeypatch.setattr(supervisor, "size_tabs_with_a_client", lambda *_: None)
    monkeypatch.setattr(supervisor.subprocess, "run", run)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    assert supervisor.start_session("busy", "", runtime, scratch) == 0
    assert supervisor.start_session("empty", "", runtime, scratch) == 0
    assert len(set(captured)) == 2
    for path in captured:
        assert path.stat().st_mode & 0o777 == 0o700
    (captured[0] / "extractions").mkdir()
    (captured[0] / "extractions" / "tree").write_bytes(b"x" * 16384)
    (captured[0] / "small").mkdir()
    (captured[0] / "small" / "file").write_bytes(b"x" * 4096)
    sample = supervisor.session_scratch_sample(scratch)
    rows = {row["session"]: row for row in sample["sessions"]}
    assert rows["empty"]["bytes"] == 0
    assert rows["busy"]["bytes"] >= 20480
    assert [item["session"] for item in sample["obligations"]] == ["busy"]
    assert sample["obligations"][0]["largest_directory"] == str(
        captured[0] / "extractions"
    )
    assert sample["total_bytes"] == rows["busy"]["bytes"]


def test_memory_backed_root_and_alias_are_refused(scratch, tmp_path):
    alias = tmp_path / "memory"
    alias.symlink_to("/dev/shm", target_is_directory=True)  # noqa: S108 - rejection input
    for root in ("/dev/shm", str(alias)):  # noqa: S108 - rejection input
        with pytest.raises(ValueError, match="requires local disk"):
            supervisor.prepare_session_tmp(
                {**scratch, "FLEET_SESSION_TMP_ROOT": root}, "decoy"
            )


def test_sample_persists_bytes_and_names_largest_directory_in_notice(scratch, capsys):
    busy = supervisor.prepare_session_tmp(scratch, "busy")
    empty = supervisor.prepare_session_tmp(scratch, "empty")
    largest = busy / "trees"
    largest.mkdir()
    (largest / "source").write_bytes(b"x" * 16384)
    assert supervisor.sample_session_scratch(scratch) == 0
    total = int(capsys.readouterr().out)
    assert total >= 16384
    state = Path(scratch["FLEET_STATE_DIR"])
    log = next((state / "health").glob("*-session-scratch.jsonl"))
    sample = json.loads(log.read_text())
    assert sample["total_bytes"] == total
    assert {row["path"] for row in sample["sessions"]} == {str(busy), str(empty)}
    assert sample["obligations"][0]["session"] == "busy"
    assert str(largest) in (state / "notice").read_text()
    assert "obligation:" in (state / "alerts.log").read_text()
    assert "empty" not in (state / "notice").read_text()


def test_total_disk_share_refuses_new_session_then_releases(
    scratch, tmp_path, monkeypatch
):
    busy = supervisor.prepare_session_tmp(scratch, "busy")
    (busy / "file").write_bytes(b"x" * 16384)
    used = (busy / "file").stat().st_blocks * 512
    monkeypatch.setattr(
        supervisor.shutil, "disk_usage", lambda _: SimpleNamespace(total=used * 2)
    )
    monkeypatch.setattr(supervisor, "session_running", lambda *_: False)
    monkeypatch.setattr(supervisor, "size_tabs_with_a_client", lambda *_: None)
    launched = []
    monkeypatch.setattr(
        supervisor.subprocess,
        "run",
        lambda argv, **kw: launched.append(kw) or subprocess.CompletedProcess(argv, 0),
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    assert supervisor.start_session("newcomer", "", runtime, scratch) == 1
    assert launched == []
    assert not supervisor.session_tmp_directory(scratch, "newcomer").exists()
    assert (
        supervisor.session_scratch_sample(scratch)["obligations"][0]["session"]
        == "busy"
    )
    (busy / "file").unlink()
    assert supervisor.start_session("newcomer", "", runtime, scratch) == 0
    assert len(launched) == 1


def test_sampling_follows_no_symlinks_and_reports_no_false_zero(
    scratch, tmp_path, monkeypatch, capsys
):
    busy = supervisor.prepare_session_tmp(scratch, "busy")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data").write_bytes(b"x" * 16384)
    (busy / "alias").symlink_to(outside, target_is_directory=True)
    (busy / "file-alias").symlink_to(outside / "data")
    assert supervisor.session_scratch_sample(scratch)["total_bytes"] == 0

    def denied(*args, **kwargs):
        kwargs["onerror"](PermissionError("decoy unreadable directory"))
        yield

    monkeypatch.setattr(supervisor.os, "walk", denied)
    assert supervisor.sample_session_scratch(scratch) == 1
    assert capsys.readouterr().out.strip() == "unknown"
    state = Path(scratch["FLEET_STATE_DIR"])
    assert "unknown" in (state / "alerts.log").read_text()
    assert not (state / "health").exists()


def test_sampling_walk_has_a_time_bound(scratch, monkeypatch):
    supervisor.prepare_session_tmp(scratch, "busy")
    ticks = iter((0, 16))
    monkeypatch.setattr(
        supervisor, "time", SimpleNamespace(monotonic=lambda: next(ticks))
    )
    with pytest.raises(TimeoutError, match="15 seconds"):
        supervisor.session_scratch_sample(scratch)


def test_flight_defaults_and_host_override(tmp_path):
    host = tmp_path / "flight.yaml"
    empty = tmp_path / "absent"
    default = flight.resolve(host_path=empty, catalogue_path=empty).config[
        "session_scratch"
    ]
    assert default == {"max_bytes": 2 * 1024**3, "disk_share": 0.5}
    host.write_text("session_scratch:\n  max_bytes: 8192\n  disk_share: 0.25\n")
    config = flight.resolve(host_path=host, catalogue_path=empty).config
    assert config["session_scratch"] == {"max_bytes": 8192, "disk_share": 0.25}
    with pytest.raises(flight.FlightConfigError):
        flight.resolve(
            host_path=empty,
            catalogue_path=empty,
            overrides={"session_scratch": {"disk_share": 0}},
        )


def test_health_sampler_calls_scratch_sampling_and_keeps_the_reading(
    tmp_path, monkeypatch
):
    from tests.test_fleet_health import sandbox

    box = sandbox(tmp_path)
    stub = box.root / "stubbin" / "scratch-python"
    stub.write_text(
        '#!/bin/bash\ncase "$*" in\n  *sample-session-scratch) echo 12345 ;;\n  *check-interpreters) echo 0 ;;\n  *) exit 9 ;;\nesac\n'.replace(
            '"', '"'
        )
    )
    stub.chmod(0o700)
    env = {**box.env, "RECKON_SHIM_PYTHON": str(stub)}
    result = subprocess.run(
        [
            "bash",
            str(Path(__file__).resolve().parents[1] / "bin/fleet-health"),
            "--once",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    with box.samples()[0].open() as sample:
        rows = list(csv.DictReader(sample, delimiter="\t"))
    assert rows[0]["session_scratch_bytes"] == "12345"


def test_excess_reaches_only_its_conversation_in_both_obligation_views(
    scratch, monkeypatch
):
    from datetime import UTC, datetime

    from reckon.crew import obligation_snapshot, obligations

    monkeypatch.setenv("FLEET_STATE_DIR", scratch["FLEET_STATE_DIR"])
    monkeypatch.setattr(obligations, "_classified_rows", lambda *_: [])
    monkeypatch.setattr(obligations, "_index_lag_notice", lambda *a, **kw: None)
    busy = supervisor.prepare_session_tmp(scratch, "busy")
    supervisor.prepare_session_tmp(scratch, "empty")
    largest = busy / "extractions"
    largest.mkdir()
    data = largest / "source"
    data.write_bytes(b"x" * 16384)
    state_dir = Path(scratch["FLEET_STATE_DIR"])
    state_dir.mkdir(parents=True)
    (state_dir / "sessions.tsv").write_text(
        "now\tbusy\t1\t/decoy\tbusy-conversation\n"
        "now\tempty\t2\t/decoy\tempty-conversation\n"
    )
    assert supervisor.sample_session_scratch(scratch) == 0
    now = datetime.now(UTC)
    state = SimpleNamespace(
        project="scratch-fixture",
        now=now,
        reviews_in_flight={},
        pointers={},
        rows=[],
        sub_floor={},
        held_worktrees={},
        merged_sections={},
        findings=[],
        acknowledged={},
        unreconciled={},
    )
    direct = obligations.obligations(state.project, "busy-conversation")
    published = obligation_snapshot.payload_for(state, "busy-conversation")
    direct_duties = [
        item
        for item in direct["obligations"]
        if item["kind"] == "session-scratch-over-bound"
    ]
    published_duties = [
        item
        for item in published["obligations"]
        if item["kind"] == "session-scratch-over-bound"
    ]
    assert len(direct_duties) == 1
    assert direct_duties[0]["paths"] == [str(largest)]
    assert direct_duties[0]["reason"] == published_duties[0]["reason"]
    assert direct_duties[0]["next_command"] == published_duties[0]["next_command"]
    assert (
        obligations.session_scratch_items(state.project, "empty-conversation", now=now)
        == []
    )
    assert (
        obligations.session_scratch_items(
            state.project, "unrelated-conversation", now=now
        )
        == []
    )
    data.unlink()
    assert supervisor.sample_session_scratch(scratch) == 0
    assert (
        obligations.session_scratch_items(state.project, "busy-conversation", now=now)
        == []
    )
    assert not any(
        item["kind"] == "session-scratch-over-bound"
        for item in obligation_snapshot.payload_for(state, "busy-conversation")[
            "obligations"
        ]
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_session_policy_uses_the_shared_nearest_mount_classification(
    tmp_path, monkeypatch, reverse
):
    from reckon.crew import dispatch_admission

    memory = tmp_path / "memory"
    disk = memory / "disk"
    disk.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(memory, target_is_directory=True)
    mounts = [(tmp_path, "xfs"), (memory, "tmpfs"), (disk, "ext4")]
    monkeypatch.setattr(
        dispatch_admission,
        "_mount_table",
        lambda: tuple(reversed(mounts)) if reverse else tuple(mounts),
    )
    assert dispatch_admission.path_filesystem_type(alias / "file") == "tmpfs"
    assert dispatch_admission._path_is_tmpfs(alias / "file") is True
    with pytest.raises(ValueError, match="requires local disk"):
        supervisor._scratch_disk(alias / "file")
    assert dispatch_admission.path_filesystem_type(alias / "disk" / "file") == "ext4"
    assert dispatch_admission._path_is_tmpfs(alias / "disk" / "file") is False
    assert supervisor._scratch_disk(alias / "disk" / "file") == disk / "file"
    monkeypatch.setattr(dispatch_admission, "_mount_table", lambda: ())
    assert dispatch_admission.path_filesystem_type(tmp_path) is None
    with pytest.raises(ValueError, match="requires local disk"):
        supervisor._scratch_disk(tmp_path)


def test_session_checks_reuse_the_dispatch_mount_table_cache(tmp_path, monkeypatch):
    from reckon.crew import dispatch_admission

    reads = []
    original = Path.read_text

    def mountinfo(path, *args, **kwargs):
        if str(path) == "/proc/self/mountinfo":
            reads.append(path)
            return "22 1 0:21 / / rw - ext4 /dev/root rw\n"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", mountinfo)
    monkeypatch.setattr(dispatch_admission, "_MOUNT_TABLE", {})
    for _ in range(3):
        assert supervisor._scratch_disk(tmp_path) == tmp_path.resolve()
        assert dispatch_admission._path_is_tmpfs(tmp_path) is False
    assert len(reads) == 1
