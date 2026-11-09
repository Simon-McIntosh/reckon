"""The reflex acceptance tests a run's commits against the remote primary branch.

Acceptance promotes a reviewed run without a coordinator once the run's own
recorded gate still passes at the merged head. It used to ask whether the run's
commits were ancestors of the checkout's local ``HEAD``, which a checkout whose
``main`` was fast-forwarded while its push failed satisfies even though no other
reader can find the work. The question is now asked of the remote branch,
through the same function the landing path uses, and a remote that cannot be
read or that carries no primary ref declines rather than promoting work it
could not check.

Each declined arm carries a control, because a guard that never fires passes the
same assertions as one that always refuses: the same run promotes when the
remote read answers promotable, so the refusal is the remote answer and not
something else about the fixture.

The declared mutation reverts the question to the local checkout: at the base
revision the run whose commits are only on local ``main`` is promoted, which is
the red log the manifest names.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import merge, recovery
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, crew_home, pointer_path
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import (
    PLAN,
    PROJECT,
    _write_origin_and_checkout,
)

RUN_ID = "r-20261109T120000000000-reflex-acceptance"
GATE_COMMAND = "sh gate.sh"
GATE_COMMAND = "sh gate.sh"


@pytest.fixture()
def real_pointer_is_not_a_fixture_target(isolated_reckon_home: Path) -> None:
    """No fixture may reach the workstation's real crew pointer directory."""
    real_pointer = (
        Path.home() / ".config" / "reckon" / "crew" / "live" / f"{RUN_ID}.json"
    )
    assert not real_pointer.exists()
    yield
    assert not real_pointer.exists()


@pytest.fixture()
def origin_and_repository(
    isolated_reckon_home: Path, tmp_path: Path
) -> tuple[Path, Path]:
    """A bare origin, a checkout of ``main``, and a green recorded gate.

    The checkout's ``docs`` directory is the project's mount, so acceptance
    resolves the repository the run names. The gate greps ``node.txt`` for
    ``satisfied``, which the seed commit writes, so the re-run at the merged
    head has a real command that passes.
    """
    assert crew_home().is_relative_to(isolated_reckon_home)
    origin, repository = _write_origin_and_checkout(tmp_path)
    (repository / "gate.sh").write_text(
        '#!/bin/sh\ngrep -q "satisfied" node.txt\n', encoding="utf-8"
    )
    (repository / "node.txt").write_text("satisfied\n", encoding="utf-8")
    _git(repository, "add", "gate.sh", "node.txt")
    _git(repository, "commit", "-q", "-m", "test: seed the recorded gate")
    _git(repository, "push", "-q", "origin", "main")
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}),
        encoding="utf-8",
    )
    return origin, repository


def _land_run_commit(repository: Path, *, push: bool) -> tuple[str, str]:
    """Commit the run's deliverable on local ``main`` and optionally push it.

    ``push=False`` is the measured state: the coordinator fast-forwarded local
    ``main`` and the push failed, so the checkout carries the run's work and the
    remote does not. Both arms commit the same deliverable, so the gate passes
    on the merged tree either way and only the remote's answer differs.
    """
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "candidate.txt").write_text("delivered\n", encoding="utf-8")
    _git(repository, "add", "candidate.txt")
    _git(repository, "commit", "-q", "-m", "feat: land the run deliverable")
    commit = _git(repository, "rev-parse", "HEAD")
    if push:
        _git(repository, "push", "-q", "origin", "main")
    return base, commit


def _gate_log(tmp_path: Path) -> Path:
    log = tmp_path / "gate.log"
    log.write_text("gate ran\nEXIT=0\n", encoding="utf-8")
    return log


def _store_review(*, base: str, head: str) -> None:
    """Store a clean review keyed to the exact revision it read."""
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN_ID,
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
            "review_run_id": f"review-of-{RUN_ID}",
        }
    )
    review_module.store_review(record)


def _write_run(repository: Path, tmp_path: Path, *, base: str, commit: str) -> None:
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: reflex-acceptance\n"
        "status: complete\n"
        f"commits: {commit}\n"
        "changed_paths: candidate.txt\n"
        f"tests: {GATE_COMMAND}\n"
        f"test_logs:\n  - {_gate_log(tmp_path)}\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": base,
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-01-01T00:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "reflex-acceptance",
                "plan": PLAN,
                "section": "s7",
                "time_budget": "25m",
                "role": "implement",
                "write_paths": ["candidate.txt"],
            },
        },
    )


def _sweep() -> dict:
    """Run the reflex sweep as a hand-run caller does, with no session filter."""
    return recovery.dispatch_awaiting_reviews(
        project=PROJECT, launcher=lambda *args, **kwargs: 0
    )


def _refusal_for(report: dict) -> dict:
    return next(
        entry
        for entry in report["reports"]
        if entry.get("run_id") == RUN_ID and "accepted" in entry
    )


