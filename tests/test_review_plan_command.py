"""Plan review composition, quiet eligibility, responses and the read surface."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import cli, flight, mcp
from reckon.crew import plan_review, recovery, review, runs

CONFIG = {
    "default_backend": "worker",
    "local_backend": "worker",
    "backends": {
        "worker": {
            "launch": "in-harness",
            "model": "test-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"review": {"backend": "worker", "execution_capable": True}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


@pytest.fixture
def project(tmp_path, monkeypatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    path = plans / "fixture.html"
    path.write_text(
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<meta name="plan-title" content="Fixture">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-version" content="3">'
        '</head><body><h2 id="delivery">Delivery</h2>'
        "<p>Extend the existing mechanism.</p></body></html>",
        encoding="utf-8",
    )
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
        ["add", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed fixture", "-m", "Supply a committed plan."],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"sample": str(repo / "docs")}))
    monkeypatch.setattr(flight, "resolve", lambda **kw: SimpleNamespace(config=CONFIG))
    return home, repo, path


def _subject(**kwargs):
    return recovery.plan_review_subject("sample", "fixture", "coordinator", **kwargs)


def _stored(path):
    record = {
        "project": "sample",
        "plan_slug": "fixture",
        "plan_version": 3,
        "reviewed_blob_sha": "a" * 40,
        "plan_fingerprint": plan_review.plan_fingerprint(path),
        "rubric": "design",
        "findings": [
            {"id": "reuse-owner", "type": "reuse_search", "text": "Name the owner."}
        ],
        "responses": {},
    }
    plan_review.store_plan_review(record)
    return record


def _quiet(path):
    stamp = time.time() - 1000
    os.utime(path, (stamp, stamp))


@pytest.mark.parametrize(
    "rubric,items",
    [
        ("design", review.PLAN_DESIGN_REVIEW_ITEMS),
        ("content", review.PLAN_REVIEW_ITEMS),
    ],
)
def test_composer_carries_rubric_snapshot_and_sidecar(project, rubric, items):
    _, repo, path = project
    subject = _subject(rubric=rubric)
    fields = recovery._review_dispatch_fields(subject)
    assert fields["node_id"] == "plan-review-of-fixture"
    brief = Path(fields["brief"]).read_text()
    assert all(item in brief for item in items)
    assert str(path) in brief and str(repo) in brief
    assert "RUBRIC" in brief and "FINDING" in brief
    directory = plan_review.review_report_directory(
        "sample", "fixture", subject["run_id"]
    )
    assert fields["write_paths"] == [str(directory)]
    assert (directory / "plan.html").read_bytes() == path.read_bytes()
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    blob = subprocess.check_output(
        ["git", "hash-object", str(path)], cwd=repo, text=True
    ).strip()
    assert sidecar["plan_slug"] == "fixture"
    assert sidecar["plan_version"] == 3
    assert sidecar["reviewed_blob_sha"] == blob
    assert sidecar["plan_fingerprint"] == plan_review.plan_fingerprint(path)
    assert sidecar["rubric"] == rubric
    argv = recovery._review_dispatch_argv(subject, config=CONFIG)
    assert argv[argv.index("--node") + 1] == fields["node_id"]
    assert argv[argv.index("--role") + 1] == "review"
    assert argv[argv.index("--spec-level") + 1] == "exact"
    assert argv[argv.index("--brief") + 1] == fields["brief"]
    assert "--plan" not in argv


def test_sweep_dispatches_only_quiet_unreviewed_content(project, monkeypatch):
    _, _, path = project
    _quiet(path)
    calls = []
    dispatch = importlib.import_module("reckon.crew.dispatch")

    def launch(**kwargs):
        calls.append(kwargs)
        assert Path(kwargs["node"].brief).with_name("plan-review.json").is_file()
        return {"run_id": f"review-attempt-{len(calls)}"}

    monkeypatch.setattr(dispatch, "dispatch", launch)
    first = recovery.dispatch_awaiting_reviews(
        project="sample", config=CONFIG, session="coordinator"
    )
    assert first["dispatched"] == ["review-attempt-1"]
    assert calls[0]["node"].id == "plan-review-of-fixture"
    assert calls[0]["session"] == "coordinator"
    assert calls[0]["local"] is True
    receipt = Path(calls[0]["node"].brief).with_name("dispatch.json")
    assert json.loads(receipt.read_text())["run_id"] == "review-attempt-1"
    _stored(path)
    second = recovery.dispatch_awaiting_reviews(
        project="sample", config=CONFIG, session="coordinator"
    )
    assert second["dispatched"] == []
    assert len(calls) == 1


def test_sweep_waits_for_settle_window(project, monkeypatch):
    _, _, path = project
    stamp = time.time() - 30
    os.utime(path, (stamp, stamp))
    calls = []
    monkeypatch.setattr(
        recovery, "dispatch_review_for_run", lambda *a, **k: calls.append(a)
    )
    report = recovery.dispatch_awaiting_reviews(
        project="sample",
        config={**CONFIG, "review": {"plan_settle_seconds": 60}},
        session="coordinator",
    )
    assert report["dispatched"] == [] and calls == []


def test_sweep_skips_delivered_unstored_report(project, monkeypatch):
    _, _, path = project
    _quiet(path)
    fields = recovery._review_dispatch_fields(_subject())
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    Path(sidecar["report_path"]).write_text("RUBRIC reuse_search: pass\n")
    assert plan_review.delivered_reports("sample", "fixture")[0]["stored"] is False
    calls = []
    monkeypatch.setattr(
        recovery, "dispatch_review_for_run", lambda *a, **k: calls.append(a)
    )
    report = recovery.dispatch_awaiting_reviews(
        project="sample", config=CONFIG, session="coordinator"
    )
    assert report["dispatched"] == [] and calls == []


def test_plan_review_in_flight_is_shared_across_sessions(project, monkeypatch):
    subject = _subject()
    monkeypatch.setattr(
        recovery,
        "list_live",
        lambda **kw: [
            {
                "run_id": "standing-review",
                "project": "sample",
                "session": "another-coordinator",
                "node": {"id": "plan-review-of-fixture"},
                "phase": "working",
            }
        ],
    )
    result = recovery.dispatch_review_for_run(subject, config=CONFIG)
    assert result["dispatched"] is False
    assert result["review_run_id"] == "standing-review"


def test_review_plan_dry_run_resolves_the_shared_composition(project):
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "review-plan",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--session",
            "coordinator",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["node"]["id"] == "plan-review-of-fixture"
    assert payload["node"]["role"] == "review"
    assert (
        payload["node"]["brief"]
        == payload["argv"][payload["argv"].index("--brief") + 1]
    )
    assert payload["dry_run"] is True
    assert payload["validation"]["ok"] is True
    assert runs.list_live(project="sample") == []


def test_review_plan_launch_passes_session_and_local(project, monkeypatch):
    dispatch = importlib.import_module("reckon.crew.dispatch")
    calls = []

    def launch(**kwargs):
        calls.append(kwargs)
        return {"run_id": "launched-review"}

    monkeypatch.setattr(dispatch, "dispatch", launch)
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "review-plan",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--session",
            "named-session",
            "--local",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["review_run_id"] == "launched-review"
    assert calls[0]["session"] == "named-session"
    assert calls[0]["local"] is True


@pytest.mark.parametrize(
    "options",
    [
        ["--declined"],
        ["--declined", ""],
        ["--acted", "--declined", "reason"],
        [],
    ],
)
def test_answer_refuses_missing_reason_or_ambiguous_action(project, options):
    _stored(project[2])
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "review-plan",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--answer",
            "reuse-owner",
            *options,
        ],
    )
    assert result.exit_code != 0
    assert plan_review.read_plan_review("sample", "fixture")["responses"] == {}


@pytest.mark.parametrize(
    "options,action",
    [
        (["--declined", "The existing owner already covers this."], "declined"),
        (["--acted"], "acted"),
    ],
)
def test_answer_records_response_through_store(project, options, action):
    _stored(project[2])
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "review-plan",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--answer",
            "reuse-owner",
            *options,
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["unanswered"] == []
    stored = plan_review.read_plan_review("sample", "fixture")
    assert stored["responses"]["reuse-owner"]["action"] == action
    if action == "declined":
        assert stored["responses"]["reuse-owner"]["reason"] == options[1]


def test_mcp_view_returns_stored_unanswered_and_delivered(project):
    assert mcp._crew(project="sample", plan="fixture", view="plan-review") == {
        "record": None,
        "unanswered": [],
        "delivered": [],
    }
    _stored(project[2])
    fields = recovery._review_dispatch_fields(_subject())
    directory = Path(fields["sidecar"]).parent
    (directory / "report.md").write_text("RUBRIC reuse_search: pass\n")
    payload = mcp._crew(project="sample", plan="fixture", view="plan-review")
    assert payload["record"]["plan_slug"] == "fixture"
    assert payload["unanswered"] == ["reuse-owner"]
    assert payload["delivered"][0]["report_path"] == str(directory / "report.md")
    assert mcp._crew(project="sample", view="plan-review")["error"] == "missing_plan"
    assert mcp._crew(plan="fixture", view="plan-review")["error"] == "missing_project"


def test_flight_accepts_and_reads_plan_settle_seconds():
    flight.validate_layer({"review": {"plan_settle_seconds": 42}}, "test")
    assert (
        flight.plan_review_settle_seconds({"review": {"plan_settle_seconds": 42}}) == 42
    )
    assert flight.plan_review_settle_seconds({}) == 600
    for value in (-1, True, "42"):
        with pytest.raises(flight.FlightConfigError, match="plan_settle_seconds"):
            flight.validate_layer({"review": {"plan_settle_seconds": value}}, "test")


def test_local_review_keeps_the_selected_lane_when_picker_routing_is_enabled(project):
    config = {
        **CONFIG,
        "local_backend": "local-worker",
        "backends": {
            **CONFIG["backends"],
            "local-worker": CONFIG["backends"]["worker"],
        },
        "routing": {"picker": "route"},
    }
    payload = recovery.dispatch_review_for_run(
        _subject(local=True), config=config, dry_run=True
    )
    assert payload["backend"] == "local-worker"
    assert payload["route"] == "deterministic"
    assert payload["local"] is True
    assert "--local" in payload["argv"]


def test_launch_returns_harness_directive_and_writes_a_live_pointer(
    project, monkeypatch, tmp_path
):
    _, repo, _ = project
    dispatch = importlib.import_module("reckon.crew.dispatch")
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()

    def prepare(*args):
        path = tmp_path / "review-worktree"
        path.mkdir()
        (path / "seed.txt").write_text("review workspace")
        return {"path": str(path), "base": "HEAD", "base_sha": head}

    monkeypatch.setattr(dispatch, "_create_worktree", prepare)
    payload = recovery.dispatch_review_for_run(_subject(), config=CONFIG)
    assert payload["dispatched"] is True, payload
    launched = payload["dispatch"]
    assert launched["launch"] == "in-harness"
    assert launched["directive"]
    pointer = runs.read_pointer(payload["review_run_id"])
    assert pointer["node"]["id"] == "plan-review-of-fixture"
    assert pointer["session"] == "coordinator"
    assert recovery._is_review_node(pointer)
    again = recovery.dispatch_review_for_run(_subject(), config=CONFIG)
    assert again["dispatched"] is False
    assert again["review_run_id"] == payload["review_run_id"]
