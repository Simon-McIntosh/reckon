"""Recovery dispatch requires an explicit project and a live coordinator."""

from __future__ import annotations

import importlib

import pytest
from click.testing import CliRunner

from reckon import cli
from reckon.crew import recovery_classification
from reckon.crew import recovery_repair_dispatch
from reckon.crew import recovery_review_acceptance
from reckon.crew import recovery_review_delivery
from reckon.crew import recovery_review_dispatch
from reckon.crew import recovery_review_subject
from reckon.crew import recovery_watch
from reckon.crew import recovery


@pytest.fixture
def scoring_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    pointers = [
        {"run_id": "r-named", "project": "named", "session": "active"},
        {"run_id": "r-foreign", "project": "foreign", "session": "active"},
    ]
    (monkeypatch.setattr(recovery_review_delivery, "list_live", lambda: pointers), monkeypatch.setattr(recovery_review_dispatch, "list_live", lambda: pointers), monkeypatch.setattr(recovery_repair_dispatch, "list_live", lambda: pointers), monkeypatch.setattr(recovery_review_acceptance, "list_live", lambda: pointers), monkeypatch.setattr(recovery_watch, "list_live", lambda: pointers))
    monkeypatch.setattr(
        importlib.import_module("reckon.crew.dispatch"),
        "observe",
        lambda run_id, config=None: next(p for p in pointers if p["run_id"] == run_id),
    )
    monkeypatch.setattr(recovery_watch, "_derive_missing_manifest", lambda p, config=None: p)
    (monkeypatch.setattr(
        recovery_review_dispatch,
        "classify_pointer",
        lambda p: {"run_id": p["run_id"], "classification": "scoring"},
    ), monkeypatch.setattr(
        recovery_review_acceptance,
        "classify_pointer",
        lambda p: {"run_id": p["run_id"], "classification": "scoring"},
    ), monkeypatch.setattr(
        recovery_watch,
        "classify_pointer",
        lambda p: {"run_id": p["run_id"], "classification": "scoring"},
    ))
    launches = []

    def launch(record, **_kwargs):
        assert _kwargs.get("prefer_local") is True
        launches.append(record["run_id"])
        return {
            "run_id": record["run_id"],
            "dispatched": True,
            "review_run_id": "review",
        }

    (monkeypatch.setattr(recovery_review_acceptance, "dispatch_review_for_run", launch), monkeypatch.setattr(recovery_watch, "dispatch_review_for_run", launch))
    monkeypatch.setattr(
        recovery.runs,
        "follower_state",
        lambda project, session: {"live": session == "active"},
    )
    return pointers, launches


def test_unscoped_classification_launches_nothing(scoring_runs):
    _pointers, launches = scoring_runs
    report = recovery.recover()
    assert len(report["runs"]) == 2
    assert launches == []
    assert report["reviews_dispatched"] == []


def test_scoped_classification_launches_nothing(scoring_runs):
    _pointers, launches = scoring_runs
    report = recovery.recover(project="named")
    assert [row["run_id"] for row in report["runs"]] == ["r-named"]
    assert launches == []


def test_opted_in_sweep_launches_only_the_named_project(scoring_runs):
    _pointers, launches = scoring_runs
    report = recovery.recover(project="named", dispatch_reviews=True)
    assert launches == ["r-named"]
    assert report["reviews_dispatched"] == ["review"]


def test_unscoped_opt_in_refuses_before_launch(scoring_runs):
    _pointers, launches = scoring_runs
    with pytest.raises(recovery.CrewError, match="--project"):
        recovery.recover(dispatch_reviews=True)
    assert launches == []


def test_departed_session_awaits_its_coordinator(scoring_runs):
    pointers, launches = scoring_runs
    pointers[0]["session"] = "departed"
    report = recovery.recover(project="named", dispatch_reviews=True)
    assert launches == []
    assert [r["run_id"] for r in report["reviews_awaiting_coordinator"]] == ["r-named"]
    assert report["reviews_awaiting_coordinator"][0]["status"] == (
        "awaiting-coordinator"
    )


def test_recover_help_names_its_opted_in_launch():
    help_text = CliRunner().invoke(cli.main, ["crew", "recover", "--help"])
    assert help_text.exit_code == 0
    assert "review" in help_text.output.split("Options:")[0].lower()
    assert "--dispatch-reviews" in help_text.output
    assert "review" in recovery.recover.__doc__.splitlines()[0].lower()


def test_cli_refuses_unscoped_review_launch():
    command = CliRunner().invoke(cli.main, ["crew", "recover", "--dispatch-reviews"])
    assert command.exit_code != 0
    assert "--project" in command.output


