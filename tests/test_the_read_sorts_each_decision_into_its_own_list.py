"""The decision read puts every record in the one list its state belongs to.

Four states are told apart rather than flattened into one list: a decision
waiting on the lead is ready, one waiting on a named run is pending, one a
session already took under a standing rule is taken and reversible, and one
written only in a plan's decisions block holds nothing behind it at all. The
plan-block record has no owning session, no recording time and no held work,
and those three are reported absent by name rather than as empty values, so a
reader cannot mistake "nothing recorded" for "recorded as nothing".

A decision found to be already settled is recorded as withdrawn, naming the
decision that settled it. It stands in no list — otherwise the lead is asked
twice — and the work waiting behind it is released by the next admission pass
rather than stranded until a coordinator notices.

The fixture is a synthesised crew home with two mounted projects, so nothing
here reads or writes the operator's crew home.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import flight
from reckon._plan_html import write_state
from reckon.crew import queue_admission, runs
from reckon.crew.lead_decisions import record_decision
from reckon.crew.node import TaskNode
from reckon.mcp_views import serve_crew_view

PROJECT = "sample"
COMPANION = "companion"
PLAN = "transport"
SESSION = "owner-session"

CONFIG = {
    "default_backend": "clive",
    "local_backend": "clive",
    "backends": {
        "clive": {
            "launch": "cli",
            "command": "codex",
            "model": "local-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "time_builder": "25m",
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


def _plan_html() -> str:
    sections = "".join(
        f'<h2 id="{name}">§{name[1:]}</h2>'
        f"<p>build {name} and ship it without regressing anything else.</p>"
        f"<p><strong>Done when</strong>: {name} ships and nothing regresses.</p>"
        for name in ("s2", "s3")
    )
    declarations = json.dumps({"s2": "implementable", "s3": "implementable"})
    return (
        "<!doctype html>\n<html><head>\n"
        f'<meta name="docs-project" content="{PROJECT}">\n'
        '<meta name="reckon-type" content="plan">\n'
        f'<meta name="plan-slug" content="{PLAN}">\n'
        '<meta name="default-plan-title" content="">\n'
        f'<meta name="plan-title" content="{PLAN}">\n'
        '<meta name="plan-status" content="active">\n'
        '<meta name="plan-version" content="3">\n'
        f'<meta name="plan-section-declarations" content="{declarations}">\n'
        "</head><body>\n"
        f"{sections}"
        "</body></html>\n"
    )


@pytest.fixture()
def crew(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(config_home / "cache" / "velocity"))
    monkeypatch.setattr(
        flight, "resolve", lambda *args, **kwargs: SimpleNamespace(config=CONFIG)
    )
    repository = tmp_path / "checkout"
    (repository / "docs" / "plans").mkdir(parents=True)
    (repository / "docs" / "plans" / f"{PLAN}.html").write_text(
        _plan_html(), encoding="utf-8"
    )
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", "docs")
    _git(repository, "commit", "-q", "-m", "chore: seed the plan")
    companion = tmp_path / COMPANION / "docs" / "plans"
    companion.mkdir(parents=True)
    (config_home / "mounts.json").write_text(
        json.dumps(
            {PROJECT: str(repository / "docs"), COMPANION: str(companion.parent)}
        ),
        encoding="utf-8",
    )
    return SimpleNamespace(home=config_home, repo=repository, companion=companion)


def _record(project: str = PROJECT, **extra) -> dict:
    return {
        "question": "Which transport carries the work?",
        "context": "The current transport is saturated. A second route is available.",
        "options": [
            {"value": "wait", "label": "Wait for the current transport to drain."},
            {"value": "route", "label": "Use the second route now."},
        ],
        "recommended": "route",
        "project": project,
        "session": SESSION,
        "plan": PLAN,
        "section": "s2",
        "held_requests": [],
        "held_sections": [],
        **extra,
    }


def _plan_block(docs: Path) -> None:
    """A decision written only in a plan's decisions block, holding nothing."""
    (docs / f"{PLAN}.html").write_text(
        write_state(
            "<html><head><title>Transport</title></head><body>"
            '<h2 id="payload">Payload</h2></body></html>',
            {
                "slug": PLAN,
                "status": "active",
                "modified": "2026-10-01",
                "decisions": {
                    "layout": {
                        "title": "Which layout carries the payload?",
                        "choices": ["flat", "nested"],
                        "option_labels": {"flat": "Flat", "nested": "Nested"},
                        "recommended": "nested",
                    }
                },
            },
        ),
        encoding="utf-8",
    )


