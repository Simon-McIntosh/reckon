"""Delivered work leaves only decisions, with bounded completion receipts."""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, cli, ledger
from reckon.crew import runs
from tests import test_delivered_rows_say_what_is_owed as delivery
from tests import test_review_promotion_waits_for_its_record as review_delivery
from tests.helpers.git import git
from tests.test_review_promotion_waits_for_its_record import (
    repository as review_repository,
)

repository = review_repository

obligations = importlib.import_module("reckon.crew.obligations")


@pytest.fixture(autouse=True)
def project_identity(monkeypatch):
    monkeypatch.setattr(delivery, "PROJECT", "review-waits-fixture")


def _complete(*run_ids, **options):
    argv = [
        "crew",
        "complete",
        "--gate",
        "passed",
        "--wait",
        "--no-commit",
        "The reports contain no repository changes.",
        "--outcome",
        "Delivered the reports.",
    ]
    for run_id in run_ids:
        argv.extend(["--run", run_id])
    for name, value in options.items():
        argv.extend(["--" + name.replace("_", "-"), value])
    return CliRunner().invoke(cli.main, argv)


def _deliver(repository, tmp_path, name, *, implementation=False):
    pointer = delivery._seed(repository, tmp_path, name, role="investigate")
    tree = tmp_path / name
    git(repository, "worktree", "add", "--detach", "-q", str(tree), pointer["base_sha"])
    pointer["worktree"] = str(tree)
    if implementation:
        (tree / "candidate.py").write_text(f"answer = {name!r}\n")
        git(tree, "add", "candidate.py")
        git(
            tree,
            "commit",
            "-qm",
            "feat: deliver candidate",
            "-m",
            "Record an isolated delivery for the promotion fixture.",
        )
        head = git(tree, "rev-parse", "HEAD")
        pointer["role"] = pointer["node"]["role"] = "implement"
        pointer["node"]["write_paths"] = ["candidate.py"]
        manifest = Path(pointer["manifest_path"])
        lines = manifest.read_text().splitlines()
        rewritten = []
        for line in lines:
            replacement = line
            if line.startswith("commits:"):
                replacement = "commits: " + json.dumps([head])
            elif line.startswith("changed_paths:"):
                replacement = 'changed_paths: ["candidate.py"]'
            elif line.startswith("after_suite:"):
                arm = json.loads(line.split(":", 1)[1])
                arm["revision"] = head
                replacement = "after_suite: " + json.dumps(arm)
            rewritten.append(replacement)
        manifest.write_text("\n".join(rewritten) + "\n")
        delivery._review(name, pointer["base_sha"], head)
    runs._write_json(runs.pointer_path(name), pointer)
    return pointer


def test_twelve_deliveries_leave_three_bounded_decisions(repository, tmp_path):
    pointers = [
        _deliver(repository, tmp_path, f"r-delivery-{index}", implementation=True)
        for index in range(12)
    ]
    for pointer in pointers[-3:]:
        pointer["admission_landing"] = {
            "status": "refused",
            "head": git(Path(pointer["worktree"]), "rev-parse", "HEAD"),
            "guard": "the merged-head gate adds tests/test_candidate.py::test_guard",
        }
        runs._write_json(runs.pointer_path(pointer["run_id"]), pointer)
    view = obligations.obligations(delivery.PROJECT, delivery.SESSION)
    rows = view["obligations"]
    print("delivered=12 automatic=9 decision_rows=", len(rows))
    assert {row["run_id"] for row in rows} == {
        pointer["run_id"] for pointer in pointers[-3:]
    }, rows
    for row in rows:
        assert len(json.dumps(row)) <= 400, row
        assert "gate" in row["reason"]
        assert row["record_action"] == "observe"
        assert "crew complete" in row["next_command"]
        assert (
            runs.read_pointer(row["run_id"])["admission_landing"]["status"] == "refused"
        )


def test_one_confirmation_promotes_both_runs_and_links_the_full_receipts(
    repository, tmp_path
):
    names = ("r-first-delivery", "r-second-delivery")
    for name in names:
        _deliver(repository, tmp_path, name)
    result = _complete(*names)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    print("completion_reply_bytes=", len(result.output.encode()))
    assert len(result.output.encode()) < 2000
    landed = {row["run_id"] for row in ledger.runs(delivery.PROJECT, root=repository)}
    print("completed_run_ids=", sorted(landed))
    assert landed == set(names)
    assert {row["run_id"] for row in payload["runs"]} == set(names)
    for row in payload["runs"]:
        record = json.loads(Path(row["record_path"]).read_text())
        assert record["pointer_removed"] is True
        assert record["record"]["gate"] == "passed"
        assert not runs.pointer_path(row["run_id"]).exists()
    assert {
        row["run_id"] for row in ledger.runs(delivery.PROJECT, root=repository)
    } == set(names)


