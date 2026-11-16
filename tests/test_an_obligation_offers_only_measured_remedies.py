"""Settlement remedies name evidence belonging to the delivered attempt."""

from __future__ import annotations

import importlib
import json
import os
import re
import shlex
import socket
from datetime import UTC, datetime, timedelta

import click
import pytest
from click.testing import CliRunner

from reckon.cli import main
from reckon.crew import promotion, promotion_requests, runs
from reckon.crew import recovery_classification as classification
from tests.helpers.git import git

obligations = importlib.import_module("reckon.crew.obligations")
hook = importlib.import_module("reckon.hooks.coordinator_obligations")


@pytest.fixture()
def delivered(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.name", "Fixture"),
        ("config", "user.email", "fixture@example.invalid"),
    ):
        git(root, *args)
    (root / "seed.txt").write_text("base\n")
    git(root, "add", "seed.txt")
    git(root, "commit", "-qm", "test: seed fixture")
    base = git(root, "rev-parse", "HEAD")
    (root / "seed.txt").write_text("delivered\n")
    git(root, "add", "seed.txt")
    git(root, "commit", "-qm", "test: deliver fixture")
    head = git(root, "rev-parse", "HEAD")
    manifest = tmp_path / "manifest.md"
    manifest.write_text(f"status: complete\ncommits: [{head}]\n")
    pointer = {
        "run_id": "r-remedy",
        "project": "remedy-fixture",
        "session": "remedy-session",
        "phase": "complete",
        "repo": str(root),
        "worktree": str(root),
        "base_sha": base,
        "manifest_path": str(manifest),
        "process_alive": False,
        "node": {"id": "remedy", "write_paths": ["seed.txt"]},
    }
    runs._write_json(runs.pointer_path(pointer["run_id"]), pointer)
    return pointer, head, manifest


def _measure(delivered, **changes):
    _pointer, head, manifest = delivered
    arm = {
        "revision": head,
        "completed": True,
        "command": "python -m pytest tests/test_guard.py",
        "exit_status": 0,
        "log_path": str(manifest.with_suffix(".log")),
    }
    arm.update(changes)
    manifest.write_text(
        f"status: complete\ncommits: [{head}]\nafter_suite: {json.dumps(arm)}\n"
    )


def _duty(pointer, kind="promotion-failed", command=None):
    return {
        "kind": kind,
        "run_id": pointer["run_id"],
        "age_seconds": 1,
        "next_command": command
        or f"reckon crew complete --run {pointer['run_id']} --gate passed",
    }


@pytest.mark.parametrize(
    "kind",
    [
        "promotion-failed",
        "review-ready",
        "promotable-stale",
        obligations.HELD_BY_FINISHED_RUN_DUTY_KIND,
    ],
)
def test_unmeasured_run_is_never_offered_a_passing_gate(delivered, kind):
    pointer, _, _ = delivered
    rows, _ = obligations._partition_acknowledged([_duty(pointer, kind)], {})
    assert rows
    assert "--gate passed" not in rows[0]["next_command"]


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"exit_status": 1},
        {"completed": False},
        {"revision": "a" * 40},
        {"exit_status": False},
    ],
)
def test_only_a_completed_passing_current_head_is_offered(delivered, changes):
    pointer, head, _ = delivered
    _measure(delivered, **changes)
    command = classification._promotion_command(pointer["run_id"], pointer, [head])
    assert ("--gate passed" in command) is (not changes), command


def test_normalizing_a_measured_command_keeps_its_landing_options(delivered):
    pointer, _, _ = delivered
    _measure(delivered)
    command = (
        "reckon crew complete --run r-remedy --gate passed --land "
        "--no-impl-change 'records a finding'"
    )
    rows, _ = obligations._partition_acknowledged([_duty(pointer, command=command)], {})
    assert rows[0]["next_command"] == command


