"""Promotion refuses to orphan a worker whose own process is still running.

Promotion deletes the run's live pointer, so a run promoted while its own
process is still alive continues with no pointer, no follower row and no
obligation to any coordinator until it exits on its own. The guard fires only
on the conjunction that makes that harm real: the recorded process is alive and
its manifest states a status that is not terminal. A run whose process has
exited, or whose manifest reads complete, blocked or failed, promotes as before.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew.runs import _write_json, pointer_path, process_alive

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
    """A synthetic config home and repository, isolated from the real fleet."""
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
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _spawn_stub() -> subprocess.Popen:
    """A live process the test starts and stops itself."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
    )


def _stop_stub(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)


def _write_manifest(path: Path, status: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"node: live-run-check\nstatus: {status}\ntests: not applicable\n",
        encoding="utf-8",
    )
    return path


def _pointer(
    repository: Path,
    run_id: str,
    base: str,
    *,
    manifest_path: Path | None = None,
    pid: int | None = None,
) -> None:
    pointer: dict[str, object] = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(repository),
        "base_sha": base,
        "launch": "in-harness",
        "role": "implement",
        "member": "worker-live-run-check",
        "backend": "native",
        "created_at": "2026-09-25T16:50:00Z",
        "manifest_path": str(manifest_path) if manifest_path else "",
        "node": {
            "id": "live-run-check",
            "plan": "fixture",
            "section": "guard",
            "time_budget": "40m",
            "write_paths": [],
        },
    }
    if pid is not None:
        pointer["pid"] = pid
    _write_json(pointer_path(run_id), pointer)


def _promote(repository: Path, run_id: str, **extra: str) -> dict:
    return crew.complete(
        run_id,
        gate="not-run",
        outcome="promotion attempted under the live-worker guard",
        root=repository,
        **extra,
    )


def test_a_live_in_progress_run_is_refused_naming_pid_and_status(
    repository: Path, tmp_path: Path
) -> None:
    """The measured defect: a live, in-progress run promoted anyway."""
    base = _git(repository, "rev-parse", "HEAD")
    run_id = "r-live-in-progress"
    manifest = _write_manifest(tmp_path / "manifests" / f"{run_id}.md", "in-progress")
    proc = _spawn_stub()
    try:
        assert process_alive(proc.pid) is True
        _pointer(repository, run_id, base, manifest_path=manifest, pid=proc.pid)

        with pytest.raises(crew.CrewError) as refusal:
            _promote(repository, run_id)

        message = str(refusal.value)
        assert str(proc.pid) in message
        assert "in-progress" in message
        # Nothing irreversible ran: the pointer survives and no row was written.
        assert pointer_path(run_id).is_file()
        assert ledger.runs(PROJECT, root=repository) == []
    finally:
        _stop_stub(proc)


def test_a_live_run_promotes_with_a_waiver_that_is_recorded(
    repository: Path, tmp_path: Path
) -> None:
    """An explicit waiver names the reason, promotes, and rides the row."""
    base = _git(repository, "rev-parse", "HEAD")
    run_id = "r-live-waived"
    manifest = _write_manifest(tmp_path / "manifests" / f"{run_id}.md", "in-progress")
    proc = _spawn_stub()
    reason = "its review record already landed; the live worker may be orphaned"
    try:
        _pointer(repository, run_id, base, manifest_path=manifest, pid=proc.pid)

        stored = _promote(repository, run_id, live_run_waiver=reason)["record"]

        assert stored["live_run_waiver"]["reason"] == reason
        assert stored["live_run_waiver"]["status"] == "in-progress"
        assert stored["live_run_waiver"]["pid"] == str(proc.pid)
        assert not pointer_path(run_id).exists()
        assert len(ledger.runs(PROJECT, root=repository)) == 1
    finally:
        _stop_stub(proc)


def test_a_run_whose_worker_exited_promotes_as_before(
    repository: Path, tmp_path: Path
) -> None:
    """A dead process with a non-terminal manifest is not the guarded case."""
    base = _git(repository, "rev-parse", "HEAD")
    run_id = "r-worker-exited"
    manifest = _write_manifest(tmp_path / "manifests" / f"{run_id}.md", "in-progress")
    proc = _spawn_stub()
    _stop_stub(proc)
    deadline = time.monotonic() + 5
    while process_alive(proc.pid) is not False and time.monotonic() < deadline:
        time.sleep(0.05)
    assert process_alive(proc.pid) is not True
    _pointer(repository, run_id, base, manifest_path=manifest, pid=proc.pid)

    stored = _promote(repository, run_id)["record"]

    assert stored["gate"] == "not-run"
    assert "live_run_waiver" not in stored
    assert not pointer_path(run_id).exists()


def test_a_live_worker_with_a_terminal_manifest_promotes_as_before(
    repository: Path, tmp_path: Path
) -> None:
    """A terminal manifest is the release step's own condition, not a refusal."""
    base = _git(repository, "rev-parse", "HEAD")
    run_id = "r-live-terminal-manifest"
    manifest = _write_manifest(tmp_path / "manifests" / f"{run_id}.md", "complete")
    proc = _spawn_stub()
    try:
        _pointer(repository, run_id, base, manifest_path=manifest, pid=proc.pid)

        stored = _promote(repository, run_id)["record"]

        assert stored["gate"] == "not-run"
        assert "live_run_waiver" not in stored
        assert not pointer_path(run_id).exists()
    finally:
        _stop_stub(proc)


def test_a_waiver_with_no_live_worker_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """An unconditional waiver would stop meaning anything."""
    base = _git(repository, "rev-parse", "HEAD")
    run_id = "r-terminal-waived"
    manifest = _write_manifest(tmp_path / "manifests" / f"{run_id}.md", "complete")
    _pointer(repository, run_id, base, manifest_path=manifest)

    with pytest.raises(crew.CrewError, match="no live, in-progress worker"):
        _promote(repository, run_id, live_run_waiver="nothing to waive")

    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_a_live_run_with_no_manifest_is_left_to_recovery(
    repository: Path, tmp_path: Path
) -> None:
    """A live process with no manifest states no status, so the guard is silent."""
    base = _git(repository, "rev-parse", "HEAD")
    run_id = "r-live-no-manifest"
    proc = _spawn_stub()
    try:
        _pointer(repository, run_id, base, pid=proc.pid)

        stored = _promote(repository, run_id)["record"]

        assert stored["gate"] == "not-run"
        assert "live_run_waiver" not in stored
        assert not pointer_path(run_id).exists()
    finally:
        _stop_stub(proc)
