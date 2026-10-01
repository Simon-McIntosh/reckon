"""A repair round answers only the findings whose severity blocks.

The severity a finding declares is what tells a defect that must be repaired
from one the reviewer recorded as a follow-on: only the blocking value
commissions work, and a finding that declares nothing blocks too, because a
record stored before the field existed carries no key and dropping it would
silently discard a finding a reviewer did raise. The brief, the goal, the ids
the done-when names and the write scope are all built from one filtered list,
so a follow-on must appear in none of them, and a round of follow-ons alone
must compose no repair at all.

The finding ids are asserted against an expectation this file derives from the
finding's own file, line and text, so a case cannot pass by agreeing with
whatever the module mints. The scope is asserted in both directions: the
blocking finding's path is present and the follow-on's is absent, so a scope
that swept every finding's path fails.
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

RUN_ID = "r-20260101T000000000000-reviewed-run"
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40

BLOCKING_PATH = "reckon/crew/thing.py"
FOLLOW_ON_PATH = "reckon/crew/other.py"

BLOCKING = {
    "file": BLOCKING_PATH,
    "line": "10",
    "text": "the guard never fires",
    "severity": review_module.BLOCKING_FINDING_SEVERITY,
}
FOLLOW_ON = {
    "file": FOLLOW_ON_PATH,
    "line": "3",
    "text": "a tidy-up left for later",
    "severity": "follow-on",
}
# A record stored before the severity field existed: the key is absent, not
# defaulted, so it must be repaired rather than dropped.
NO_SEVERITY = {
    "file": "reckon/crew/legacy.py",
    "line": "7",
    "text": "an older record with no declared severity",
}


def _expected_id(finding: dict[str, str]) -> str:
    """The id the documented rule yields, re-derived here as the oracle."""
    material = "\x00".join(
        (finding["file"].strip(), finding["line"].strip(), finding["text"].strip())
    )
    return "f" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]


def _review(findings: list[dict[str, str]]) -> dict[str, object]:
    return {
        "project": "reckon",
        "reviewed_run_id": RUN_ID,
        "reviewed_base_sha": BASE_SHA,
        "reviewed_head_sha": HEAD_SHA,
        "status": "parsed",
        "findings": list(findings),
    }


def test_mixed_findings_compose_a_node_naming_only_the_blocking_one() -> None:
    review = _review([BLOCKING, FOLLOW_ON])

    node = repair.compose_repair_node(review)

    assert node is not None
    blocking_id = _expected_id(BLOCKING)
    follow_on_id = _expected_id(FOLLOW_ON)

    # The brief, the goal and the done-when name the blocking finding and only
    # it: a follow-on is recorded on the review, not commissioned as work.
    assert blocking_id in node["brief"]
    assert follow_on_id not in node["brief"]
    assert blocking_id in node["goal"]
    assert follow_on_id not in node["goal"]
    assert blocking_id in node["done_when"]
    assert follow_on_id not in node["done_when"]

    # The scope is asserted in both directions, so a scope that swept every
    # finding's path — the unfiltered behaviour — fails on the absent half.
    assert BLOCKING_PATH in node["write_paths"]
    assert FOLLOW_ON_PATH not in node["write_paths"]


def test_a_round_of_follow_ons_composes_no_repair() -> None:
    assert repair.compose_repair_node(_review([FOLLOW_ON])) is None


def test_a_finding_with_no_severity_key_is_repaired() -> None:
    node = repair.compose_repair_node(_review([NO_SEVERITY]))

    assert node is not None
    legacy_id = _expected_id(NO_SEVERITY)
    assert legacy_id in node["brief"]
    assert legacy_id in node["goal"]
    assert NO_SEVERITY["file"] in node["write_paths"]


def test_review_findings_keeps_the_severity_and_blocking_findings_filters() -> None:
    review = _review([BLOCKING, FOLLOW_ON, NO_SEVERITY])

    # Every finding is readable with its declared severity carried through, and
    # a finding that declared none carries no key rather than a defaulted one.
    all_findings = repair.review_findings(review)
    assert len(all_findings) == 3
    by_id = {finding["id"]: finding for finding in all_findings}
    assert by_id[_expected_id(BLOCKING)]["severity"] == (
        review_module.BLOCKING_FINDING_SEVERITY
    )
    assert by_id[_expected_id(FOLLOW_ON)]["severity"] == "follow-on"
    assert "severity" not in by_id[_expected_id(NO_SEVERITY)]

    # The filtered list is the blocking finding plus the unstated one, in the
    # record's order, and the declared follow-on is the only omission.
    kept = [finding["id"] for finding in repair.blocking_findings(review)]
    assert kept == [_expected_id(BLOCKING), _expected_id(NO_SEVERITY)]


# ── The reflex names why an all-follow-on round composes no repair ───────────
# The composer's own refusal is not enough: the reflex reports a reason to the
# coordinator and records it on the reviewed run, and a round whose findings are
# all follow-ons has a cause distinct from the re-read-differs race the same
# return value otherwise covers. The reason must name that cause and how many
# findings were left as follow-ons, so a reader can tell the two apart.

PROJECT = "sample"
NODE_ID = "a-reviewed-node"

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
        '<h2 id="s2">An all-follow-on round names its cause</h2>',
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
        "node": {"id": NODE_ID, "plan": "fixture", "section": "s2"},
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
    """Write the reviewed run's review record into the isolated store root."""
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


def _stub_resume(monkeypatch) -> list[dict]:
    """Replace the resume entry point an unpromoted run's repair now uses."""
    calls: list[dict] = []

    def fake_resume(run_id, record, *, config=None, launcher=None, advice=""):
        calls.append({"run_id": run_id, "advice": advice})
        return {"pid": os.getpid(), "turn": 1, "log_path": "resume-1.jsonl"}

    monkeypatch.setattr(resumption, "_resume", fake_resume)
    return calls


@pytest.mark.arms_watch_producer
def test_an_all_follow_on_round_names_its_cause_and_the_follow_on_count(
    dispatch_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """Every finding a follow-on: the reason names no blocking finding and the count.

    The reflex returns this reason and records it on the reviewed run, so a
    round the reflex considered is not indistinguishable from one it never saw.
    The count is read from this file's own finding list, so a reason naming no
    figure — or the re-read-differs reason the same branch otherwise covers —
    fails here.
    """
    config_home, repo, head_sha = dispatch_project
    record = _reviewed_pointer(config_home, repo)
    _store_review(head_sha, [FOLLOW_ON])

    report, calls = _dispatch_repair(record, monkeypatch)

    assert report["dispatched"] is False
    assert calls == []
    assert "the review round carried no blocking finding" in report["reason"]
    assert "1 follow-on" in report["reason"]

    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "decline-only"
    assert recorded["reason"] == report["reason"]


@pytest.mark.arms_watch_producer
def test_a_blocking_finding_still_reaches_the_resume(
    dispatch_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """Control: the same fixture is acted on when a finding blocks.

    Without this arm the all-follow-on reason could pass on a fixture that never
    composed a repair at all, which is the false pass the reason exists to avoid.
    An unpromoted run's repair is a resume of the run itself rather than a new
    node, so the blocking finding reaches the resume advice and nothing is
    dispatched.
    """
    config_home, repo, head_sha = dispatch_project
    record = _reviewed_pointer(config_home, repo)
    _store_review(head_sha, [BLOCKING])
    resumed = _stub_resume(monkeypatch)

    report, calls = _dispatch_repair(record, monkeypatch)

    assert report.get("resumed") is True
    assert len(resumed) == 1
    assert _expected_id(BLOCKING) in resumed[0]["advice"]
    assert calls == []
