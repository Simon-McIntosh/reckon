"""The lanes view carries a declared local lane's admission reading."""

from __future__ import annotations

import json
from pathlib import Path

from reckon import mcp_views
from reckon.flight import resolve

OBSERVED_AT = "2026-09-25T12:42:30Z"


def _write_flight_config(path: Path, *, lane_document: Path | None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    declaration = (
        f"    lane_document: {lane_document}\n" if lane_document is not None else ""
    )
    path.write_text(
        f"backends:\n  clive:\n    launch: in-harness\n{declaration}",
        encoding="utf-8",
    )
    return path


def _resolved_config(tmp_path: Path, *, lane_document: Path | None) -> dict:
    shipped = _write_flight_config(
        tmp_path / "shipped" / "flight.yaml", lane_document=lane_document
    )
    return resolve(
        shipped_path=shipped,
        host_path=tmp_path / "absent" / "flight.yaml",
    ).config


def test_declared_lane_document_carries_router_congestion(tmp_path: Path) -> None:
    lane_document = tmp_path / "lane.json"
    lane_document.write_text(
        json.dumps(
            {
                "headroom": 6,
                "running": 14,
                "waiting": 0,
                "concurrent_requests": 19,
                "observed_at": OBSERVED_AT,
                "state": "measured",
                "router_generation_gate": {
                    "width": 14,
                    "in_flight": 14,
                    "waiting": 9,
                },
            }
        ),
        encoding="utf-8",
    )

    view = mcp_views.crew_lanes_view(
        _resolved_config(tmp_path, lane_document=lane_document),
        [],
        composed_at=OBSERVED_AT,
    )
    lane = next(row for row in view["lanes"] if row["backend"] == "clive")

    assert lane["lane_document"] == str(lane_document)
    assert lane["probe_status"] == "answered"
    assert lane["engine_headroom"] == 6
    assert lane["admission_headroom"] == -9
    assert lane["headroom"] == -9
    assert lane["router_generation_gate"] == {
        "width": 14,
        "in_flight": 14,
        "waiting": 9,
    }
    assert lane["admission_verdict"] == "congested"
    assert "width 14 - in_flight 14 - waiting 9" in lane["admission_reason"]


def test_undeclared_lane_document_remains_not_declared(tmp_path: Path) -> None:
    view = mcp_views.crew_lanes_view(
        _resolved_config(tmp_path, lane_document=None),
        [],
        composed_at=OBSERVED_AT,
    )
    lane = next(row for row in view["lanes"] if row["backend"] == "clive")

    assert lane["probe_status"] == "not_declared"
    assert "lane_document" not in lane
    assert "admission_headroom" not in lane
