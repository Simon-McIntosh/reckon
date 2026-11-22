"""A measurement run whose record is stored gives up its pointer on the next pass.

A measurement-only run is journaled rather than landed: its promotion writes the
run's record, and a promotion that could not retire the pointer leaves a
pending-landing journal beside it. The pending-record pass owns a journal
because it finishes the merge a landing owes — but a measurement run's head is
never merged, so that pass has nothing to finish for it, and reading the
journal as a reason to keep the pointer left the run's paths claimed after its
measurement was recorded. The landing pass releases such a run on its record
alone, and leaves the journal exactly as the promotion wrote it.

A run with no record is kept: nothing has been recorded for it, so nothing has
proved that its measurement is finished with.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import pending_landing, runs
from tests.helpers.git import git
from tests.helpers.landing_repository import PROJECT, _write_origin_and_checkout

SESSION = "owner"
REASON = "paired replay of a landed section for the token A/B"


def _dead_pid() -> int:
    """A pid this host has already reaped, so its liveness reads False."""
    process = subprocess.Popen(["/bin/true"])
    process.wait()
    assert not Path(f"/proc/{process.pid}").exists(), (
        f"pid {process.pid} was reaped but /proc still names a process"
    )
    return process.pid


def _measurement_pointer(
    run_id: str, repo: Path, tree: Path, manifest: Path, pid: int
) -> dict:
    return {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(tree),
        "session": SESSION,
        "role": "implement",
        "phase": "complete",
        "launch": "in-harness",
        "manifest_path": str(manifest),
        "node": {
            "id": run_id,
            "time_budget": "20m",
            "measure_only": REASON,
            "write_paths": ["delivered.txt"],
        },
        "attempt": 1,
        "pid": pid,
        "measure_only": {"reason": REASON},
    }


@pytest.fixture()
def cohort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    home = tmp_path = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    _origin, repo = _write_origin_and_checkout(tmp_path)
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repo / "docs")}))
    (repo / "docs" / "state" / PROJECT / "runs").mkdir(parents=True, exist_ok=True)
    base = git(repo, "rev-parse", "HEAD")
    dead = _dead_pid()
    members: dict[str, dict] = {}
    for name, recorded in (("recorded", True), ("recordless", False)):
        run_id = f"r-{name}"
        tree = tmp_path / "worktrees" / name
        git(repo, "worktree", "add", "--detach", str(tree), base)
        (tree / f"{name}.txt").write_text(f"delivered {name}\n")
        git(tree, "add", f"{name}.txt")
        git(tree, "commit", "-qm", f"feat: deliver {name}")
        head = git(tree, "rev-parse", "HEAD")
        directory = runs.run_dir(run_id)
        directory.mkdir(parents=True)
        manifest = directory / "manifest.md"
        manifest.write_text(f'status: complete\ncommits: ["{head}"]\nblockers: none\n')
        pointer = _measurement_pointer(run_id, repo, tree, manifest, dead)
        runs._write_json(runs.pointer_path(run_id), pointer)
        member = {"pointer": pointer, "tree": tree, "head": head}
        if recorded:
            ledger.write_run_file(
                PROJECT,
                {
                    **pointer,
                    "commits": [head],
                    "gate": "passed",
                    "outcome": "measured",
                },
                repo,
            )
            pending_landing.write_pending(run_id, PROJECT, pointer=pointer, head=head)
            member["journal"] = pending_landing._record_path(run_id)
            member["journal_bytes"] = member["journal"].read_bytes()
        members[name] = member
    return {"repo": repo, "base": base, "members": members}


def test_a_recorded_measurement_run_is_released_and_a_recordless_one_is_kept(
    cohort: dict,
) -> None:
    repo = cohort["repo"]
    recorded = cohort["members"]["recorded"]
    recordless = cohort["members"]["recordless"]
    assert runs.pointer_path("r-recorded").exists()
    assert runs.pointer_path("r-recordless").exists()
    assert recorded["tree"].is_dir()
    assert recordless["tree"].is_dir()
    assert recorded["journal"].is_file()

    rows = pending_landing.finish_pending_landings()

    by_run = {row["run_id"]: row for row in rows}
    # The recorded run's pointer and workspace leave, and its journal is kept
    # byte for byte: the record is the evidence its measurement was recorded.
    assert not runs.pointer_path("r-recorded").exists(), by_run.get("r-recorded")
    assert not recorded["tree"].exists()
    assert recorded["journal"].is_file()
    assert recorded["journal"].read_bytes() == recorded["journal_bytes"]
    row = json.loads(ledger.run_path(PROJECT, "r-recorded", repo).read_text())
    assert row["release"]["worktree_released"] is True
    # A measurement run's head is never merged: the release records its own
    # outcome commit and the replay itself stays off the branch.
    assert (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", recorded["head"], "HEAD"],
            cwd=repo,
            check=False,
            capture_output=True,
        ).returncode
        != 0
    ), "the measurement run's head reached the branch"
    # Nothing recorded has proved the other run's measurement is finished with.
    assert runs.pointer_path("r-recordless").exists()
    assert recordless["tree"].is_dir()

    # A second pass neither recreates the released pointer nor takes the kept one.
    again = pending_landing.finish_pending_landings()
    assert not runs.pointer_path("r-recorded").exists()
    assert runs.pointer_path("r-recordless").exists()
    assert recordless["tree"].is_dir()
    assert {row["run_id"] for row in again if row["run_id"] == "r-recorded"} == set()
