"""Published headroom contains Codex, Claude, and local-lane observations."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckon import budget, crew
from reckon.crew import paid_lanes

NOW = datetime(2026, 9, 25, 13, 40, tzinfo=UTC)


def _write_rollout(root: Path) -> None:
    path = root / "2026" / "09" / "25" / "rollout-codex-profile.jsonl"
    path.parent.mkdir(parents=True)
    records = [
        {
            "type": "session_meta",
            "payload": {"session_id": "codex-session"},
        },
        {
            "timestamp": (NOW - timedelta(minutes=8)).isoformat(),
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "limit_id": "codex",
                    "primary": {
                        "used_percent": 15.0,
                        "window_minutes": 10080,
                        "resets_at": int((NOW + timedelta(days=3)).timestamp()),
                    },
                    "secondary": {
                        "used_percent": 5.0,
                        "window_minutes": 300,
                        "resets_at": int((NOW + timedelta(hours=3)).timestamp()),
                    },
                },
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


def _lane_fixture(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "state": "measured",
                "running": 14,
                "concurrent_requests": 21,
                "headroom": 7,
                "waiting": 0,
                "kv_occupancy": 0.31,
                "prefix_hit_rate": 0.9705,
                "observed_at": (NOW - timedelta(seconds=20)).isoformat(),
                "router_generation_gate": {
                    "width": 14,
                    "in_flight": 14,
                    "waiting": 4,
                },
            }
        )
    )


def _write_claude_stream(root: Path) -> None:
    path = root / "claude-run" / "stream.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "type": "rate_limit_event",
                "timestamp": (NOW - timedelta(minutes=4)).isoformat(),
                "rate_limit_info": {
                    "unifiedWindows": {
                        "five_hour": {
                            "utilization": 0.24,
                            "resetsAt": (NOW + timedelta(hours=4)).isoformat(),
                        },
                        "seven_day": {
                            "utilization": 0.11,
                            "resetsAt": (NOW + timedelta(days=6)).isoformat(),
                        },
                    }
                },
            }
        )
        + "\n"
    )


def test_publish_reads_codex_rollout_claude_stream_and_local_lane(
    tmp_path: Path, monkeypatch
) -> None:
    rollout_root = tmp_path / "sessions"
    lane_path = tmp_path / "lane.json"
    output_path = tmp_path / "paid-lanes.json"
    stream_root = tmp_path / "crew-runs"
    _write_rollout(rollout_root)
    _lane_fixture(lane_path)
    _write_claude_stream(stream_root)
    monkeypatch.setattr(crew, "run_dir", lambda run_id: stream_root / run_id)

    pointers = [
        {
            "project": "demo",
            "backend": "codex-luna",
            "session_id": "codex-session",
            "observed_at": (NOW - timedelta(minutes=8)).isoformat(),
        },
        {
            "project": "demo",
            "backend": "claude",
            "run_id": "claude-run",
            "observed_at": (NOW - timedelta(minutes=4)).isoformat(),
        },
    ]
    sources = paid_lanes.gather_sources(
        ["codex-luna", "claude"],
        project="demo",
        pointers=pointers,
        rollout_root=rollout_root,
        moment=NOW,
    )
    document = paid_lanes.compose_document(
        ["codex-luna", "claude"],
        sources=sources,
        local_lane=paid_lanes.read_local_lane(lane_path, moment=NOW),
        moment=NOW,
    )
    paid_lanes.write_document_atomically(document, output_path)
    published = json.loads(output_path.read_text())

    for account in ("codex-luna", "claude"):
        assert published["accounts"][account]["state"] == paid_lanes.OBSERVED
        assert any(
            window["state"] == paid_lanes.OBSERVED
            for window in published["accounts"][account]["windows"].values()
        )
    assert published["accounts"]["codex-luna"]["source"] == "rollout"
    assert published["accounts"]["claude"]["source"] == "stream"
    assert published["local_lane"] == {
        "state": "measured",
        "running": 14,
        "ceiling": 21,
        "headroom": 7,
        "gate_width": 14,
        "in_flight": 14,
        "waiting": 0,
        "gate_waiting": 4,
        "kv_occupancy": 0.31,
        "prefix_hit_rate": 0.9705,
        "observed_at": (NOW - timedelta(seconds=20)).isoformat(),
        "age_seconds": 20.0,
        "stale": False,
    }


def test_removing_rollout_reader_makes_codex_account_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    rollout_root = tmp_path / "sessions"
    _write_rollout(rollout_root)
    monkeypatch.setattr(paid_lanes, "_codex_rollout_candidates", lambda *a, **k: {})
    sources = paid_lanes.gather_sources(
        ["codex-luna"],
        project="demo",
        pointers=[
            {
                "project": "demo",
                "backend": "codex-luna",
                "session_id": "missing-session",
                "observed_at": NOW.isoformat(),
            }
        ],
        rollout_root=rollout_root,
        moment=NOW,
    )
    document = paid_lanes.compose_document(["codex-luna"], sources=sources, moment=NOW)
    assert document["accounts"]["codex-luna"]["state"] == paid_lanes.UNKNOWN


def test_local_lane_uses_its_published_shelf_life(tmp_path: Path) -> None:
    path = tmp_path / "lane.json"
    path.write_text(
        json.dumps(
            {
                "state": "measured",
                "observed_at": (NOW - timedelta(minutes=30)).isoformat(),
                "suggested_shelf_life_seconds": 45,
            }
        )
    )

    reading = paid_lanes.read_local_lane(path, moment=NOW)

    assert reading["age_seconds"] == 1800.0
    assert reading["stale"] is True


def test_removing_stream_reader_makes_claude_account_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    stream_root = tmp_path / "crew-runs"
    _write_claude_stream(stream_root)
    monkeypatch.setattr(crew, "run_dir", lambda run_id: stream_root / run_id)
    monkeypatch.setattr(budget, "_newest_stream_reading", lambda *a, **k: None)
    sources = paid_lanes.gather_sources(
        ["claude"],
        project="demo",
        pointers=[
            {
                "project": "demo",
                "backend": "claude",
                "run_id": "claude-run",
                "observed_at": (NOW - timedelta(minutes=4)).isoformat(),
            }
        ],
        moment=NOW,
    )

    document = paid_lanes.compose_document(["claude"], sources=sources, moment=NOW)

    assert document["accounts"]["claude"]["state"] == paid_lanes.UNKNOWN
