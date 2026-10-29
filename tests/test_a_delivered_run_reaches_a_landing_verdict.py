"""A delivered run always reaches a landing verdict.

Two exits used to drop a delivered run with nothing written down. A run whose
head carried no review was skipped by the landing pass in silence, so a
coordinator could not tell it from a run the pass never saw. And the ``crew
complete --land`` step the pass runs read the worker's ``commits:`` entry for
itself, so an entry carrying its subject after the revision — or several
revisions on one line — was refused as an unresolvable commit by the very step
the pass had just decided to take.

Both are measured here by making the thing happen: the pass's own record is
read back from the live pointer, and the land step's own argv is run through
``crew complete --land`` on a repository whose manifest carries each entry
shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon.crew import queue_landing, runs
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import PLAN, PROJECT, _write_origin_and_checkout

RUN = "r-delivered-implementation"


def _seed_repository(repo: Path) -> tuple[str, str]:
    """A repository with one delivered commit on top of its base."""
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(repo, *arguments)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "test: seed repository")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "work.txt").write_text("the delivered work\n", encoding="utf-8")
    _git(repo, "add", "work.txt")
    _git(repo, "commit", "-q", "-m", "feat: deliver the work")
    return base, _git(repo, "rev-parse", "HEAD")


def _delivered_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dispatch: dict | None = None,
) -> dict:
    """A complete implementation run at a head, with no review stored for it.

    The pointer, manifest and repository are the three things the pass reads,
    so a case here judges the pass rather than a stand-in for it.
    """
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    base, head = _seed_repository(repo)
    manifest = tmp_path / "manifests" / f"{RUN}.md"
    manifest.parent.mkdir(parents=True)
    gate_log = tmp_path / "gate.log"
    gate_log.write_text(
        "# command: python -m pytest -q tests/test_delivered.py\n1 passed\nEXIT=0\n",
        encoding="utf-8",
    )
    manifest.write_text(
        "node: implementation\n"
        "status: complete\n"
        f"commits:\n  - {head}\n"
        "tests: python -m pytest -q tests/test_delivered.py\n"
        f"test_logs: {gate_log}\n",
        encoding="utf-8",
    )
    pointer = {
        "run_id": RUN,
        "project": PROJECT,
        "session": "owner",
        "repo": str(repo),
        "worktree": str(repo),
        "base_sha": base,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": "implementation",
            "plan": PLAN,
            "section": "s1",
            "role": "implement",
            "write_paths": ["work.txt"],
        },
    }
    if dispatch is not None:
        pointer["review_dispatch"] = dispatch
    _write_json(pointer_path(RUN), pointer)
    return {
        "repo": repo,
        "base": base,
        "head": head,
        "home": home,
        "manifest": manifest,
        "gate_log": gate_log,
        "pointer": pointer,
    }


def test_a_delivered_run_with_no_review_records_what_it_waits_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pass writes ``awaiting-review`` and names the reflex's own state.

    A skipped run and a run the pass never saw are indistinguishable on the
    pointer, which is the whole defect: the record is what tells them apart.
    """
    delivery = _delivered_run(
        tmp_path,
        monkeypatch,
        dispatch={"status": "dispatched", "run_id": "r-review-of-delivery"},
    )

    result = queue_landing.land_once()

    assert result["landed"] == [], result
    landing = runs.read_pointer(RUN)["admission_landing"]
    assert landing["status"] == "awaiting-review", landing
    assert landing["review_state"] == "dispatched", landing
    assert queue_landing.recovery.same_revision(landing["head"], delivery["head"])


