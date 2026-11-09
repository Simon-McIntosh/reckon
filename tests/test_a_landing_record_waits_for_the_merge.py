"""A --land promotion whose merge never ran hands its head to the landing pass.

On 9 November a ``crew complete --land`` committed its promotion, 7b71afeb13,
which wrote the ledger row as passed and the plan comment, and then the
pointer-removal guard raised ``run ... changed while promotion ran``: a resume
had started the same run again between the ledger write and the merge, so the
pointer's attempt and phase had moved. The merge never ran, the row read
landed, and the reviewed head stayed off the primary branch.

The pending-landing pass already finishes a promoted run whose head is not on
the branch. What it could not see was this state: it reads a run's pointer only
when the record carries a merge conflict, so a marked pointer whose merge never
ran was skipped. The promotion now keeps the pointer it could not remove and
marks it ``pending_landing_repair``, and the pass reads a live pointer to tell a
marked one from one a newer attempt owns.

A temporary repository with a bare remote as ``origin`` and a temporary crew
home are synthesised per test, and the real pointer directory is asserted
untouched, because an isolated read does not prove an isolated write.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from reckon import _plan_html, crew
from reckon.crew import merge, pending_landing, promotion
from reckon.crew.runs import _write_json, pointer_path, read_pointer
from tests.helpers.git import git as _git

PROJECT = "landing-record-fixture"
PLAN = "landing-record-target"
RUN_ID = "r-20261109T140200000000-landing-record"
REAL_LIVE = Path.home() / ".config" / "reckon" / "crew" / "live"

GATE_CHECK: dict[str, Any] = {
    "command": "pytest -q tests/test_a_landing_record_waits_for_the_merge.py",
    "exit_status": 0,
    "log_path": str(Path(tempfile.gettempdir()) / "landing-record-gate.log"),
    "log_digest": "sha256:0",
}


@pytest.fixture(autouse=True)
def the_real_pointer_directory_is_not_a_fixture_target() -> None:
    real_pointer = REAL_LIVE / f"{RUN_ID}.json"
    assert not real_pointer.exists()
    yield
    assert not real_pointer.exists()


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return tmp_path


def _write_plan(repository: Path) -> None:
    path = repository / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Landing record target",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


def _seed(home: Path) -> tuple[Path, Path, str]:
    """A checkout with a bare ``origin`` and a reviewed head off the remote branch."""
    remote = home / "remote.git"
    remote.mkdir()
    _git(remote, "init", "-q", "--bare", "-b", "main")
    repo = home / "repo"
    _git(home, "clone", "-q", str(remote), str(repo))
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    _write_plan(repo)
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    _git(repo, "add", "docs")
    _git(repo, "commit", "-q", "-m", "test: seed repository")
    _git(repo, "push", "-q", "origin", "main")
    # The reviewed change: committed on local main, never pushed, so the remote
    # branch does not carry it until a landing puts it there.
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-q", "-m", "feat: the reviewed change")
    return repo, remote, _git(repo, "rev-parse", "HEAD")


def _pointer(repository: Path, *, attempt: int = 3, phase: str = "delivered") -> None:
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "attempt": attempt,
            "phase": phase,
            "created_at": "2026-11-09T14:00:00Z",
            "manifest_path": "/durable/manifest.md",
            "node": {
                "id": "landing-record-target",
                "plan": PLAN,
                "section": "the landing record",
                "time_budget": "25m",
                "write_paths": [],
            },
        },
    )


def _promote(repository: Path) -> dict[str, Any]:
    return crew.complete(
        RUN_ID,
        gate="passed",
        outcome="the promotion whose merge did not run",
        root=repository,
        require_gate_check=True,
        gate_check=GATE_CHECK,
    )


def _resume_during_promotion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start the run again after the ledger write, as the repair reflex did."""
    real_commit = promotion._commit_landing_writes

    def commit_then_resume(*args: Any, **kwargs: Any) -> Any:
        written = real_commit(*args, **kwargs)
        current = read_pointer(RUN_ID)
        current["attempt"] = int(current.get("attempt") or 3) + 1
        current["phase"] = "running"
        _write_json(pointer_path(RUN_ID), current)
        return written

    monkeypatch.setattr(promotion, "_commit_landing_writes", commit_then_resume)


def _on_branch(remote: Path, head: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(remote), "merge-base", "--is-ancestor", head, "main"],
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


def test_a_promotion_that_could_not_hand_over_is_finished_by_the_next_pass(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote, head = _seed(home)
    pending_landing.write_pending(RUN_ID, PROJECT)
    _pointer(repo)
    monkeypatch.setattr(merge, "project_repository", lambda project: repo)
    _resume_during_promotion(monkeypatch)

    with pytest.raises(promotion.CrewError) as error:
        _promote(repo)
    assert "changed while promotion ran" in str(error.value)

    kept = read_pointer(RUN_ID)
    assert kept.get("pending_landing_repair") is True
    assert pointer_path(RUN_ID).exists()

    results = pending_landing.finish_pending_landings()

    assert results[0]["status"] == "finished", results
    assert results[0]["head"] == head
    assert not pointer_path(RUN_ID).exists()
    assert not list(pending_landing.pending_dir().glob("*.json"))
    assert _on_branch(remote, head), "the reviewed head never reached the branch"


def test_an_undisturbed_landing_promotes_and_lands(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote, _reviewed = _seed(home)
    pending_landing.write_pending(RUN_ID, PROJECT)
    _pointer(repo)
    monkeypatch.setattr(merge, "project_repository", lambda project: repo)

    result = _promote(repo)

    assert result["pointer_removed"] is True
    assert not pointer_path(RUN_ID).exists()
    head = str(result["record"]["promoted_revision"])
    assert head, "the promotion recorded no reviewed head"

    merge.land(repo, head=head, message="land the reviewed head")
    pending_landing.clear_pending(RUN_ID)

    assert _on_branch(remote, head)
    assert not list(pending_landing.pending_dir().glob("*.json"))


def test_an_unmarked_live_pointer_is_left_alone(home: Path, monkeypatch) -> None:
    """The pass reads a live pointer to tell a marked one from a newer attempt."""
    pending_landing.write_pending(RUN_ID, PROJECT)
    monkeypatch.setattr(
        pending_landing,
        "_promoted_row",
        lambda project, run_id: {"promoted_revision": "abc"},
    )
    monkeypatch.setattr(merge, "project_repository", lambda project: Path("/repo"))
    _write_json(pointer_path(RUN_ID), {"run_id": RUN_ID, "project": PROJECT})

    results = pending_landing.finish_pending_landings(
        land=lambda *a, **k: pytest.fail("an unmarked live pointer must not land"),
        live_run_ids={RUN_ID},
    )

    assert results == []
    assert pointer_path(RUN_ID).exists()
