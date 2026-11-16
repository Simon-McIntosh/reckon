"""A run-store refusal rolls back the plan write before dispatch reads it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger, run_store
from reckon.crew import open_run_index, routing
from reckon.crew.node import TaskNode
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git

PROJECT = "store-refusal-fixture"
PLAN = "promotion-target"
RUN_ID = "r-store-refusal"
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
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
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


def _refuse_store(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    committed = git(repository, "show", f"HEAD:{PLAN_PATH}")

    def refuse(project: str, record: dict) -> None:
        assert project == PROJECT
        assert record["run_id"] == RUN_ID
        # Reach the real append boundary with both forward writes present.
        assert (repository / PLAN_PATH).read_text().strip() != committed
        assert ledger.run_path(PROJECT, RUN_ID, repository).is_file()
        raise RuntimeError("injected store refusal")

    monkeypatch.setattr(run_store, "append", refuse)


def _promote(repository: Path) -> dict:
    return crew.complete(
        RUN_ID, gate="passed", outcome="Delivered the change", root=repository
    )


@pytest.mark.parametrize("lookup_refuses", [False, True])
def test_a_store_refusal_restores_the_committed_plan(
    repository: Path, monkeypatch: pytest.MonkeyPatch, lookup_refuses: bool
) -> None:
    before = (repository / PLAN_PATH).read_bytes()
    head = git(repository, "rev-parse", "HEAD")
    pointer = pointer_path(RUN_ID).read_bytes()
    _refuse_store(repository, monkeypatch)
    if lookup_refuses:
        lookup = open_run_index.row_for_run

        def refuse_lookup(*args, **kwargs):
            if (repository / PLAN_PATH).read_bytes() != before:
                raise ledger.LedgerError("injected store read refusal")
            return lookup(*args, **kwargs)

        monkeypatch.setattr(open_run_index, "row_for_run", refuse_lookup)

    with pytest.raises(ledger.LedgerError, match=r"injected store.*refusal"):
        _promote(repository)

    assert (repository / PLAN_PATH).read_bytes() == before
    assert git(repository, "status", "--porcelain", "--", PLAN_PATH) == ""
    assert git(repository, "rev-parse", "HEAD") == head
    assert pointer_path(RUN_ID).read_bytes() == pointer
    assert not ledger.run_path(PROJECT, RUN_ID, repository).exists()


def test_dispatch_sees_a_committed_plan_after_the_store_refuses(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _dispatch_visibility(repository)
    _refuse_store(repository, monkeypatch)

    with pytest.raises(ledger.LedgerError, match="injected store refusal"):
        _promote(repository)

    assert _dispatch_visibility(repository) == head


def test_a_successful_promotion_commits_its_plan_comment(repository: Path) -> None:
    result = _promote(repository)

    assert result["plan_comment"]["recorded"] is True
    committed = git(repository, "show", f"HEAD:{PLAN_PATH}")
    assert "Delivered the change" in committed
    assert committed == (repository / PLAN_PATH).read_text().strip()
    assert git(repository, "status", "--porcelain", "--", PLAN_PATH) == ""
    assert _dispatch_visibility(repository) == git(repository, "rev-parse", "HEAD")
    assert [row["run_id"] for row in ledger.runs(PROJECT, root=repository)] == [RUN_ID]
