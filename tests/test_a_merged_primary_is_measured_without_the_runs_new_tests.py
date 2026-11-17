"""Primary attribution measures the files that revision actually contains."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys

import pytest

from reckon import ledger
from reckon.crew import promotion_records
from reckon.crew.review import gate_log_failure_ids
from tests.helpers.git import git

EXISTING = "tests/test_existing.py"
ADDED = "tests/test_added.py"
INHERITED = f"{EXISTING}::test_existing"
INTRODUCED = f"{ADDED}::test_added"
PREFIX = f"PYTHONPATH=. {shlex.quote(sys.executable)} -m pytest -p no:cacheprovider -q"
COMMAND = f"{PREFIX} {EXISTING} {ADDED}"


def _commit(tree, path, text):
    target = tree / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git(tree, "add", path)
    git(
        tree,
        "commit",
        "-qm",
        "test: record fixture",
        "-m",
        "Exercise revision measurements.",
    )
    return git(tree, "rev-parse", "HEAD")


def _observe(tree, command, log):
    result = subprocess.run(
        shlex.split(command)[1:],
        cwd=tree,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": ".", "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=30,
        check=False,
    )
    text = result.stdout + result.stderr
    log.write_text(f"# command: {command}\n{text}EXIT={result.returncode}\n")
    ids = sorted(gate_log_failure_ids(text) or [])
    return {
        "revision": git(tree, "rev-parse", "HEAD"),
        "command": command,
        "exit_status": result.returncode,
        "completed": result.returncode in (0, 1),
        "failure_count": len(ids),
        "failure_ids": ids,
        "log_path": str(log),
    }


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    root = tmp_path / "primary"
    root.mkdir()
    git(root, "init", "-qb", "main")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    _commit(root, "state.py", "BAD = False\n")
    base = _commit(
        root, EXISTING, "import state\ndef test_existing():\n    assert not state.BAD\n"
    )
    worker = tmp_path / "worker"
    git(root, "worktree", "add", "--detach", str(worker), base)
    added_text = "def test_added():\n    assert True\n"
    _commit(worker, ADDED, added_text)
    # The legacy armed runner carries added tests into the dispatch baseline.
    (root / ADDED).write_text(added_text)
    baseline = _observe(root, COMMAND, tmp_path / "baseline.log")
    (root / ADDED).unlink()
    primary = _commit(root, "state.py", "BAD = True\n")
    git(worker, "merge", "--no-ff", primary, "-m", "Merge primary fixture")
    after = _observe(worker, COMMAND, tmp_path / "after.log")
    manifest = tmp_path / "manifest.md"
    record = {
        "run_id": "r-present-tests",
        "repo": str(root),
        "worktree": str(worker),
        "base_sha": base,
        "role": "implement",
        "suite_command": COMMAND,
        "manifest_path": str(manifest),
        "node": {"write_paths": [EXISTING, ADDED]},
    }
    state = {
        "record": record,
        "root": root,
        "worker": worker,
        "primary": primary,
        "baseline": baseline,
        "after": after,
        "calls": [],
        "pointer": {},
    }

    def write():
        manifest.write_text(
            "status: complete\n"
            + "\n".join(
                f"{name}_suite: {json.dumps(state[name])}"
                for name in ("baseline", "after")
            )
            + "\n"
        )

    def measure(run_id, measured_record, head, command, **options):
        assert measured_record["base_sha"] == primary
        assert options["partition"] == "all_debug"
        state["calls"].append((command, measured_record))
        return {"baseline_suite": _observe(root, command, tmp_path / "primary.log")}

    def mutate(run_id, change):
        state["pointer"] = change(dict(state["pointer"]))

    state["write"] = write
    monkeypatch.setattr(promotion_records, "_measure_missing_arms", measure)
    monkeypatch.setattr(promotion_records, "run_dir", lambda _: tmp_path / "run")
    monkeypatch.setattr(promotion_records, "_mutate_pointer", mutate)
    write()
    yield state
    git(root, "worktree", "remove", str(worker))


def _evaluate(state, waiver="", **options):
    return promotion_records._evaluate_suite_delta(
        "r-present-tests", state["record"], waiver_reason=waiver, **options
    )


def test_primary_measures_existing_file_and_records_absent_file(scenario):
    assert scenario["baseline"]["failure_ids"] == []
    assert scenario["after"]["failure_ids"] == [INHERITED]
    row = _evaluate(scenario)
    assert row["status"] == "clean"
    assert row["added_failure_ids"] == []
    assert row["inherited_failure_ids"] == {INHERITED: scenario["primary"]}
    assert row["primary_suite"]["command"] == f"{PREFIX} {EXISTING}"
    assert row["primary_suite"]["absent_test_files"] == [ADDED]
    assert row["primary_suite"]["failure_ids"] == [INHERITED]
    assert promotion_records._node_test_files(scenario["calls"][0][1]) == [EXISTING]
    assert _evaluate(scenario)["primary_suite"] == row["primary_suite"]
    assert len(scenario["calls"]) == 1


def test_failure_in_added_file_is_charged_to_head(scenario, tmp_path):
    _commit(scenario["worker"], ADDED, "def test_added():\n    assert False\n")
    scenario["after"] = _observe(scenario["worker"], COMMAND, tmp_path / "red-head.log")
    scenario["write"]()
    with pytest.raises(ledger.SuiteDeltaError) as refusal:
        _evaluate(scenario)
    assert refusal.value.added_failure_ids == [INTRODUCED]
    row = scenario["pointer"]["suite_delta_refusal"]
    assert row["inherited_failure_ids"] == {INHERITED: scenario["primary"]}
    assert row["primary_suite"]["absent_test_files"] == [ADDED]


@pytest.mark.parametrize(
    "waiver", ["", "primary measurement unavailable in this environment"]
)
def test_impossible_primary_measurement_requires_recorded_waiver(
    scenario, monkeypatch, waiver
):
    monkeypatch.setattr(
        promotion_records, "_measure_missing_arms", lambda *a, **k: None
    )
    if not waiver:
        with pytest.raises(ledger.SuiteDeltaError, match="primary suite evidence"):
            _evaluate(scenario)
        return
    row = _evaluate(scenario, waiver)
    assert row["status"] == "waived"
    assert row["waiver_reason"] == waiver
    assert row["primary_suite"]["status"] == "waived"
    assert row["primary_suite"]["waiver_reason"] == waiver
    assert row["primary_suite"]["revision"] == scenario["primary"]
    assert row["primary_suite"]["absent_test_files"] == [ADDED]
    assert row["inherited_failure_ids"] == {}


def test_dry_run_names_filtered_measurement_without_writes(scenario, tmp_path):
    row = _evaluate(scenario, dry_run=True)
    assert row["status"] == "would-measure"
    assert row["primary_suite"]["command"] == f"{PREFIX} {EXISTING}"
    assert row["primary_suite"]["absent_test_files"] == [ADDED]
    assert scenario["calls"] == []
    assert not (tmp_path / "run").exists()


def test_scoped_declaration_records_absence_too(scenario):
    scenario["record"]["suite_command"] = f"{PREFIX} {{node_test_files}}"
    scenario["baseline"]["command"] = f"{PREFIX} {EXISTING}"
    scenario["write"]()
    row = _evaluate(scenario)
    assert row["status"] == "clean"
    assert row["primary_suite"]["absent_test_files"] == [ADDED]


def test_absent_only_scope_is_judged_at_head(scenario, tmp_path):
    scenario["record"]["suite_command"] = f"{PREFIX} {{node_test_files}}"
    scenario["record"]["node"]["write_paths"] = [ADDED]
    scenario["baseline"] = "none"
    _commit(scenario["worker"], ADDED, "def test_added():\n    assert False\n")
    scenario["after"] = _observe(
        scenario["worker"], f"{PREFIX} {ADDED}", tmp_path / "added-only.log"
    )
    scenario["write"]()
    with pytest.raises(ledger.SuiteDeltaError) as refusal:
        _evaluate(scenario)
    assert refusal.value.added_failure_ids == [INTRODUCED]
    assert scenario["calls"] == []


def test_unresolvable_revision_is_unknown_instead_of_absent(scenario):
    assert (
        promotion_records._paths_present_in_tree(
            scenario["worker"], "missing-revision", [EXISTING, ADDED]
        )
        is None
    )
    assert promotion_records._paths_present_in_tree(
        scenario["worker"], scenario["primary"], [EXISTING, ADDED]
    ) == {EXISTING}
