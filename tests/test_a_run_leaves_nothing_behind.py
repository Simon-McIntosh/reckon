"""Terminal operations retire a run's workspace and delivery directory."""

import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import gc, promotion, promotion_release, runs, terminal_cleanup
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
    run = _run(tmp_path, monkeypatch)
    result = _complete_land(run)
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["steps"]["land"]["pushed"] is True
    _assert_retired(run)


def test_discard_retires_the_entire_run(tmp_path, monkeypatch):
    run = _run(tmp_path, monkeypatch)
    git(run["repository"], "merge", "--ff-only", run["head"])
    result = promotion.discard(run["run_id"])
    assert result["pointer_removed"]
    assert promotion.discard_recorded(run["run_id"])
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


def test_sweep_recovers_a_crash_after_the_end_was_recorded(
    tmp_path, monkeypatch, capsys
):
    run = _run(tmp_path, monkeypatch)
    git(run["repository"], "merge", "--ff-only", run["head"])
    record = runs.read_pointer(run["run_id"])
    terminal_cleanup.queue_cleanup(record, kind="promoted", gate="passed")
    runs.pointer_path(run["run_id"]).unlink()
    rows = terminal_cleanup.sweep_ended_runs(limit=1)
    assert rows[0]["worktree_released"]
    _assert_retired(run)
    output = capsys.readouterr().err
    for path in (run["worktree"], run["directory"], run["scratch"]):
        assert str(path) in output
    assert str(terminal_cleanup.cleanup_path(run["run_id"])) in output


def test_archived_population_is_outside_the_gc_walk(tmp_path, monkeypatch):
    run = _run(tmp_path, monkeypatch)
    runs.pointer_path(run["run_id"]).unlink()
    # Historical runs have already ended and are archived by their own
    # operations. The positive control is five live directories the gc
    # extraction reader must actually visit.
    for index in range(200):
        directory = terminal_cleanup.archive_dir(f"ended-{index}")
        directory.mkdir(parents=True)
        (directory / "end.json").write_text("{}")
    live = runs.runs_dir()
    run["directory"].rename(terminal_cleanup.archive_dir(run["run_id"]))
    names = {f"live-{index}" for index in range(5)}
    for name in names:
        directory = live / name
        directory.mkdir()
        runs._write_json(
            runs.pointer_path(name), {"run_id": name, "project": "fixture"}
        )
    from reckon.crew import fleet_supervisor

    visited = []
    original = fleet_supervisor.run_directory_source_trees

    def observe(directory):
        visited.append(directory)
        return original(directory)

    monkeypatch.setattr(fleet_supervisor, "run_directory_source_trees", observe)
    gc.garbage_collect(repo=run["repository"], project="fixture")
    assert {path.name for path in visited} == names
    assert len(visited) == 5
