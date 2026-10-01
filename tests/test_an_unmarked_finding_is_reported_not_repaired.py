"""A finding with no severity is reported as unmarked and still repaired.

The store accepts a finding that declares no severity: the write-time severity
audit flags such a finding to the reviewer but does not refuse the write, so a
record stamped after the audit may still carry one. The repair composer
therefore always repairs an unmarked finding as blocking and, on a record
written after the audit began flagging them, additionally lists it by file and
line so the coordinator can see the record left the severity unstated.

The cases below pin both halves: an unmarked finding on a post-audit record is
repaired and flagged; on a pre-audit record it is repaired and not flagged; and
a stored three-finding record with no severities at all still composes one
repair node naming all three, which is the shape a drop would have broken.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery, repair, resumption, runs
from reckon.crew import review as review_module

RUN_ID = "r-20260101T000000000000-unmarked-reviewed-run"
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40

# One stamp either side of the audit moment the module names.
BEFORE_AUDIT = "2026-10-01T06:00:00+00:00"
AFTER_AUDIT = "2026-10-01T08:00:00+00:00"

BLOCKING_PATH = "reckon/crew/thing.py"
UNMARKED_PATH = "reckon/crew/unmarked.py"
SECOND_UNMARKED_PATH = "reckon/crew/another.py"
THIRD_UNMARKED_PATH = "reckon/crew/third.py"

BLOCKING = {
    "file": BLOCKING_PATH,
    "line": "10",
    "text": "the guard never fires",
    "severity": review_module.BLOCKING_FINDING_SEVERITY,
}
UNMARKED = {
    "file": UNMARKED_PATH,
    "line": "42",
    "text": "a finding the store admitted without a severity",
}


def _expected_id(finding: dict[str, str]) -> str:
    """The id the documented rule yields, re-derived here as the oracle."""
    material = "\x00".join(
        (finding["file"].strip(), finding["line"].strip(), finding["text"].strip())
    )
    return "f" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]


def _review(findings: list[dict[str, str]], *, timestamp: str) -> dict[str, object]:
    return {
        "project": "reckon",
        "reviewed_run_id": RUN_ID,
        "reviewed_base_sha": BASE_SHA,
        "reviewed_head_sha": HEAD_SHA,
        "timestamp": timestamp,
        "status": "parsed",
        "findings": list(findings),
    }


def test_an_unmarked_finding_is_repaired_and_flagged_on_a_post_audit_record() -> None:
    """The finding reaches the repair and is also listed as unmarked."""
    node = repair.compose_repair_node(_review([UNMARKED], timestamp=AFTER_AUDIT))

    assert node is not None
    unmarked_id = _expected_id(UNMARKED)
    assert unmarked_id in node["brief"]
    assert UNMARKED_PATH in node["write_paths"]
    assert [(entry["file"], entry["line"]) for entry in node["unmarked_findings"]] == [
        (UNMARKED_PATH, UNMARKED["line"])
    ]


def test_an_unmarked_finding_on_a_pre_audit_record_is_repaired_without_a_flag() -> None:
    """A record before the audit repairs the finding and flags nothing."""
    node = repair.compose_repair_node(_review([UNMARKED], timestamp=BEFORE_AUDIT))

    assert node is not None
    assert _expected_id(UNMARKED) in node["brief"]
    assert node["unmarked_findings"] == []


def test_a_mixed_record_repairs_both_and_flags_only_the_unmarked_one() -> None:
    """Both findings become work; only the unmarked one is flagged."""
    node = repair.compose_repair_node(
        _review([BLOCKING, UNMARKED], timestamp=AFTER_AUDIT)
    )

    assert node is not None
    assert _expected_id(BLOCKING) in node["brief"]
    assert _expected_id(UNMARKED) in node["brief"]
    assert BLOCKING_PATH in node["write_paths"]
    assert UNMARKED_PATH in node["write_paths"]
    assert [entry["file"] for entry in node["unmarked_findings"]] == [UNMARKED_PATH]


def test_a_three_finding_unmarked_record_composes_one_node() -> None:
    """No finding is dropped: a three-finding record composes one node.

    This is the shape the refusal-by-timestamp change broke — a stored review
    whose findings all lack a severity must still compose one repair naming all
    three, not compose nothing.
    """
    findings = [
        {**UNMARKED, "file": UNMARKED_PATH},
        {**UNMARKED, "file": SECOND_UNMARKED_PATH, "line": "7", "text": "second"},
        {**UNMARKED, "file": THIRD_UNMARKED_PATH, "line": "9", "text": "third"},
    ]
    review = _review(findings, timestamp=AFTER_AUDIT)

    node = repair.compose_repair_node(review)

    assert node is not None
    for finding in findings:
        assert _expected_id(finding) in node["brief"]
        assert finding["file"] in node["write_paths"]
    assert len(node["unmarked_findings"]) == 3


# ── The reflex repairs the round rather than declining it ────────────────────
# A stored round whose findings carry no severity is finding-bearing work, so
# the reflex composes a repair for it rather than recording a decline-only
# outcome. The fixture stores through the real store, which stamps the current
# moment — after the audit — so this is the post-audit case by construction.

PROJECT = "sample"
NODE_ID = "an-unmarked-reviewed-node"

CONFIG = {
    "default_backend": "alpha",
    "local_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
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
def dispatch_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path, str]:
    """A project whose review store, live pointers and repo live under a temp root."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s3">An unmarked finding is reported not repaired</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (config_home / "mounts.json").write_text(
        '{"sample": "' + str(repo / "docs") + '"}', encoding="utf-8"
    )
    return config_home, repo, head_sha


