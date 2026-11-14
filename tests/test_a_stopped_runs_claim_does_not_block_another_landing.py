"""Source peers share writes while stopped artifact claims retain their safeguards.

Acceptance of a source path is independent of a peer's liveness or manifest.
An artifact held by a live or undelivered peer still refuses; a stopped,
terminal holder is recorded on an artifact acceptance. Every case runs in a
temporary repository and crew home.
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
from reckon.crew import promotion_scope
from reckon.crew.dispatch_claims import refuse_widen_scope_conflicts
from reckon.crew.node import CrewError, ScopeConflict
from reckon.crew.runs import _write_json, pointer_path, read_pointer

PROJECT = "sample"
OURS = "r-landing-run"
PEER = "r-stopped-peer"
ACCEPTED = "declared-b.txt"
DECLARED = "declared-a.txt"


@pytest.fixture(params=[False, True], autouse=True)
def exclusive_claim(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> bool:
    """Keep artifact refusals beside source-admission controls."""
    exclusive = bool(request.param)
    suffix = "receipt" if exclusive else "txt"
    monkeypatch.setitem(globals(), "ACCEPTED", f"declared-b.{suffix}")
    monkeypatch.setitem(globals(), "DECLARED", f"declared-a.{suffix}")
    return exclusive


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


def _accept(head: str) -> list[dict]:
    record = read_pointer(OURS)
    return promotion_scope._accepted_scope_exceptions(
        OURS,
        [ACCEPTED],
        {ACCEPTED: "companion source or artifact edit"},
        record=record,
        tree=Path(record["worktree"]),
        commits=[head],
    )


def test_stopped_artifact_peer_is_recorded_on_the_acceptance(
    repository: Path, tmp_path: Path, exclusive_claim: bool
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
    expected = {
        "path": ACCEPTED,
        "reason": "companion of the stopped peer's declared path",
    }
    if exclusive_claim:
        expected["stopped_peer_claims"] = [{"run_id": PEER, "claim": ACCEPTED}]
    assert stored["scope_acceptances"] == [expected]
    assert not pointer_path(OURS).exists()
    assert peer_record() == peer_before
    assert ledger.runs(PROJECT, root=repository)


def test_live_peer_acceptance_follows_claim_exclusivity(
    repository: Path, tmp_path: Path, exclusive_claim: bool
) -> None:
    _, ours_head = build_pair(
        repository, tmp_path, peer_pid=os.getpid(), peer_status="complete"
    )
    peer_before = peer_record()

    if exclusive_claim:
        with pytest.raises(CrewError, match=PEER) as refused:
            _accept(ours_head)
        assert ACCEPTED in str(refused.value)
    else:
        assert _accept(ours_head) == [
            {"path": ACCEPTED, "reason": "companion source or artifact edit"}
        ]
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(OURS).is_file()
    assert peer_record() == peer_before


def test_an_undelivered_peer_only_refuses_an_artifact(
    repository: Path, tmp_path: Path, exclusive_claim: bool
) -> None:
    _, ours_head = build_pair(
        repository, tmp_path, peer_pid=dead_pid(), peer_status="in-progress"
    )
    peer_before = peer_record()

    if exclusive_claim:
        with pytest.raises(CrewError, match=PEER):
            _accept(ours_head)
    else:
        assert _accept(ours_head) == [
            {"path": ACCEPTED, "reason": "companion source or artifact edit"}
        ]
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(OURS).is_file()
    assert peer_record() == peer_before


def test_a_stale_manifest_only_refuses_an_artifact(
    repository: Path, tmp_path: Path, exclusive_claim: bool
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

    if exclusive_claim:
        with pytest.raises(CrewError, match=PEER):
            _accept(ours_head)
    else:
        assert _accept(ours_head) == [
            {"path": ACCEPTED, "reason": "companion source or artifact edit"}
        ]
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(OURS).is_file()
    assert peer_record() == peer_before


def test_a_dispatch_over_a_stopped_peer_follows_claim_exclusivity(
    repository: Path, tmp_path: Path, exclusive_claim: bool
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

    if exclusive_claim:
        with pytest.raises(ScopeConflict, match=PEER):
            refuse_widen_scope_conflicts(candidate, [ACCEPTED])
    else:
        refuse_widen_scope_conflicts(candidate, [ACCEPTED])

    assert peer_record() == peer_before