@pytest.mark.parametrize(
    ("declared", "expected"), [(None, "local"), ("codex", "codex")]
)
def test_sweep_review_lane_uses_local_unless_node_declares_one(
    monkeypatch, declared, expected
):
    record = {
        "run_id": "r-source",
        "project": "named",
        "repo": "/unused/repository",
        "backend": "codex",
        "node": {"lane_declaration": {"backend": declared}} if declared else {},
    }
    config = {"local_backend": "local", "backends": {"local": {}, "codex": {}}}
    (monkeypatch.setattr(
        recovery_review_dispatch, "classify_pointer", lambda _: {"classification": "scoring"}
    ), monkeypatch.setattr(
        recovery_review_acceptance, "classify_pointer", lambda _: {"classification": "scoring"}
    ), monkeypatch.setattr(
        recovery_watch, "classify_pointer", lambda _: {"classification": "scoring"}
    ))
    (monkeypatch.setattr(recovery_review_dispatch, "_stored_review", lambda _: (None, "")), monkeypatch.setattr(recovery_repair_dispatch, "_stored_review", lambda _: (None, "")), monkeypatch.setattr(recovery_classification, "_stored_review", lambda _: (None, "")))
    monkeypatch.setattr(recovery_review_dispatch, "_review_in_flight", lambda _: "")
    monkeypatch.setattr(recovery_review_dispatch, "carry_review_forward", lambda *_a, **_k: None)
    (monkeypatch.setattr(recovery_review_subject, "_worktree_reclaimed", lambda _: False), monkeypatch.setattr(recovery_review_dispatch, "_worktree_reclaimed", lambda _: False))
    (monkeypatch.setattr(recovery_review_subject, "_failed_review_backend", lambda _: ""), monkeypatch.setattr(recovery_review_dispatch, "_failed_review_backend", lambda _: ""))
    (monkeypatch.setattr(recovery_review_dispatch, "_record_review_dispatch", lambda *_a, **_k: None), monkeypatch.setattr(recovery_review_acceptance, "_record_review_dispatch", lambda *_a, **_k: None))
    (monkeypatch.setattr(recovery_review_subject, "_resolved_review_config", lambda *_a: config), monkeypatch.setattr(recovery_review_dispatch, "_resolved_review_config", lambda *_a: config), monkeypatch.setattr(recovery_repair_dispatch, "_resolved_review_config", lambda *_a: config), monkeypatch.setattr(recovery_watch, "_resolved_review_config", lambda *_a: config))
    (monkeypatch.setattr(
        recovery_review_subject,
        "_review_dispatch_fields",
        lambda *_a, **_k: {
            "run_id": "r-source",
            "project": "named",
            "repo": "/unused/repository",
            "head": "abc123",
            "node_id": "review-of-source",
            "goal": "review source",
            "done_when": "review is stored",
            "plan": "sample",
            "section": "review",
            "write_paths": ["review.json"],
            "time_budget": "20m",
            "session": "active",
        },
    ), monkeypatch.setattr(
        recovery_review_dispatch,
        "_review_dispatch_fields",
        lambda *_a, **_k: {
            "run_id": "r-source",
            "project": "named",
            "repo": "/unused/repository",
            "head": "abc123",
            "node_id": "review-of-source",
            "goal": "review source",
            "done_when": "review is stored",
            "plan": "sample",
            "section": "review",
            "write_paths": ["review.json"],
            "time_budget": "20m",
            "session": "active",
        },
    ), monkeypatch.setattr(
        recovery_repair_dispatch,
        "_review_dispatch_fields",
        lambda *_a, **_k: {
            "run_id": "r-source",
            "project": "named",
            "repo": "/unused/repository",
            "head": "abc123",
            "node_id": "review-of-source",
            "goal": "review source",
            "done_when": "review is stored",
            "plan": "sample",
            "section": "review",
            "write_paths": ["review.json"],
            "time_budget": "20m",
            "session": "active",
        },
    ))
    monkeypatch.setattr(
        importlib.import_module("reckon.flight"),
        "select_local_backend",
        lambda cfg: cfg,
    )
    launches = []

    def launch(**kwargs):
        launches.append(kwargs)
        return {"run_id": "r-review"}

    monkeypatch.setattr(
        importlib.import_module("reckon.crew.dispatch"), "dispatch", launch
    )
    report = recovery.dispatch_review_for_run(record, config=config, prefer_local=True)
    assert report["dispatched"] is True
    assert report["backend"] == expected
    assert launches[0]["local"] is (expected == "local")
