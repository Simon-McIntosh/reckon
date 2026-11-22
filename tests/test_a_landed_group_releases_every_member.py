"""A landed landing group releases every member's pointer and its waiters.

A group lands as one head onto the primary branch. Each member's work is then on
the branch, but no member shows a promotion of its own, so nothing retires the
member's live pointer. While a member's pointer lives, a section wait on that
member resolves as unlanded, and every request waiting on the group is held for
as long as the pointer survives. One pass over a landed group must therefore
leave no member pointer and release every request waiting on a member; a group
whose head is not on the primary branch keeps every member and every wait.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon.crew import (
    pending_landing,
    promotion,
    promotion_release,
    queue_landing,
    recovery_repair_dispatch,
    review_landing,
    runs,
)
from tests.helpers.git import git

PROJECT = "landing-fixture"
PLAN = "plan-a"
SECTIONS = ("s1", "s2", "s3")


def _plan_html() -> str:
    declarations = dict.fromkeys(SECTIONS, "done")
    return (
        "<!doctype html>\n"
        f'<meta name="docs-project" content="{PROJECT}">\n'
        '<meta name="reckon-type" content="plan">\n'
        f'<meta name="plan-slug" content="{PLAN}">\n'
        '<meta name="plan-status" content="active">\n'
        '<meta name="plan-section-declarations" content="'
        + json.dumps(declarations).replace('"', "&quot;")
        + '">\n'
        + "".join(f'<h2 id="{section}">§{section}</h2>\n' for section in SECTIONS)
    )


def _run(repository: Path, *arguments: str) -> str:
    return git(repository, *arguments)


@pytest.fixture
def group(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / "docs" / "plans" / f"{PLAN}.html").write_text(
        _plan_html(), encoding="utf-8"
    )
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "seed").write_text("seed\n")
    git(repo, "add", "seed")
    git(repo, "add", "docs")
    git(repo, "commit", "-qm", "test: seed repository")
    base = git(repo, "rev-parse", "HEAD")
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "--bare", "-q", "-b", "main")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    config = runs.crew_home().parent
    config.mkdir(parents=True, exist_ok=True)
    (config / "mounts.json").write_text(json.dumps({PROJECT: str(repo / "docs")}))

    members = {}
    for index, section in enumerate(SECTIONS):
        tree = tmp_path / section
        git(repo, "worktree", "add", "--detach", str(tree), base)
        (tree / section).write_text(section + "\n")
        git(tree, "add", section)
        git(tree, "commit", "-q", "-m", f"test: deliver {section}")
        head = git(tree, "rev-parse", "HEAD")
        run_id = f"r-{section}"
        pointer = {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repo),
            "worktree": str(tree),
            "landing_group": "r-s1",
            "node": {"id": f"n{index}", "plan": PLAN, "section": section},
        }
        members[f"r-{section}"] = {
            "pointer": pointer,
            "head": head,
            "options": {"run_id": run_id},
        }
    request = {
        "run_id": "r-s1",
        "kind": "landing-group",
        "options": {"members": list(members.values())},
    }
    journal = {
        "run_id": "r-s1",
        "request": request,
        "members": members,
        "head": members["r-s1"]["head"],
        "primary": base,
    }
    pending_landing.save_group(journal)

    monkeypatch.setattr(recovery_repair_dispatch, "_reviewed_run_is_busy", lambda _: "")
    monkeypatch.setattr(pending_landing, "landed_workspace_refusal", lambda *args: "")
    monkeypatch.setattr(pending_landing, "_delivered_repair", lambda *args: "")

    def release(pointer, **kwargs):
        return {"worktree_released": True}

    monkeypatch.setattr(promotion_release, "_release_run_workspace", release)
    monkeypatch.setattr(
        promotion,
        "_record_release_on_ledger",
        lambda **kwargs: (kwargs["release"], {"run_id": kwargs["run_id"]}),
    )
    monkeypatch.setattr(review_landing, "finish_landing", lambda *args: "")
    # A group that is not fully merged must not run a real measurement.
    monkeypatch.setattr(
        queue_landing,
        "measure_landing_group",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            runs.CrewError("candidate held for inspection")
        ),
    )
    return SimpleNamespace(
        repo=repo,
        project=PROJECT,
        members=members,
        journal=journal,
        tmp_path=tmp_path,
    )


def _write_waiters(group) -> dict[str, dict[str, Path]]:
    """One request waiting on each member's section, plus its live pointer."""
    waiters = {}
    for index, section in enumerate(SECTIONS):
        run_id = f"w-{section}"
        pointer = {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(group.repo),
            "phase": "awaiting-upstream",
            "waits": [f"{PLAN}#{section}"],
            "node": {
                "id": f"w{index}",
                "plan": PLAN,
                "section": f"w{index}",
            },
        }
        runs._write_json(runs.pointer_path(run_id), pointer)
        waiters[run_id] = pointer
    return waiters


def _present(group) -> list[str]:
    return [run_id for run_id in group.members if runs.pointer_path(run_id).exists()]


def _phases(waiters) -> dict[str, str]:
    return {
        run_id: str(runs.read_pointer(run_id).get("phase") or "") for run_id in waiters
    }


def _land_the_group(group, run_ids=None) -> None:
    """Merge the named member heads, push, and drop the group journal.

    A group that finishes unlinks its journal as it writes its outcome, so
    these are the records a landed group leaves behind: its heads on the
    primary branch, its own outcome, and only the member pointers that some
    later write left alive.
    """
    chosen = list(run_ids or group.members)
    for run_id in chosen:
        git(
            group.repo,
            "merge",
            "--no-ff",
            "-m",
            f"test: integrate {run_id}",
            group.members[run_id]["head"],
        )
    git(group.repo, "push", "-q", "origin", "main")
    outcome = {
        "ok": True,
        "run_id": group.journal["run_id"],
        "members": list(group.members),
        "candidate": git(group.repo, "rev-parse", "HEAD"),
        "commits": [group.members[run_id]["head"] for run_id in group.members],
        "outcome": "all member heads are on the primary branch",
    }
    runs._write_json(
        runs.run_dir(group.journal["run_id"]) / "landing-group.json", outcome
    )
    pending_landing.group_path(group.journal["run_id"]).unlink(missing_ok=True)


def test_a_landed_group_leaves_no_member_pointer_and_releases_every_wait(group):
    waiters = _write_waiters(group)
    for run_id, member in group.members.items():
        runs._write_json(runs.pointer_path(run_id), member["pointer"])
    _land_the_group(group)

    pending_landing.finish_pending_landings()

    assert _present(group) == []
    assert _phases(waiters) == dict.fromkeys(waiters, "queued")


def test_an_unlanded_group_keeps_every_pointer_and_every_wait(group):
    waiters = _write_waiters(group)
    for run_id, member in group.members.items():
        runs._write_json(runs.pointer_path(run_id), member["pointer"])

    pending_landing.finish_pending_landings()

    assert _present(group) == list(group.members)
    assert _phases(waiters) == dict.fromkeys(waiters, "awaiting-upstream")


def test_a_finished_group_releases_only_the_members_whose_head_landed(group):
    waiters = _write_waiters(group)
    for run_id, member in group.members.items():
        runs._write_json(runs.pointer_path(run_id), member["pointer"])
    _land_the_group(group, run_ids=["r-s1", "r-s2"])

    pending_landing.finish_pending_landings()

    assert _present(group) == ["r-s3"]
    assert _phases(waiters) == {
        "w-s1": "queued",
        "w-s2": "queued",
        "w-s3": "awaiting-upstream",
    }
