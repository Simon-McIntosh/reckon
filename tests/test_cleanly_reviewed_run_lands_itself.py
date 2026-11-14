"""A stored review releases a delivery through a measured merge onto primary."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon import _plan_html, ledger
from reckon.crew import landing_measure, pending_landing, recovery, runs
from reckon.crew import review as review_module
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import PLAN, PROJECT, _write_origin_and_checkout

RUN = "r-clean-delivery"


@pytest.fixture()
def delivery(tmp_path, isolated_reckon_home, monkeypatch):
    origin, repo = _write_origin_and_checkout(tmp_path)
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")})
    )
    state_home = isolated_reckon_home / "state"
    state_home.mkdir()
    (state_home / PROJECT).symlink_to(repo / "docs" / "state" / PROJECT)
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    (repo / ".venv").symlink_to(Path(sys.executable).parent.parent)
    plan = repo / "docs" / "plans" / f"{PLAN}.html"
    state = _plan_html.read_state_file(plan)
    state["section_declarations"] = {"s2": "implementable"}
    plan.write_text(
        _plan_html.write_state(
            plan.read_text().replace(
                "</main>",
                '<h2 id="s2">Delivered behavior</h2><p>Land the delivery.</p></main>',
            ),
            state,
        )
    )
    (repo / "left.txt").write_text("base\n")
    (repo / "right.txt").write_text("base\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_contract.py").write_text(
        "from pathlib import Path\n"
        "def test_merge_contract():\n"
        '    left = Path("left.txt").read_text().strip()\n'
        '    right = Path("right.txt").read_text().strip()\n'
        '    assert (left, right) != ("feature", "incompatible")\n'
    )
    _git(repo, "add", "docs", "left.txt", "right.txt", "tests/test_contract.py")
    _git(repo, "commit", "-q", "-m", "test: seed the delivery contract")
    _git(repo, "push", "-q", "origin", "main")
    base = _git(repo, "rev-parse", "HEAD")
    worker = tmp_path / "worker"
    _git(repo, "worktree", "add", "-q", "--detach", str(worker), base)
    (worker / "left.txt").write_text("feature\n")
    _git(worker, "add", "left.txt")
    _git(worker, "commit", "-q", "-m", "feat: deliver the behavior")
    head = _git(worker, "rev-parse", "HEAD")
    gate = f"PYTHONPATH=. {shlex.quote(sys.executable)} -m pytest -p no:cacheprovider -q tests/test_contract.py"
    log = tmp_path / "delivered-gate.log"
    log.write_text("1 passed\nEXIT=0\n")
    manifest = runs.run_dir(RUN) / "manifest.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        f"node: delivery\nstatus: complete\ncommits: [{head}]\n"
        f"changed_paths: [left.txt]\ntests: {gate}\ntest_logs: [{log}]\n"
    )
    runs._write_json(
        runs.pointer_path(RUN),
        {
            "run_id": RUN,
            "project": PROJECT,
            "repo": str(repo),
            "worktree": str(worker),
            "base_sha": base,
            "launch": "in-harness",
            "role": "implement",
            "session": "owner",
            "process_alive": False,
            "manifest_path": str(manifest),
            "created_at": "2026-01-01T00:00:00Z",
            "node": {
                "id": "delivery",
                "plan": PLAN,
                "section": "s2",
                "role": "implement",
                "time_budget": "25m",
                "write_paths": ["left.txt"],
            },
        },
    )
    record = review_module.parse_review(
        "\n".join(
            f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
        )
    )
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN,
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
            "review_run_id": "r-review-delivery",
            "added_failure_count": 0,
        }
    )
    review_module.store_review(record)
    submissions = []

    def scheduler(argv, **options):
        """Execute the partition's exact archive script in this test's allocation."""
        options.pop("check", None)
        if argv[0] != "sbatch":
            return subprocess.run(argv, check=False, **options)
        submissions.append(argv)
        assert "--partition=all_debug" in argv
        step = shlex.split(argv[argv.index("--wrap") + 1])
        script = step[step.index("-c") + 1]
        destination = Path(step[step.index(">") + 1])
        completed = subprocess.run(["bash", "-c", script], check=False, **options)
        destination.write_text((completed.stdout or "") + (completed.stderr or ""))
        return subprocess.CompletedProcess(argv, completed.returncode, "12345\n", "")

    # The test gate itself is allocated on all_debug. Only scheduler transport
    # is replaced: checkout construction, the command and receipt reading run.
    monkeypatch.setattr(landing_measure, "_RUN", scheduler)
    return {
        "repo": repo,
        "origin": origin,
        "head": head,
        "base": base,
        "worker": worker,
        "plan": plan,
        "submissions": submissions,
    }


def _on_primary(delivery):
    result = subprocess.run(
        [
            "git",
            "-C",
            str(delivery["repo"]),
            "merge-base",
            "--is-ancestor",
            delivery["head"],
            "main",
        ],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _pass():
    return recovery.dispatch_awaiting_reviews(
        project=PROJECT, launcher=lambda *args, **kwargs: 0
    )


def _outcome(report):
    return next(
        row
        for row in report["reports"]
        if row.get("run_id") == RUN and "accepted" in row
    )


def test_a_clean_review_lands_and_records_the_measured_head(delivery):
    assert not _on_primary(delivery)
    report = _pass()
    assert _on_primary(delivery), report
    assert (delivery["repo"] / "left.txt").read_text() == "feature\n"
    assert report["accepted"] == [RUN], report
    outcome = _outcome(report)
    assert outcome["merged"] is True
    assert outcome["gate_report"]["integrated_verdict"] == "passed"
    assert Path(outcome["gate_report"]["log_path"]).read_text().endswith("EXIT=0\n")
    assert delivery["submissions"]
    recorded = ledger.runs(PROJECT, root=delivery["repo"])[0]
    assert ledger.runs(PROJECT)[0]["run_id"] == RUN
    assert recorded["integrated_gate_check"]["integrated_verdict"] == "passed"
    assert (
        _plan_html.read_state_file(delivery["plan"])["section_declarations"]["s2"]
        == "done"
    )

    committed = _git(delivery["repo"], "show", f"HEAD:docs/plans/{PLAN}.html")
    assert _plan_html.read_state(committed)["section_declarations"]["s2"] == "done"


def test_a_conflicting_head_stays_delivered_and_names_the_conflict(delivery):
    repo = delivery["repo"]
    (repo / "left.txt").write_text("primary\n")
    _git(repo, "add", "left.txt")
    _git(repo, "commit", "-q", "-m", "feat: change the same behavior")
    _git(repo, "push", "-q", "origin", "main")
    before = _git(repo, "rev-parse", "HEAD")
    report = _pass()
    assert not _on_primary(delivery)
    assert _git(repo, "rev-parse", "HEAD") == before
    assert runs.pointer_path(RUN).is_file()
    assert ledger.runs(PROJECT, root=repo) == []
    assert "left.txt" in _outcome(report)["reason"]
    obligation = runs.read_pointer(RUN)["admission_landing"]
    assert "conflict" in obligation["guard"]
    assert "reckon crew" in obligation["remedy"]
    assert not delivery["submissions"]
    _pass()
    assert runs.read_pointer(RUN)["admission_landing"] == obligation


def test_a_merged_gate_failure_stays_delivered_with_its_test_ids(delivery):
    repo = delivery["repo"]
    (repo / "right.txt").write_text("incompatible\n")
    _git(repo, "add", "right.txt")
    _git(repo, "commit", "-q", "-m", "feat: change the companion behavior")
    _git(repo, "push", "-q", "origin", "main")
    before = _git(repo, "rev-parse", "HEAD")
    report = _pass()
    assert not _on_primary(delivery)
    assert _git(repo, "rev-parse", "HEAD") == before
    assert runs.pointer_path(RUN).is_file()
    assert ledger.runs(PROJECT, root=repo) == []
    outcome = _outcome(report)
    assert outcome["gate_report"]["integrated_verdict"] == "failed"
    assert "tests/test_contract.py::test_merge_contract" in outcome["reason"]
    assert "test_merge_contract" in runs.read_pointer(RUN)["admission_landing"]["guard"]


def test_a_plan_reserving_landing_for_its_coordinator_is_untouched(delivery):
    plan = delivery["plan"]
    state = _plan_html.read_state_file(plan)
    state["decisions"] = {
        "landing": {
            "choice": "manual",
            "rationale": "The coordinator lands it by hand.",
            "when": "2026-01-01",
            "by": "owner",
        }
    }
    plan.write_text(_plan_html.write_state(plan.read_text(), state))
    _git(delivery["repo"], "add", str(plan))
    _git(delivery["repo"], "commit", "-q", "-m", "docs: reserve the landing decision")
    before = _git(delivery["repo"], "rev-parse", "HEAD")
    report = _pass()
    assert not _on_primary(delivery)
    assert _git(delivery["repo"], "rev-parse", "HEAD") == before
    assert runs.pointer_path(RUN).is_file()
    assert "coordinator" in _outcome(report)["reason"]
    assert not delivery["submissions"]
    assert not pending_landing._record_path(RUN).exists()


def test_a_promoted_conflict_reports_pending_and_never_landed(delivery, monkeypatch):
    from reckon.crew import merge
    from reckon.crew_run_commands import _land_a_promoted_run

    pointer = runs.read_pointer(RUN)
    pending_landing.write_pending(RUN, PROJECT, pointer=pointer, head=delivery["head"])
    # Promote for real, then introduce the conflicting primary change before
    # the merge, as another landing can do in this interval.
    from reckon.crew import promotion

    promoted = promotion.complete(
        RUN,
        gate="passed",
        root=delivery["repo"],
        no_impl_change="The section closes after the merge.",
    )
    repo = delivery["repo"]
    (repo / "left.txt").write_text("primary\n")
    _git(repo, "add", "left.txt")
    _git(repo, "commit", "-q", "-m", "feat: move primary before the merge")
    monkeypatch.setattr(merge, "project_repository", lambda project: repo)
    from reckon.crew import recovery_repair_dispatch

    monkeypatch.setattr(
        recovery_repair_dispatch,
        "dispatch_merge_repair",
        lambda *args, **kwargs: {"status": "advice-hold", "reason": "Resolve left.txt"},
    )
    outcome, code = _land_a_promoted_run(
        promoted, run_id=RUN, gate="passed", checkout_path=repo, pointer=pointer
    )
    assert code != 0
    assert outcome["merged"] is False
    assert outcome["pending_landing"] == str(pending_landing._record_path(RUN))
    assert not _on_primary(delivery)
    assert "awaits its merge" in recovery.classify_pointer(pointer)["detail"]
    snapshot = recovery._watch_snapshot(pointer, moment=time.time(), stall_seconds=3600)
    assert snapshot["state"] != "promoted"
    week = runs._drain_week_view(PROJECT, [pointer])
    assert week["project"]["phases"]["landed"] == 0
    assert week["plans"][PLAN]["phases"]["landed"] == 0
    from reckon.crew.promotion_requests import _settled_state
    from reckon.crew.recovery_watch import _ledger_run_id_reader

    assert _ledger_run_id_reader(PROJECT).for_runs([RUN])[RUN] == "complete"
    assert _settled_state({"run_id": RUN, "project": PROJECT}) != "landed"


def test_the_admission_lander_also_respects_the_manual_plan_decision(delivery):
    from reckon.crew import queue_landing

    plan = delivery["plan"]
    state = _plan_html.read_state_file(plan)
    state["decisions"] = {
        "landing": {"choice": "manual", "rationale": "Coordinator integration."}
    }
    plan.write_text(_plan_html.write_state(plan.read_text(), state))
    report = queue_landing.land_once()
    assert not _on_primary(delivery)
    assert not report["landed"]
    assert "coordinator" in report["obligations"][0]["guard"]
    assert not delivery["submissions"]


def test_a_gate_failure_after_promotion_is_not_bypassed_by_recovery(
    delivery, monkeypatch
):
    from reckon.crew import promotion

    original = promotion.complete

    def promote_then_move(*args, **kwargs):
        promoted = original(*args, **kwargs)
        repo = delivery["repo"]
        (repo / "right.txt").write_text("incompatible\n")
        _git(repo, "add", "right.txt")
        _git(repo, "commit", "-q", "-m", "feat: move the companion behavior")
        _git(repo, "push", "-q", "origin", "main")
        return promoted

    monkeypatch.setattr(promotion, "complete", promote_then_move)
    report = _pass()
    outcome = _outcome(report)
    assert outcome["promoted"] is True
    assert outcome["merged"] is False
    assert outcome["accepted"] is False
    assert not _on_primary(delivery)
    assert pending_landing._record_path(RUN).is_file()
    retried = pending_landing.finish_pending_landings(only_run=RUN)
    assert retried[0]["status"] == "failed", retried
    assert "test_merge_contract" in retried[0]["error"]
    assert not _on_primary(delivery)
