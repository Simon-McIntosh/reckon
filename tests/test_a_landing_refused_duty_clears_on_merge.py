"""A landing refusal for a head the primary branch already carries is stale.

A run whose automatic landing was refused is owed a repair by its coordinator.
When the same head is merged by hand, the refusal describes a merge that has
since finished, and a duty left standing sends a coordinator to repair evidence
to work the branch already holds. These cases read the obligations view before
and after the merge, and keep the unmerged refusal visible so the drop is the
ancestry of this head and not the reading of refusals in general.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import obligations, recovery_classification, runs

PROJECT = "landing-fixture"
SESSION = "owner"
RUN = "r-landing-refused"


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path | str]:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    # A read taken seconds after a merge must see the branch's new revision, so
    # the primary-branch revision is read fresh for each of these reads.
    monkeypatch.setattr(recovery_classification, "_PRIMARY_REVISION_TTL_SECONDS", 0.0)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    plan = repo / "docs" / "plans" / "delivery-plan.html"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        '<!doctype html><meta name="docs-project" content="landing-fixture">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="delivery-plan">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-section-declarations" '
        'content="{&quot;s2&quot;: &quot;implementable&quot;}">'
        '<h2 id="s2">Downstream</h2>\n',
        encoding="utf-8",
    )
    state = repo / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}),
        encoding="utf-8",
    )
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills" / "reckon-build" / "scripts"
    (scripts / "worktree_fleet.py").write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    (repo / "marker.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "marker.txt", "docs", "skills")
    _git(repo, "commit", "-q", "-m", "chore: seed fixture")
    base = _git(repo, "rev-parse", "HEAD")
    # The run's work is a linked worktree: its head is a commit on the delivery
    # branch, so the run's tree carries the head while the primary branch does
    # not, exactly as an unmerged delivery does.
    worktree = tmp_path / "run-worktree"
    _git(repo, "worktree", "add", "-q", "-b", "delivery", str(worktree))
    (worktree / "marker.txt").write_text("delivered\n", encoding="utf-8")
    _git(worktree, "add", "marker.txt")
    _git(worktree, "commit", "-q", "-m", "feat: deliver marker")
    head = _git(worktree, "rev-parse", "HEAD")
    manifest = runs.run_dir(RUN) / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "\n".join(
            (
                "node: implementation",
                "status: complete",
                f"commits: [{head}]",
                "",
            )
        ),
        encoding="utf-8",
    )
    runs._write_json(
        runs.pointer_path(RUN),
        {
            "run_id": RUN,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(repo),
            "worktree": str(worktree),
            "base_sha": base,
            "suite_command": "true",
            "process_alive": False,
            "launch": "in-harness",
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {"id": "implementation", "write_paths": ["marker.txt"]},
            "admission_landing": {
                "status": "refused",
                "head": head,
                "guard": "promote: missing exit_status",
                "remedy": "write a terminal EXIT record into the gate log",
            },
        },
    )
    return {"repo": repo, "worktree": worktree, "head": head, "base": base}


def _landing_rows() -> list[dict]:
    duties = obligations.obligations(PROJECT, SESSION)
    return [item for item in duties["obligations"] if item["kind"] == "landing-refused"]


def test_the_refused_duty_is_listed_before_the_merge(
    delivery: dict[str, Path | str],
) -> None:
    rows = _landing_rows()
    assert [row["run_id"] for row in rows] == [RUN], rows


def test_the_duty_clears_once_the_head_is_merged(
    delivery: dict[str, Path | str],
) -> None:
    assert _landing_rows(), "the refusal is not listed before the merge"

    _git(
        Path(str(delivery["repo"])),
        "merge",
        "--no-ff",
        "-m",
        "merge delivery",
        "delivery",
    )

    assert _landing_rows() == []


def test_an_unmerged_refusal_still_lists_the_duty(
    delivery: dict[str, Path | str],
) -> None:
    """The negative control: without the merge, the second read keeps the duty."""
    assert _landing_rows()

    assert [row["run_id"] for row in _landing_rows()] == [RUN]
