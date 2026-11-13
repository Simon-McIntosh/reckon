"""A landed reading follows the primary branch, not the landing's own record.

Promotion writes the run's ledger row and pushes its commits; ``crew
complete --land`` merges the delivered head afterwards. A landing record under
the crew home is one account of that merge, written by the landing pass that
owes it — but a promotion that ended before writing one still leaves a run
whose ledger row exists and whose change no branch carries, and that a reader
drew as ``landed``.

So the reading asks the branch itself. A promoted run reads landed only when
its delivered head is an ancestor of the primary branch, whether or not a
landing record exists, and otherwise reads as ``complete`` — the word the pane
draws as delivered — with a detail naming what the merge waits on.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import pending_landing, recovery

PROJECT = "proj"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Resolve every crew directory under the pytest temporary home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return tmp_path


def _repository(tmp_path: Path) -> tuple[Path, str]:
    """A repository whose primary branch does not carry the delivered head."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-q", "-m", "chore: base")
    _git(repo, "checkout", "-q", "-b", "work")
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-q", "-m", "feat: the delivered change")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return repo, head


def _on_primary(repo: Path, head: str) -> bool:
    answer = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", head, "main"],
        check=False,
    )
    return answer.returncode == 0


def _promoted_run(tmp_path: Path, run_id: str, repo: Path, head: str) -> None:
    """A promoted run: a live pointer, a completion manifest, a ledger row."""
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: node-a\nstatus: complete\ncommits: {head}\nblockers: none\n",
        encoding="utf-8",
    )
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repo),
            "node": {"id": "node-a", "plan": "plan-a", "time_budget": "20m"},
            "phase": "complete",
            "created_at": datetime.now(UTC).isoformat(),
            "manifest_path": str(manifest),
            "process_alive": None,
        },
    )
    row = ledger.run_path(PROJECT, run_id, str(repo))
    row.parent.mkdir(parents=True, exist_ok=True)
    row.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": PROJECT,
                "commits": [head],
                "promoted_revision": head,
            }
        ),
        encoding="utf-8",
    )


def _snapshot(run_id: str) -> dict:
    """The state the follower draws for this run, as the row policy reads it."""
    return recovery._watch_snapshot(
        crew.read_pointer(run_id), moment=time.time(), stall_seconds=3600
    )


def _assert_awaiting_merge(row: dict) -> None:
    """The row is delivered — not landed — and says its merge is still owed."""
    # ``promoted`` is the state the pane draws as ``landed``.
    assert row["state"] != "promoted"
    assert row["state"] == "complete"
    assert "awaits its merge" in row["detail"]


def test_a_promoted_run_with_no_record_and_an_unmerged_head_is_not_landed(
    home: Path, tmp_path: Path
) -> None:
    run_id = "r-no-record"
    repo, head = _repository(tmp_path)
    _promoted_run(tmp_path, run_id, repo, head)

    assert not _on_primary(repo, head), "the delivered head must not be on the branch"
    assert not pending_landing._record_path(run_id).exists()
    row = _snapshot(run_id)

    _assert_awaiting_merge(row)
    assert "primary branch" in row["detail"]


def test_a_plain_unfinished_record_reads_awaiting_its_merge(
    home: Path, tmp_path: Path
) -> None:
    run_id = "r-plain-record"
    repo, head = _repository(tmp_path)
    _promoted_run(tmp_path, run_id, repo, head)
    # A landing in flight with no merge conflict to repair.
    pending_landing.write_pending(run_id, PROJECT)

    assert not _on_primary(repo, head), "the delivered head must not be on the branch"
    row = _snapshot(run_id)

    _assert_awaiting_merge(row)
    assert "awaiting-merge" in row["detail"]


def test_a_promoted_run_whose_head_is_merged_reads_landed(
    home: Path, tmp_path: Path
) -> None:
    run_id = "r-merged"
    repo, head = _repository(tmp_path)
    _promoted_run(tmp_path, run_id, repo, head)
    pending_landing.write_pending(run_id, PROJECT)
    _git(repo, "merge", "--no-ff", "work", "-m", "merge the delivered work")
    # The landing pass clears the record only once the merge is on the branch.
    pending_landing.clear_pending(run_id)

    assert _on_primary(repo, head), "the delivered head must be on the branch"
    row = _snapshot(run_id)

    assert row["state"] == "promoted"


def test_a_record_left_behind_a_finished_merge_does_not_hold_the_run(
    home: Path, tmp_path: Path
) -> None:
    """The ancestry decides: a stale record naming a merge already made is not owed."""
    run_id = "r-record-then-merge"
    repo, head = _repository(tmp_path)
    _promoted_run(tmp_path, run_id, repo, head)
    pending_landing.write_pending(run_id, PROJECT)
    assert _snapshot(run_id)["state"] == "complete"

    _git(repo, "merge", "--no-ff", "work", "-m", "merge the delivered work")
    # The record is still there when the sweep reads, and the branch decides.
    # The moment stands after the reading window the primary revision is
    # reused for, which is how a later tick sees the merge.
    assert pending_landing._record_path(run_id).exists()
    row = recovery._watch_snapshot(
        crew.read_pointer(run_id),
        moment=time.time() + 6.0,
        stall_seconds=3600,
    )

    assert row["state"] == "promoted"


def test_a_promoted_run_whose_head_cannot_be_read_does_not_read_landed(
    home: Path, tmp_path: Path
) -> None:
    """An unreadable head is an unverified landing, never a landed one."""
    run_id = "r-head-unreadable"
    repo, head = _repository(tmp_path)
    _promoted_run(tmp_path, run_id, repo, head)
    # A ledger row promotion wrote with no revision it can name.
    ledger.run_path(PROJECT, run_id, str(repo)).write_text(
        json.dumps({"run_id": run_id, "project": PROJECT, "commits": []}),
        encoding="utf-8",
    )

    row = _snapshot(run_id)

    assert row["state"] != "promoted"
    assert row["state"] == "complete"
    assert "landing is unverified" in row["detail"]
