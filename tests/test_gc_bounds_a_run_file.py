"""gc bounds a promoted run's captured file to the configured size.

A promoted run keeps the streams a later reader opens as evidence. Nothing else
bounds them, so a runaway log grows with the run directory's retention. These
tests build a promoted run directory holding an over-limit log and an
over-limit checkpoint, and assert gc bounds the log to its head and tail around
a marker naming the original size and sha256, reports the checkpoint and leaves
it untouched, and never reaches a run the ledger does not record.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from reckon import cli, ledger

PROJECT = "gc-proj"
HEAD_LINE = "HEAD-LINE the worker's first line"
TAIL_LINE = "TAIL-LINE the worker's last line"
FILLER = "filler line of some bytes padding the captured stream"
MiB = 1024 * 1024


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "seed.txt").write_text("seed\n")
    git(repo, "add", "seed.txt")
    git(repo, "commit", "-q", "-m", "test: seed")
    return repo


def _host_limit(home: Path, mb: int) -> None:
    """Set the run-file bound in the host flight layer.

    The limit is a host or project layer's to set — the shipped defaults layer
    carries no ``gc`` block — so the test sets it where a real host would.
    """
    home.mkdir(parents=True, exist_ok=True)
    (home / "flight.yaml").write_text(f"gc:\n  run_file_max_mb: {mb}\n")


def _write_log(path: Path, size: int) -> None:
    """Write a log of roughly ``size`` bytes with a distinct first and last line."""
    body = (HEAD_LINE + "\n").encode()
    body += (FILLER + "\n").encode() * ((size // (len(FILLER) + 1)) - 2)
    body += (TAIL_LINE + "\n").encode()
    path.write_bytes(body)


def _promoted_run(
    repo: Path,
    home: Path,
    run_id: str,
    *,
    extra_manifest: str = "",
) -> tuple[Path, Path, Path, Path]:
    """Build a promoted run directory with an over-limit log and checkpoint.

    Returns the run directory, the log, the checkpoint and the manifest path.
    """
    run_dir = home / "crew" / "runs" / run_id
    run_dir.mkdir(parents=True)
    log = run_dir / "worker.log"
    checkpoint = run_dir / "checkpoint.pt"
    manifest = run_dir / "manifest.md"
    _write_log(log, 3 * MiB)
    checkpoint.write_bytes(b"\x00" * (3 * MiB))
    manifest.write_text(
        f"status: complete\nnode: node-a\n{extra_manifest}", encoding="utf-8"
    )
    record = ledger.build_record(
        run_id=run_id,
        plan="plan-a",
        gate="passed",
        node="node-a",
        project=PROJECT,
        manifest_path=str(manifest),
    )
    ledger.append_run(PROJECT, record, root=repo)
    return run_dir, log, checkpoint, manifest


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _marker_length(size: int, digest: str) -> int:
    return len(
        f"\n[reckon gc: truncated from {size} bytes; sha256 {digest}]\n".encode()
    )


def test_gc_bounds_a_promoted_runs_log_and_reports_its_checkpoint(
    tmp_path, monkeypatch
):
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    _host_limit(home, 1)
    repo = repository(tmp_path)

    _, log, checkpoint, _ = _promoted_run(repo, home, "run-promoted")
    original_size = log.stat().st_size
    original_digest = _sha256(log)
    checkpoint_bytes = checkpoint.read_bytes()

    result = CliRunner().invoke(
        cli.main, ["crew", "gc", "--repo", str(repo), "--apply"]
    )
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    rows = {row["path"]: row for row in payload["run_files"]}
    assert rows[str(log)]["bounded"] is True
    assert rows[str(log)]["kind"] == "run-file"
    assert rows[str(checkpoint)]["binary"] is True
    assert rows[str(checkpoint)]["action"] == "reported"
    assert sum(1 for row in payload["run_files"] if row["bounded"]) == 1
    assert sum(1 for row in payload["run_files"] if row["binary"]) == 1

    # The log keeps its head and its tail around the marker, so it is the limit
    # plus the marker and never more.
    bounded = log.stat().st_size
    marker_len = _marker_length(original_size, original_digest)
    assert bounded <= 1 * MiB + marker_len, (
        f"the bounded log is {bounded} bytes against a {1 * MiB + marker_len}-byte "
        "ceiling of limit plus marker"
    )
    text = log.read_text(encoding="utf-8")
    assert text.splitlines()[0] == HEAD_LINE, "the log lost its first line"
    assert text.splitlines()[-1] == TAIL_LINE, "the log lost its last line"
    assert original_digest in text, "the marker does not carry the original sha256"

    # The binary artefact is reported, never rewritten.
    assert checkpoint.read_bytes() == checkpoint_bytes
    assert checkpoint.stat().st_size == 3 * MiB


def test_gc_leaves_an_unpromoted_run_directory_untouched(tmp_path, monkeypatch):
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    _host_limit(home, 1)
    repo = repository(tmp_path)

    _promoted_run(repo, home, "run-promoted")
    unpromoted = home / "crew" / "runs" / "run-unpromoted"
    unpromoted.mkdir(parents=True)
    log = unpromoted / "worker.log"
    _write_log(log, 3 * MiB)
    before = log.read_bytes()

    result = CliRunner().invoke(
        cli.main, ["crew", "gc", "--repo", str(repo), "--apply"]
    )

    assert result.exit_code == 0, result.output
    assert log.read_bytes() == before, (
        "an unpromoted run's log was bounded; only a promoted run is held to the limit"
    )


def test_gc_bounds_a_declared_gate_log_outside_the_run_directory(tmp_path, monkeypatch):
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    _host_limit(home, 1)
    repo = repository(tmp_path)

    gate_log = tmp_path / "gate" / "NODE-gate.log"
    gate_log.parent.mkdir(parents=True)
    _write_log(gate_log, 3 * MiB)
    original_digest = _sha256(gate_log)

    _promoted_run(
        repo,
        home,
        "run-promoted",
        extra_manifest=f"test_logs: {gate_log}\n",
    )

    result = CliRunner().invoke(
        cli.main, ["crew", "gc", "--repo", str(repo), "--apply"]
    )
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    rows = {row["path"]: row for row in payload["run_files"]}
    assert rows[str(gate_log)]["kind"] == "gate-log"
    assert rows[str(gate_log)]["bounded"] is True
    assert gate_log.stat().st_size <= 1 * MiB + _marker_length(
        rows[str(gate_log)]["size"], original_digest
    )
    assert original_digest in gate_log.read_text(encoding="utf-8")