def _read_as_promotable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: answer the remote question as promotable, whatever the remote is.

    Patching ``merge.head_on_remote_branch`` patches the name the acceptance
    module resolves, because it imports the module and calls through it. If the
    run then promotes, the refusal in the arm beside this one is the remote
    answer and nothing else about the fixture.
    """
    monkeypatch.setattr(
        merge,
        "head_on_remote_branch",
        lambda *args, **kwargs: {"on_branch": True, "cause": None},
    )


def test_a_run_only_on_local_main_is_not_promoted(
    origin_and_repository: tuple[Path, Path], tmp_path: Path
) -> None:
    """The measured defect: local ``main`` carries the run, the remote does not."""
    _origin, repository = origin_and_repository
    base, commit = _land_run_commit(repository, push=False)
    _write_run(repository, tmp_path, base=base, commit=commit)
    _store_review(base=base, head=commit)

    report = _sweep()

    assert report["accepted"] == [], report
    refusal = _refusal_for(report)
    assert refusal["accepted"] is False
    assert refusal["unmerged_commits"] == [commit]
    assert commit[:12] in refusal["reason"]
    assert pointer_path(RUN_ID).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_a_run_whose_commits_are_on_the_remote_is_promoted(
    origin_and_repository: tuple[Path, Path], tmp_path: Path
) -> None:
    """A landed run promotes as before; the remote read is not a blanket refusal."""
    _origin, repository = origin_and_repository
    base, commit = _land_run_commit(repository, push=True)
    _write_run(repository, tmp_path, base=base, commit=commit)
    _store_review(base=base, head=commit)

    report = _sweep()

    assert report["accepted"] == [RUN_ID], report
    assert not pointer_path(RUN_ID).exists()
    rows = ledger.runs(PROJECT, root=repository)
    assert [row["run_id"] for row in rows] == [RUN_ID]
    assert rows[0]["promoted_by"] == "review-acceptance"


def test_an_unreachable_remote_declines(
    origin_and_repository: tuple[Path, Path], tmp_path: Path
) -> None:
    """A remote that cannot be read leaves the question unanswered, so no promotion."""
    _origin, repository = origin_and_repository
    _git(repository, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    base, commit = _land_run_commit(repository, push=False)
    _write_run(repository, tmp_path, base=base, commit=commit)
    _store_review(base=base, head=commit)

    report = _sweep()

    assert report["accepted"] == [], report
    refusal = _refusal_for(report)
    assert refusal["accepted"] is False
    assert refusal["remote_cause"] == merge.REMOTE_UNREACHABLE
    assert "could not be read" in refusal["reason"]
    assert pointer_path(RUN_ID).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_an_unreachable_answer_read_as_promotable_would_promote(
    origin_and_repository: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control for the arm above: the refusal is the remote answer, not the fixture."""
    _origin, repository = origin_and_repository
    _git(repository, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    base, commit = _land_run_commit(repository, push=False)
    _write_run(repository, tmp_path, base=base, commit=commit)
    _store_review(base=base, head=commit)
    _read_as_promotable(monkeypatch)

    report = _sweep()

    assert report["accepted"] == [RUN_ID], report
    assert not pointer_path(RUN_ID).exists()


def test_a_remote_with_no_remote_primary_branch_declines(
    origin_and_repository: tuple[Path, Path], tmp_path: Path
) -> None:
    """A reachable remote carrying no primary ref cannot vouch for the commits."""
    origin, repository = origin_and_repository
    _git(origin, "update-ref", "-d", "refs/heads/main")
    base, commit = _land_run_commit(repository, push=False)
    _write_run(repository, tmp_path, base=base, commit=commit)
    _store_review(base=base, head=commit)

    report = _sweep()

    assert report["accepted"] == [], report
    refusal = _refusal_for(report)
    assert refusal["accepted"] is False
    assert refusal["remote_cause"] == merge.REMOTE_BRANCH_MISSING
    assert "carries no" in refusal["reason"]
    assert pointer_path(RUN_ID).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_a_missing_branch_read_as_promotable_would_promote(
    origin_and_repository: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control for the arm above: reading the missing branch as promotable promotes."""
    origin, repository = origin_and_repository
    _git(origin, "update-ref", "-d", "refs/heads/main")
    base, commit = _land_run_commit(repository, push=False)
    _write_run(repository, tmp_path, base=base, commit=commit)
    _store_review(base=base, head=commit)
    _read_as_promotable(monkeypatch)

    report = _sweep()

    assert report["accepted"] == [RUN_ID], report
    assert not pointer_path(RUN_ID).exists()


def test_a_checkout_with_no_remote_judges_its_own_branch(
    isolated_reckon_home: Path, tmp_path: Path
) -> None:
    """No remote configured means no remote branch to be off, so local stands.

    A fixture repository with no ``origin`` keeps the behaviour a repository
    without a remote has always had: the checkout's own branch is the answer.
    """
    repository = tmp_path / "local-repo"
    repository.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("config", "commit.gpgsign", "false"),
    ):
        _git(repository, *arguments)
    (repository / "gate.sh").write_text(
        '#!/bin/sh\ngrep -q "satisfied" node.txt\n', encoding="utf-8"
    )
    (repository / "node.txt").write_text("satisfied\n", encoding="utf-8")
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    _git(repository, "add", "gate.sh", "node.txt", "docs")
    _git(repository, "commit", "-q", "-m", "test: seed the recorded gate")
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    base, commit = _land_run_commit(repository, push=False)
    _write_run(repository, tmp_path, base=base, commit=commit)
    _store_review(base=base, head=commit)

    report = _sweep()

    assert report["accepted"] == [RUN_ID], report
    assert not pointer_path(RUN_ID).exists()


def test_acceptance_still_promotes_the_base_case_with_no_remote_read_needed(
    origin_and_repository: tuple[Path, Path], tmp_path: Path
) -> None:
    """A run merged and pushed into the same commit the checkout carries."""
    _origin, repository = origin_and_repository
    base, commit = _land_run_commit(repository, push=True)
    _write_run(repository, tmp_path, base=base, commit=commit)
    _store_review(base=base, head=commit)
    assert _git(repository, "rev-parse", "HEAD") == commit

    report = _sweep()

    assert report["accepted"] == [RUN_ID], report
