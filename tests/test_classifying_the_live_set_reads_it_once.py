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

    # The single-record reading retains the base revision's independent read.
    # It supplies an oracle for every resulting category before measuring the
    # partition, whose classifications must stay identical.
    expected = {
        pointer["run_id"]: recovery.classify_pointer(pointer)["classification"]
        for pointer in pointers
    }
    assert len(expected) == 50
    assert len(reads) > 1  # positive control: independent reads are counted
    reads.clear()

    live, interrupted = partition_live_runs(PROJECT, pointers)
    rows = live[PLAN] + interrupted.get(PLAN, [])
    observed = {row["run_id"]: row["classification"] for row in rows}
    assert observed == expected
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
