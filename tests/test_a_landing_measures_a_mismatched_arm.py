"""A landing measures a mismatched suite arm rather than refusing on it.

A delivered run records two suite arms and promotion judges the pair. When an
arm is missing, was recorded at another revision, or names another command, the
run has simply not produced the evidence its own landing needs — and refusing on
that cost a worker a resume over evidence the landing can take itself. These
tests drive the landing's own measurement with a stubbed partition runner and a
temporary repository, crew home and manifest, and show that a measured arm is
recorded on the run as the landing's, and that a measured arm adding a failure
still refuses naming it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon import ledger
from reckon.crew import landing_measure, promotion_records, queue_landing, runs

SUITE = "python -m pytest -q tests/test_sample.py"
BASE_FAILURE = ""


def _git(repo: Path, *arguments: str) -> str:
    finished = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return finished.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    """A repository with a base commit and one commit beyond it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "worker")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_sample.py").write_text(
        "def test_existing():\n    assert True\n", encoding="utf-8"
    )
    _git(repo, "add", "tests/test_sample.py")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "head_only.py").write_text("# delivered change\n", encoding="utf-8")
    _git(repo, "add", "head_only.py")
    _git(repo, "commit", "-q", "-m", "head")
    head_sha = _git(repo, "rev-parse", "HEAD")
    return repo, base_sha, head_sha


def _arm(revision: str, *, failure_ids: list[str] | None = None) -> dict[str, Any]:
    ids = list(failure_ids or [])
    return {
        "revision": revision,
        "command": SUITE,
        "exit_status": 1 if ids else 0,
        "completed": True,
        "failure_count": len(ids),
        "failure_ids": ids,
        "log_path": f"/scratch/{revision[:7]}.log",
    }


def _record(
    tmp_path: Path, repo: Path, base_sha: str, *, baseline: Any, after: Any
) -> tuple[str, dict[str, Any]]:
    """A run pointer and its manifest, as promotion reads them."""
    run_id = "r-test-landing-measures"
    manifest = tmp_path / "manifest.md"
    lines = ["node: node", "status: complete", "tests: example"]
    if baseline is not None:
        lines.append(f"baseline_suite: {json.dumps(baseline)}")
    if after is not None:
        lines.append(f"after_suite: {json.dumps(after)}")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    record = {
        "run_id": run_id,
        "project": "demo",
        "role": "implement",
        "repo": str(repo),
        "worktree": str(repo),
        "base_sha": base_sha,
        "head_sha": base_sha,
        "suite_command": SUITE,
        "manifest_path": str(manifest),
    }
    if baseline is not None:
        record["baseline_suite"] = baseline
    if after is not None:
        record["after_suite"] = after
    pointer = runs.pointer_path(run_id)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps(record), encoding="utf-8")
    return run_id, record


@pytest.fixture(name="crew_home")
def crew_home_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "crew-home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


@pytest.fixture(name="measured")
def measured_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """Stub the partition runner and record every arm pair it is asked for."""
    state: dict[str, Any] = {"calls": 0, "head_ids": [], "base_ids": []}
    monkeypatch.setattr(
        landing_measure, "worker_scratch_root", lambda: tmp_path / "scratch"
    )
    monkeypatch.setattr(
        queue_landing,
        "_measure_config",
        lambda _record: {"landing": {"measure_partition": "all_debug"}},
    )
    monkeypatch.setattr(queue_landing, "_measure_timeout", lambda _record: 120.0)

    def fake_run(argv, **options):
        tree = Path(options["cwd"])
        is_base = not (tree / "head_only.py").exists()
        ids = state["base_ids"] if is_base else state["head_ids"]
        state["calls"] += 1
        text = "".join(f"FAILED {test_id}\n" for test_id in ids) or "1 passed\n"
        return subprocess.CompletedProcess(argv, 1 if ids else 0, text, "")

    def stub_partition(runner, **_options):
        return fake_run

    monkeypatch.setattr(landing_measure, "_partition_runner", stub_partition)
    return state


def _evaluate(run_id: str, record: dict[str, Any], monkeypatch, head: str):
    monkeypatch.setattr(promotion_records, "_run_head", lambda _record: head)
    return promotion_records._evaluate_suite_delta(run_id, record, waiver_reason="")


