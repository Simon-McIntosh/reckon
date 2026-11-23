"""Every plan state write that answers as landed survives a concurrent promotion.

Six kinds of plan state write meet one run's promotion on a landing plan file: a
scalar set, a section declaration set, a followup resolve, a followup append, a
decision lock and a section append. Each is made before, during and after the
landing, and each must answer as landed while its change is still in the
checkout's plan file once the landing has finished — in the landing's commit, or
as an uncommitted change, never reverted.

Two store writes meeting each other are not this measure: the store already
refuses a stale version. The other writer here is the promotion's own landing —
its write of the plan's landing comment and the commit that lands that write and
the run's head together.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from reckon import _plan_html, _store
from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path
from reckon.mcp_edit_plan import _edit_plan
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import _complete_land, _write_origin_and_checkout

PROJECT = "proj"
PLAN = "plan-a"
PLAN_PATH = f"docs/plans/{PLAN}.html"
SECTION = "s2"
SUMMARY = "written while a landing was in flight"
RUN_ID = "r-20261121T214500000000-evsw-landing"

_CAPABILITY = {
    "version": "1.0",
    "class": "general",
    "requirements": {
        "reasoning": "standard",
        "verification": "strict",
        "risk": "low",
    },
}


def _state(document: str) -> dict[str, Any]:
    return _plan_html.read_state(document)


def _summary_written(state: dict[str, Any]) -> bool:
    return state.get("summary") == SUMMARY


def _declaration_written(state: dict[str, Any]) -> bool:
    return (state.get("section_declarations") or {}).get(SECTION) == "done"


def _followup_resolved(state: dict[str, Any]) -> bool:
    return any(
        str(row.get("id")) == "f1" and str(row.get("status") or "") == "resolved"
        for row in state.get("followups") or []
    )


def _followup_appended(state: dict[str, Any]) -> bool:
    return any(str(row.get("id")) == "f2" for row in state.get("followups") or [])


def _decision_locked(state: dict[str, Any]) -> bool:
    return (state.get("decisions") or {}).get("transport", {}).get("choice") == "stdio"


def _section_appended(state: dict[str, Any]) -> bool:
    return any(str(row.get("id")) == "s9" for row in state.get("sections") or [])


#: One op batch per kind of state write, and the predicate its change leaves.
WRITES: dict[str, tuple[list[dict[str, Any]], Callable[[dict[str, Any]], bool]]] = {
    "scalar-set": (
        [{"op": "set", "path": "summary", "value": SUMMARY}],
        _summary_written,
    ),
    "declaration-set": (
        [{"op": "set", "path": f"section_declarations.{SECTION}", "value": "done"}],
        _declaration_written,
    ),
    "followup-resolve": (
        [
            {
                "op": "resolve",
                "target": "followups",
                "id": "f1",
                "by": "worker",
                "outcome": "no followup — the chain ends here",
            }
        ],
        _followup_resolved,
    ),
    "followup-append": (
        [
            {
                "op": "append",
                "target": "followups",
                "item": {
                    "id": "f2",
                    "written_by": "worker",
                    "written_at": "2026-11-21",
                    "title": "next",
                    "body": "do it",
                    "prompt": "/reckon-build plan-b §1",
                },
            }
        ],
        _followup_appended,
    ),
    "decision-lock": (
        [
            {
                "op": "lock",
                "key": "transport",
                "choice": "stdio",
                "rationale": "default",
                "by": "worker",
            }
        ],
        _decision_locked,
    ),
    "section-append": (
        [
            {
                "op": "insert_section",
                "id": "s9",
                "title": "Nine",
                "body": "<p>Appended while a landing was in flight.</p>",
                "effort_hours": 1.0,
                "capability": _CAPABILITY,
                "links": [],
            }
        ],
        _section_appended,
    ),
}


def _seed_plan(repository: Path) -> None:
    """Give the plan a landing section, an open followup and an open decision."""
    _state, version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    result = _edit_plan(
        PROJECT,
        PLAN,
        [
            {
                "op": "insert_section",
                "id": SECTION,
                "title": "Two",
                "body": "<p>The landing section.</p>",
                "effort_hours": 1.0,
                "capability": _CAPABILITY,
                "links": [],
            },
            {
                "op": "append",
                "target": "followups",
                "item": {
                    "id": "f1",
                    "written_by": "worker",
                    "written_at": "2026-11-21",
                    "title": "first",
                    "body": "finish it",
                    "prompt": "/reckon-build plan-b §1",
                },
            },
            {
                "op": "append",
                "target": "decisions",
                "key": "transport",
                "item": {
                    "title": "Which transport?",
                    "context": "ctx",
                    "choices": ["stdio", "http"],
                    "choice": "",
                },
            },
        ],
        version,
        checkout_path=str(repository),
    )
    assert result["ok"] is True, result
    _git(repository, "add", "--", PLAN_PATH)
    _git(repository, "commit", "-q", "-m", "test: seed the landing plan")
    _git(repository, "push", "-q", "origin", "main")


@pytest.fixture()
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A finished run whose head carries one committed change, ready to land."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    _seed_plan(repository)
    worktree = tmp_path / "worktrees" / "landing"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    (worktree / "reviewed.txt").write_text("the reviewed change\n", encoding="utf-8")
    _git(worktree, "add", "reviewed.txt")
    _git(worktree, "commit", "-q", "-m", "add the reviewed change")
    head = _git(worktree, "rev-parse", "HEAD")
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: landed-state-write\nstatus: complete\n", encoding="utf-8"
    )
    gate_log = tmp_path / "gate.log"
    gate_log.write_text("# command: true\nEXIT=0\n", encoding="utf-8")
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "attempt": 1,
            "phase": "delivered",
            "created_at": "2026-11-21T21:45:00Z",
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "manifest_path": str(manifest),
            "node": {
                "id": "landed-state-write",
                "plan": PLAN,
                "section": SECTION,
                "time_budget": "35m",
                "write_paths": ["reviewed.txt"],
            },
        },
    )
    return {
        "origin": origin,
        "repository": repository,
        "worktree": worktree,
        "run_id": RUN_ID,
        "head": head,
        "gate_log": gate_log,
    }


def _write(kind: str, repository: Path) -> dict[str, Any]:
    """Make one state write through the surface every editor reaches."""
    _current, version = _store.read_plan(
        PROJECT, PLAN, repository, artifact_type="plan"
    )
    ops, holds = WRITES[kind]
    result = _edit_plan(PROJECT, PLAN, ops, version, checkout_path=str(repository))
    assert result.get("ok") is True, result
    written, written_version = _store.read_plan(
        PROJECT, PLAN, repository, artifact_type="plan"
    )
    assert result["new_version"] == written_version
    assert holds(written), f"the {kind} write did not reach the file"
    return result


def _assert_answered_and_held(
    run: dict[str, Any], kind: str, result: dict[str, Any]
) -> None:
    """The write answered as landed and its change is in the plan afterwards."""
    _ops, holds = WRITES[kind]
    assert result.get("ok") is True, result
    path = run["repository"] / PLAN_PATH
    on_disk = path.read_text(encoding="utf-8")
    assert holds(_state(on_disk)), f"the landed {kind} write was reverted"
    assert _git(run["repository"], "show", "HEAD:reviewed.txt") == "the reviewed change"
    assert _git(run["origin"], "show", "main:reviewed.txt") == "the reviewed change"
    assert any(
        row.get("id") == f"c-run-{RUN_ID}"
        for rows in _state(on_disk)["comments"].values()
        for row in rows
    ), "the promotion's landing comment did not reach the checkout"
    # A write the landing committed is in HEAD; one it did not is still an
    # uncommitted change in the checkout. Either is a survival.
    if not _git(run["repository"], "status", "--porcelain", "--", PLAN_PATH):
        committed = _git(run["repository"], "show", f"HEAD:{PLAN_PATH}")
        assert holds(_state(committed)), (
            f"the landing committed a plan file without the {kind} write"
        )


@pytest.mark.parametrize("kind", sorted(WRITES))
def test_a_state_write_made_before_the_landing_survives(
    run: dict[str, Any], kind: str
) -> None:
    result = _write(kind, run["repository"])
    landing = _complete_land(run)
    assert landing.exit_code == 0, landing.output
    _assert_answered_and_held(run, kind, result)


@pytest.mark.parametrize("kind", sorted(WRITES))
@pytest.mark.parametrize("boundary", ["before-comment", "after-comment"])
def test_a_state_write_made_during_the_landing_survives(
    run: dict[str, Any], kind: str, boundary: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = promotion._record_comment_with_snapshot
    outcomes: dict[str, Any] = {}

    def wrapper(snapshot, **kwargs):
        if boundary == "before-comment" and snapshot is not None and not outcomes:
            outcomes["write"] = _write(kind, run["repository"])
        comment = real(snapshot, **kwargs)
        if boundary == "after-comment" and snapshot is not None and not outcomes:
            outcomes["write"] = _write(kind, run["repository"])
        return comment

    monkeypatch.setattr(promotion, "_record_comment_with_snapshot", wrapper)
    landing = _complete_land(run)
    assert landing.exit_code == 0, landing.output
    _assert_answered_and_held(run, kind, outcomes["write"])


@pytest.mark.parametrize("kind", sorted(WRITES))
def test_a_state_write_made_after_the_landing_survives(
    run: dict[str, Any], kind: str
) -> None:
    landing = _complete_land(run)
    assert landing.exit_code == 0, landing.output
    result = _write(kind, run["repository"])
    _assert_answered_and_held(run, kind, result)


def test_an_unrecorded_prose_edit_after_a_state_write_still_refuses(
    run: dict[str, Any],
) -> None:
    _write("section-append", run["repository"])
    path = run["repository"] / PLAN_PATH
    document = path.read_text(encoding="utf-8")
    assert "Appended while a landing was in flight." in document
    edited = document.replace(
        "Appended while a landing was in flight.", "Uncommitted prose stays separate."
    )
    path.write_text(edited, encoding="utf-8")

    landing = _complete_land(run)

    assert landing.exit_code != 0, landing.output
    assert "authored content outside those store-owned regions" in landing.output
    assert path.read_text(encoding="utf-8") == edited
    assert _section_appended(_state(edited))
    assert not (run["repository"] / "reviewed.txt").exists()


def test_a_state_write_whose_provenance_cannot_be_saved_rolls_back(
    run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = run["repository"] / PLAN_PATH
    before = path.read_bytes()

    def refuse_receipt(*args, **kwargs):
        raise OSError("the provenance destination is unavailable")

    monkeypatch.setattr(_store, "_record_plan_text_write", refuse_receipt)

    with pytest.raises(OSError, match="provenance destination is unavailable"):
        _write("section-append", run["repository"])

    assert path.read_bytes() == before
