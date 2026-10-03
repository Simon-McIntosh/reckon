"""An already-integrated companion path is accepted despite a live peer claim.

A run's promotion writes the ledger record of a landing, and by the time it
runs its commits may already be behind the integration head while a live peer
claims a companion path outside the run's fence. The claim refuses an
acceptance only while it protects a concurrent edit: a change already reachable
from the integration head is admitted, with the peer's claim and pointer left
untouched and the acceptance recording both facts. A path whose change is not
yet on the integration head stays refused on a live claim.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "sample"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "allowed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "allowed.txt"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _pointer(
    repository: Path,
    run_id: str,
    base: str,
    *,
    write_paths: tuple[str, ...],
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": base,
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-10-03T01:00:00Z",
            "node": {
                "id": "companion-claim",
                "plan": "fixture",
                "section": "guard",
                "time_budget": "25m",
                "write_paths": list(write_paths),
            },
        },
    )


def _commit_artifact_with_companion(run_tree: Path) -> str:
    (run_tree / "artifact.json").write_text('{"result": "ready"}\n')
    (run_tree / "artifact.png").write_bytes(b"companion image\n")
    _git(run_tree, "add", "artifact.json", "artifact.png")
    _git(run_tree, "commit", "-q", "-m", "test: generate artifact pair")
    return _git(run_tree, "rev-parse", "HEAD")


def _run_with_companion(repository: Path, tmp_path: Path, run_id: str) -> str:
    """Build a run that changed a declared path plus an undeclared companion."""
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = tmp_path / f"{run_id}-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(run_tree), "HEAD")
    _pointer(repository, run_id, base, write_paths=("artifact.json",))
    return _commit_artifact_with_companion(run_tree)


def test_integrated_companion_path_is_accepted_under_a_live_peer_claim(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-integrated-companion"
    peer_runs = ("r-companion-owner-a", "r-companion-owner-b")
    commit = _run_with_companion(repository, tmp_path, run_id)
    for peer_run in peer_runs:
        _pointer(repository, peer_run, commit, write_paths=("artifact.png",))
    peers_before = {
        peer_run: pointer_path(peer_run).read_bytes() for peer_run in peer_runs
    }
    # The run's commit is merged strictly behind the new head, so its change to
    # the companion path is already on the integration head while the peers are
    # still live and still claim it.
    _git(repository, "merge", "-q", "--no-ff", commit, "-m", "test: merge the run")

    stored = crew.complete(
        run_id,
        gate="passed",
        commits=[commit],
        root=repository,
        accepted_paths={"artifact.png": "rendered companion"},
    )["record"]

    assert stored["scope_acceptances"] == [
        {
            "path": "artifact.png",
            "reason": "rendered companion",
            "already_integrated": True,
            "peer_claims": [
                {"run_id": "r-companion-owner-a", "claim": "artifact.png"},
                {"run_id": "r-companion-owner-b", "claim": "artifact.png"},
            ],
        }
    ]
    assert not pointer_path(run_id).exists()
    for peer_run in peer_runs:
        assert pointer_path(peer_run).read_bytes() == peers_before[peer_run], (
            f"the promotion rewrote live run {peer_run}'s pointer"
        )
    assert [row["run_id"] for row in ledger.runs(PROJECT, root=repository)] == [run_id]


def test_unintegrated_companion_path_is_still_refused_on_a_live_claim(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-unintegrated-companion"
    peer_run = "r-unintegrated-companion-owner"
    commit = _run_with_companion(repository, tmp_path, run_id)
    _pointer(repository, peer_run, commit, write_paths=("artifact.png",))
    peer_pointer_before = pointer_path(peer_run).read_bytes()
    # The commit is not reachable from the integration head: no commit merged
    # past it, so the head is still the seed the run branched from.
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    assert ancestor.returncode == 1, "fixture error: the run is already merged"

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(
            run_id,
            gate="passed",
            commits=[commit],
            root=repository,
            accepted_paths={"artifact.png": "rendered companion"},
        )

    assert (
        f"cannot accept artifact.png: live run '{peer_run}' claims artifact.png"
    ) in str(refusal.value)
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()
    assert pointer_path(peer_run).read_bytes() == peer_pointer_before