def test_normalizing_a_commitless_delivery_keeps_its_declaration(delivered):
    pointer, head, _ = delivered
    pointer["base_sha"] = head
    runs._write_json(runs.pointer_path(pointer["run_id"]), pointer)
    command = classification._promotion_command("r-remedy", pointer, [head])
    rows, _ = obligations._partition_acknowledged([_duty(pointer, command=command)], {})
    assert rows[0]["next_command"] == command


def test_waiting_request_keeps_its_row_when_the_remedy_target_is_live(delivered):
    pointer, _, _ = delivered
    runs._write_json(
        runs.run_dir(pointer["run_id"]) / "attempt-2-worker.json",
        {"pid": os.getpid(), "host": socket.gethostname(), "attempt": 2},
    )
    duty = _duty(pointer, kind="queued-awaiting-session")
    duty["run_id"] = "r-waiting"
    rows, _ = obligations._partition_acknowledged([duty], {})
    assert [row["run_id"] for row in rows] == ["r-waiting"]
    assert rows[0]["next_command"] == "reckon crew observe --run r-remedy"


def test_acknowledged_departed_request_keeps_its_safe_deferred_row():
    duty = _duty({"run_id": "r-departed"})
    rows, deferred = obligations._partition_acknowledged(
        [duty],
        {"r-departed": {"reason": "the refusal has been deferred", "until": "later"}},
    )
    assert rows == []
    assert [row["run_id"] for row in deferred] == ["r-departed"]
    assert deferred[0]["next_command"] == "reckon crew observe --run r-departed"


@pytest.mark.parametrize("delivered_by_attempt", [True, False])
def test_a_dated_terminal_delivery_is_distinct_from_a_live_undelivered_attempt(
    delivered, delivered_by_attempt
):
    pointer, head, manifest = delivered
    _measure(delivered)
    launch = datetime.now(UTC) - timedelta(minutes=1)
    runs._write_json(
        runs.run_dir(pointer["run_id"]) / "worker.json",
        {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "attempt": 2,
            "launched_at": launch.isoformat(),
        },
    )
    if not delivered_by_attempt:
        inherited = (launch - timedelta(minutes=1)).timestamp()
        os.utime(manifest, (inherited, inherited))
    rows, _ = obligations._partition_acknowledged([_duty(pointer)], {})
    if delivered_by_attempt:
        assert len(rows) == 1
        assert "--gate passed" in rows[0]["next_command"]
        assert head in classification._promotion_command("r-remedy", pointer, [head])
    else:
        assert rows == []


@pytest.mark.parametrize(
    "command",
    [
        "reckon crew complete --run r-remedy --gate passed",
        "reckon crew repair --run r-remedy --status complete --reason 'delivered work'",
    ],
)
def test_a_live_repair_attempt_withdraws_settlement_duties(delivered, command):
    pointer, _, _ = delivered
    _measure(delivered)
    runs._write_json(
        runs.run_dir(pointer["run_id"]) / "attempt-2-worker.json",
        {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "attempt": 2,
        },
    )
    rows, _ = obligations._partition_acknowledged([_duty(pointer, command=command)], {})
    assert rows == []
    assert "crew complete" not in classification._promotion_command(
        "r-remedy", pointer, []
    )


def test_discarded_request_has_no_promotion_duty(delivered):
    pointer, _, _ = delivered
    marker = promotion.discard_record_path(pointer["run_id"])
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}")
    path = promotion_requests._requests_dir() / "discarded.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": pointer["run_id"],
                "phase": "promotion-failed",
                "project": pointer["project"],
                "session": pointer["session"],
            }
        )
    )
    assert (
        promotion_requests.failure_obligations(pointer["project"], pointer["session"])
        == []
    )


def _parse(command):
    tokens = shlex.split(command)
    assert tokens.pop(0) == "reckon"
    group = main
    context = None
    while isinstance(group, click.Group):
        context = group.make_context(group.name, tokens, parent=context)
        name, group, tokens = group.resolve_command(
            context, context._protected_args + context.args
        )
    return group.make_context(name, tokens, parent=context)


