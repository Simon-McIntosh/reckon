"""A clean text review does not hide a blocking finding in the figure review.

A run that drew a figure carries two stored reviews at one head: its text review
and its figure review. The repair reflex reads the stored review once and takes
three readings from it — whether any finding was raised, whether any of them
blocks, and which paths the round's scope grants. Read from the text review
alone, a run whose text review is clean and whose figure review carries a
blocking finding reads as no finding at all, so the round that should answer the
figure defect is never composed.

This drives the reflex entry point itself
(:func:`reckon.crew.recovery_repair_dispatch.dispatch_repair_for_run`) and pins
the four cases the round must distinguish: a blocking figure finding behind a
clean text review opens the round and names the figure; the same figure finding
once disposed opens nothing; a run whose text and figure reviews are both clean
declines with the reason the reflex already prints; and a text blocking finding
gets the round it got before the figure review was folded in.

Every store, pointer and repository is built under the test's own temporary
tree, and nothing outside it is read.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon import crew
from reckon.crew import recovery, resumption, review, runs

PROJECT = "figure-repair-fixture"
RUN_ID = "r-20260101T000000000000-reviewed-run"
TEXT_REVIEW_RUN_ID = "r-20260101T010000000000-text-review"
FIGURE_REVIEW_RUN_ID = "r-20260101T020000000000-figure-review"
FIGURE = "docs/figures/figure-repair-fixture/plot.svg"
FIGURE_SUBTREE = "docs/figures/figure-repair-fixture"

FIGURE_FINDING = {
    "file": FIGURE,
    "line": "",
    "text": "the axis label is clipped at the canvas edge",
    "severity": review.BLOCKING_FINDING_SEVERITY,
}
TEXT_FINDING = {
    "file": "reckon/crew/thing.py",
    "line": "12",
    "text": "the loop off-by-one the reviewer found",
    "severity": review.BLOCKING_FINDING_SEVERITY,
}
CONFIG = {
    "default_backend": "alpha",
    "local_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "fixture",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def reflex_fixture(tmp_path: Path, isolated_reckon_home: Path) -> dict[str, Any]:
    """A run whose repository, fence, pointer and review store live in tmp_path."""
    assert isolated_reckon_home.is_relative_to(tmp_path.parent)
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="figure-repair-fixture">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s2">Repair findings</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.name", "Worker"),
        ("config", "user.email", "worker@example.invalid"),
        ("add", "seed.txt", "docs/plans/fixture.html"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    manifest = isolated_reckon_home / "manifest.md"
    manifest.write_text("node: reviewed\nstatus: complete\n", encoding="utf-8")
    pointer = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(repo),
        "role": "implement",
        # The run was granted the figure's subtree, so a finding under the figure
        # it drew is work the run already held rather than the fleet's own record.
        "node": {
            "id": "reviewed",
            "plan": "fixture",
            "section": "s2",
            "write_paths": [FIGURE_SUBTREE],
        },
        "backend": "alpha",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    }
    crew._write_json(crew.pointer_path(RUN_ID), pointer)
    return {
        "home": isolated_reckon_home,
        "repo": repo,
        "head": head,
        "pointer": pointer,
    }


def _store_text_review(
    fixture: dict[str, Any], findings: list[dict[str, Any]]
) -> dict[str, Any]:
    record = {
        "project": PROJECT,
        "reviewed_run_id": RUN_ID,
        "review_run_id": TEXT_REVIEW_RUN_ID,
        "reviewed_base_sha": fixture["head"],
        "reviewed_head_sha": fixture["head"],
        "status": "parsed",
        "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 15),
        "findings": findings,
    }
    path = review.store_review(record)
    assert path.is_relative_to(fixture["home"])
    return record


def _store_figure_review(
    fixture: dict[str, Any],
    findings: list[dict[str, Any]],
    *,
    dispositions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "project": PROJECT,
        "reviewed_run_id": RUN_ID,
        "review_run_id": FIGURE_REVIEW_RUN_ID,
        "reviewed_base_sha": fixture["head"],
        "reviewed_head_sha": fixture["head"],
        "rubric": review.FIGURE_REVIEW_RUBRIC,
        "status": "parsed",
        "rubric_items": dict.fromkeys(review.FIGURE_REVIEW_ITEMS, "pass"),
        "findings": findings,
    }
    if dispositions is not None:
        record[review.FINDING_DISPOSITIONS_KEY] = dispositions
    path = review.store_review(record)
    assert path.is_relative_to(fixture["home"])
    return record


def _run_reflex(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], list[str], dict[str, Any] | None]:
    """Drive the reflex: its report, the advice resumes carried, and its own record.

    A reflex that refuses before it decides about the round — no review stored,
    or a review carrying no finding — records nothing on the run, so the third
    element is ``None`` there rather than a round outcome.
    """
    resumed: list[str] = []
    dispatched: list[dict[str, Any]] = []

    def fake_resume(run_id, record, *, config=None, launcher=None, advice=""):
        resumed.append(advice)
        return {"pid": os.getpid(), "turn": 1, "log_path": "resume-1.jsonl"}

    def fake_dispatch(**kwargs):
        dispatched.append(kwargs)
        return {"run_id": "r-repair-stub"}

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    monkeypatch.setattr(resumption, "_resume", fake_resume)
    monkeypatch.setattr(dispatch_module, "dispatch", fake_dispatch)
    with runs.follower_claim(PROJECT, "session-orchestrating", delivery="stream"):
        result = recovery.dispatch_repair_for_run(
            fixture["pointer"], config=CONFIG, launcher=lambda *a, **k: os.getpid()
        )
    assert dispatched == []
    assert runs.pointer_path(RUN_ID).is_relative_to(fixture["home"])
    assert fixture["home"] != Path.home() / ".config" / "reckon"
    return result, resumed, runs.read_pointer(RUN_ID).get("repair_dispatch")


def test_a_blocking_figure_finding_behind_a_clean_text_review_resumes(
    reflex_fixture, monkeypatch
):
    """The figure finding is the work the round answers, despite a clean text review."""
    _store_text_review(reflex_fixture, [])
    _store_figure_review(reflex_fixture, [FIGURE_FINDING])

    result, resumed, recorded = _run_reflex(reflex_fixture, monkeypatch)

    assert result["dispatched"] is False
    assert result.get("resumed") is True
    assert len(resumed) == 1
    assert FIGURE in resumed[0]
    assert FIGURE_FINDING["text"] in resumed[0]
    assert recorded is not None and recorded["status"] == "resumed"


def test_a_disposed_figure_finding_resumes_nothing(reflex_fixture, monkeypatch):
    """A figure finding already answered on its own record is not repaired twice."""
    _store_text_review(reflex_fixture, [])
    _store_figure_review(
        reflex_fixture,
        [FIGURE_FINDING],
        dispositions={
            "0": {"kind": "exempted", "reason": "accepted risk"},
        },
    )

    result, resumed, recorded = _run_reflex(reflex_fixture, monkeypatch)

    assert result["dispatched"] is False
    assert resumed == []
    assert recorded is not None and recorded["status"] == "decline-only"
    assert "disposed" in recorded["reason"]


def test_two_clean_reviews_decline_with_todays_reason(reflex_fixture, monkeypatch):
    """The no-finding case is unchanged: it declines naming the absent finding."""
    _store_text_review(reflex_fixture, [])
    _store_figure_review(reflex_fixture, [])

    result, resumed, _ = _run_reflex(reflex_fixture, monkeypatch)

    assert result["dispatched"] is False
    assert resumed == []
    assert (
        result["reason"]
        == "the stored review carries no finding, so composes no repair"
    )


def test_a_text_blocking_finding_still_resumes(reflex_fixture, monkeypatch):
    """A run with no figure finding gets the round it got before the merge."""
    _store_text_review(reflex_fixture, [TEXT_FINDING])

    result, resumed, recorded = _run_reflex(reflex_fixture, monkeypatch)

    assert result["dispatched"] is False
    assert result.get("resumed") is True
    assert len(resumed) == 1
    assert TEXT_FINDING["file"] in resumed[0]
    assert TEXT_FINDING["text"] in resumed[0]
    assert FIGURE not in resumed[0]
    assert recorded is not None and recorded["status"] == "resumed"
