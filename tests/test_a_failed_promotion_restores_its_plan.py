"""A failed landing preserves the plan's bytes and its own index entry."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from reckon import _plan_html, _store, crew, ledger
from reckon.crew import promotion, routing
from reckon.crew.node import CrewError, TaskNode
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git

PROJECT = "plan-rollback-fixture"
PLAN = "promotion-target"
RUN_ID = "r-plan-rollback"
PLAN_PATH = f"docs/plans/{PLAN}.html"


@pytest.fixture()
def repository(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "repo"
    plan = root / PLAN_PATH
    plan.parent.mkdir(parents=True)
    plan.write_text(
        _plan_html.write_state(
            '<!doctype html><html><head></head><body><main class="plan-doc">'
            '<h2 id="delivery">Delivery</h2></main></body></html>\n',
            {
                "type": "plan",
                "slug": PLAN,
                "title": "Promotion target",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "--", PLAN_PATH),
        ("commit", "-q", "-m", "test: seed committed plan"),
    ):
        git(root, *args)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(root),
            "worktree": str(root),
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-11-16T08:00:00Z",
            "node": {
                "id": "delivery",
                "plan": PLAN,
                "section": "delivery",
                "write_paths": [],
                "time_budget": "20m",
            },
        },
    )
    return root


def _dispatch_visibility(repository: Path) -> str:
    return routing.require_plan_section_visible(
        node=TaskNode(
            id="next-delivery", goal="Continue delivery", plan=PLAN, section="delivery"
        ),
        project=PROJECT,
        repo=repository,
        base="HEAD",
        authority=routing.resolve_dispatch_authority(PROJECT, repository),
    )


def _plan_snapshot(repository: Path) -> tuple:
    path = repository / PLAN_PATH
    _state, version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    return (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        version,
        path.stat().st_mode,
        git(repository, "ls-files", "--stage", "--", PLAN_PATH),
        git(repository, "status", "--porcelain", "--", PLAN_PATH),
        git(repository, "rev-parse", "HEAD"),
    )


def _pending_state(repository: Path, state: str) -> None:
    path = repository / PLAN_PATH
    if state == "clean":
        return
    text = path.read_text()
    path.write_text(
        text.replace(
            'name="plan-version" content="0"', 'name="plan-version" content="1"'
        )
    )
    if state in {"staged", "split"}:
        git(repository, "add", "--", PLAN_PATH)
    if state == "split":
        path.write_text(
            text.replace(
                'name="plan-version" content="0"', 'name="plan-version" content="2"'
            )
        )
    assert _plan_snapshot(repository)[1] > 0


def _promote(repository: Path) -> dict:
    return crew.complete(
        RUN_ID, gate="passed", outcome="Delivered the change", root=repository
    )


def _force_failure(repository: Path, monkeypatch: pytest.MonkeyPatch, boundary: str):
    before = (repository / PLAN_PATH).read_bytes()

    def refuse(*args, **kwargs):
        assert (repository / PLAN_PATH).read_bytes() != before
        assert "Delivered the change" in (repository / PLAN_PATH).read_text()
        raise CrewError(f"injected {boundary} refusal after plan comment")

    if boundary == "profile":
        monkeypatch.setattr(promotion, "_profile_fold_anchor", refuse)
    elif boundary == "store":

        def refuse_store(*args, **kwargs):
            refuse()

        monkeypatch.setattr(ledger, "append_run", refuse_store)
    elif boundary == "commit":

        def refuse_commit(*args, **kwargs):
            git(repository, "add", "--", PLAN_PATH)
            refuse()

        monkeypatch.setattr(promotion, "_commit_landing_writes", refuse_commit)
    elif boundary == "stop":

        def stop(stage):
            if stage == "the promotion commit":
                assert (repository / PLAN_PATH).read_bytes() != before
                raise promotion._PromotionStopped("injected stop after plan comment")

        monkeypatch.setattr(promotion, "_stop_at_stage_boundary", stop)


@pytest.mark.parametrize("boundary", ["profile", "store", "commit", "stop"])
@pytest.mark.parametrize("state", ["clean", "unstaged", "staged", "split"])
def test_failed_promotion_restores_the_plan_and_its_index(
    repository: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, state: str
) -> None:
    _pending_state(repository, state)
    unrelated = repository / "unrelated.txt"
    unrelated.write_text("pending peer edit\n")
    git(repository, "add", "--", "unrelated.txt")
    unrelated_index = git(repository, "ls-files", "--stage", "--", "unrelated.txt")
    before = _plan_snapshot(repository)
    _force_failure(repository, monkeypatch, boundary)

    with pytest.raises((CrewError, promotion._PromotionStopped)) as error:
        _promote(repository)

    assert _plan_snapshot(repository) == before
    assert "restored" in str(error.value)
    assert "index" in str(error.value)
    assert pointer_path(RUN_ID).exists()
    assert (
        git(repository, "ls-files", "--stage", "--", "unrelated.txt") == unrelated_index
    )


def test_dispatch_validates_the_same_plan_after_a_failed_promotion(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _dispatch_visibility(repository)
    _force_failure(repository, monkeypatch, "profile")
    with pytest.raises(CrewError, match="injected profile refusal"):
        _promote(repository)
    # Try dispatch before comparing bytes, so the refusal itself is measured.
    assert _dispatch_visibility(repository) == head


def test_successful_promotion_commits_the_comment(repository: Path) -> None:
    before = _plan_snapshot(repository)
    result = _promote(repository)
    after = _plan_snapshot(repository)
    assert result["plan_comment"]["recorded"] is True
    assert "Delivered the change" in git(repository, "show", f"HEAD:{PLAN_PATH}")
    assert after[1] == before[1] + 1
    assert after[4] == ""
    assert _dispatch_visibility(repository) == after[5]


def test_cleanup_failure_preserves_the_committed_comment(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args, **kwargs):
        raise CrewError("injected cleanup refusal")

    monkeypatch.setattr(promotion, "capture_run_session", refuse)
    with pytest.raises(CrewError, match="injected cleanup refusal"):
        _promote(repository)
    assert "Delivered the change" in git(repository, "show", f"HEAD:{PLAN_PATH}")
    assert git(repository, "status", "--porcelain", "--", PLAN_PATH) == ""
