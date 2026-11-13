"""Terminal operations retire a run's workspace and delivery directory."""

import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import (
    gc,
    promotion,
    promotion_release,
    routing,
    runs,
    terminal_cleanup,
)
from tests.helpers.git import git
from tests.helpers.landing_repository import _complete_land
from tests.test_a_landed_head_releases_its_worktree import _landed_run


def _run(tmp_path, monkeypatch):
    run = _landed_run(tmp_path, monkeypatch)
    directory = runs.run_dir(run["run_id"])
    directory.mkdir(parents=True)
    record = runs.read_pointer(run["run_id"])
    manifest = directory / "manifest.md"
    manifest.write_text(Path(record["manifest_path"]).read_text())
    record["manifest_path"] = str(manifest)
    scratch = tmp_path / "scratch" / run["run_id"]
    scratch.mkdir(parents=True)
    (scratch / "temporary").write_text("scratch")
    record["scratch"] = str(scratch)
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(scratch.parent))
    runs._write_json(runs.pointer_path(run["run_id"]), record)
    run.update(directory=directory, scratch=scratch)
    return run


def _assert_retired(run):
    assert not run["worktree"].exists(), "ended worktree is still on disk"
    assert str(run["worktree"]) not in subprocess.check_output(
        ["git", "-C", str(run["repository"]), "worktree", "list", "--porcelain"],
        text=True,
    )
    assert not run["scratch"].exists(), "ended scratch is still on disk"
    assert not run["directory"].exists(), "ended directory remains in walked runs"
    archive = runs.crew_home() / "archive" / "runs" / run["run_id"]
    assert (archive / "end.json").is_file(), "archive receipt is missing"
    assert (archive / "manifest.md").read_text().startswith("node:")
    assert runs.run_dir(run["run_id"]) == archive
    assert runs.resolve_run_path(run["directory"] / "manifest.md").is_file()


def test_landing_retires_the_entire_run(tmp_path, monkeypatch):
    from reckon.crew import promotion_evidence, review

    run = _run(tmp_path, monkeypatch)
    result = _complete_land(run)
    report = json.loads(result.output)
    assert report["steps"]["land"]["pushed"] is True
    _assert_retired(run)
    assert result.exit_code == 0, result.output
    archived_record = report["promotion"]["record"]
    assert promotion_evidence._manifest_text(archived_record).startswith("node:")
    gate = review._read_gate_log(
        archived_record["gate_check"]["log_path"], runs.run_dir(run["run_id"])
    )
    assert gate is not None and "EXIT=0" in gate


def test_discard_retires_the_entire_run(tmp_path, monkeypatch):
    run = _run(tmp_path, monkeypatch)
    git(run["repository"], "merge", "--ff-only", run["head"])
    result = promotion.discard(run["run_id"])
    assert result["pointer_removed"]
    assert promotion.discard_recorded(run["run_id"])
    assert Path(result["discard_record"]).is_file()
    assert result["run_archived"] == str(terminal_cleanup.archive_dir(run["run_id"]))
    _assert_retired(run)


def test_promotion_with_nothing_to_merge_retires_the_entire_run(tmp_path, monkeypatch):
    run = _run(tmp_path, monkeypatch)
    git(run["repository"], "merge", "--ff-only", run["head"])
    result = promotion.complete(
        run["run_id"],
        gate="passed",
        commits=[run["head"]],
        root=run["repository"],
        no_impl_change="the fixture changes no plan state",
        gate_check={
            "command": "true",
            "exit_status": 0,
            "log_path": str(run["gate_log"]),
        },
    )
    assert result["pointer_removed"]
    _assert_retired(run)


@pytest.mark.parametrize("status", ["blocked", "waiting"])
def test_recoverable_run_keeps_its_workspace(tmp_path, monkeypatch, status):
    run = _run(tmp_path, monkeypatch)
    record = runs.read_pointer(run["run_id"])
    Path(record["manifest_path"]).write_text(f"node: node-a\nstatus: {status}\n")
    result = promotion_release._release_after_promotion(
        run["run_id"], record, gate="passed"
    )
    assert not result["worktree_released"]
    assert run["worktree"].is_dir()
    assert run["directory"].is_dir()
    assert run["scratch"].is_dir()
    assert result["scratch_withheld"] == "run retained for recovery"