def test_two_of_three_manual_decisions_land_from_the_twelve_run_list(
    repository, tmp_path
):
    from reckon.crew import recovery_review_acceptance as acceptance

    automatic = [
        _deliver(repository, tmp_path, f"r-automatic-{index}", implementation=True)
        for index in range(9)
    ]
    source = repository / "docs" / "plans" / f"{delivery.PLAN}.html"
    state = _plan_html.read_state(source.read_text())
    state.update(
        slug="confirm-reports",
        decisions={
            "landing": {
                "choice": "manual",
                "rationale": "Confirm the delivered reports.",
            },
        },
    )
    reserved = source.with_name("confirm-reports.html")
    reserved.write_text(_plan_html.write_state(source.read_text(), state))
    git(repository, "add", str(reserved.relative_to(repository)))
    git(
        repository,
        "commit",
        "-qm",
        "test: reserve report publication",
        "-m",
        "Leave three reports for explicit confirmation.",
    )
    names = [f"r-confirm-report-{index}" for index in range(3)]
    for name in names:
        subject = name + "-subject"
        review_delivery._write_pointer(repository, tmp_path, name, subject, pid=None)
        review_delivery._store_record(name, subject)
        review_delivery._write_exit_record(name)
        pointer = runs.read_pointer(name)
        pointer.update(session=delivery.SESSION, phase="blocked")
        pointer["node"]["plan"] = "confirm-reports"
        runs._write_json(runs.pointer_path(name), pointer)
        assert (
            acceptance.review_self_state(pointer)[0] == acceptance.REVIEW_SELF_REFUSED
        )
    rows = obligations.obligations(delivery.PROJECT, delivery.SESSION)["obligations"]
    assert {row["run_id"] for row in rows} == set(names)
    assert all(len(json.dumps(row)) <= 400 for row in rows)
    response = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "complete",
            "--gate",
            "not-run",
            "--wait",
            "--run",
            names[0],
            "--run",
            names[1],
        ],
    )
    assert response.exit_code == 0, response.output
    assert len(response.output.encode()) < 2000
    receipts = json.loads(response.output)["runs"]
    assert {row["run_id"] for row in receipts} == set(names[:2])
    for receipt in receipts:
        assert (
            json.loads(Path(receipt["record_path"]).read_text())["pointer_removed"]
            is True
        )
    assert all(runs.pointer_path(pointer["run_id"]).exists() for pointer in automatic)
    remaining = obligations.obligations(delivery.PROJECT, delivery.SESSION)[
        "obligations"
    ]
    assert [row["run_id"] for row in remaining] == names[2:]


def test_a_refused_member_does_not_skip_the_other_confirmation(repository, tmp_path):
    names = ("r-refused-delivery", "r-valid-delivery")
    for name in names:
        _deliver(repository, tmp_path, name)
    pointer = runs.read_pointer(names[0])
    manifest = Path(pointer["manifest_path"])
    log = tmp_path / "red-gate.log"
    log.write_text("FAILED tests/test_candidate.py::test_guard\n1 failed\nEXIT=1\n")
    lines = manifest.read_text().splitlines()
    for index, line in enumerate(lines):
        if line.startswith("after_suite:"):
            arm = json.loads(line.split(":", 1)[1])
            arm.update(
                exit_status=1,
                log_path=str(log),
                failure_count=1,
                failure_ids=["tests/test_candidate.py::test_guard"],
            )
            lines[index] = "after_suite: " + json.dumps(arm)
    manifest.write_text("\n".join(lines) + f"\ntest_logs: {json.dumps([str(log)])}\n")
    result = _complete(*names)
    assert result.exit_code != 0, result.output
    payload = json.loads(result.output)
    assert not payload["ok"]
    assert [row["ok"] for row in payload["runs"]] == [False, True]
    assert runs.pointer_path(names[0]).exists()
    assert not runs.pointer_path(names[1]).exists()
    for row in payload["runs"]:
        assert Path(row["record_path"]).is_file()


