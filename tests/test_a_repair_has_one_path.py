"""A repair has one path: it resumes the reviewed run, or it is refused.

The reflex composes the one repair a finding-bearing review round owes, and
delivers it by resuming the reviewed run itself — the run already owns the
worktree and the commit claim a second node would be refused for. There is no
second, dispatch path: a run the reflex cannot resume is a run it refuses, and
the refusal is *reported* so the sweep that called it carries on to the rest of
the fleet. Two facts are recorded here.

* A reviewed run whose live pointer has gone is refused with the gone-pointer
  reason, read through the real ``runs`` API: the pointer file is removed and
  ``read_pointer`` is *not* stubbed, so the raise ``runs.read_pointer`` takes on
  a missing pointer is exercised rather than stubbed away. The refusal must be
  returned, not raised out of ``dispatch_repair_for_run``.
* ``recovery_repair_dispatch`` no longer calls ``dispatch()`` at all: the launch
  block that did was removed, so the module's source names no import of the
  dispatch module and contains no dispatch call.

``tests/test_reflex_dispatches_one_repair.py`` covers the same gone-pointer case
driven through the fleet sweep; this file drives the reflex directly and adds the
source-level assertion that the dispatch path is gone.
This is the record for the section's premise: every unpromoted reviewed run
resumes, and every other is refused, so nothing is ever queued for a waiver to
ride.
"""

# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.

from __future__ import annotations

import copy
import os
import time
from pathlib import Path

from reckon.crew import recovery, recovery_repair_dispatch, runs
from reckon.crew import review as review_module
from tests.test_a_queued_run_is_admitted import (  # noqa: F401 - fixtures
    CONFIG,
    PROJECT,
    SESSION,
    _lane_document,
    _launcher,
    admit_repo,
)

GRACE = "1s"
REVIEWED_RUN = "r-delivered-awaiting-repair"
GONE_POINTER_REASON = "the reviewed run's live pointer is gone"

FINDINGS = [
    {
        "file": "reckon/crew/thing.py",
        "line": "10",
        "text": "off-by-one in the loop",
        "severity": "blocking",
    },
]


def _config(tmp_path: Path, *, worker_slots: float) -> dict:
    config = copy.deepcopy(CONFIG)
    config.setdefault("fences", {})["unreconciled_run_grace"] = GRACE
    config.setdefault("roles", {})["review"] = {}
    config.setdefault("roles", {})["implement"] = {}
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=worker_slots)
    )
    return config


def _delivered_reviewed_run(repo: Path, head_sha: str) -> dict:
    """A completed implement run, older than the grace, carrying a finding review."""
    manifest = runs.crew_home() / "manifests" / f"{REVIEWED_RUN}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: source-{REVIEWED_RUN}\nstatus: complete\ncommits: none\n",
        encoding="utf-8",
    )
    old = time.time() - 3600
    os.utime(manifest, (old, old))
    record = {
        "run_id": REVIEWED_RUN,
        "project": PROJECT,
        "session": SESSION,
        "process_alive": False,
        "repo": str(repo),
        "worktree": str(repo),
        "backend": "clive",
        "launch": "cli",
        "role": "implement",
        "phase": "starting",
        "node": {"id": "source-node", "plan": "plan-a", "section": "dispatch"},
        "manifest_path": str(manifest),
    }
    runs._write_json(runs.pointer_path(REVIEWED_RUN), record)
    review_module.store_review(
        {
            "project": PROJECT,
            "reviewed_run_id": REVIEWED_RUN,
            "reviewed_base_sha": head_sha,
            "reviewed_head_sha": head_sha,
            "status": "parsed",
            "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15),
            "total": 75,
            "findings": list(FINDINGS),
        }
    )
    return record


def test_a_gone_pointer_is_refused_not_raised(admit_repo: Path, tmp_path: Path) -> None:
    """The reviewed run's pointer is *removed*, and the reflex reports the refusal.

    The pointer is removed through the real ``runs`` API — the file
    ``runs.pointer_path`` names is unlinked and ``read_pointer`` is left
    unstubbed — so ``runs.read_pointer`` raises its ``CrewError`` on the missing
    file exactly as it does in production. The reflex must turn that into the
    gone-pointer refusal rather than letting it escape.
    """
    record = _delivered_reviewed_run(admit_repo, _head(admit_repo))
    pointer = runs.pointer_path(REVIEWED_RUN)
    assert pointer.exists()
    pointer.unlink()
    assert not pointer.exists()

    report = recovery.dispatch_repair_for_run(
        record, config=_config(tmp_path, worker_slots=1), launcher=_launcher([])
    )

    assert report["dispatched"] is False
    assert report.get("refused") is True
    assert report["reason"] == GONE_POINTER_REASON


def test_the_module_no_longer_calls_dispatch() -> None:
    """The dispatch launch block is gone: the module source names no dispatch call."""
    source = Path(recovery_repair_dispatch.__file__).read_text(encoding="utf-8")
    assert "importlib" not in source
    assert "dispatch_module" not in source
    assert ".dispatch(" not in source


def _head(repo: Path) -> str:
    import subprocess

    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