@pytest.mark.parametrize("gate", ["passed", "not-run"])
def test_sweep_recovers_a_crash_after_the_end_was_recorded(
    tmp_path, monkeypatch, capsys, gate
):
    run = _run(tmp_path, monkeypatch)
    git(run["repository"], "merge", "--ff-only", run["head"])
    record = runs.read_pointer(run["run_id"])
    if gate == "not-run":
        Path(record["manifest_path"]).write_text("node: node-a\nstatus: blocked\n")
    terminal_cleanup.queue_cleanup(record, kind="promoted", gate=gate)
    runs.pointer_path(run["run_id"]).unlink()
    rows = terminal_cleanup.sweep_ended_runs(limit=1)
    assert rows[0]["worktree_released"]
    _assert_retired(run)
    output = capsys.readouterr().err
    for path in (run["worktree"], run["directory"], run["scratch"]):
        assert str(path) in output
    assert str(terminal_cleanup.cleanup_path(run["run_id"])) in output


def test_a_withheld_cleanup_does_not_starve_the_next_slice(tmp_path, monkeypatch):
    run = _run(tmp_path, monkeypatch)
    git(run["repository"], "merge", "--ff-only", run["head"])
    record = runs.read_pointer(run["run_id"])
    live = {"run_id": "live-held", "repo": str(run["repository"])}
    runs._write_json(runs.pointer_path(live["run_id"]), live)
    terminal_cleanup.queue_cleanup(live, kind="promoted", gate="passed")
    terminal_cleanup.queue_cleanup(record, kind="promoted", gate="passed")
    runs.pointer_path(run["run_id"]).unlink()
    assert terminal_cleanup.sweep_ended_runs(limit=1)[0]["withheld"] == "live pointer"
    assert runs.pointer_path(live["run_id"]).is_file()
    assert run["worktree"].is_dir()
    assert terminal_cleanup.sweep_ended_runs(limit=1)[0]["worktree_released"]
    _assert_retired(run)


def test_archived_population_is_outside_the_gc_walk(tmp_path, monkeypatch):
    from tests.test_crew_gc import repository

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = repository(tmp_path)
    # Historical runs have already ended and are archived by their own
    # operations. The positive control is five live directories the gc
    # extraction reader must actually visit.
    for index in range(200):
        directory = terminal_cleanup.archive_dir(f"ended-{index}")
        directory.mkdir(parents=True)
        (directory / "end.json").write_text("{}")
    live = runs.runs_dir()
    live.mkdir(parents=True)
    names = {f"live-{index}" for index in range(5)}
    for name in names:
        directory = live / name
        directory.mkdir()
        runs._write_json(
            runs.pointer_path(name), {"run_id": name, "project": "fixture"}
        )
    from reckon.crew import fleet_supervisor

    visited = []
    archive_reads = []
    archive_root = terminal_cleanup.archive_dir("ended-0").parent
    real_stat = Path.stat
    original = fleet_supervisor.run_directory_source_trees

    def observe(directory):
        visited.append(directory)
        return original(directory)

    def observe_stat(path, *args, **kwargs):
        if path == archive_root or archive_root in path.parents:
            archive_reads.append(path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(fleet_supervisor, "run_directory_source_trees", observe)
    monkeypatch.setattr(Path, "stat", observe_stat)
    gc.garbage_collect(repo=repo, project="fixture")
    assert {path.name for path in visited} == names
    assert len(visited) == 5
    assert archive_reads == []
    # The same observer must see a known archived directory before its empty
    # reading can establish that the gc left history untouched.
    terminal_cleanup.archive_dir("ended-0").stat()
    assert archive_reads == [archive_root / "ended-0"]


def test_discard_of_an_unlaunched_request_creates_no_run_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    record = {"run_id": "unlaunched", "repo": str(tmp_path), "phase": "queued"}
    runs._write_json(runs.pointer_path(record["run_id"]), record)
    result = promotion.discard(record["run_id"])
    assert result["pointer_removed"]
    assert Path(result["discard_record"]).is_file()
    assert not runs.run_dir(record["run_id"]).exists()
    assert not terminal_cleanup.cleanup_path(record["run_id"]).exists()


def test_gc_does_not_compare_history_of_a_live_worktree(tmp_path, monkeypatch):
    run = _run(tmp_path, monkeypatch)
    calls = []

    def compare(*args):
        calls.append(args)
        raise RuntimeError("historical patch comparison reached a live worktree")

    monkeypatch.setattr(routing, "_commits_beyond_merge_base", compare)
    with pytest.raises(RuntimeError, match="historical patch comparison"):
        compare(run["repository"], run["worktree"], "HEAD")
    assert len(calls) == 1
    calls.clear()
    result = routing._inspect_workspace(
        run["repository"],
        run["worktree"],
        "HEAD",
        [run["run_id"]],
        release_residue=True,
    )
    assert result["classification"] == "live-referenced"
    assert result["non_equivalent_commits"] is None
    assert "live pointer" in result["commits_unmeasured_reason"]
    assert calls == []
    assert run["worktree"].is_dir()