def test_a_refused_review_is_recorded_with_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reflex that could not dispatch says so, and the pass keeps its reason."""
    _delivered_run(
        tmp_path,
        monkeypatch,
        dispatch={"status": "refused", "reason": "no lane serves this project"},
    )

    queue_landing.land_once()

    landing = runs.read_pointer(RUN)["admission_landing"]
    assert landing["status"] == "awaiting-review", landing
    assert landing["review_state"] == "refused", landing
    assert landing["review_detail"] == "no lane serves this project", landing


def test_a_run_the_reflex_never_saw_still_carries_a_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No dispatch record at all is a refusal to dispatch, not a silent skip."""
    _delivered_run(tmp_path, monkeypatch)

    queue_landing.land_once()

    landing = runs.read_pointer(RUN)["admission_landing"]
    assert landing["status"] == "awaiting-review", landing
    assert landing["review_state"] == "refused", landing
    assert "no attempt" in landing["review_detail"], landing


def _land_through_the_step(run_id: str):
    """Run the land step's own argv, minus the fleet wrapper that launches it.

    ``_land_command`` builds the ``crew complete --land`` call the pass makes;
    running that argv is the step, so this asserts what the step asserts rather
    than a hand-built equivalent of it.
    """
    command = queue_landing._land_command(run_id)
    return CliRunner().invoke(cli_module.main, command[command.index("crew") :])


def _promotable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry) -> dict:
    """A run whose last ``commits:`` entry is spelled by ``entry``."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    _origin, repository = _write_origin_and_checkout(tmp_path)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    run_id = "r-20261019T140000000000-entry-shape"
    worktree = tmp_path / "worktrees" / "run"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    base = _git(repository, "rev-parse", "HEAD")
    revisions = []
    for index in range(3):
        changed = worktree / f"delivered{index}.txt"
        changed.write_text(f"change {index}\n", encoding="utf-8")
        _git(worktree, "add", f"delivered{index}.txt")
        _git(worktree, "commit", "-q", "-m", f"feat: change {index}")
        revisions.append(_git(worktree, "rev-parse", "HEAD"))
    head = revisions[-1]
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True)
    gate_log = tmp_path / "gate.log"
    gate_log.write_text(
        "# command: python -m pytest -q tests/test_delivered.py\n1 passed\nEXIT=0\n",
        encoding="utf-8",
    )
    entries = [f"  - {revision}" for revision in revisions]
    entries[-1] = "  - " + entry(revisions)
    manifest.write_text(
        "node: implementation\n"
        "status: complete\n"
        "commits:\n" + "\n".join(entries) + "\n"
        "tests: python -m pytest -q tests/test_delivered.py\n"
        f"test_logs: {gate_log}\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-10-28T12:00:00Z",
            "base_sha": base,
            "manifest_path": str(manifest),
            "node": {
                "id": "implementation",
                "plan": PLAN,
                "section": "s1",
                "write_paths": [f"delivered{index}.txt" for index in range(3)],
            },
        },
    )
    return {"run_id": run_id, "head": head, "revisions": revisions}


def test_a_subject_bearing_entry_lands_through_the_land_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry carrying its subject after the revision is a delivery, not a typo."""
    run = _promotable(
        tmp_path, monkeypatch, lambda revisions: f"{revisions[-1]} feat: change 2"
    )

    result = _land_through_the_step(run["run_id"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["ok"] is True, report
    row = report["ledger"] if isinstance(report.get("ledger"), dict) else {}
    assert row.get("run_id", run["run_id"]) == run["run_id"]


def test_an_entry_joining_three_revisions_lands_through_the_land_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One entry naming several revisions delivers the last of them."""
    run = _promotable(tmp_path, monkeypatch, " ".join)

    result = _land_through_the_step(run["run_id"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["ok"] is True, report


def test_the_land_step_presents_what_the_pass_itself_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The step presents the revision the one reader placed, not the raw entry."""
    run = _promotable(
        tmp_path, monkeypatch, lambda revisions: f"{revisions[-1]} feat: change 2"
    )

    presented = queue_landing._delivered_revisions(run["run_id"])

    assert presented == tuple(run["revisions"]), presented
    command = queue_landing._land_command(run["run_id"])
    for revision in presented:
        index = command.index(revision)
        assert ["--commit", revision] == command[index - 1 : index + 1]
