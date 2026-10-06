"""A plan-review attempt that launches nothing leaves no run directory behind.

Every plan-review attempt composes its run directory under the plan's report
root — the brief, the plan snapshot and the sidecar — before the lane admits
it. An attempt the lane pauses or refuses would otherwise keep that directory,
so the dispatch removes the directories it created whenever it launches
nothing, through one exit the refusal branches share, and never removes a
directory that already existed when the call began. These cases hold both
halves: a paused lane leaves no new directory and still reports ``lane-paused``,
an admitted dispatch keeps its directory, and a directory that predates the
call survives a refusal.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import plan_review, recovery

PAUSED_GATE_REASON = "the engine relaunch is in progress"


def _config(gate: Path | None) -> dict:
    worker = {
        "launch": "cli",
        "command": "codex",
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
        "time_budget": "20m",
    }
    if gate is not None:
        worker["gate_document"] = str(gate)
    return {
        "default_backend": "worker",
        "local_backend": "worker",
        "backends": {"worker": worker},
        "roles": {"review": {"backend": "worker", "execution_capable": True}},
        "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
    }


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(home / "cache" / "velocity"))
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
    return home, repo, path


def _subject(**kwargs):
    return recovery.plan_review_subject("sample", "fixture", "coordinator", **kwargs)


def _paused_gate(tmp_path: Path) -> Path:
    gate = tmp_path / "router-gate.json"
    gate.write_text(
        json.dumps({"paused": True, "reason": PAUSED_GATE_REASON}), encoding="utf-8"
    )
    return gate


def _report_root() -> Path:
    return plan_review.review_report_directory("sample", "fixture", "unused").parent


def _run_directories(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def test_a_plan_review_directory_lands_under_the_report_root(
    project: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """The positive control: the composed run directory is where refusal removes it."""
    _home, _repo, _path = project
    subject = _subject()

    fields = recovery._review_dispatch_fields(subject)

    directory = plan_review.review_report_directory(
        "sample", "fixture", subject["run_id"]
    )
    assert Path(fields["write_path"]) == directory
    assert directory.is_dir()
    assert fields["report_directories_created"][0] == str(directory)


def test_a_paused_lane_leaves_no_run_directory_and_still_reports_lane_paused(
    project: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _home, _repo, _path = project
    gate = _paused_gate(tmp_path)
    root = _report_root()
    subject = _subject()
    before = _run_directories(root)

    result = recovery.dispatch_review_for_run(subject, config=_config(gate))

    assert result["dispatched"] is False
    assert result["error"] == "lane-paused"
    assert result["reason"] == PAUSED_GATE_REASON
    assert _run_directories(root) == before
    assert not plan_review.review_report_directory(
        "sample", "fixture", subject["run_id"]
    ).exists()


def test_an_admitted_dispatch_keeps_its_run_directory(
    project: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, _repo, _path = project
    dispatch = importlib.import_module("reckon.crew.dispatch")
    monkeypatch.setattr(
        dispatch, "dispatch", lambda **kwargs: {"run_id": "launched-review"}
    )
    subject = _subject()

    result = recovery.dispatch_review_for_run(subject, config=_config(None))

    assert result["dispatched"] is True
    directory = plan_review.review_report_directory(
        "sample", "fixture", subject["run_id"]
    )
    assert (directory / "brief.md").is_file()
    assert (directory / "plan.html").is_file()
    assert (directory / "plan-review.json").is_file()


def test_a_run_directory_that_existed_before_the_call_survives_a_refusal(
    project: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _home, _repo, _path = project
    gate = _paused_gate(tmp_path)
    subject = _subject()
    directory = plan_review.review_report_directory(
        "sample", "fixture", subject["run_id"]
    )
    directory.mkdir(parents=True)
    sentinel = directory / "report.md"
    sentinel.write_text("RUBRIC reuse_search: pass\n", encoding="utf-8")

    result = recovery.dispatch_review_for_run(subject, config=_config(gate))

    assert result["dispatched"] is False
    assert result["error"] == "lane-paused"
    assert directory.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "RUBRIC reuse_search: pass\n"


def test_the_shared_exit_is_one_private_function_the_refusal_branches_call() -> None:
    """The removal lives in one helper rather than being copied into each branch."""
    source = Path(recovery.__file__).read_text(encoding="utf-8")
    assert "def _discard_composed_plan_review(" in source
    # the lane-paused branch is one of the exits that share it
    assert source.count("_discard_composed_plan_review(record, fields)") >= 3
    # and no branch removes the directory itself
    assert "shutil.rmtree(" in source
    assert source.count("shutil.rmtree(") == 1


def test_the_docstring_citation_of_store_delivered_report_still_resolves() -> None:
    source = Path(recovery.__file__).read_text(encoding="utf-8")
    assert ":func:`plan_review.store_delivered_report`" in source
    assert callable(plan_review.store_delivered_report)
