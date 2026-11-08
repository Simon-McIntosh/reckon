"""A --land killed between its promotion and its merge is finished by the next pass.

``crew complete --land`` promotes, pushing its promote and release commits, and
merges afterwards. On 7 November a landing was cut off between the two: run
r-20261107T152305119816 read as promoted and was drawn as landed while its three
commits stayed off main for three hours. ``--land`` now writes a pending record
before it promotes and clears it after the merge, and the landing pass finishes
any record whose run promoted but whose head is not on the branch.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon.crew import merge, pending_landing


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    return tmp_path


def _promoted(monkeypatch, rows: dict[str, dict]) -> None:
    monkeypatch.setattr(
        pending_landing, "_promoted_row", lambda project, run_id: rows.get(run_id)
    )
    monkeypatch.setattr(merge, "project_repository", lambda project: Path("/repo"))


def test_a_promoted_unmerged_run_is_landed_and_its_record_cleared(
    home: Path, monkeypatch
) -> None:
    pending_landing.write_pending("r-1", "proj")
    _promoted(monkeypatch, {"r-1": {"promoted_revision": "abc123"}})
    calls: list[str] = []

    results = pending_landing.finish_pending_landings(
        land=lambda repo, head, **_: calls.append(head) or {"commit": "m1"},
        live_run_ids=(),
    )

    assert calls == ["abc123"]
    assert results == [
        {"run_id": "r-1", "status": "finished", "head": "abc123", "commit": "m1"}
    ]
    assert not list(pending_landing.pending_dir().glob("*.json"))


def test_a_failed_merge_is_retried_then_left_for_a_coordinator(
    home: Path, monkeypatch
) -> None:
    pending_landing.write_pending("r-2", "proj")
    _promoted(monkeypatch, {"r-2": {"commits": ["c1", "c2"]}})

    def refuse(repo, head, **_):
        raise merge.MergeError("conflict in reckon/x.py")

    statuses = [
        pending_landing.finish_pending_landings(land=refuse, live_run_ids=())[0][
            "status"
        ]
        for _ in range(pending_landing.MAX_FINISH_ATTEMPTS + 1)
    ]

    assert statuses == ["failed"] * pending_landing.MAX_FINISH_ATTEMPTS + ["gave-up"]


def test_a_run_that_never_promoted_is_kept_while_live_and_dropped_when_old(
    home: Path, monkeypatch
) -> None:
    pending_landing.write_pending("r-3", "proj", now=1000.0)
    _promoted(monkeypatch, {})
    never = lambda *a, **k: pytest.fail("nothing to land")  # noqa: E731

    live = pending_landing.finish_pending_landings(
        land=never, live_run_ids={"r-3"}, now=1000.0 + 10 * 3600
    )
    dropped = pending_landing.finish_pending_landings(
        land=never, live_run_ids=(), now=1000.0 + 10 * 3600
    )

    assert live == []
    assert dropped == [{"run_id": "r-3", "status": "dropped-unpromoted"}]


def test_a_killed_landing_ends_with_its_head_on_the_remote_branch(
    home: Path, monkeypatch
) -> None:
    remote = home / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True
    )
    repo = home / "repo"
    subprocess.run(["git", "clone", "-q", str(remote), str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-q", "-m", "chore: base")
    _git(repo, "push", "-q", "origin", "main")
    # The reviewed head: a commit the promotion recorded but no merge carried.
    _git(repo, "checkout", "-q", "-b", "work")
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-q", "-m", "feat: the reviewed change")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")

    pending_landing.write_pending("r-killed", "proj")
    monkeypatch.setattr(
        pending_landing,
        "_promoted_row",
        lambda project, run_id: {"promoted_revision": head},
    )
    monkeypatch.setattr(merge, "project_repository", lambda project: repo)

    results = pending_landing.finish_pending_landings(live_run_ids=())

    assert results[0]["status"] == "finished", results
    remote_main = _git(remote, "rev-parse", "main")
    ancestor = subprocess.run(
        ["git", "-C", str(remote), "merge-base", "--is-ancestor", head, remote_main],
        check=False,
    )
    assert ancestor.returncode == 0, "the reviewed head is not on the remote branch"
