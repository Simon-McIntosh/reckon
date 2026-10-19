"""A landing checks the paths it writes, before it stages and after it commits.

Every repository, run directory and config home here is created under a
temporary directory, so a case that passes leaves nothing of its own on the
machine it ran on.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew.node import CrewError
from reckon.crew.promotion_gate import record_gate_rerun_at_integrated_revision
from reckon.crew.promotion_release import _record_release_on_ledger
from reckon.crew.promotion_scope import _commit_landing_writes
from reckon.crew.runs import run_dir

PROJECT = "proj"
HOOK_REFUSAL = "landing refused by the pre-commit hook"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def _write_hook(repo: Path, name: str, script: str) -> None:
    hook = repo / ".git" / "hooks" / name
    hook.write_text(script, encoding="utf-8")
    hook.chmod(0o755)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "modified.txt").write_text("original\n", encoding="utf-8")
    git(repo, "add", "--", "modified.txt")
    git(repo, "commit", "-q", "-m", "test: seed")
    return repo


def _landing(repo: Path, run_id: str, paths: list[Path]) -> dict:
    return _commit_landing_writes(
        run_id=run_id, verdict="passed", checkout=repo, paths=paths
    )


def test_a_staged_reversal_of_an_earlier_landing_is_reported(repository: Path) -> None:
    created = repository / "created.txt"
    modified = repository / "modified.txt"
    second = repository / "second.txt"

    created.write_text("landing one\n", encoding="utf-8")
    modified.write_text("landing one\n", encoding="utf-8")
    first_landing = _landing(repository, "r-first", [created, modified])
    assert first_landing["committed"] is True
    first_commit = _head(repository)

    # Stage the first landing back: the created path removed from the index and
    # working tree, the tracked path restored to what it held before the commit.
    created.unlink()
    git(repository, "add", "--", "created.txt")
    modified.write_text("original\n", encoding="utf-8")
    git(repository, "add", "--", "modified.txt")

    second.write_text("landing two\n", encoding="utf-8")
    second_landing = _landing(repository, "r-second", [second])

    reversals = {
        entry["path"]: entry for entry in second_landing["landing_check"]["reversals"]
    }
    assert set(reversals) == {"created.txt", "modified.txt"}
    assert reversals["created.txt"]["commit"] == first_commit
    assert reversals["modified.txt"]["commit"] == first_commit


def test_a_staged_path_that_is_not_a_reversal_is_not_reported(
    repository: Path,
) -> None:
    created = repository / "created.txt"
    created.write_text("landing one\n", encoding="utf-8")
    _landing(repository, "r-first", [created])

    # An ordinary edit staged on top of the landing is not a reversal: the
    # staged content is not what the landing's parent held.
    created.write_text("an ordinary peer edit\n", encoding="utf-8")
    git(repository, "add", "--", "created.txt")

    another = repository / "another.txt"
    another.write_text("landing two\n", encoding="utf-8")
    second_landing = _landing(repository, "r-second", [another])

    assert second_landing["landing_check"]["reversals"] == []


def test_a_path_changed_after_the_commit_is_reported(repository: Path) -> None:
    _write_hook(
        repository,
        "post-commit",
        "#!/bin/sh\nprintf 'rewritten after the commit\\n' > written.txt\n",
    )
    written = repository / "written.txt"
    written.write_text("the landing's content\n", encoding="utf-8")

    landing = _landing(repository, "r-mismatch", [written])
    check = landing["landing_check"]
    assert check["ok"] is False
    assert [entry["path"] for entry in check["mismatched"]] == ["written.txt"]
    mismatch = check["mismatched"][0]
    head_blob = git(repository, "rev-parse", "HEAD:written.txt")
    assert mismatch["head"] == head_blob
    assert mismatch["index"] == head_blob
    assert mismatch["worktree"] not in (None, head_blob)

    # A clean landing in the same repository reports ok.
    (repository / ".git" / "hooks" / "post-commit").unlink()
    clean = repository / "clean.txt"
    clean.write_text("untouched\n", encoding="utf-8")
    clean_landing = _landing(repository, "r-clean", [clean])
    assert clean_landing["landing_check"]["ok"] is True
    assert clean_landing["landing_check"]["mismatched"] == []


def test_a_refused_commit_records_the_step_and_appends(repository: Path) -> None:
    _write_hook(
        repository,
        "pre-commit",
        f"#!/bin/sh\nprintf '{HOOK_REFUSAL}\\n' >&2\nexit 1\n",
    )
    run_id = "r-refused"
    path = repository / "landing.txt"
    path.write_text("one\n", encoding="utf-8")
    before = _head(repository)

    with pytest.raises(CrewError):
        _landing(repository, run_id, [path])

    record_path = run_dir(run_id) / "landing.json"
    entries = json.loads(record_path.read_text(encoding="utf-8"))
    assert len(entries) == 1
    first_entry = entries[0]
    assert first_entry["step"] == "commit"
    assert HOOK_REFUSAL in first_entry["message"]
    assert first_entry["head"] == before
    assert first_entry["rollback"] == {str(path.resolve()): True}

    path.write_text("two\n", encoding="utf-8")
    with pytest.raises(CrewError):
        _landing(repository, run_id, [path])

    entries = json.loads(record_path.read_text(encoding="utf-8"))
    assert len(entries) == 2
    assert entries[0] == first_entry


def test_a_held_index_lock_records_the_stage_step(repository: Path) -> None:
    run_id = "r-staged"
    path = repository / "locked.txt"
    path.write_text("content\n", encoding="utf-8")
    lock = repository / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    try:
        with pytest.raises(CrewError):
            _landing(repository, run_id, [path])
    finally:
        lock.unlink(missing_ok=True)

    entries = json.loads((run_dir(run_id) / "landing.json").read_text(encoding="utf-8"))
    assert len(entries) == 1
    assert entries[0]["step"] == "stage"


def test_a_release_that_changes_nothing_lands_no_commit(repository: Path) -> None:
    run_id = "r-release"
    run_file = ledger.run_path(PROJECT, run_id, repository)
    run_file.parent.mkdir(parents=True, exist_ok=True)
    run_file.write_text(
        ledger.serialize_run({"run_id": run_id, "project": PROJECT}),
        encoding="utf-8",
    )
    git(repository, "add", "--", str(run_file.relative_to(repository)))
    git(repository, "commit", "-q", "-m", "test: run record")

    release = {"worktree_released": True}
    _record_release_on_ledger(
        project=PROJECT,
        root=repository,
        run_id=run_id,
        release=release,
        checkout=repository,
    )
    after_first = _head(repository)

    # The same release recorded again writes the same bytes, so the landing has
    # nothing to commit and reports neither a commit nor a refusal.
    recorded, row = _record_release_on_ledger(
        project=PROJECT,
        root=repository,
        run_id=run_id,
        release=release,
        checkout=repository,
    )
    # Read the release back from the file the record wrote rather than
    # comparing the return value against the dict handed in: both sides of that
    # comparison move together, so it holds even when nothing is recorded. The
    # persisted value is checked against a literal.
    persisted = json.loads(run_file.read_text(encoding="utf-8"))
    assert persisted["release"] == {"worktree_released": True}
    assert persisted["run_id"] == run_id
    assert recorded["worktree_released"] is True
    assert row is not None
    assert _head(repository) == after_first


def test_a_reversal_found_while_landing_a_release_is_reported(
    repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = "r-release-reversal"
    run_file = ledger.run_path(PROJECT, run_id, repository)
    run_file.parent.mkdir(parents=True, exist_ok=True)
    run_file.write_text(
        ledger.serialize_run({"run_id": run_id, "project": PROJECT}),
        encoding="utf-8",
    )
    git(repository, "add", "--", str(run_file.relative_to(repository)))
    git(repository, "commit", "-q", "-m", "test: run record")

    created = repository / "created.txt"
    created.write_text("an earlier landing\n", encoding="utf-8")
    _landing(repository, "r-first", [created])
    first_commit = _head(repository)

    # A peer stages the earlier landing back while this release lands. The
    # release's own landing runs the same pre-staging check a promotion's does,
    # so the reversal reaches stderr and the returned receipt, not only the
    # record file.
    created.unlink()
    git(repository, "add", "--", "created.txt")

    recorded, _row = _record_release_on_ledger(
        project=PROJECT,
        root=repository,
        run_id=run_id,
        release={"worktree_released": True},
        checkout=repository,
    )

    reversals = {
        entry["path"]: entry for entry in recorded["landing_check"]["reversals"]
    }
    assert set(reversals) == {"created.txt"}
    assert reversals["created.txt"]["commit"] == first_commit
    assert "staged in reverse of" in capsys.readouterr().err


def test_a_reversal_found_while_landing_a_gate_rerun_is_reported(
    repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = "r-gate"
    run_file = ledger.run_path(PROJECT, run_id, repository)
    run_file.parent.mkdir(parents=True, exist_ok=True)
    run_file.write_text(
        ledger.serialize_run({"run_id": run_id, "project": PROJECT, "gate": "passed"}),
        encoding="utf-8",
    )
    git(repository, "add", "--", str(run_file.relative_to(repository)))
    git(repository, "commit", "-q", "-m", "test: run record")

    created = repository / "created.txt"
    created.write_text("an earlier landing\n", encoding="utf-8")
    _landing(repository, "r-first", [created])
    first_commit = _head(repository)

    # A peer stages the earlier landing back before the gate re-run lands its
    # report. The gate's landing runs the same pre-staging check a promotion's
    # does, so the reversal reaches stderr and the returned receipt, not only
    # the record file.
    created.unlink()
    git(repository, "add", "--", "created.txt")

    result = record_gate_rerun_at_integrated_revision(
        project=PROJECT,
        run_id=run_id,
        repository=repository,
        root=repository,
        command="true",
    )

    reversals = {entry["path"]: entry for entry in result["landing_check"]["reversals"]}
    assert set(reversals) == {"created.txt"}
    assert reversals["created.txt"]["commit"] == first_commit
    assert "staged in reverse of" in capsys.readouterr().err
