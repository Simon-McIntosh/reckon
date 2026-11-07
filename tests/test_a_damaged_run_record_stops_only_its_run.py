"""A damaged run document is reported without hiding healthy runs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import ledger
from reckon.cli import main
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "sample"


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.STDOUT
    ).strip()


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    root = tmp_path / "repo"
    state = root / "docs" / "state" / PROJECT
    records = state / "runs"
    records.mkdir(parents=True)
    (state / "index.json").write_text('{"project":"sample","data":{}}\n')
    ledger.ledger_path(PROJECT, root).write_text(
        json.dumps({"data": {"members": [], "runs": [], "holds": []}})
    )
    (records / "r-healthy.json").write_text('{"run_id":"r-healthy"}\n')
    damaged = records / "r-damaged.json"
    damaged.write_bytes(b"")
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(root / "docs")}))
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "add", "docs/state/sample/index.json", "docs/state/sample/crew.json")
    _git(
        root,
        "commit",
        "-qm",
        "test: seed repository",
        "-m",
        "Provide an isolated destination for promotion.",
    )
    return root, damaged


def _assert_healthy_row_and_damage(
    root: Path, damaged: Path, *, use_index: bool
) -> None:
    data, _version = ledger.load(PROJECT, root, use_index=use_index)
    assert [row["run_id"] for row in data["runs"]] == ["r-healthy"]
    assert ledger.damaged_run_records(PROJECT) == (damaged,)


def test_index_read_keeps_the_healthy_run(
    store: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    root, damaged = store
    _assert_healthy_row_and_damage(root, damaged, use_index=True)
    assert str(damaged) in caplog.text
    _assert_healthy_row_and_damage(root, damaged, use_index=True)


def test_fallback_read_keeps_the_healthy_run(
    store: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    root, damaged = store
    _assert_healthy_row_and_damage(root, damaged, use_index=False)
    assert str(damaged) in caplog.text


def _pointer(root: Path, tmp_path: Path, run_id: str) -> Path:
    worktree = tmp_path / f"tree-{run_id}"
    _git(root, "worktree", "add", "--detach", str(worktree), "HEAD")
    manifest = tmp_path / f"{run_id}.md"
    manifest.write_text(
        f"status: complete\nchanged_paths: {tmp_path / (run_id + '-report.md')}\n"
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(root),
            "worktree": str(worktree),
            "base_sha": _git(root, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "role": "investigate",
            "backend": "native",
            "created_at": "2026-01-01T00:00:00Z",
            "manifest_path": str(manifest),
            "node": {"id": run_id, "write_paths": [], "time_budget": "1m"},
        },
    )
    return worktree


def _complete(root: Path, tmp_path: Path, run_id: str):
    _pointer(root, tmp_path, run_id)
    gate_log = tmp_path / f"{run_id}.log"
    gate_log.write_text("promotion fixture ready\nEXIT=0\n")
    return CliRunner().invoke(
        main,
        [
            "crew",
            "complete",
            "--run",
            run_id,
            "--gate",
            "passed",
            "--checkout-path",
            str(root),
            "--gate-command",
            f"{sys.executable} -c pass",
            "--gate-exit-status",
            "0",
            "--gate-log-path",
            str(gate_log),
            "--completed-at",
            "2026-01-01T00:00:01Z",
        ],
    )


def test_promotion_refuses_only_the_damaged_run(
    store: tuple[Path, Path], tmp_path: Path
) -> None:
    root, damaged = store
    healthy = _complete(root, tmp_path, "r-fresh")
    assert healthy.exit_code == 0, healthy.output
    assert json.loads(healthy.output)["ok"] is True
    refused = _complete(root, tmp_path, "r-damaged")
    assert refused.exit_code != 0
    assert str(damaged) in refused.output
    assert ledger.run_path(PROJECT, "r-fresh", root).stat().st_size != 0
    assert damaged.stat().st_size == 0
