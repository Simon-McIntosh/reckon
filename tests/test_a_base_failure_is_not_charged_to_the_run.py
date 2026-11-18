"""Promotion attributes failures to the primary revision a run merged."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import promotion_evidence, promotion_records
from reckon.crew.node import CrewError
from reckon.crew.review import gate_log_failure_ids

TEST_FILE = "tests/test_contract.py"
INHERITED = f"{TEST_FILE}::test_imported"
INTRODUCED = f"{TEST_FILE}::test_owned"
COMMAND = f"PYTHONPATH=. {sys.executable} -m pytest -p no:cacheprovider -q {TEST_FILE}"


def git(tree, *args):
    return subprocess.run(
        ["git", "-C", str(tree), *args], check=True, text=True, capture_output=True
    ).stdout.strip()


def commit(tree, path, text):
    (tree / path).write_text(text)
    git(tree, "add", path)
    git(
        tree,
        "commit",
        "-qm",
        "fix: change fixture state",
        "-m",
        "Exercise failure attribution.",
    )
    return git(tree, "rev-parse", "HEAD")


def observe(tree, log):
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", TEST_FILE],
        cwd=tree,
        env={**os.environ, "PYTHONPATH": "."},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    revision = git(tree, "rev-parse", "HEAD")
    log.write_text(
        f"# revision: {revision}\n# command: {COMMAND}\n"
        + run.stdout
        + run.stderr
        + f"EXIT={run.returncode}\n"
    )
    ids = sorted(gate_log_failure_ids(run.stdout) or [])
    return {
        "revision": revision,
        "command": COMMAND,
        "exit_status": run.returncode,
        "completed": run.returncode in (0, 1),
        "failure_count": len(ids),
        "failure_ids": ids,
        "log_path": str(log),
    }


@pytest.fixture()
def scenario(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew"))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    root = tmp_path / "primary"
    (root / "tests").mkdir(parents=True)
    (root / "imported.py").write_text("BAD = False\n")
    (root / "owned.py").write_text("BAD = False\n")
    (root / TEST_FILE).write_text(
        "import imported\nimport owned\n"
        "def test_imported():\n    assert not imported.BAD\n"
        "def test_owned():\n    assert not owned.BAD\n"
    )
    git(root, "init", "-qb", "main")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "add", "imported.py", "owned.py", TEST_FILE)
    git(
        root,
        "commit",
        "-qm",
        "test: seed fixture",
        "-m",
        "Exercise independent branches.",
    )
    baseline = observe(root, tmp_path / "baseline.log")
    primary = commit(root, "imported.py", "BAD = True\n")
    primary_arm = observe(root, tmp_path / "primary.log")
    worker = tmp_path / "worker"
    git(root, "worktree", "add", "--detach", str(worker), baseline["revision"])
    owned_head = commit(worker, "owned.py", "BAD = True\n")
    git(worker, "merge", "--no-ff", primary, "-m", "Merge fixture primary")
    after = observe(worker, tmp_path / "after.log")
    manifest = tmp_path / "manifest.md"
    record = {
        "run_id": "r-attribution",
        "base_sha": baseline["revision"],
        "repo": str(root),
        "worktree": str(worker),
        "role": "implement",
        "manifest_path": str(manifest),
        "suite_command": COMMAND,
        "node": {"write_paths": [TEST_FILE]},
    }
    state = {
        "baseline": baseline,
        "after": after,
        "primary_arm": primary_arm,
        "primary": primary,
        "worker": worker,
        "root": root,
        "owned_head": owned_head,
        "record": record,
        "measures": [],
        "pointer": {},
        "directory": tmp_path / "run",
    }

    def write():
        manifest.write_text(
            "status: complete\n"
            + "\n".join(
                f"{name}: {json.dumps(state[key])}"
                for name, key in (
                    ("baseline_suite", "baseline"),
                    ("after_suite", "after"),
                )
            )
            + "\n"
        )

    def measure(run_id, measured_record, head, command, **options):
        assert measured_record["base_sha"] == primary
        assert head == state["after"]["revision"]
        assert command == COMMAND
        assert options["partition"] == "all_debug"
        state["measures"].append(primary)
        return {"baseline_suite": state["primary_arm"], "after_suite": state["after"]}

    def mutate(run_id, change):
        state["pointer"] = change(dict(state["pointer"]))

    state["write"] = write
    monkeypatch.setattr(promotion_records, "_measure_missing_arms", measure)
    monkeypatch.setattr(promotion_records, "run_dir", lambda run_id: state["directory"])
    monkeypatch.setattr(promotion_records, "_mutate_pointer", mutate)
    write()
    yield state
    git(root, "worktree", "remove", str(worker))


def evaluate(state, **options):
    return promotion_records._evaluate_suite_delta(
        "r-attribution", state["record"], waiver_reason="", **options
    )


def remove_owned_failure(state):
    commit(state["worker"], "owned.py", "BAD = False\n")
    state["after"] = observe(
        state["worker"], state["worker"].parent / "clean-owned.log"
    )
    state["write"]()


def gate(state):
    return {
        "command": COMMAND,
        "exit_status": state["after"]["exit_status"],
        "log_path": state["after"]["log_path"],
    }


def test_only_the_runs_own_failure_is_added_and_the_primary_id_is_recorded(scenario):
    assert scenario["baseline"]["failure_ids"] == []
    assert scenario["primary_arm"]["failure_ids"] == [INHERITED]
    assert scenario["after"]["failure_ids"] == [INHERITED, INTRODUCED]
    with pytest.raises(ledger.SuiteDeltaError) as refusal:
        evaluate(scenario)
    assert refusal.value.added_failure_ids == [INTRODUCED]
    row = scenario["pointer"]["suite_delta_refusal"]
    assert row["inherited_failure_ids"] == {INHERITED: scenario["primary"]}
    assert row["primary_suite"]["revision"] == scenario["primary"]
    assert row["baseline_suite"]["revision"] == scenario["baseline"]["revision"]


def test_removing_the_runs_failure_admits_promotion_with_its_primary_receipt(scenario):
    remove_owned_failure(scenario)
    promotion_evidence._require_gate_log_agrees(
        "r-attribution", gate(scenario), scenario["record"], verdict="passed"
    )
    promotion_evidence._require_verdict_matches_exit_status(
        "r-attribution", scenario["record"], gate(scenario), verdict="passed"
    )
    row = evaluate(scenario)
    assert row["status"] == "clean"
    assert row["added_failure_ids"] == []
    assert row["inherited_failure_ids"] == {INHERITED: scenario["primary"]}
    assert row["baseline_suite"] == ledger.normalized_suite_observation(
        scenario["baseline"]
    )
    assert row["primary_suite"]["failure_ids"] == [INHERITED]


def test_a_new_failure_on_primary_after_the_merge_is_still_charged_to_the_run(scenario):
    commit(scenario["root"], "owned.py", "BAD = True\n")
    with pytest.raises(ledger.SuiteDeltaError) as refusal:
        evaluate(scenario)
    assert refusal.value.added_failure_ids == [INTRODUCED]
    assert scenario["pointer"]["suite_delta_refusal"]["inherited_failure_ids"] == {
        INHERITED: scenario["primary"]
    }


@pytest.mark.parametrize(
    "defect",
    ["incomplete", "wrong_revision", "wrong_command", "log_disagrees", "no_summary"],
)
def test_an_unreadable_primary_receipt_cannot_excuse_a_failure(scenario, defect):
    arm = dict(scenario["primary_arm"])
    if defect == "incomplete":
        arm["completed"] = False
    elif defect == "wrong_revision":
        arm["revision"] = scenario["baseline"]["revision"]
    elif defect == "wrong_command":
        arm["command"] = "false"
    elif defect == "no_summary":
        Path(arm["log_path"]).write_text(f"FAILED {INHERITED} - assertion\nEXIT=1\n")
    else:
        arm["failure_ids"] = [INHERITED, INTRODUCED]
        arm["failure_count"] = 2
    scenario["primary_arm"] = arm
    with pytest.raises(ledger.SuiteDeltaError, match="primary suite evidence"):
        evaluate(scenario)


def test_a_dry_run_names_the_primary_measurement_and_writes_nothing(scenario):
    row = evaluate(scenario, dry_run=True)
    assert row["status"] == "would-measure"
    assert row["primary_suite"]["revision"] == scenario["primary"]
    assert row["primary_suite"]["command"] == COMMAND
    assert scenario["measures"] == []
    assert scenario["pointer"] == {}
    assert not scenario["directory"].exists()


def test_a_primary_receipt_is_reused_only_while_its_log_agrees(scenario):
    remove_owned_failure(scenario)
    assert evaluate(scenario)["status"] == "clean"
    assert evaluate(scenario)["status"] == "clean"
    assert scenario["measures"] == [scenario["primary"]]
    Path(scenario["primary_arm"]["log_path"]).write_text("2 passed in 0.1s\nEXIT=0\n")
    with pytest.raises(ledger.SuiteDeltaError, match="primary suite evidence"):
        evaluate(scenario)


def test_unmerged_primary_failures_and_unrelated_gates_are_not_excused(scenario):
    unrelated = {**gate(scenario), "command": "false"}
    with pytest.raises(CrewError):
        promotion_evidence._require_verdict_matches_exit_status(
            "r-attribution", scenario["record"], unrelated, verdict="passed"
        )
    unmerged = scenario["worker"].parent / "unmerged"
    git(
        scenario["root"],
        "worktree",
        "add",
        "--detach",
        str(unmerged),
        scenario["owned_head"],
    )
    try:
        scenario["record"]["worktree"] = str(unmerged)
        scenario["after"] = observe(unmerged, unmerged.parent / "unmerged.log")
        scenario["write"]()
        with pytest.raises(ledger.SuiteDeltaError) as refusal:
            evaluate(scenario)
        assert refusal.value.added_failure_ids == [INTRODUCED]
        assert scenario["measures"] == []
    finally:
        git(scenario["root"], "worktree", "remove", str(unmerged))


def test_a_later_merge_uses_the_primary_revision_it_actually_imported(scenario):
    newest = commit(scenario["root"], "imported.py", "BAD = False\n")
    git(scenario["worker"], "merge", "--no-ff", newest, "-m", "Merge fixture primary")
    assert promotion_evidence._merged_primary_revision(scenario["record"]) == newest


def test_a_scoped_test_absent_on_primary_is_not_transplanted_for_attribution(scenario):
    record = {
        **scenario["record"],
        "suite_command": "python -m pytest -q {node_test_files}",
        "node": {"write_paths": ["tests/test_created.py"]},
    }
    result = promotion_records._merged_primary_baseline(
        "r-attribution",
        record,
        head=scenario["after"]["revision"],
        command="python -m pytest -q tests/test_created.py",
        dry_run=False,
    )
    assert result is None
    assert scenario["measures"] == []


@pytest.mark.parametrize("omit_failure", [False, True])
def test_a_composite_ctest_primary_receipt_covers_every_label(
    scenario, monkeypatch, omit_failure
):
    command = "ctest -L inherited; ctest -L clean"
    log = scenario["worker"].parent / "primary-labels.log"
    log.write_text(
        "Test project /build\n"
        "0% tests passed, 1 tests failed out of 1\n"
        "The following tests FAILED:\n"
        "    1 - inherited (Failed)\n"
        "EXIT=8\n"
        "Test project /build\n"
        "100% tests passed, 0 tests failed out of 1\n"
        "EXIT=0\n"
    )
    arm = {
        **scenario["primary_arm"],
        "command": command,
        "exit_status": 0,
        "failure_count": 0 if omit_failure else 1,
        "failure_ids": [] if omit_failure else ["inherited"],
        "log_path": str(log),
    }
    scenario["baseline"]["command"] = command
    scenario["after"] = {
        **scenario["after"],
        "command": command,
        "exit_status": 0,
        "failure_count": 1,
        "failure_ids": ["inherited"],
        "log_path": str(log),
    }
    scenario["record"]["suite_command"] = command
    scenario["write"]()

    calls = []

    def measure(*args, **kwargs):
        calls.append(scenario["primary"])
        return {"baseline_suite": arm, "after_suite": scenario["after"]}

    monkeypatch.setattr(promotion_records, "_measure_missing_arms", measure)
    if omit_failure:
        with pytest.raises(ledger.SuiteDeltaError, match="primary suite evidence"):
            evaluate(scenario)
    else:
        for _ in range(2):
            row = evaluate(scenario)
            assert row["status"] == "clean"
            assert row["added_failure_ids"] == []
            assert row["inherited_failure_ids"] == {"inherited": scenario["primary"]}
        assert calls == [scenario["primary"]]
