"""A live-set partition reuses its pointer snapshot for review subjects."""

from __future__ import annotations

from typing import Any

import pytest

from reckon.crew import (
    recovery,
    recovery_review_delivery,
    recovery_review_dispatch,
    runs,
)
from reckon.mcp_views import partition_live_runs

PROJECT = "snapshot-fixture"
PLAN = "fixture-plan"
BASE_CLASSIFICATIONS = {
    "r-source-0": "scoring",
    "r-source-1": "scoring",
    "r-source-2": "scoring",
    "r-source-3": "scoring",
    "r-source-4": "scoring",
    "r-source-5": "scoring",
    "r-source-6": "scoring",
    "r-source-7": "scoring",
    "r-source-8": "scoring",
    "r-source-9": "scoring",
    "r-source-10": "scoring",
    "r-source-11": "scoring",
    "r-source-12": "scoring",
    "r-source-13": "scoring",
    "r-source-14": "scoring",
    "r-source-15": "scoring",
    "r-source-16": "scoring",
    "r-source-17": "scoring",
    "r-source-18": "scoring",
    "r-source-19": "scoring",
    "r-source-20": "scoring",
    "r-source-21": "scoring",
    "r-source-22": "scoring",
    "r-source-23": "scoring",
    "r-source-24": "scoring",
    "r-source-25": "scoring",
    "r-source-26": "scoring",
    "r-source-27": "scoring",
    "r-source-28": "scoring",
    "r-source-29": "scoring",
    "r-source-30": "scoring",
    "r-source-31": "scoring",
    "r-source-32": "scoring",
    "r-source-33": "scoring",
    "r-source-34": "scoring",
    "r-source-35": "scoring",
    "r-source-36": "scoring",
    "r-source-37": "scoring",
    "r-source-38": "scoring",
    "r-source-39": "scoring",
    "r-review-0": "running",
    "r-review-1": "running",
    "r-review-2": "running",
    "r-review-3": "running",
    "r-review-4": "running",
    "r-review-5": "running",
    "r-review-6": "running",
    "r-review-7": "running",
    "r-review-8": "running",
    "r-review-9": "running",
}


def _pointers(tmp_path) -> list[dict[str, Any]]:
    pointers = []
    for index in range(40):
        run_id = f"r-source-{index}"
        manifest = tmp_path / f"manifest-{index}.md"
        manifest.write_text(
            f"node: source-{index}\nstatus: complete\ncommits: abcdef1234\n"
        )
        pointers.append(
            {
                "run_id": run_id,
                "project": PROJECT,
                "phase": "working",
                "launcher_host": "another-host",
                "process_alive": False,
                "manifest_path": str(manifest),
                "node": {
                    "id": f"source-{index}",
                    "plan": PLAN,
                    "section": "implementation",
                },
            }
        )
    for index in range(10):
        pointer = {
            "run_id": f"r-review-{index}",
            "project": PROJECT,
            "phase": "working",
            "role": "review",
            "launcher_host": "another-host",
            "process_alive": True,
            "node": {
                "id": f"review-of-source-{index}",
                "plan": PLAN,
                "section": "implementation",
            },
        }
        runs._write_json(runs.pointer_path(pointer["run_id"]), pointer)
        pointers.append(pointer)
    assert len(pointers) == 50
    assert sum(pointer.get("role") == "review" for pointer in pointers) == 10
    return pointers


def test_partition_classifies_fifty_from_one_live_read(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    pointers = _pointers(tmp_path)
    reads: list[str] = []

    def list_live(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        reads.append("live")
        return pointers

    # These modules import the reader by name. Patching only runs.list_live
    # would miss their calls and make a false zero look like success.
    monkeypatch.setattr(runs, "list_live", list_live)
    monkeypatch.setattr(recovery_review_dispatch, "list_live", list_live)
    monkeypatch.setattr(recovery_review_delivery, "list_live", list_live)

    # A standalone classification exercises the counter before the partition
    # is measured; an empty counter must not pass as a cheap live-set read.
    recovery.classify_pointer(pointers[0])
    assert reads
    reads.clear()

    live, interrupted = partition_live_runs(PROJECT, pointers)
    rows = live[PLAN] + interrupted.get(PLAN, [])
    observed = {row["run_id"]: row["classification"] for row in rows}
    assert observed == BASE_CLASSIFICATIONS
    assert len(reads) <= 1


def test_partition_passes_only_its_projects_pointers_to_review_lookup(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    pointers = _pointers(tmp_path)
    foreign = {
        "run_id": "r-other-project",
        "project": "other-project",
        "node": {"id": "source-0", "plan": PLAN},
    }
    seen: list[set[str]] = []
    original = recovery_review_dispatch._review_subject

    def capture_subject(pointer, project, *, pointers=None):
        if pointers is not None:
            seen.append({item["project"] for item in pointers})
        return original(pointer, project, pointers=pointers)

    monkeypatch.setattr(recovery_review_dispatch, "_review_subject", capture_subject)
    partition_live_runs(PROJECT, [foreign, *pointers])
    assert seen
    assert all(projects == {PROJECT} for projects in seen)
