"""A batch's roadmap build skips resolving critical-path coordinators.

``crew dispatch --batch`` derives each project's roadmap once and serves queue
ordering and the wait check from it. Neither reads who coordinates a critical
path link, and resolving that reads every run's ledger document: on 8 November
a py-spy of a batch put 87% of its time there, at about four and a half minutes
per entry. The build inside the batch now runs with that resolution off.
"""

from __future__ import annotations

from pathlib import Path

from reckon import mcp_discovery
from reckon import roadmap as roadmap_module
from reckon.crew.dispatch_batch import _built_once


def test_the_batch_build_runs_with_coordinator_resolution_off(
    tmp_path: Path, monkeypatch
) -> None:
    seen: list[bool] = []

    def discover(project, root=None):
        scope = roadmap_module._BUILT_ROADMAP.get()
        seen.append(scope.resolve_coordinators if scope is not None else True)
        return {}

    monkeypatch.setattr(mcp_discovery, "_discover_project", discover)
    docs = tmp_path / "docs"
    docs.mkdir()

    with _built_once():
        roadmap_module.build_roadmap("proj", [], [], docs_dir=docs)

    assert seen == [False]
