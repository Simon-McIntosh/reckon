"""A finding with no severity on a post-audit record is reported, not repaired.

The store began refusing a finding that declares no severity when the write-time
severity audit landed. From that moment a finding that reached the store without
a severity is there by omission, so the repair composer reports it as unmarked
rather than repairing it as blocking. A record stamped before the audit is left
as before: the field did not yet exist, and its unmarked findings stay blocking.

The boundary is the record's own ``timestamp`` against the module constant that
names the audit moment. Each case sets a stamp on one side of it, so a composer
that ignored the timestamp would pass one case and fail the other rather than
passing both.
"""

from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery, repair, runs
from reckon.crew import review as review_module

RUN_ID = "r-20260101T000000000000-unmarked-reviewed-run"
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40

# One stamp either side of the audit moment the module names, so a decision read
# from the wrong side reddens the case it does not belong to.
BEFORE_AUDIT = "2026-10-01T06:00:00+00:00"
AFTER_AUDIT = "2026-10-01T08:00:00+00:00"

BLOCKING_PATH = "reckon/crew/thing.py"
UNMARKED_PATH = "reckon/crew/unmarked.py"

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
OLD_UNMARKED = {
    "file": "reckon/crew/legacy.py",
    "line": "7",
    "text": "an older record written before the severity field existed",
}


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


def test_an_old_unmarked_record_is_repaired() -> None:
    """A record before the audit keeps repairing its unmarked finding."""
    node = repair.compose_repair_node(_review([OLD_UNMARKED], timestamp=BEFORE_AUDIT))

    assert node is not None
    assert OLD_UNMARKED["file"] in node["write_paths"]
    assert node["unmarked_findings"] == []


def test_a_new_unmarked_only_record_composes_no_repair_and_names_the_count() -> None:
    """A post-audit unmarked-only round composes nothing and names the count."""
    review = _review([UNMARKED], timestamp=AFTER_AUDIT)

    assert repair.compose_repair_node(review) is None
    assert repair.unmarked_findings(review)
    assert repair.blocking_findings(review) == []


def test_a_new_mixed_record_repairs_only_the_blocking_finding() -> None:
    """The blocking finding is repaired; the unmarked one is listed, not scoped."""
    review = _review([BLOCKING, UNMARKED], timestamp=AFTER_AUDIT)
    node = repair.compose_repair_node(review)

    assert node is not None
    assert [finding["file"] for finding in repair.blocking_findings(review)] == [
        BLOCKING_PATH
    ]

    # The blocking finding is work; the unmarked finding is not.
    assert BLOCKING_PATH in node["write_paths"]
    assert UNMARKED_PATH not in node["write_paths"]

    # The unmarked finding is listed by file and line, so a reader sees what was
    # reported rather than repaired.
    listed = node["unmarked_findings"]
    assert [(entry["file"], entry["line"]) for entry in listed] == [
        (UNMARKED_PATH, UNMARKED["line"])
    ]


# ── The reflex records the unmarked count beside the follow-on count ─────────
# The composer's own refusal is not enough: the reflex records a reason on the
# reviewed run, and a round whose findings are all follow-ons and one whose
# findings are all unmarked are different causes. The reason must name both
# counts, and the unmarked findings must be listed by file and line, so a reader
# can tell a declined follow-on from a finding the store admitted unmarked.

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
    after the audit, which is the post-audit case this fixture exists to drive.
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
def test_an_unmarked_only_round_records_the_unmarked_count(
    dispatch_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """A stored unmarked-only round dispatches nothing and names the count.

    The reason names both counts, so an all-follow-on round and an all-unmarked
    round are not reported with one string. The unmarked finding is listed by
    file and line, and the same record is retrievable on the reviewed run.
    """
    config_home, repo, head_sha = dispatch_project
    record = _reviewed_pointer(config_home, repo)
    _store_review(head_sha, [UNMARKED])

    report, calls = _dispatch_repair(record, monkeypatch)

    assert report["dispatched"] is False
    assert calls == []
    assert "no blocking finding" in report["reason"]
    assert "0 follow-on" in report["reason"]
    assert "1 unmarked" in report["reason"]
    assert report["unmarked_findings"] == [
        {"file": UNMARKED_PATH, "line": UNMARKED["line"]}
    ]

    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "decline-only"
    assert recorded["reason"] == report["reason"]
