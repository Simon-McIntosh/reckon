"""A whole-plan dispatch uses the batch queue and reports every section."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import crew_dispatch_commands
from reckon.cli import main
from reckon.crew import dispatch_batch, runs
from tests.test_a_batch_queues_the_chosen_sections import CONFIG, PROJECT, SESSION
from tests.test_a_batch_queues_the_chosen_sections import batch_repo as _batch_repo


@pytest.fixture()
def plan_repo(tmp_path, monkeypatch):
    batch_repo = _batch_repo.__wrapped__(tmp_path, monkeypatch)
    path = batch_repo / "docs/plans/whole-plan.html"
    declarations = dict.fromkeys(("s1", "s2", "s3", "s4", "s5"), "implementable")
    declarations.update(s6="done", s7="deferred")
    waits = {"s1": ["whole-plan#s3"], "s2": ["whole-plan#s3"]}
    path.write_text(
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-slug" content="whole-plan">'
        '<meta name="docs-project" content="proj">'
        '<meta name="plan-section-declarations" content="'
        + json.dumps(declarations).replace('"', "&quot;")
        + '"><meta name="plan-section-depends-on" content="'
        + json.dumps(waits).replace('"', "&quot;")
        + '">'
        + "".join(
            f'<h2 id="{section}">Build component {section}</h2>'
            f"<p>Write paths: <code>{section}.py</code>.</p>"
            "<p><strong>Done when.</strong> The component test passes.</p>"
            for section in declarations
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_dispatch_resolved_flight", lambda *args: CONFIG
    )
    return batch_repo


def _call(repository: Path, *extra: str, expected_exit: int = 0):
    result = CliRunner().invoke(
        main,
        [
            "crew",
            "dispatch",
            "--project",
            PROJECT,
            "--plan",
            "whole-plan",
            "--session",
            SESSION,
            "--repo",
            str(repository),
            *extra,
        ],
    )
    assert result.exit_code == expected_exit, result.output
    return json.loads(result.output)


def test_one_call_queues_five_sections_and_repeating_skips_the_same_runs(plan_repo):
    result = _call(plan_repo)
    counts = Counter(row["status"] for row in result["results"])
    assert counts == {"queued": 3, "awaiting-upstream": 2, "skipped": 2}
    assert result["queued"] == 5
    rows = {row["section"]: row for row in result["results"]}
    assert rows["s6"]["reason"] == "done"
    assert rows["s7"]["reason"] == "deferred"
    pointers = list(runs.list_live(project=PROJECT))
    assert len(pointers) == 5
    assert sum(pointer["phase"] == "awaiting-upstream" for pointer in pointers) == 2
    for pointer in pointers:
        node = pointer["node"]
        assert node["write_paths"] == [f"{node['section']}.py"]
        assert "component test passes" in node["done_when"]
    original = {
        row["section"]: row["run_id"] for row in result["results"] if "run_id" in row
    }
    again = _call(plan_repo)
    assert again["queued"] == 0
    assert all(row["status"] == "skipped" for row in again["results"])
    assert {
        row["section"]: row["run_id"] for row in again["results"] if "run_id" in row
    } == original
    assert len(list(runs.list_live(project=PROJECT))) == 5
    print(
        "MEASURE: queued=3 awaiting-upstream=2 repeat-queued=0 repeat-existing-skipped=5 done-skipped=1 deferred-skipped=1"
    )


def test_a_missing_done_when_refuses_only_its_row(plan_repo):
    path = plan_repo / "docs/plans/whole-plan.html"
    text = path.read_text()
    text = text.replace(
        "<p><strong>Done when.</strong> The component test passes.</p>", "", 1
    )
    path.write_text(text)
    result = _call(plan_repo, expected_exit=1)
    assert result["refused"] == 1
    assert result["queued"] == 4
    refused = next(row for row in result["results"] if row["status"] == "refused")
    assert refused["section"] == "s1"
    assert "no done-when" in refused["reason"]


def test_a_live_section_of_another_session_names_that_session(plan_repo):
    result = _call(plan_repo)
    run_id = result["results"][0]["run_id"]
    runs._mutate_pointer(
        run_id, lambda pointer: {**pointer, "session": "another-session"}
    )
    # A newer dispatch still belongs to this caller, so the plan owner gate
    # permits enumeration and the per-section skip names the other session.
    another = _call(plan_repo)
    row = another["results"][0]
    assert row["status"] == "skipped"
    assert row["run_id"] == run_id
    assert row["session"] == "another-session"
    assert "another-session" in row["reason"]


def test_the_resolved_plan_coordinator_owns_the_whole_call(plan_repo):
    _call(plan_repo)
    result = _call(plan_repo, "--session", "foreign-session")
    assert result["queued"] == 0
    assert result["skipped"] == 7
    assert all(row["session"] == SESSION for row in result["results"])
    assert all(SESSION in row["reason"] for row in result["results"])
    assert len(list(runs.list_live(project=PROJECT))) == 5


@pytest.mark.parametrize("phase", ["complete", "delivered", "promoted", "landed"])
def test_a_finished_run_is_skipped_even_without_a_live_pointer(plan_repo, phase):
    result = _call(plan_repo)
    held = result["results"][0]
    pointer = runs.read_pointer(held["run_id"])
    state = plan_repo / "docs/state/proj/runs"
    state.mkdir(exist_ok=True)
    (state / f"{held['run_id']}.json").write_text(
        json.dumps(
            {
                "run_id": held["run_id"],
                "project": PROJECT,
                "plan": "whole-plan",
                "section": "s1",
                "node": pointer["node"]["id"],
                "phase": phase,
                "session": SESSION,
                "dispatched_at": datetime.now(UTC).isoformat(),
            }
        )
    )
    runs.pointer_path(held["run_id"]).unlink()
    again = _call(plan_repo)
    assert again["results"][0]["status"] == "skipped"
    assert again["results"][0]["run_id"] == held["run_id"]
    assert len(list(runs.list_live(project=PROJECT))) == 4


def test_a_whole_plan_dry_run_writes_no_requests(plan_repo):
    result = CliRunner().invoke(
        main,
        [
            "crew",
            "dispatch",
            "--project",
            PROJECT,
            "--plan",
            "whole-plan",
            "--session",
            SESSION,
            "--repo",
            str(plan_repo),
            "--dry-run",
        ],
    )
    assert result.exit_code != 0
    assert "dry-run" in result.output
    assert list(runs.list_live(project=PROJECT)) == []


def test_a_malformed_section_contract_refuses_only_its_row(plan_repo):
    path = plan_repo / "docs/plans/whole-plan.html"
    path.write_text(
        path.read_text().replace("<code>s1.py</code>", "<code>../escape.py</code>")
    )
    result = _call(plan_repo, expected_exit=1)
    assert result["refused"] == 1
    assert result["queued"] == 4
    row = result["results"][0]
    assert row["status"] == "refused"
    assert "parent-directory segment" in row["reason"]


def test_a_request_deduplicated_during_queueing_names_its_session(
    plan_repo, monkeypatch
):
    original = dispatch_batch.queue_batch

    def raced(*args, **kwargs):
        result = original(*args, **kwargs)
        row = result["results"][0]
        runs._mutate_pointer(
            row["run_id"], lambda pointer: {**pointer, "session": "racing-session"}
        )
        row["status"] = "already-queued"
        return result

    monkeypatch.setattr(dispatch_batch, "queue_batch", raced)
    result = _call(plan_repo)
    assert result["queued"] == 4
    row = result["results"][0]
    assert row["status"] == "skipped"
    assert row["session"] == "racing-session"
    assert "racing-session" in row["reason"]
