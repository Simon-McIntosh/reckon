"""Recording release preserves every commit another session could publish."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _store, ledger
from reckon.cli import main
from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path
from tests.test_promotion_stages_its_one_run_file import _git, _observe_writes


def test_write_observer_refuses_a_protected_write(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    target = protected / "pointer.json"
    with _observe_writes((protected,)) as (observed, forbidden):
        with pytest.raises(
            AssertionError, match="completion tried to mutate real state"
        ):
            target.write_text("{}\n")
        assert observed == forbidden == [target]
    assert not target.exists()


@pytest.mark.parametrize("peer_commit", [False, True])
@pytest.mark.parametrize("dirty_worktree", [False, True])
def test_complete_preserves_the_promotion_commit_through_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    peer_commit: bool,
    dirty_worktree: bool,
) -> None:
    with monkeypatch.context() as context:
        context.delenv("RECKON_HOME", raising=False)
        real_home = _store._config_home().resolve()
    config = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(config))
    monkeypatch.delenv("RECKON_RUN_STORE", raising=False)
    repo = tmp_path / "repository"
    state = repo / "docs" / "state" / "reckon"
    state.mkdir(parents=True)
    (state / "index.json").write_text('{"project":"reckon","data":{}}\n')
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "add", "docs/state/reckon/index.json")
    _git(
        repo, "commit", "-qm", "test: seed repository", "-m", "Isolate promotion state."
    )
    config.mkdir()
    (config / "mounts.json").write_text(json.dumps({"reckon": str(repo / "docs")}))
    worktree = tmp_path / "worker"
    _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
    if dirty_worktree:
        (worktree / "keep.txt").write_text("uncommitted worker result\n")
    run_id = "r-release"
    manifest = tmp_path / "manifest.md"
    manifest.write_text("status: complete\nchanged_paths: []\n")
    gate_log = tmp_path / "gate.log"
    command = [sys.executable, "-c", "print('promotion fixture ready')"]
    with gate_log.open("w") as log:
        checked = subprocess.run(
            command, stdout=log, stderr=subprocess.STDOUT, check=True
        )
        log.write(f"\nEXIT={checked.returncode}\n")
    run_file = state / "runs" / f"{run_id}.json"
    relative = run_file.relative_to(repo).as_posix()
    release_step = promotion._release_after_promotion
    boundary = {}

    def observe_release(*args, **kwargs):
        boundary["promote"] = _git(repo, "rev-parse", "HEAD")
        boundary["object"] = _git(repo, "cat-file", "commit", "HEAD")
        boundary["row"] = _git(repo, "show", f"HEAD:{relative}")
        assert json.loads(boundary["row"])["run_id"] == run_id
        assert "release" not in json.loads(boundary["row"])
        if peer_commit:
            (repo / "peer.txt").write_text("a peer landed while release ran\n")
            _git(repo, "add", "peer.txt")
            _git(
                repo,
                "commit",
                "-qm",
                "test: peer landing",
                "-m",
                "Exercise interleaving.",
            )
            boundary["peer"] = _git(repo, "rev-parse", "HEAD")
            # A release commit must leave unrelated staged work for its owner.
            (repo / "pending.txt").write_text("a peer has more work staged\n")
            _git(repo, "add", "pending.txt")
        result = release_step(*args, **kwargs)
        boundary["after_release"] = _git(repo, "rev-parse", "HEAD")
        return result

    monkeypatch.setattr(promotion, "_release_after_promotion", observe_release)
    with _observe_writes((real_home,)) as (observed, forbidden):
        pointer = pointer_path(run_id)
        assert pointer.parent == config / "crew" / "live"
        _write_json(
            pointer,
            {
                "run_id": run_id,
                "project": "reckon",
                "repo": str(repo),
                "worktree": str(worktree),
                "base_sha": _git(repo, "rev-parse", "HEAD"),
                "launch": "in-harness",
                "role": "investigate",
                "backend": "native",
                "created_at": "2026-01-01T00:00:00Z",
                "manifest_path": str(manifest),
                "node": {"id": "release", "write_paths": [], "time_budget": "1m"},
            },
        )
        result = CliRunner().invoke(
            main,
            [
                "crew",
                "complete",
                "--run",
                run_id,
                "--gate",
                "passed",
                "--checkout-path",
                str(repo),
                "--gate-command",
                " ".join(command),
                "--gate-exit-status",
                "0",
                "--gate-log-path",
                str(gate_log),
                "--completed-at",
                "2026-01-01T00:00:01Z",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert payload["pointer_removed"] is True
        assert not pointer.exists()
        assert payload["release"]["worktree_released"] is not dirty_worktree
        if dirty_worktree:
            assert (worktree / "keep.txt").read_text() == "uncommitted worker result\n"
            assert "uncommitted changes" in payload["release"]["worktree_withheld"]
        else:
            assert not worktree.exists()

        reflog = _git(repo, "reflog", "--format=%H %gs")
        print(f"promote before release: {boundary['promote']}")
        print(f"HEAD after release: {boundary['after_release']}")
        print(f"HEAD after complete: {_git(repo, 'rev-parse', 'HEAD')}")
        print(reflog)
        assert "commit (amend)" not in reflog, f"no-amend reflog assertion: {reflog}"
        assert boundary["after_release"] == boundary.get("peer", boundary["promote"])
        _git(repo, "merge-base", "--is-ancestor", boundary["promote"], "HEAD")
        assert (
            _git(repo, "cat-file", "commit", boundary["promote"]) == boundary["object"]
        )
        assert (
            _git(repo, "show", f"{boundary['promote']}:{relative}") == boundary["row"]
        )
        assert _git(repo, "rev-parse", "HEAD^") == boundary["after_release"]
        assert (
            _git(repo, "log", "--diff-filter=A", "--format=%H", "--", relative)
            == boundary["promote"]
        )
        assert (
            _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
            == relative
        )
        row = json.loads(_git(repo, "show", f"HEAD:{relative}"))
        assert row == payload["record"]
        assert row["release"] == payload["release"]
        assert run_file.read_text() == ledger.serialize_run(row)
        if peer_commit:
            _git(repo, "merge-base", "--is-ancestor", boundary["peer"], "HEAD")
            assert (
                _git(repo, "show", "HEAD:peer.txt") == "a peer landed while release ran"
            )
            assert _git(repo, "diff", "--cached", "--name-only") == "pending.txt"
        else:
            assert _git(repo, "status", "--porcelain") == ""
        assert any(path.is_relative_to(config / "crew" / "live") for path in observed)
        assert any(
            path.is_relative_to(config) and path.suffix == ".db" for path in observed
        )
        assert forbidden == [], f"real config home changed: {forbidden}"
