"""One closure's completion is counted in one place.

Both surfaces that report a closure's shipped and total count read the shared
helper, so replacing that helper changes what each of them reports. A surface
keeping its own count would ignore the replacement and disagree with the other.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import pytest

from reckon import graph_target
from reckon.roadmap import build_roadmap, resolve_graph_target
from tests.roadmap_fixtures import plan_row

SENTINEL: tuple[int, int] = (7, 9)


def _inventory() -> list[dict[str, Any]]:
    return [
        plan_row(
            "source",
            title="Source",
            status="shipped",
            impl=1.0,
            gates=[],
            followups=[],
        ),
        plan_row(
            "middle", title="Middle", depends_on=["source"], gates=[], followups=[]
        ),
        plan_row(
            "endpoint",
            title="Endpoint",
            depends_on=["middle"],
            graph_handle="release",
            gates=[],
            followups=[],
        ),
    ]


@pytest.fixture
def shared_count(monkeypatch: pytest.MonkeyPatch) -> list[list[Any]]:
    """Replace the shared count with a sentinel and record its call sites."""

    calls: list[list[Any]] = []

    def _sentinel(
        plans: Mapping[Any, Mapping[str, Any]], member_keys: Iterable[Any]
    ) -> tuple[int, int]:
        calls.append(list(member_keys))
        return SENTINEL

    # ``raising=False`` so a tree that keeps a surface's own count still runs,
    # and fails on the figure it reports rather than on the patch itself.
    monkeypatch.setattr(graph_target, "_closure_completion", _sentinel, raising=False)
    return calls


def test_graph_target_reports_the_shared_count(shared_count: list[list[Any]]) -> None:
    result = resolve_graph_target("release", {"sample": _inventory()})

    assert shared_count, "the graph-target view never consulted the shared count"
    assert result["completion"] == {"shipped": SENTINEL[0], "total": SENTINEL[1]}
    assert result["shipped_of_total"] == "7/9"
    assert len(shared_count[0]) == 3


def test_endpoint_view_reports_the_shared_count(shared_count: list[list[Any]]) -> None:
    rows = build_roadmap("sample", _inventory(), [], review={})["endpoints"]

    assert shared_count, "the endpoint view never consulted the shared count"
    assert len(rows) == 1
    shipped, total = SENTINEL
    assert rows[0]["completion"] == {"shipped": shipped, "total": total}
    assert rows[0]["shipped_fraction"] == round(shipped / total, 3)
    assert len(shared_count[0]) == 3
