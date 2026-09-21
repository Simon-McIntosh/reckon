"""Rollback preserves committed paths and reports an append before later failures."""

import subprocess
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    tracked = root / "tracked file.txt"
    tracked.write_text("committed content\n")
    _git(root, "add", "--", str(tracked))
    _git(root, "commit", "-qm", "test: seed", "-m", "Seed the tracked path.")
    return root


def test_head_path_survives_unrelated_restore_failure(repository, monkeypatch):
    tracked = repository / "tracked file.txt"
    assert _git(repository, "cat-file", "-e", "HEAD:tracked file.txt").returncode == 0
    tracked.write_text("landing content\n")
    real_git = promotion._git
    restores = []

    def fail_restore(checkout, *args, **kwargs):
        if args[0] == "restore":
            restores.append(args)
            return subprocess.CompletedProcess(args, 128, "", "index is locked")
        return real_git(checkout, *args, **kwargs)

    monkeypatch.setattr(promotion, "_git", fail_restore)
    promotion._restore_landing_writes(repository, [tracked])
    assert len(restores) == 1
    assert tracked.is_file(), (
        "HEAD carries this path; restore failure cannot authorize unlink"
    )
    assert tracked.read_text() == "landing content\n"


def test_path_absent_from_head_is_dropped(repository, monkeypatch):
    created = repository / "created file.txt"
    created.write_text("landing content\n")
    _git(repository, "add", "--", str(created))
    real_git = promotion._git
    calls = []

    def fail_restore(checkout, *args, **kwargs):
        calls.append(args[0])
        if args[0] == "restore":
            return subprocess.CompletedProcess(args, 128, "", "index is locked")
        return real_git(checkout, *args, **kwargs)

    monkeypatch.setattr(promotion, "_git", fail_restore)
    promotion._restore_landing_writes(repository, [created])
    assert "restore" in calls
    assert "rm" in calls
    assert not created.exists()
    assert not _git(repository, "ls-files", "--", str(created)).stdout.strip()


def test_path_outside_checkout_survives_with_staging_error(repository):
    outside = repository.parent / "outside.txt"
    outside.write_text("unrelated content\n")

    with pytest.raises(promotion.CrewError, match="could not stage the landing writes"):
        promotion._commit_landing_writes(
            run_id="outside", verdict="passed", checkout=repository, paths=[outside]
        )

    assert outside.is_file()
    assert outside.read_text() == "unrelated content\n"


def test_real_index_lock_preserves_head_path(repository, monkeypatch):
    tracked = repository / "tracked file.txt"
    tracked.write_text("landing content\n")
    lock = repository / ".git" / "index.lock"
    lock.touch()
    real_git = promotion._git
    calls = []

    def record_git(checkout, *args, **kwargs):
        result = real_git(checkout, *args, **kwargs)
        calls.append((args[0], result.returncode, result.stderr))
        return result

    monkeypatch.setattr(promotion, "_git", record_git)
    try:
        with pytest.raises(promotion.CrewError, match="could not stage"):
            promotion._commit_landing_writes(
                run_id="locked", verdict="passed", checkout=repository, paths=[tracked]
            )
    finally:
        lock.unlink()
    print(f"index.lock receipts: {calls!r}")
    assert [(cmd, code) for cmd, code, _ in calls if cmd in {"add", "restore"}] == [
        ("add", 128),
        ("restore", 128),
    ]
    assert tracked.is_file()


def _pointer(repository):
    run_id = "append-receipt"
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "sample",
            "repo": str(repository),
            "worktree": str(repository),
            "launch": "in-harness",
            "role": "investigate",
            "node": {"id": "receipt", "write_paths": []},
        },
    )
    return run_id


@pytest.mark.parametrize(
    "failure", ["stage", "commit", "capture", "pointer", "release", "fleet"]
)
def test_failure_after_append_names_the_written_row(repository, monkeypatch, failure):
    run_id = _pointer(repository)
    real_append = ledger.append_run
    real_git = promotion._git
    appended = []
    calls = []
    lock = repository / ".git" / "index.lock"

    def append(*args, **kwargs):
        result = real_append(*args, **kwargs)
        appended.append(result["run"]["run_id"])
        assert ledger.load("sample", root=repository)[0]["runs"][-1]["run_id"] == run_id
        if failure == "stage":
            lock.touch()
        return result

    def git(checkout, *args, **kwargs):
        if failure == "commit" and args[0] == "commit":
            return subprocess.CompletedProcess(args, 1, "", "commit hook refused")
        result = real_git(checkout, *args, **kwargs)
        calls.append((args[0], result.returncode))
        return result

    def fail(*args, **kwargs):
        raise OSError(f"injected {failure} failure")

    monkeypatch.setattr(ledger, "append_run", append)
    monkeypatch.setattr(promotion, "_git", git)
    if failure in {"capture", "release", "fleet"}:
        monkeypatch.setattr(
            promotion,
            {
                "capture": "_capture_member_session",
                "release": "_release_after_promotion",
                "fleet": "_fleet_state_reading",
            }[failure],
            fail,
        )
    if failure == "pointer":
        real_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == pointer_path(run_id):
                fail()
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink)
    try:
        with pytest.raises(Exception) as caught:
            promotion._complete_locked(run_id, gate="not-run", root=repository)
    finally:
        lock.unlink(missing_ok=True)
    print(f"failure={failure}; appended={appended}; git={calls}; error={caught.value}")
    assert appended == [run_id]
    assert isinstance(caught.value, promotion.CrewError)
    assert f"ledger row for run {run_id!r} is already written" in str(caught.value)
    assert "do not re-promote" in str(caught.value)
    assert caught.value.__cause__ is not None
    if failure == "stage":
        assert ("add", 128) in calls
        assert ("restore", 128) in calls


def test_failure_before_append_does_not_claim_a_written_row(repository, monkeypatch):
    run_id = _pointer(repository)

    def refuse(*args, **kwargs):
        raise ledger.LedgerError("append refused")

    monkeypatch.setattr(ledger, "append_run", refuse)
    with pytest.raises(ledger.LedgerError, match="append refused") as caught:
        promotion._complete_locked(run_id, gate="not-run", root=repository)
    assert "already written" not in str(caught.value)
    assert pointer_path(run_id).is_file()
    assert ledger.load("sample", root=repository)[0]["runs"] == []


def test_existing_ledger_row_is_named_when_cleanup_fails(repository, monkeypatch):
    run_id = _pointer(repository)
    promotion._complete_locked(run_id, gate="not-run", root=repository)
    assert len(ledger.load("sample", root=repository)[0]["runs"]) == 1
    _pointer(repository)

    def refuse(*args, **kwargs):
        raise OSError("session capture refused")

    monkeypatch.setattr(promotion, "_capture_member_session", refuse)
    with pytest.raises(promotion.CrewError, match="do not re-promote") as caught:
        promotion._complete_locked(run_id, gate="not-run", root=repository)
    assert f"ledger row for run {run_id!r} is already written" in str(caught.value)
    assert len(ledger.load("sample", root=repository)[0]["runs"]) == 1
