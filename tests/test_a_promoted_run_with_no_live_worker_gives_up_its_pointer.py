"""A promoted run whose recorded worker is gone gives up its pointer.

The release decision is driven from the live pointer alone: a mark naming an
earlier attempt, an unfinished resume stream and a head that is never merged
are not reasons to keep a promoted pointer whose worker is dead, and a live
worker process is the one fact that keeps it.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import pending_landing, runs, station
from tests.helpers.git import git
from tests.helpers.landing_repository import PROJECT, _write_origin_and_checkout

SESSION = "owner"


def _dead_pid() -> int:
    """A pid this host has already reaped, so its liveness reads False."""
    process = subprocess.Popen(["/bin/true"])
    process.wait()
    assert not Path(f"/proc/{process.pid}").exists(), (
        f"pid {process.pid} was reaped but /proc still names a process"
    )
    return process.pid


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """Whether ``ancestor`` is reachable from ``descendant``, without raising."""
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=repo,
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


def _pointer(run_id: str, repo: Path, tree: Path, manifest: Path, **extra) -> dict:
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(tree),
        "session": SESSION,
        "role": "implement",
        "phase": "complete",
        "launch": "in-harness",
        "manifest_path": str(manifest),
        "node": {"id": run_id, "time_budget": "20m", "write_paths": ["delivered.txt"]},
    }
    pointer.update(extra)
    return pointer


@pytest.fixture()
def cohort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    _origin, repo = _write_origin_and_checkout(tmp_path)
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repo / "docs")}))
    (repo / "docs" / "state" / PROJECT / "runs").mkdir(parents=True, exist_ok=True)
    base = git(repo, "rev-parse", "HEAD")
    dead = _dead_pid()
    live = os.getpid()
    delivered: dict[str, dict] = {}
    for name in (
        "marked-earlier",
        "stale-turn",
        "measured",
        "live-attempt",
        "unnamed-attempt",
        "dirty",
        "beyond",
    ):
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
        extra: dict = {}
        if name in {
            "marked-earlier",
            "stale-turn",
            "live-attempt",
            "unnamed-attempt",
        }:
            # The measured shape: the mark names the attempt that wrote it and
            # the pointer has since moved on to a later one.
            extra["attempt"] = 3
            extra["attempt_kind"] = "resume"
            if name == "live-attempt":
                extra["pid"] = live
            elif name == "unnamed-attempt":
                # The later attempt registers no process, so nothing proves its
                # worker stopped: the run is kept, whatever its mark names.
                extra["attempt"] = 4
            else:
                extra["pid"] = dead
                extra["pid_start_time"] = "950634665"
            extra["pending_landing_repair"] = True
            extra["pending_landing_repair_attempt"] = {
                "attempt": 3 if name == "unnamed-attempt" else 2,
                "phase": "working",
                "pid": dead,
                "pid_start_time": "951113889",
            }
        else:
            extra["attempt"] = 1
            extra["pid"] = dead
        if name == "measured":
            extra["measure_only"] = {
                "reason": "paired replay of a landed section for the token A/B"
            }
        pointer = _pointer(run_id, repo, tree, manifest, **extra)
        runs._write_json(runs.pointer_path(run_id), pointer)
        if name != "measured":
            git(repo, "merge", "--no-ff", "-m", f"merge: deliver {name}", head)
        ledger.write_run_file(
            PROJECT,
            {
                **pointer,
                "commits": [head],
                "promoted_revision": head,
                "gate": "passed",
            },
            repo,
        )
        delivered[name] = {"pointer": pointer, "tree": tree, "head": head}
    for name in ("stale-turn", "unnamed-attempt"):
        if not (tmp_path / "worktrees" / name).is_dir():
            continue
        # A resumed turn whose stream holds an answer and whose last record is
        # not the result line a finished turn writes. It reads as a turn in
        # progress on both runs; one records a dead process and is released,
        # the other records none and is kept.
        stream = runs.run_dir(f"r-{name}") / "resume-1.jsonl"
        stream.write_text(
            "\n".join(
                [
                    json.dumps({"type": "assistant", "text": "still working"}),
                    json.dumps({"type": "user", "text": "go on"}),
                ]
            )
            + "\n"
        )
    (delivered["dirty"]["tree"] / "uncommitted.txt").write_text("keep this work\n")
    beyond = delivered["beyond"]
    (beyond["tree"] / "beyond.txt").write_text("not on the branch\n")
    git(beyond["tree"], "add", "beyond.txt")
    git(beyond["tree"], "commit", "-qm", "feat: deliver beyond the promoted head")
    beyond["beyond_head"] = git(beyond["tree"], "rev-parse", "HEAD")
    git(repo, "push", "-q", "origin", "main")
    return {"repo": repo, "base": base, "members": delivered}


def _sweep(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """One station sweep, returning the rows its release step produced."""
    produced: list[list[dict]] = []
    original = pending_landing.retire_promoted_pointers

    def traced(*args, **kwargs):
        rows = original(*args, **kwargs)
        produced.append(rows)
        return rows

    monkeypatch.setattr(pending_landing, "retire_promoted_pointers", traced)
    beat = station.SweepBeat(sweep=lambda *args, **kwargs: {}, start=lambda run: run())
    assert beat.request(PROJECT, SESSION, moment=datetime.now(UTC), changed=True)
    assert len(produced) == 1, produced
    return produced[0]


def test_a_promoted_run_with_no_live_worker_gives_up_its_pointer(
    cohort: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = cohort["repo"]
    members = cohort["members"]
    released = ("marked-earlier", "stale-turn", "measured")
    for name in released:
        assert runs.pointer_path(f"r-{name}").exists()
        assert members[name]["tree"].is_dir()
    assert not _is_ancestor(repo, members["measured"]["head"], "main"), (
        "the measurement run's head is by design never merged"
    )
    kept = ("live-attempt", "unnamed-attempt", "dirty", "beyond")
    for name in kept:
        assert _is_ancestor(repo, members[name]["head"], "main"), name

    rows = _sweep(monkeypatch)
    by_run = {}
    for row in rows:
        by_run.setdefault(row["run_id"], []).append(row)
    for name in released:
        run_id = f"r-{name}"
        assert not runs.pointer_path(run_id).exists(), (name, by_run.get(run_id))
        assert not members[name]["tree"].exists(), name
        assert [row["status"] for row in by_run[run_id]] == ["already-landed"]
        receipt = json.loads(
            (runs.run_dir(run_id) / "release.json").read_text(encoding="utf-8")
        )
        assert receipt["worktree_released"] is True, name
        row = json.loads(ledger.run_path(PROJECT, run_id, repo).read_text())
        assert row["release"]["worktree_released"] is True, name
    for name, reason in (
        ("live-attempt", "live"),
        ("unnamed-attempt", "turn in progress"),
        ("dirty", "uncommitted"),
        ("beyond", "unintegrated"),
    ):
        run_id = f"r-{name}"
        assert runs.pointer_path(run_id).exists(), name
        assert members[name]["tree"].is_dir(), name
        assert len(by_run[run_id]) == 1, (name, by_run[run_id])
        held = by_run[run_id][0]
        assert held["status"] in {"release-held", "release-refused"}, held
        assert reason in (held.get("reason") or held.get("error") or ""), held

    # A second sweep neither recreates a pointer nor removes a kept tree.
    before = {path.name for path in runs.live_dir().glob("*.json")}
    again = _sweep(monkeypatch)
    assert {path.name for path in runs.live_dir().glob("*.json")} == before
    for name in released:
        assert not runs.pointer_path(f"r-{name}").exists(), name
        assert not members[name]["tree"].exists(), name
    assert {row["run_id"] for row in again} <= {f"r-{name}" for name in kept}
    for name in kept:
        assert members[name]["tree"].is_dir(), name
