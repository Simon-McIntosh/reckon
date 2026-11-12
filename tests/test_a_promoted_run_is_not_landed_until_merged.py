"""A promoted run reads landed only once its delivered head is on the branch.

Promotion writes the run's ledger row and pushes its promote and release
commits, and ``crew complete --land`` merges the delivered head afterwards. A
process that stops between the two leaves a run whose ledger row exists and
whose change no branch carries. The follower drew such runs as ``landed`` while
a pending landing record read ``awaiting-merge-repair``, so a coordinator
fleet-wide read said the run's work was settled when it was still outstanding.

The row policy takes its word from the run's state, and the state read here is
the ledger row's presence rather than the branch. A pending landing record that
still exists is the landing's own account that its merge has not finished, so
the run reads delivered and names the landing's status instead.
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
        json.dumps({"run_id": run_id, "project": PROJECT, "commits": [head]}),
        encoding="utf-8",
    )


def _snapshot(run_id: str) -> dict:
    """The state the follower draws for this run, as the row policy reads it."""
    return recovery._watch_snapshot(
        crew.read_pointer(run_id), moment=time.time(), stall_seconds=3600
    )


def test_a_promoted_run_awaiting_its_merge_does_not_read_landed(
    home: Path, tmp_path: Path
) -> None:
    run_id = "r-awaiting-merge"
    repo, head = _repository(tmp_path)
    _promoted_run(tmp_path, run_id, repo, head)
    # The landing's own account: it promoted and its merge has met a conflict
    # it is repairing, so the delivered head is on no branch.
    pending_landing.write_pending(run_id, PROJECT)
    record = pending_landing._read(pending_landing._record_path(run_id))
    assert record is not None
    record["merge_conflict"] = {"outcome": "merge-conflict", "status": "resumed"}
    pending_landing._record_path(run_id).write_text(json.dumps(record), "utf-8")

    assert not _on_primary(repo, head), "the delivered head must not be on the branch"
    row = _snapshot(run_id)

    # ``promoted`` is the state the pane draws as ``landed``. A promoted run
    # whose landing is unfinished is delivered, and says which landing step the
    # merge is waiting on.
    assert row["state"] != "promoted"
    assert row["state"] == "complete"
    assert "awaiting-merge-repair" in row["detail"]


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
