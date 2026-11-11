"""A request that never launched does not block another run's landing.

A claim protects a file from a second concurrent editor. A request that is
still parked — queued, waiting upstream, or held — has no worker and no
worktree, so it edits nothing and its claim protects nothing. A promotion that
accepts the same path is admitted and records the claim it walked past, while
the request's own pointer is left untouched, so the claim fences the path again
when the request reaches launch. A peer that is actually running keeps refusing
exactly as before.
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
from reckon.crew.node import WAITING_REQUEST_PHASES
from reckon.crew.runs import _write_json, list_live, pointer_path

PROJECT = "sample"
OURS = "r-landing-run"
PEER = "r-parked-request"
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
    (root / "docs" / "state" / PROJECT / "shared-write-paths.json").write_text(
        json.dumps({"shared_roots": []}), encoding="utf-8"
    )
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
    worktree: Path | None,
    head: str,
    pid: int | None,
    manifest: Path,
    phase: str | None = None,
) -> None:
    pointer: dict[str, object] = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "base_sha": base,
        "head": head,
        "launch": "in-harness",
        "launcher_host": socket.gethostname(),
        "role": "implement",
        "backend": "native",
        "created_at": "2026-11-03T00:00:00Z",
        "manifest_path": str(manifest),
        "node": {
            "id": run_id,
            "plan": "fixture",
            "section": "guard",
            "time_budget": "25m",
            "write_paths": [declared],
        },
    }
    if worktree is not None:
        pointer["worktree"] = str(worktree)
    if pid is not None:
        pointer["pid"] = pid
    if phase is not None:
        pointer["phase"] = phase
    _write_json(pointer_path(run_id), pointer)


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
    peer_phase: str | None,
    peer_pid: int | None,
) -> tuple[str, str]:
    """A landed run beside a parked request that claims the path it accepts."""
    base = git(repository, "rev-parse", "HEAD")
    ours_tree = detached_tree(repository, tmp_path / "ours-tree")
    ours_head = commit_a_file(ours_tree, ACCEPTED)
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
        worktree=None,
        head="",
        pid=peer_pid,
        manifest=write_manifest(tmp_path / "peer-manifest.md", "in-progress"),
        phase=peer_phase,
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
        "pytest tests/test_a_parked_request_does_not_block_a_landing.py",
        "--gate-exit-status",
        "0",
        "--gate-log-path",
        "/durable/parked-request-landing.log",
    ]


def peer_record() -> str:
    return pointer_path(PEER).read_text(encoding="utf-8")


@pytest.mark.parametrize("phase", sorted(WAITING_REQUEST_PHASES))
def test_parked_request_claim_is_recorded_on_the_acceptance(
    repository: Path, tmp_path: Path, phase: str
) -> None:
    _, ours_head = build_pair(repository, tmp_path, peer_phase=phase, peer_pid=None)
    git(repository, "merge", "-q", "--no-ff", ours_head, "-m", "Merge ours")

    result = CliRunner().invoke(
        cli_main,
        [
            *complete_arguments(ours_head),
            "--accept-path",
            ACCEPTED,
            "companion of the parked request's declared path",
        ],
    )

    assert result.exit_code == 0, result.output
    stored = json.loads(result.output)["record"]
    assert stored["scope_acceptances"] == [
        {
            "path": ACCEPTED,
            "reason": "companion of the parked request's declared path",
            "waiting_request_claims": [
                {"run_id": PEER, "claim": ACCEPTED, "phase": phase}
            ],
        }
    ]
    assert not pointer_path(OURS).exists()
    assert ledger.runs(PROJECT, root=repository)


def test_working_peer_claim_still_refuses_the_acceptance(
    repository: Path, tmp_path: Path
) -> None:
    _, ours_head = build_pair(
        repository, tmp_path, peer_phase="running", peer_pid=os.getpid()
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


def test_the_parked_requests_claim_is_still_recorded_after_the_acceptance(
    repository: Path, tmp_path: Path
) -> None:
    _, ours_head = build_pair(
        repository, tmp_path, peer_phase="awaiting-upstream", peer_pid=None
    )
    git(repository, "merge", "-q", "--no-ff", ours_head, "-m", "Merge ours")
    peer_before = peer_record()

    result = CliRunner().invoke(
        cli_main,
        [
            *complete_arguments(ours_head),
            "--accept-path",
            ACCEPTED,
            "companion of the parked request's declared path",
        ],
    )

    assert result.exit_code == 0, result.output
    assert peer_record() == peer_before
    assert PEER in {str(row.get("run_id") or "") for row in list_live()}
