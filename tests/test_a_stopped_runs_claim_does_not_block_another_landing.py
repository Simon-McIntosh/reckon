"""A stopped run's claim does not block another run's landing.

A claim protects a file from a second concurrent editor. Once its holder's
process has ended and its manifest is terminal, that holder edits nothing, so a
promotion that accepts the same path is admitted and records the claim it walked
past. A peer that is still running, or that ended without delivering a terminal
manifest, keeps refusing exactly as before, and so does a dispatch.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import ledger
from reckon.cli import main as cli_main
from reckon.crew.dispatch_claims import refuse_widen_scope_conflicts
from reckon.crew.node import ScopeConflict
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "sample"
OURS = "r-landing-run"
PEER = "r-stopped-peer"
ACCEPTED = "declared-b.txt"
DECLARED = "declared-a.txt"


def git(repository: Path, *arguments: str) -> str:
    environment = {
        key: value for key, value in os.environ.items() if key != "RECKON_RUN_ID"
    }
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def dead_pid() -> int:
    for candidate in (999999, 999998, 999997, 999996):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
    raise AssertionError("no dead pid available")


def write_manifest(path: Path, status: str) -> Path:
    path.write_text(f"status: {status}\ncheckpoint: delivered\n", encoding="utf-8")
    return path


def write_pointer(
    repository: Path,
    run_id: str,
    base: str,
    *,
    declared: str,
    worktree: Path,
    head: str,
    pid: int,
    manifest: Path,
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "base_sha": base,
            "head": head,
            "launch": "in-harness",
            "launcher_host": socket.gethostname(),
            "pid": pid,
            "role": "implement",
            "backend": "native",
            "created_at": "2026-10-24T00:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": run_id,
                "plan": "fixture",
                "section": "guard",
                "time_budget": "25m",
                "write_paths": [declared],
            },
        },
    )


def detached_tree(repository: Path, path: Path) -> Path:
    git(repository, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    return path


def commit_a_file(tree: Path, filename: str) -> str:
    (tree / filename).write_text(f"{filename}\n", encoding="utf-8")
    git(tree, "add", filename)
    git(tree, "commit", "-q", "-m", f"test: change {filename}")
    return git(tree, "rev-parse", "HEAD")


def build_pair(
    repository: Path,
    tmp_path: Path,
    *,
    peer_pid: int,
    peer_status: str,
) -> tuple[str, str]:
    """Two stopped runs, each changing a path the other declares."""
    base = git(repository, "rev-parse", "HEAD")
    ours_tree = detached_tree(repository, tmp_path / "ours-tree")
    peer_tree = detached_tree(repository, tmp_path / "peer-tree")
    ours_head = commit_a_file(ours_tree, ACCEPTED)
    peer_head = commit_a_file(peer_tree, DECLARED)
    write_pointer(
        repository,
        OURS,
        base,
        declared=DECLARED,
        worktree=ours_tree,
        head=ours_head,
        pid=dead_pid(),
        manifest=write_manifest(tmp_path / "ours-manifest.md", "complete"),
    )
    write_pointer(
        repository,
        PEER,
        base,
        declared=ACCEPTED,
        worktree=peer_tree,
        head=peer_head,
        pid=peer_pid,
        manifest=write_manifest(tmp_path / "peer-manifest.md", peer_status),
    )
    return base, ours_head


def complete_arguments(head: str) -> list[str]:
    return [
        "crew",
        "complete",
        "--run",
        OURS,
        "--gate",
        "passed",
        "--commit",
        head,
        "--gate-command",
        "pytest tests/test_a_stopped_runs_claim_does_not_block_another_landing.py",
        "--gate-exit-status",
        "0",
        "--gate-log-path",
        "/durable/stopped-peer-landing.log",
    ]


def peer_record() -> str:
    return pointer_path(PEER).read_text(encoding="utf-8")


def test_stopped_peer_claim_is_recorded_on_the_acceptance(
    repository: Path, tmp_path: Path
) -> None:
    _, ours_head = build_pair(
        repository, tmp_path, peer_pid=dead_pid(), peer_status="complete"
    )
    git(repository, "merge", "-q", "--no-ff", ours_head, "-m", "Merge ours")
    peer_before = peer_record()

    result = CliRunner().invoke(
        cli_main,
        [
            *complete_arguments(ours_head),
            "--accept-path",
            ACCEPTED,
            "companion of the stopped peer's declared path",
        ],
    )

    assert result.exit_code == 0, result.output
    stored = json.loads(result.output)["record"]
    assert stored["scope_acceptances"] == [
        {
            "path": ACCEPTED,
            "reason": "companion of the stopped peer's declared path",
            "stopped_peer_claims": [{"run_id": PEER, "claim": ACCEPTED}],
        }
    ]
    assert not pointer_path(OURS).exists()
    assert peer_record() == peer_before
    assert ledger.runs(PROJECT, root=repository)


def test_live_peer_claim_still_refuses_the_acceptance(
    repository: Path, tmp_path: Path
) -> None:
    _, ours_head = build_pair(
        repository, tmp_path, peer_pid=os.getpid(), peer_status="complete"
    )
    peer_before = peer_record()

    result = CliRunner().invoke(
        cli_main,
        [
            *complete_arguments(ours_head),
            "--accept-path",
            ACCEPTED,
            "companion of a peer still editing",
        ],
    )

    assert result.exit_code == 1
    assert PEER in result.output
    assert ACCEPTED in result.output
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(OURS).is_file()
    assert peer_record() == peer_before


def test_ended_peer_without_a_terminal_manifest_still_refuses(
    repository: Path, tmp_path: Path
) -> None:
    _, ours_head = build_pair(
        repository, tmp_path, peer_pid=dead_pid(), peer_status="in-progress"
    )
    peer_before = peer_record()

    result = CliRunner().invoke(
        cli_main,
        [
            *complete_arguments(ours_head),
            "--accept-path",
            ACCEPTED,
            "companion of a crashed peer",
        ],
    )

    assert result.exit_code == 1
    assert PEER in result.output
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(OURS).is_file()
    assert peer_record() == peer_before


def test_manifest_from_a_previous_attempt_still_refuses(
    repository: Path, tmp_path: Path
) -> None:
    _, ours_head = build_pair(
        repository, tmp_path, peer_pid=dead_pid(), peer_status="complete"
    )
    stale = json.loads(peer_record())
    manifest = Path(stale["manifest_path"])
    # A resumed attempt writes a baseline after its predecessor's delivery, so
    # the terminal manifest still sitting at this path belongs to the run that
    # came before and is not this attempt's verdict.
    stale["manifest_baseline_mtime_ns"] = manifest.stat().st_mtime_ns + 1_000_000_000
    _write_json(pointer_path(PEER), stale)
    peer_before = peer_record()

    result = CliRunner().invoke(
        cli_main,
        [
            *complete_arguments(ours_head),
            "--accept-path",
            ACCEPTED,
            "companion of a peer whose manifest predates its attempt",
        ],
    )

    assert result.exit_code == 1
    assert PEER in result.output
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(OURS).is_file()
    assert peer_record() == peer_before


def test_a_dispatch_over_the_stopped_peers_path_still_refuses(
    repository: Path, tmp_path: Path
) -> None:
    base, _ = build_pair(
        repository, tmp_path, peer_pid=dead_pid(), peer_status="complete"
    )
    peer_before = peer_record()
    candidate = {
        "run_id": "r-newcomer",
        "project": PROJECT,
        "repo": str(repository),
        "base_sha": base,
        "node": {
            "id": "r-newcomer",
            "plan": "fixture",
            "section": "guard",
            "time_budget": "25m",
            "write_paths": [ACCEPTED],
        },
    }

    with pytest.raises(ScopeConflict, match=PEER):
        refuse_widen_scope_conflicts(candidate, [ACCEPTED])

    assert peer_record() == peer_before
