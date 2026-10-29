"""Disposed review findings do not commission work in a repair round."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon import crew
from reckon.crew import recovery, repair, resumption, review, review_dispositions, runs

PROJECT = "disposed-finding-fixture"
RUN_ID = "r-20260101T000000000000-reviewed-run"
REVIEW_RUN_ID = "r-20260101T010000000000-review-run"
FINDINGS = [
    {"file": "reckon/crew/first.py", "line": "10", "text": "first defect"},
    {"file": "reckon/crew/second.py", "line": "20", "text": "second defect"},
]
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
def round_fixture(tmp_path: Path, isolated_reckon_home: Path) -> dict[str, Any]:
    assert isolated_reckon_home.is_relative_to(tmp_path.parent)
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="disposed-finding-fixture">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s2">Repair findings</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
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
        "node": {"id": "reviewed", "plan": "fixture", "section": "s2"},
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


def _store(fixture: dict[str, Any], findings: list[dict[str, str]]) -> dict[str, Any]:
    record = {
        "project": PROJECT,
        "reviewed_run_id": RUN_ID,
        "review_run_id": REVIEW_RUN_ID,
        "reviewed_base_sha": fixture["head"],
        "reviewed_head_sha": fixture["head"],
        "status": "parsed",
        "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 15),
        "findings": findings,
    }
    stored_path = review.store_review(record)
    assert stored_path.is_relative_to(fixture["home"])
    return record


def _dispose(index: int, *, kind: str, target: str) -> None:
    path = review_dispositions.record_finding_disposition(
        PROJECT,
        RUN_ID,
        index,
        kind=kind,
        node=target if kind == "folded" else None,
        reason=target if kind == "exempted" else None,
    )
    assert path.is_file()
    assert path.is_relative_to(Path(os.environ["RECKON_HOME"]))


def _round(fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch):
    resumed: list[str] = []
    dispatched: list[dict[str, Any]] = []
    dispatch_module = importlib.import_module("reckon.crew.dispatch")

    def fake_resume(run_id, record, *, config=None, launcher=None, advice=""):
        resumed.append(advice)
        return {"pid": os.getpid(), "turn": 1, "log_path": "resume-1.jsonl"}

    def fake_dispatch(**kwargs):
        dispatched.append(kwargs)
        return {"run_id": "r-repair-stub"}

    monkeypatch.setattr(resumption, "_resume", fake_resume)
    monkeypatch.setattr(dispatch_module, "dispatch", fake_dispatch)
    with runs.follower_claim(PROJECT, "session-orchestrating", delivery="stream"):
        result = recovery.dispatch_repair_for_run(
            fixture["pointer"], config=CONFIG, launcher=lambda *a, **k: os.getpid()
        )
    assert dispatched == []
    assert runs.pointer_path(RUN_ID).is_relative_to(fixture["home"])
    assert fixture["home"] != Path.home() / ".config" / "reckon"
    return result, resumed, runs.read_pointer(RUN_ID)["repair_dispatch"]


@pytest.mark.arms_watch_producer
def test_two_exempted_unmarked_findings_decline_without_resume(
    round_fixture, monkeypatch
):
    _store(round_fixture, FINDINGS)
    _dispose(0, kind="exempted", target="follow-up elsewhere")
    _dispose(1, kind="exempted", target="follow-up elsewhere")

    result, resumed, recorded = _round(round_fixture, monkeypatch)

    assert result["dispatched"] is False
    assert resumed == []
    assert recorded["status"] == "decline-only"
    assert "every blocking finding was disposed" in recorded["reason"]
    assert "0" in recorded["reason"] and "1" in recorded["reason"]
    assert recorded["reason"].count("exempted") == 2


def test_newer_finding_disposition_wins_when_review_copies_disagree():
    older = {
        review.FINDING_DISPOSITIONS_KEY: {
            "0": {
                "kind": "exempted",
                "reason": "older",
                "recorded_at": "2026-01-01T00:00:00Z",
            }
        }
    }
    newer = {
        review.FINDING_DISPOSITIONS_KEY: {
            "0": {
                "kind": "folded",
                "node": "other-node",
                "recorded_at": "2026-01-02T00:00:00Z",
            },
            "1": {
                "kind": "exempted",
                "reason": "separate",
                "recorded_at": "2026-01-01T00:00:00Z",
            },
        }
    }

    merged = review._merge_record_answers([newer, older])

    assert merged[review.FINDING_DISPOSITIONS_KEY]["0"]["kind"] == "folded"
    assert merged[review.FINDING_DISPOSITIONS_KEY]["1"]["reason"] == "separate"


@pytest.mark.arms_watch_producer
def test_folded_first_leaves_only_second_in_brief_and_scope(round_fixture, monkeypatch):
    _store(round_fixture, FINDINGS)
    _dispose(0, kind="folded", target="other-node")
    _, selected = review.stored_record(PROJECT, RUN_ID)
    assert selected is not None
    composed = repair.compose_repair_node(selected)
    assert composed is not None
    assert FINDINGS[0]["file"] not in composed["write_paths"]
    assert FINDINGS[1]["file"] in composed["write_paths"]
    assert repair.finding_id(FINDINGS[0]) not in composed["brief"]
    assert repair.finding_id(FINDINGS[1]) in composed["brief"]

    result, resumed, _ = _round(round_fixture, monkeypatch)
    assert result["resumed"] is True
    assert len(resumed) == 1
    assert FINDINGS[0]["file"] not in resumed[0]
    assert FINDINGS[1]["file"] in resumed[0]
    _, persisted = review.stored_record(PROJECT, RUN_ID)
    assert persisted is not None
    assert persisted["findings"] == [
        {**finding, "id": repair.finding_id(finding)} for finding in FINDINGS
    ]
    assert persisted[review.FINDING_DISPOSITIONS_KEY]["0"]["kind"] == "folded"


@pytest.mark.arms_watch_producer
def test_open_unmarked_findings_both_resume(round_fixture, monkeypatch):
    _store(round_fixture, FINDINGS)
    result, resumed, _ = _round(round_fixture, monkeypatch)
    assert result["resumed"] is True
    assert len(resumed) == 1
    assert all(repair.finding_id(finding) in resumed[0] for finding in FINDINGS)


@pytest.mark.arms_watch_producer
def test_exempted_explicit_blocking_finding_declines(round_fixture, monkeypatch):
    _store(round_fixture, [{**FINDINGS[0], "severity": "blocking"}])
    _dispose(0, kind="exempted", target="accepted risk")
    result, resumed, recorded = _round(round_fixture, monkeypatch)
    assert result["dispatched"] is False
    assert resumed == []
    assert recorded["status"] == "decline-only"
    assert "0" in recorded["reason"] and "exempted" in recorded["reason"]


@pytest.mark.arms_watch_producer
def test_exemption_survives_review_restore_and_still_declines(
    round_fixture, monkeypatch
):
    original = _store(round_fixture, [FINDINGS[0]])
    _dispose(0, kind="exempted", target="accepted elsewhere")
    committed = review.store_committed_review(original, root=round_fixture["repo"])
    stored = json.loads(committed.read_text(encoding="utf-8"))
    assert stored[review.FINDING_DISPOSITIONS_KEY]["0"]["kind"] == "exempted"
    result, resumed, recorded = _round(round_fixture, monkeypatch)
    assert result["dispatched"] is False
    assert resumed == []
    assert recorded["status"] == "decline-only"