def test_several_runs_refuse_evidence_that_cannot_be_assigned(repository, tmp_path):
    for name in ("r-one", "r-two"):
        delivery._seed(repository, tmp_path, name, role="investigate")
    result = _complete("r-one", "r-two", commit="a" * 40)
    assert result.exit_code != 0
    assert "require one --run" in result.output
    assert all(runs.pointer_path(name).exists() for name in ("r-one", "r-two"))


def test_a_single_reply_keeps_large_evidence_in_its_readable_record(
    repository, tmp_path, monkeypatch
):
    from reckon import crew

    delivery._seed(repository, tmp_path, "r-large", role="investigate")
    monkeypatch.setattr(
        crew,
        "complete",
        lambda *a, **k: {
            "ok": True,
            "record": {"evidence": "x" * 60000},
        },
    )
    result = _complete("r-large")
    assert result.exit_code == 0, result.output
    assert len(result.output.encode()) < 2000
    receipt = json.loads(result.output)
    assert (
        json.loads(Path(receipt["record_path"]).read_text())["record"]["evidence"]
        == "x" * 60000
    )


def test_a_nested_landing_refusal_keeps_its_condition_in_the_compact_reply(
    repository, tmp_path, monkeypatch
):
    from reckon import crew

    _deliver(repository, tmp_path, "r-refused-land")
    monkeypatch.setattr(
        crew,
        "complete",
        lambda *a, **k: {
            "ok": False,
            "steps": {"land": {"ok": False, "error": "merged gate added test_guard"}},
        },
    )
    response = _complete("r-refused-land")
    assert response.exit_code != 0
    receipt = json.loads(response.output)
    assert "test_guard" in receipt["error"]
    assert Path(receipt["record_path"]).is_file()


def test_the_tool_callback_returns_the_same_bounded_record_link(
    repository, tmp_path, monkeypatch
):
    from reckon import crew
    from reckon.mcp_crew_actions import crew_action

    _deliver(repository, tmp_path, "r-tool-delivery")
    monkeypatch.setattr(
        crew,
        "complete",
        lambda *a, **k: {
            "ok": True,
            "record": {"evidence": "x" * 60000},
        },
    )
    receipt = crew_action(
        "complete",
        project=delivery.PROJECT,
        run_id="r-tool-delivery",
        gate="passed",
        no_commit="The report changes no repository files.",
        outcome="Delivered the report.",
    )
    assert receipt["ok"], receipt
    assert len(json.dumps(receipt).encode()) < 2000
    record = json.loads(Path(receipt["record_path"]).read_text())
    assert record["record"]["evidence"] == "x" * 60000


def test_a_missing_runs_receipt_does_not_create_a_run(repository):
    from reckon import mcp

    run_id = "run-missing"
    directory = runs.run_dir(run_id)
    assert not directory.exists()
    receipt = mcp._crew(action="complete", run_id=run_id, gate="passed")
    assert receipt["ok"] is False
    assert any(run_id in str(guard) for guard in receipt["guards"]), receipt
    assert not directory.exists()
    assert json.loads(Path(receipt["record_path"]).read_text())["ok"] is False


def test_a_ready_report_is_left_to_its_existing_automatic_promoter(
    repository, tmp_path
):
    from reckon.crew import recovery_review_acceptance as acceptance

    pointer = delivery._review_run(repository, tmp_path)
    assert acceptance.review_self_state(pointer)[0] == acceptance.REVIEW_SELF_READY
    view = obligations.obligations(delivery.PROJECT, delivery.SESSION)
    assert view["obligations"] == []
    assert runs.pointer_path(pointer["run_id"]).exists()


def test_automatic_delivery_reuses_the_head_the_duty_already_read(
    repository, tmp_path, monkeypatch
):
    from reckon.crew import recovery

    pointer = _deliver(repository, tmp_path, "r-head-read", implementation=True)
    row = recovery.classify_pointer(pointer)
    actual = recovery._review_head_and_tree
    reads = []

    def resolve(record):
        reads.append(record["run_id"])
        return actual(record)

    monkeypatch.setattr(recovery, "_review_head_and_tree", resolve)
    assert (
        obligations._review_duty_item(
            delivery.PROJECT,
            row,
            pointer,
            now=datetime.now(UTC),
            grace=900,
        )
        is None
    )
    assert reads == [pointer["run_id"]]


def test_a_held_report_still_names_the_coordinators_decision(repository, tmp_path):
    from reckon.crew.resume import hold_delivery

    pointer = delivery._review_run(repository, tmp_path)
    hold_delivery(pointer["run_id"], "Awaiting the paired report.")
    view = obligations.obligations(delivery.PROJECT, delivery.SESSION)
    [row] = view["obligations"]
    assert "held" in row["reason"]
    assert "crew release" in row["next_command"]