def _ids(view: dict, key: str) -> list[str]:
    return [str(row["id"]) for row in view[key]]


def test_each_state_lands_in_its_own_list_and_in_no_other(crew) -> None:
    _plan_block(crew.companion)
    ready = record_decision(_record())
    pending = record_decision(_record(pending_on={"run_id": "awaited-run"}))
    taken = record_decision(
        _record(choice="route", reversible=True, standing_rule="Use spare capacity")
    )

    view = serve_crew_view("decisions")
    assert view["ok"] is True, view
    assert _ids(view, "decisions") == [ready["id"]]
    assert _ids(view, "pending") == [pending["id"]]
    assert _ids(view, "taken_and_reversible") == [taken["id"]]
    block = [f"plan:{COMPANION}:{PLAN}:layout"]
    assert _ids(view, "plan_block") == block

    lists = {
        key: _ids(view, key)
        for key in ("decisions", "pending", "taken_and_reversible", "plan_block")
    }
    reported = [identifier for ids in lists.values() for identifier in ids]
    assert len(reported) == len(set(reported)), lists
    assert view["total_count"] == len(reported) == 4
    assert view["count"] == 1


def test_a_plan_block_record_names_its_absent_fields(crew) -> None:
    _plan_block(crew.companion)

    row = serve_crew_view("decisions")["plan_block"][0]
    assert row["state"] == "plan-block"
    assert row["session"] is None
    assert row["recorded_at"] is None
    assert row["held_work_count"] is None
    assert row["absent"] == ["session", "recorded_at", "held_work"]
    assert row["held_requests"] == []
    assert row["plan"] == PLAN
    assert row["section"] is None
    assert row["options"][0]["value"] == "nested"


def _queue_one(repository: Path, run_id: str = "r-held") -> None:
    queue_admission.queue_requests(
        [
            TaskNode(
                id="n-held",
                goal="build section two",
                plan=PLAN,
                section="s2",
                done_when="pytest reports passed for section two",
                write_paths=["s2.txt"],
                time_budget="20m",
                spec_level="exact",
            )
        ],
        project=PROJECT,
        session=SESSION,
        repo_root=repository,
        config=CONFIG,
        run_ids={"n-held": run_id},
    )


def _hold_behind(run_id: str, decision_id: str) -> None:
    """Hold a queued request for the lead's word on one decision."""
    from reckon.crew import queue_edit

    queue_edit.hold(run_id, reason="waiting on the lead's decision")
    path = runs.pointer_path(run_id)
    pointer = json.loads(path.read_text(encoding="utf-8"))
    pointer["queue_hold"]["decision_id"] = decision_id
    path.write_text(json.dumps(pointer), encoding="utf-8")


def test_a_withdrawn_report_stands_in_no_list_and_releases_what_it_held(crew) -> None:
    ready = record_decision(_record())
    _queue_one(crew.repo)
    withdrawn = record_decision(
        _record(withdrawn_by=ready["id"], held_requests=["r-held"])
    )
    _hold_behind("r-held", withdrawn["id"])
    assert runs.read_pointer("r-held")["phase"] == "held"

    view = serve_crew_view("decisions")
    assert _ids(view, "decisions") == [ready["id"]]
    reported = [
        identifier
        for key in ("decisions", "pending", "taken_and_reversible", "plan_block")
        for identifier in _ids(view, key)
    ]
    assert withdrawn["id"] not in reported
    assert view["total_count"] == 1

    result = queue_admission.admit_once(
        config_for=lambda pointer: CONFIG,
        launcher=lambda plan, **kwargs: os.getpid(),
    )
    assert result["released_withdrawn"] == ["r-held"], result
    pointer = runs.read_pointer("r-held")
    assert pointer["phase"] != "held"
    assert "queue_hold" not in pointer
