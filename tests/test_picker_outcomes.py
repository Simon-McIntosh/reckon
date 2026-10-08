"""Picker outcomes use recorded decisions and run results."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from reckon import cli, crew_dispatch_commands
from reckon.crew import picker
from reckon.crew.picker import outcomes

STAMP = "2026-10-03T04:00:00Z"


def row(
    name,
    *,
    gate="passed",
    confidence=0.4,
    action="route",
    reason=None,
    backend="local",
    wall=100,
    review=80,
    burn=1.5,
):
    return {
        "run_id": name,
        "node": name,
        "plan": "sample",
        "role": "implement",
        "spec_level": "guided",
        "backend": backend,
        "route_mode": "picker",
        "gate": gate,
        "outcome": "",
        "review": {"total": review},
        "wall_seconds": wall,
        "dispatched_at": STAMP,
        "completed_at": STAMP,
        "picker_selection": {
            "action": action,
            "backend": backend if action == "route" else None,
            "confidence": confidence,
            "latency_ms": wall,
            "fallback_reason": reason,
            "offered": [
                {
                    "backend": "codex",
                    "family": "codex",
                    "burn_multiple": burn,
                    "pace_allowance": 0.4,
                },
                {"backend": "local", "family": "local"},
            ],
        },
    }


def test_figures_use_known_outcomes_and_offered_spend():
    rows = [
        row("one", confidence=0.49, wall=10, review=70),
        row("two", gate="failed", confidence=0.5, wall=20, review=90),
        row(
            "three", gate="not-run", confidence=0.7, wall=30, review=80, backend="codex"
        ),
        row("four", confidence=0.85, wall=40, review=100, backend="codex"),
        row("timeout", action="fallback", reason="timeout", confidence=None),
        row(
            "error", action="fallback", reason="jev-error: ValueError", confidence=None
        ),
        row(
            "input",
            action="fallback",
            reason="input records: unavailable",
            confidence=None,
        ),
        row("refused", action="refuse", confidence=None),
    ]
    rows[2]["outcome"] = "review scored 94, 1 finding(s)"
    rows[3]["attempt_kind"] = "redispatch"
    rows[4]["picker_selection"]["latency_ms"] = 50
    rows[7]["gate"] = "not-run"
    report = outcomes.summarize(
        {"demo": rows},
        {},
        profiles={
            "demo": {
                "groups": [
                    {
                        "backend": "local",
                        "role": "implement",
                        "spec_level": "guided",
                        "runs": 5,
                        "wall_seconds_median": 25,
                    }
                ]
            }
        },
    )
    assert report["mechanics"]["actions"] == {
        "route": 4,
        "hold": 0,
        "fallback": 3,
        "refuse": 1,
    }
    assert report["mechanics"]["fallback_reasons"] == {
        "timeout": 1,
        "jev-error": 1,
        "input-error": 1,
    }
    assert report["mechanics"]["latency_ms"]["overall"] == {
        "count": 8,
        "p50_ms": 45,
        "p90_ms": 100,
    }
    assert report["mechanics"]["latency_ms"]["demo"]["p90_ms"] == 100
    local = next(g for g in report["routed_outcomes"] if g["backend"] == "local")
    assert local["count"] == 2
    assert local["success_rate"] == 0.5
    assert local["review_score_median"] == 80
    assert local["profile_p50_seconds"] == 25
    assert local["wall_vs_profile_p50"] == 0.6
    codex = next(g for g in report["routed_outcomes"] if g["backend"] == "codex")
    assert codex["repair_or_resume_count"] == 1
    assert report["calibration"]["below_0.5"]["success_rate"] == 1
    assert report["calibration"]["0.5_to_0.7"]["success_rate"] == 0
    assert report["calibration"]["0.7_to_0.85"]["success_rate"] == 1
    assert report["calibration"]["0.85_and_above"]["success_rate"] == 1
    assert report["metered_spend"]["offered_codex_by_burn"]["1_to_2"] == {
        "count": 8,
        "chosen_codex": 2,
        "share": 0.25,
    }
    assert report["metered_spend"]["codex_runs"][0]["pace_allowance"] == 0.4
    rows[0]["picker_selection"]["offered"][0]["burn_multiple"] = None
    unknown_burn = outcomes.summarize({"demo": [rows[0]]}, {})
    assert (
        unknown_burn["metered_spend"]["offered_codex_by_burn"]["unknown"]["count"] == 1
    )


def test_route_mismatch_cannot_claim_the_run_outcome():
    mismatch = row("mismatch", backend="local")
    mismatch["picker_selection"]["backend"] = "codex"
    report = outcomes.summarize({"demo": [mismatch]}, {})
    assert report["mechanics"]["actions"]["route"] == 1
    assert report["routed_outcomes"] == []
    assert report["calibration"]["below_0.5"]["count"] == 0


def test_route_mode_separates_picker_from_shadow_and_legacy_matches():
    routed = row("routed")
    routed["route_mode"] = "picker"
    shadow = row("shadow")
    shadow["route_mode"] = "shadow"
    explicit = row("explicit")
    explicit["route_mode"] = "explicit"
    legacy = row("legacy")
    legacy.pop("route_mode")
    report = outcomes.summarize({"demo": [routed, shadow, explicit, legacy]}, {})
    assert [
        (group["attribution"], group["count"]) for group in report["routed_outcomes"]
    ] == [("picker", 1)]
    assert [
        (group["attribution"], group["count"])
        for group in report["approximate_outcomes"]
    ] == [("approximate", 1)]
    assert report["calibration"]["below_0.5"]["count"] == 1


def test_review_score_success_requires_eighty_without_overriding_failed_gate():
    low = row("low", gate="not-run")
    low["outcome"] = "review scored 79, 1 finding(s)"
    threshold = row("threshold", gate="not-run")
    threshold["outcome"] = "review scored 80, 1 finding(s)"
    failed_gate = row("failed", gate="failed")
    failed_gate["outcome"] = "review scored 95, 0 finding(s)"
    unknown = row("unknown", gate="not-run")
    unknown["outcome"] = "review score pending"
    report = outcomes.summarize({"demo": [low, threshold, failed_gate, unknown]}, {})
    group = report["routed_outcomes"][0]
    assert group["known_outcomes"] == 3
    assert group["success_rate"] == 1 / 3
    assert "80" in report["rules"]["success"]


def test_empty_and_malformed_rows_do_not_fabricate_success():
    empty = outcomes.summarize({"demo": []}, {})
    assert empty["mechanics"]["actions"]["route"] == 0
    assert empty["calibration"]["below_0.5"]["success_rate"] is None
    malformed = outcomes.summarize(
        {"demo": [None, {"picker_selection": {"action": "strange"}}]}, {}
    )
    assert malformed["malformed_rows"] == 2
    unknown = outcomes.summarize({"demo": [row("unknown", gate="not-run")]}, {})
    assert unknown["routed_outcomes"][0]["known_outcomes"] == 0
    assert unknown["routed_outcomes"][0]["success_rate"] is None


def test_hold_record_is_durable_and_finds_later_run(tmp_path):
    docs = tmp_path / "docs"
    hold = outcomes.record_picker_hold(
        project="demo",
        docs=docs,
        node="one",
        plan="sample",
        selection={"confidence": 0.8},
        reason="picker selected hold",
        now=datetime(2026, 10, 3, 3, tzinfo=UTC),
    )
    assert "run_id" not in hold
    saved = (docs / "state" / "demo" / "picker-holds.jsonl").read_text()
    assert json.loads(saved)["confidence"] == 0.8
    records, malformed = outcomes._read_holds(docs, "demo")
    assert malformed == 0
    report = outcomes.summarize(
        {"demo": [row("one", backend="local")]}, {"demo": records}
    )
    assert report["holds"]["count"] == 1
    assert report["holds"]["records"][0]["later_run_id"] == "one"
    assert report["holds"]["records"][0]["later_backend"] == "local"
    assert report["holds"]["records"][0]["later_success"] is True


def test_dispatch_refuses_and_records_picker_hold(tmp_path, monkeypatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    scripts = root / "skills" / "reckon-build" / "scripts"
    plans.mkdir(parents=True)
    scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / source.name).write_text(source.read_text())
    (plans / "example.html").write_text(
        '<!doctype html><html><head><meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="example"></head>'
        '<body><h2 id="dispatch">Dispatch</h2></body></html>'
    )
    (root / "seed.txt").write_text("seed\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/example.html"],
        ["commit", "-q", "-m", "chore: seed repository"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"proj": str(root / "docs")}))
    config = {
        "default_backend": "local",
        "backends": {
            "local": {
                "launch": "in-harness",
                "model": "local-model",
                "sandbox": "worktree-full",
                "time_budget": "25m",
            }
        },
        "roles": {"implement": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }
    monkeypatch.setattr(crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config)
    monkeypatch.setattr(crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None)
    selection = {
        "action": "hold",
        "backend": None,
        "confidence": 0.42,
        "probabilities": {"hold": 0.42, "local": 0.58},
        "excluded": [],
        "fallback_reason": None,
        "latency_ms": 3,
    }
    monkeypatch.setattr(
        picker, "pick", lambda *_a, **_k: SimpleNamespace(as_dict=lambda: selection)
    )
    args = [
        "crew",
        "dispatch",
        "--project",
        "proj",
        "--plan",
        "example",
        "--section",
        "dispatch",
        "--role",
        "implement",
        "--spec-level",
        "exact",
        "--node",
        "hold-node",
        "--goal",
        "record a decision",
        "--done-when",
        "pytest checks the hold",
        "--write-path",
        "result.json",
        "--session",
        "session",
        "--repo",
        str(root),
        "--no-watch",
        "--route",
        "picker",
    ]
    result = CliRunner().invoke(cli.main, args)
    assert result.exit_code == 3, result.output
    assert json.loads(result.output)["error"] == "budget-hold"
    saved, malformed = outcomes._read_holds(root / "docs", "proj")
    assert malformed == 0
    assert len(saved) == 1
    assert saved[0]["node"] == "hold-node"
    assert saved[0]["confidence"] == 0.42
    assert "run_id" not in saved[0]


def test_cli_exposes_same_report(monkeypatch):
    expected = outcomes.summarize({"demo": [row("one")]}, {})
    monkeypatch.setattr(outcomes, "read_outcomes", lambda **kwargs: expected)
    result = CliRunner().invoke(
        cli.main, ["crew", "pick", "--outcomes", "--project", "demo"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["rules"]["success"] == outcomes.SUCCESS_RULE
