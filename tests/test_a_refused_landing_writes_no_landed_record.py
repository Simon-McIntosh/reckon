"""A refused landing writes no landed record and keeps the run's pointer.

A landing writes its close comment, its section declaration and the release of
the run's pointer only once the run's head is on the primary branch and its
ledger row is written. A landing refused at any step before that leaves the plan
file, the declaration and the pointer as it found them, and records the
refusal. A delivery the primary branch already carries with no ledger row behind
it is recorded before anything is released, so the branch and the run's record
agree.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import _plan_html, _store, crew, ledger
from reckon.crew import (
    merge,
    pending_landing,
    promotion_gate,
    queue_landing,
    review_landing,
    runs,
)
from reckon.crew.runs import _write_json, pointer_path, read_pointer
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import (
    _write_origin_and_checkout,
    install_in_place_scheduler,
)

# Importing these fixtures registers them for this module.
from tests.test_a_landed_head_is_not_landed_again import (  # noqa: F401
    delivery_case,
    settled_delivery,
)
from tests.test_the_landing_pass_measures_and_lands import PLAN, PROJECT
from tests.test_the_landing_pass_measures_and_lands import RUN as PASS_RUN

RUN_ID = "r-20261123T090000000000-a-refused-landing"
SECTION = "s9"


def _gate(tmp_path: Path) -> dict:
    """A merged-head gate that passes, measured in place by the suite."""
    return {
        "command": "true",
        "exit_status": 0,
        "log_path": str(tmp_path / "gate.log"),
        "log_digest": "sha256:0",
        "merged_head_gate": True,
    }


@pytest.fixture()
def landing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A delivered run whose plan cannot be landed without a promotion."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    install_in_place_scheduler(monkeypatch)
    origin, repository = _write_origin_and_checkout(tmp_path)
    path = repository / "docs" / "plans" / f"{PLAN}.html"
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc">'
        f'<h2 id="{SECTION}">The refused section</h2><p>Its body.</p>'
        "</main></body></html>\n"
    )
    path.write_text(
        _plan_html.write_state(
            bare,
            {
                "project": PROJECT,
                "type": "plan",
                "slug": PLAN,
                "title": "Refused landing target",
                "status": "active",
                "version": 0,
                "section_declarations": {SECTION: "implementable"},
                "sections": [
                    {
                        "id": SECTION,
                        "effort_hours": 1.0,
                        "capability": {
                            "version": "1.0",
                            "class": "general",
                            "requirements": {
                                "reasoning": "standard",
                                "verification": "strict",
                                "risk": "low",
                            },
                        },
                        "attempts": 0,
                        "status": "implementable",
                        "links": [],
                    }
                ],
                "comments": {},
            },
        ),
        encoding="utf-8",
    )
    _git(repository, "add", "docs")
    _git(
        repository, "commit", "-q", "-m", "test: declare the section the landing closes"
    )
    _git(repository, "push", "-q", "origin", "main")
    base = _git(repository, "rev-parse", "HEAD")
    worktree = tmp_path / "worker-tree"
    _git(repository, "worktree", "add", "--detach", str(worktree), base)
    (worktree / "delivered.txt").write_text("delivered\n", encoding="utf-8")
    _git(worktree, "add", "delivered.txt")
    _git(worktree, "commit", "-q", "-m", "feat: the reviewed delivery")
    head = _git(worktree, "rev-parse", "HEAD")
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: landing-node\nstatus: complete\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            # No plan is reserved for a coordinator, so the landing is automatic.
            "session": "owner",
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "phase": "delivered",
            "created_at": "2026-11-23T09:00:00Z",
            "base_sha": base,
            "manifest_path": str(manifest),
            "node": {
                "id": "landing-node",
                "plan": PLAN,
                "section": SECTION,
                "time_budget": "25m",
                "write_paths": ["delivered.txt"],
            },
        },
    )
    monkeypatch.setattr(merge, "project_repository", lambda project: repository)
    return {
        "gate": _gate(tmp_path),
        "origin": origin,
        "repo": repository,
        "worktree": worktree,
        "base": base,
        "head": head,
        "plan": path,
    }


def _plan_state(repository: Path) -> dict:
    state, _version = _store.read_plan(PROJECT, PLAN, str(repository))
    return state


def _close_comments(case: dict) -> list[str]:
    state = _plan_state(Path(str(case["repo"])))
    return [
        str(comment.get("body") or "")
        for comment in (state.get("comments") or {}).get(SECTION, ())
    ]


def _declaration(case: dict) -> str:
    state = _plan_state(Path(str(case["repo"])))
    return str((state.get("section_declarations") or {}).get(SECTION) or "")


def _row(repository: Path, run_id: str) -> dict | None:
    path = ledger.run_path(PROJECT, run_id, str(repository))
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_landing_refused_at_its_gate_replay_writes_no_landed_record(
    landing: dict,
) -> None:
    case = landing
    repository = Path(str(case["repo"]))
    # The replay at the merged revision exits 3 and writes no log, so it is no
    # verdict on the run: the promotion it judged never happened and the run
    # carries no ledger row.
    report = promotion_gate.rerun_gate_at_integrated_revision(
        repository=repository,
        gate_check={"command": "exit 3"},
        integrated_revision="HEAD",
        run=lambda argv, **_kwargs: SimpleNamespace(returncode=3, stdout="", stderr=""),
    )
    assert report["ok"] is False
    assert report["log_path"] is None
    assert "3" in str(report.get("reason") or ""), report
    assert _row(repository, RUN_ID) is None
    pointer = read_pointer(RUN_ID)

    # The publication step refuses before it writes anything. At the base
    # revision it writes the close comment and closes the section here, because
    # it never asks for the ledger row that a refused promotion did not write.
    refusal = review_landing.finish_landing(pointer, RUN_ID)

    assert refusal, "the landing published with no ledger row behind it"
    assert "ledger row" in refusal, refusal
    assert _close_comments(case) == []
    assert _declaration(case) == "implementable"
    # The release step is refused for the same reason and keeps the pointer.
    retired, _release, refusal = pending_landing._release_integrated_pointer(
        pointer, repository, str(case["base"]), None
    )
    assert retired == pending_landing.POINTER_HELD, retired
    assert "ledger row" in refusal, refusal
    assert pointer_path(RUN_ID).exists()
    assert _row(repository, RUN_ID) is None


def test_a_delivery_the_primary_carries_is_recorded_before_its_pointer_is_released(
    settled_delivery,  # noqa: F811
) -> None:
    case = settled_delivery
    repository = Path(str(case["repo"]))
    assert _row(repository, PASS_RUN) is None

    queue_landing.land_once()

    row = _row(repository, PASS_RUN)
    assert row is not None, "the settled delivery kept no ledger row"
    assert row["run_id"] == PASS_RUN
    assert row["gate"] == "passed", row
    carrying = str(row.get("promoted_revision") or "")
    assert carrying, row
    _git(repository, "merge-base", "--is-ancestor", carrying, "HEAD")
    assert not runs.pointer_path(PASS_RUN).exists()


def test_a_landing_whose_merge_and_row_succeed_writes_its_record_as_today(
    landing: dict,
) -> None:
    case = landing
    repository = Path(str(case["repo"]))
    pointer = read_pointer(RUN_ID)
    pending_landing.write_pending(
        RUN_ID,
        PROJECT,
        pointer={**pointer, "automatic_landing_gate": dict(case["gate"])},
        head=str(case["head"]),
    )
    crew.complete(
        RUN_ID,
        gate="passed",
        commits=(str(case["head"]),),
        outcome="the reviewed delivery reached the primary branch",
        root=repository,
        require_gate_check=False,
        gate_check=dict(case["gate"]),
    )

    results = pending_landing.finish_pending_landings(only_run=RUN_ID)

    assert [row["status"] for row in results] == ["finished"], results
    assert _declaration(case) == "done"
    assert any(RUN_ID in body for body in _close_comments(case)), _close_comments(case)
    assert not runs.pointer_path(RUN_ID).exists()
    row = _row(repository, RUN_ID)
    assert row is not None
    assert row["run_id"] == RUN_ID
    assert row["gate"] == "passed", row
    _git(repository, "merge-base", "--is-ancestor", str(case["head"]), "HEAD")