def test_a_long_run_name_keeps_its_condition_and_a_working_evidence_read(
    repository, tmp_path
):
    name = "r-" + "delivered-candidate-" * 4
    pointer = _deliver(repository, tmp_path, name, implementation=True)
    pointer["admission_landing"] = {
        "status": "refused",
        "head": git(Path(pointer["worktree"]), "rev-parse", "HEAD"),
        "guard": "the merged-head gate adds tests/test_candidate.py::test_guard",
    }
    runs._write_json(runs.pointer_path(name), pointer)
    [row] = obligations.obligations(delivery.PROJECT, delivery.SESSION)["obligations"]
    assert len(json.dumps(row)) <= 400, row
    assert "gate" in row["reason"]
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            row["record_action"],
            "--run",
            row["run_id"],
        ],
    )
    assert result.exit_code == 0, result.output
    assert "test_guard" in result.output


def test_escaped_condition_text_stays_inside_the_serialized_row_bound():
    row = obligations._with_action_form(
        {
            "kind": "landing-refused",
            "run_id": "r-escaped-condition",
            "age_seconds": 1,
            "reason": "convergence failed: " + 'μ"\\' * 100,
            "next_command": "reckon crew complete --run r-escaped-condition --gate passed --land",
            "_delivery_decision": "delivered",
        }
    )
    assert len(json.dumps(row)) <= 400, row
    assert "convergence" in row["reason"]


def test_the_default_batch_requests_both_promotions_without_running_them(
    repository, tmp_path, monkeypatch
):
    from reckon import crew
    from reckon.crew import promotion_requests

    names = ("r-queued-first", "r-queued-second")
    for name in names:
        _deliver(repository, tmp_path, name)
    calls = []
    monkeypatch.setattr(crew, "complete", lambda *a, **k: calls.append(a))
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "complete",
            "--gate",
            "not-run",
            "--run",
            names[0],
            "--run",
            names[1],
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert not calls
    assert {row["run_id"] for row in payload["runs"]} == set(names)
    assert all(row["phase"] == promotion_requests.REQUESTED for row in payload["runs"])
    assert all(promotion_requests.read_promotion(name) for name in names)


def test_the_drain_keeps_the_full_receipt_for_stored_click_options(
    repository, tmp_path
):
    from reckon.crew.promotion_requests import _execute_command

    pointer = _deliver(repository, tmp_path, "r-drain-delivery")
    receipt = _execute_command(
        {
            "run_id": (pointer["run_id"],),
            "gate": "passed",
            "wait": True,
            "no_commit": "The report changes no repository files.",
            "outcome": "Delivered the report.",
        }
    )
    assert receipt["record"]["gate"] == "passed"
    assert receipt["pointer_removed"] is True


def test_a_large_pretty_batch_keeps_its_reply_bounded(
    repository, tmp_path, monkeypatch
):
    from reckon import crew

    names = [f"r-batch-{index}" for index in range(20)]
    template = _deliver(repository, tmp_path, names[0])
    for name in names[1:]:
        runs._write_json(runs.pointer_path(name), {**template, "run_id": name})
    monkeypatch.setattr(
        crew, "complete", lambda *a, **k: {"ok": True, "record": {"gate": "passed"}}
    )
    argv = ["crew", "complete", "--gate", "passed", "--wait", "--pretty"]
    for name in names:
        argv.extend(["--run", name])
    result = CliRunner().invoke(cli.main, argv)
    assert result.exit_code == 0, result.output
    assert len(result.output.encode()) < 2000
    receipt = json.loads(result.output)
    assert receipt["count"] == len(names)
    record = json.loads(Path(receipt["record_path"]).read_text())
    assert {row["run_id"] for row in record["runs"]} == set(names)
    assert all(Path(row["record_path"]).is_file() for row in record["runs"])


def test_a_dry_run_returns_guard_evidence_without_creating_completion_receipts(
    repository, tmp_path
):
    pointer = _deliver(repository, tmp_path, "r-preview")
    directory = runs.run_dir(pointer["run_id"])
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "complete",
            "--run",
            pointer["run_id"],
            "--gate",
            "not-run",
            "--dry-run",
            "--no-commit",
            "The report changes no repository file.",
            "--outcome",
            "Delivered the report.",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["dry_run"] is True
    assert not list(directory.glob("completion-*.json"))
    assert runs.pointer_path(pointer["run_id"]).exists()