def test_every_composed_settlement_command_parses(delivered):
    pointer, head, _ = delivered
    _measure(delivered)
    commands = [classification._promotion_command("r-remedy", pointer, [head])]
    for kind in ("promotion-failed", "review-ready", "promotable-stale"):
        rows, _ = obligations._partition_acknowledged([_duty(pointer, kind)], {})
        commands.extend(row["next_command"] for row in rows)
    pointer["base_sha"] = head
    commands.append(classification._promotion_command("r-remedy", pointer, [head]))
    for command in commands:
        parsed = _parse(command)
        if "gate" in parsed.params:
            assert parsed.params["gate"] in {"passed", "failed", "not-run"}


def test_a_node_review_disposition_uses_the_default_cli_rubric(delivered, monkeypatch):
    pointer, _, _ = delivered
    record = {
        "rubric": "node",
        "scores": {"evidence": 5},
        "review_run_id": "r-review",
        "timestamp": datetime.now(UTC).isoformat(),
    }
    monkeypatch.setattr(obligations, "_live_pointers", lambda project: [pointer])
    monkeypatch.setattr(obligations, "stored_review_for_run", lambda *a: (record, ""))
    monkeypatch.setattr(obligations, "_review_is_hot", lambda *a, **k: True)
    monkeypatch.setattr(obligations.review_module, "read_review", lambda *a: record)
    monkeypatch.setattr(
        obligations.review_records, "review_entries_signature", lambda *a: ()
    )
    rows = obligations._sub_floor_items_by_session(
        pointer["project"], {"evidence": 10}, now=datetime.now(UTC)
    )[pointer["session"]]
    assert rows
    for row in rows:
        assert "--rubric node" not in row["next_command"]
        _parse(row["next_command"])


@pytest.mark.parametrize("rubric", ["node", "figure"])
def test_blocking_finding_commands_use_cli_rubric_names(delivered, monkeypatch, rubric):
    pointer, _, _ = delivered
    monkeypatch.setattr(obligations, "_reflex_review_window_seconds", lambda: 0)
    monkeypatch.setattr(
        obligations.recovery_review_dispatch,
        "review_dispatch_refusal",
        lambda record: {
            "refused": True,
            "reason": "an open finding needs a disposition",
            "undisposed_findings": [0],
            "review_rubric": rubric,
            "review_run_id": "r-review",
        },
    )
    duty = obligations._reflex_open_refusal(pointer["project"], pointer, age=1)
    commands = re.findall(r"`(reckon[^`]+)`", duty["next_command"])
    assert len(commands) == 2
    for command in commands:
        assert "--rubric node" not in command, command
        parsed = _parse(command)
        assert parsed.params["review_rubric"] == (
            "figure" if rubric == "figure" else None
        )


def test_unknown_gate_is_refused_before_it_can_be_queued(monkeypatch):
    calls = []
    monkeypatch.setattr(
        promotion_requests,
        "request_promotion",
        lambda *a, **k: calls.append(k) or {"ok": True},
    )
    result = CliRunner().invoke(
        main, ["crew", "complete", "--run", "r-remedy", "--gate", "pass"]
    )
    assert result.exit_code == 1, result.output
    assert "unknown gate verdict" in result.output
    assert calls == []


def test_hook_prints_the_shared_unmeasured_remedy(delivered, monkeypatch):
    pointer, _, _ = delivered
    monkeypatch.setattr(hook, "pending_model_notices", list)
    rows, _ = obligations._partition_acknowledged([_duty(pointer)], {})
    text = hook.format_checklist(
        {
            "project": pointer["project"],
            "session": pointer["session"],
            "obligations": rows,
            "summary": {"count": len(rows)},
        },
        limit_resets=[],
    )
    assert "--gate passed" not in text