def test_a_superseded_after_arm_is_measured_at_the_head_and_lands(
    tmp_path, crew_home, measured, monkeypatch
):
    repo, base_sha, head_sha = _repository(tmp_path)
    superseded = _git(repo, "rev-parse", "HEAD~1")
    run_id, record = _record(
        tmp_path, repo, base_sha, baseline=_arm(base_sha), after=_arm(superseded)
    )

    delta = _evaluate(run_id, record, monkeypatch, head_sha)

    assert delta["status"] == "clean"
    assert delta["measured_by"] == "landing"
    assert delta["after_suite"]["revision"] == head_sha
    assert measured["calls"] == 2
    stored = json.loads(
        (runs.run_dir(run_id) / "landing-measure" / "measured-arms.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["head"] == head_sha
    assert stored["after_suite"]["measured_by"] == "landing"
    pointer = json.loads(runs.pointer_path(run_id).read_text(encoding="utf-8"))
    assert pointer["suite_measured_arms"]["after_suite"]["revision"] == head_sha


def test_a_baseline_naming_another_command_is_measured(
    tmp_path, crew_home, measured, monkeypatch
):
    repo, base_sha, head_sha = _repository(tmp_path)
    other = dict(_arm(base_sha), command="python -m pytest -q tests/other.py")
    run_id, record = _record(
        tmp_path, repo, base_sha, baseline=other, after=_arm(head_sha)
    )

    delta = _evaluate(run_id, record, monkeypatch, head_sha)

    assert delta["status"] == "clean"
    assert delta["measured_by"] == "landing"
    assert delta["baseline_suite"]["command"] == SUITE


def test_a_run_recording_no_after_arm_is_measured(
    tmp_path, crew_home, measured, monkeypatch
):
    repo, base_sha, head_sha = _repository(tmp_path)
    run_id, record = _record(
        tmp_path, repo, base_sha, baseline=_arm(base_sha), after=None
    )

    delta = _evaluate(run_id, record, monkeypatch, head_sha)

    assert delta["status"] == "clean"
    assert delta["measured_by"] == "landing"
    assert delta["after_suite"]["revision"] == head_sha


def test_a_matching_recorded_pair_is_used_without_a_measurement(
    tmp_path, crew_home, measured, monkeypatch
):
    repo, base_sha, head_sha = _repository(tmp_path)
    run_id, record = _record(
        tmp_path, repo, base_sha, baseline=_arm(base_sha), after=_arm(head_sha)
    )

    delta = _evaluate(run_id, record, monkeypatch, head_sha)

    assert delta["status"] == "clean"
    assert delta["measured_by"] == "run"
    assert measured["calls"] == 0


def test_a_measured_arm_that_adds_a_failure_refuses_naming_it(
    tmp_path, crew_home, measured, monkeypatch
):
    repo, base_sha, head_sha = _repository(tmp_path)
    added = "tests/test_sample.py::test_new"
    superseded = _git(repo, "rev-parse", "HEAD~1")
    measured["head_ids"] = [added]
    run_id, record = _record(
        tmp_path, repo, base_sha, baseline=_arm(base_sha), after=_arm(superseded)
    )

    with pytest.raises(ledger.SuiteDeltaError) as refused:
        _evaluate(run_id, record, monkeypatch, head_sha)

    assert added in refused.value.added_failure_ids
    assert added in str(refused.value)


def test_refusing_on_the_mismatched_arm_makes_the_first_case_refuse(
    tmp_path, crew_home, measured, monkeypatch
):
    """The declared negative control: without the measurement the pair refuses."""
    repo, base_sha, head_sha = _repository(tmp_path)
    superseded = _git(repo, "rev-parse", "HEAD~1")
    run_id, record = _record(
        tmp_path, repo, base_sha, baseline=_arm(base_sha), after=_arm(superseded)
    )
    monkeypatch.setattr(
        promotion_records, "_measure_missing_arms", lambda *_a, **_k: None
    )

    with pytest.raises(ledger.SuiteDeltaError) as refused:
        _evaluate(run_id, record, monkeypatch, head_sha)

    assert "after_suite.revision_matches_head" in refused.value.missing_fields