def _reviewed_pointer(config_home: Path, repo: Path) -> dict:
    """The reviewed run: a completed implement run whose manifest reads complete."""
    manifest = config_home / "manifests" / (RUN_ID + ".md")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: " + RUN_ID + "\nstatus: complete\ncommits: " + RUN_ID + "\n",
        encoding="utf-8",
    )
    record = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "repo": str(repo),
        "role": "implement",
        "node": {"id": NODE_ID, "plan": "fixture", "section": "s3"},
        "backend": "alpha",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    }
    crew._write_json(crew.pointer_path(RUN_ID), record)
    return record


def _store_review(head_sha: str, findings: list[dict[str, str]]) -> None:
    """Write the reviewed run's review record into the isolated store root.

    No timestamp is supplied, so the store stamps the current moment — at or
    after the audit, which is the post-audit case this fixture drives.
    """
    review_module.store_review(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN_ID,
            "reviewed_base_sha": head_sha,
            "reviewed_head_sha": head_sha,
            "status": "parsed",
            "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15),
            "total": 75,
            "findings": findings,
        }
    )


def _stub_resume(monkeypatch) -> list[dict]:
    """Replace the resume entry point an unpromoted run's repair uses."""
    calls: list[dict] = []

    def fake_resume(run_id, record, *, config=None, launcher=None, advice=""):
        calls.append({"run_id": run_id, "advice": advice})
        return {"pid": os.getpid(), "turn": 1, "log_path": "resume-1.jsonl"}

    monkeypatch.setattr(resumption, "_resume", fake_resume)
    return calls


def _dispatch_repair(record: dict, monkeypatch) -> tuple[dict, list[dict]]:
    """Drive the reflex's repair dispatch with the launch call stubbed out."""
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    calls: list[dict] = []

    def fake_dispatch(**kwargs):
        calls.append(kwargs)
        return {"run_id": "r-repair-stub"}

    monkeypatch.setattr(dispatch_module, "dispatch", fake_dispatch)
    with runs.follower_claim(PROJECT, "session-orchestrating", delivery="stream"):
        report = recovery.dispatch_repair_for_run(
            record, config=CONFIG, launcher=lambda *a, **k: os.getpid()
        )
    return report, calls


@pytest.mark.arms_watch_producer
def test_an_unmarked_only_round_is_repaired_not_declined(
    dispatch_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """A stored unmarked round reaches the repair, not a decline-only outcome."""
    config_home, repo, head_sha = dispatch_project
    record = _reviewed_pointer(config_home, repo)
    _store_review(head_sha, [UNMARKED])
    resumed = _stub_resume(monkeypatch)

    report, calls = _dispatch_repair(record, monkeypatch)

    assert report.get("resumed") is True
    assert len(resumed) == 1
    assert _expected_id(UNMARKED) in resumed[0]["advice"]
    assert calls == []

    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] != "decline-only"
