"""The completion command commits each run and its release receipt together."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _store, ledger
from reckon.cli import main
from reckon.crew.runs import _write_json, pointer_path


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stderr=subprocess.STDOUT
    ).strip()


@contextmanager
def _observe_writes(protected: tuple[Path, ...]):
    """Refuse a real-home mutation while observing writes to the test's home."""
    observed: list[Path] = []
    forbidden: list[Path] = []
    active = True

    def audit(event: str, args: tuple) -> None:
        if not active:
            return
        paths = []
        if event == "open" and args[2] & (
            os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        ):
            paths = args[:1]
        elif event in {"os.rename", "os.link", "os.symlink"}:
            paths = args[:2]
        elif event in {
            "os.remove",
            "os.rmdir",
            "os.mkdir",
            "os.chmod",
            "os.chown",
            "os.truncate",
            "os.utime",
            "sqlite3.connect",
        }:
            paths = args[:1]
        for value in paths:
            if isinstance(value, (str, bytes, os.PathLike)):
                path = Path(os.fsdecode(value)).resolve()
                observed.append(path)
                if any(path.is_relative_to(root) for root in protected):
                    forbidden.append(path)
                    raise AssertionError(
                        f"completion tried to mutate real state: {path}"
                    )

    sys.addaudithook(audit)
    try:
        yield observed, forbidden
    finally:
        active = False


def test_write_observer_detects_a_forbidden_pointer(tmp_path: Path) -> None:
    protected = tmp_path / "protected-home"
    live = protected / "crew" / "live"
    live.mkdir(parents=True)
    target = live / "r-control.json"
    with (
        _observe_writes((protected, live)) as (observed, forbidden),
        pytest.raises(
            AssertionError, match="completion tried to mutate real state"
        ),
    ):
        target.write_text("{}\n")
    assert target in observed
    assert forbidden == [target]
    assert not target.exists()


@pytest.mark.parametrize("aggregate_present", [False, True])
def test_complete_commits_one_run_file_per_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, aggregate_present: bool
) -> None:
    # Resolve the default independently of the suite's temporary RECKON_HOME.
    with monkeypatch.context() as context:
        context.delenv("RECKON_HOME", raising=False)
        real_home = _store._config_home().resolve()
    real_live = real_home / "crew" / "live"
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
    aggregate = state / "crew.json"
    if aggregate_present:
        aggregate.write_text(
            json.dumps({"data": {"members": [], "runs": [], "holds": []}})
        )
        _git(repo, "add", "docs/state/reckon/crew.json")
    _git(
        repo,
        "commit",
        "-qm",
        "test: seed repository",
        "-m",
        "Provide an isolated promotion destination.",
    )
    aggregate_before = aggregate.read_bytes() if aggregate_present else None
    config.mkdir()
    (config / "mounts.json").write_text(json.dumps({"reckon": str(repo / "docs")}))
    gate_log = tmp_path / "gate.log"
    command = [sys.executable, "-c", "print('promotion fixture ready')"]
    with gate_log.open("w") as log:
        checked = subprocess.run(
            command, stdout=log, stderr=subprocess.STDOUT, check=True
        )
        log.write(f"\nEXIT={checked.returncode}\n")

    committed = {}
    with _observe_writes((real_home, real_live)) as (observed, forbidden):
        for name in ("alpha", "beta"):
            run_id = f"r-promotion-{name}"
            worktree = tmp_path / f"worktree-{name}"
            _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
            manifest = tmp_path / f"{run_id}.md"
            manifest.write_text("status: complete\nchanged_paths: []\n")
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
                    "node": {"id": name, "write_paths": [], "time_budget": "1m"},
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
            run_file = state / "runs" / f"{run_id}.json"
            files = _git(
                repo,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                "--root",
                "HEAD",
            ).splitlines()
            assert files == [run_file.relative_to(repo).as_posix()], (
                f"one-run-file commit assertion: {files}; completion: {result.output}"
            )
            assert result.exit_code == 0, result.output
            payload = json.loads(result.output)
            assert payload["ok"] is True
            assert payload["ledger_path"] == str(run_file)
            assert payload["pointer_removed"] is True
            assert not pointer.exists()
            assert payload["release"]["worktree_released"] is True
            assert not worktree.exists()
            row = json.loads(run_file.read_text())
            assert row["run_id"] == run_id
            assert row["release"] == payload["release"]
            assert row == payload["record"]
            assert run_file.read_text() == ledger.serialize_run(row)
            assert (
                _git(repo, "show", f"HEAD:{run_file.relative_to(repo)}")
                == run_file.read_text().strip()
            )
            for prior, content in committed.items():
                assert prior.read_bytes() == content
            committed[run_file] = run_file.read_bytes()
            assert (
                aggregate.read_bytes() if aggregate.exists() else None
            ) == aggregate_before
            assert _git(repo, "status", "--porcelain") == ""
        assert any(path.is_relative_to(config / "crew" / "live") for path in observed)
        assert any(
            path.is_relative_to(config) and path.suffix == ".db" for path in observed
        )
        assert forbidden == [], (
            f"real config home or pointer directory changed: {forbidden}"
        )
    assert len(committed) == 2
