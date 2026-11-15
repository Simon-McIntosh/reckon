"""The prompt hook judges staleness by the source its rows came from.

On 14 November a power_convertor session was told twice that "the exported
station snapshot's heartbeat is 1h52m old" and to run ``crew watch --ensure``,
while its own producer had rewritten its obligations within the minute. The
station store held no row for the project, so the hook fell back to the
station's export, judged the session by that file's beat, and named a remedy
that could not clear it.
"""

# ruff: noqa: F811 - the fixtures are imported and also named as each test's parameter.

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon.crew import obligation_snapshot, station_obligations
from tests.test_obligations_read_the_station import (  # noqa: F401 - fixtures
    BLOCKED_RUN_ID,
    PROJECT,
    SESSION,
    _drive,
    _injected,
    _live_blocked_run,
    _station,
    config_home,
    follower,
    repository,
    runtime_root,
)

FRESH_RUN_ID = "run-the-producer-reported"


def _stale_export(runtime_root: Path, config_home: Path) -> Path:
    long_ago = datetime.now(tz=UTC) - timedelta(hours=2)
    store = _station(runtime_root, now=long_ago)
    path = station_obligations.export_snapshot(
        store, project=PROJECT, home=config_home / "crew", now=long_ago
    )
    store.close()
    assert path is not None
    return path


def _fresh_producer_snapshot() -> None:
    row: dict[str, Any] = {
        "kind": "blocked",
        "run_id": FRESH_RUN_ID,
        "node": "fresh-node",
        "reason": "the producer's own reading",
        "next_command": "reckon crew observe --run " + FRESH_RUN_ID,
        "age_since": datetime.now(tz=UTC).isoformat(),
    }
    payload = {
        "project": PROJECT,
        "session": SESSION,
        "obligations": [row],
        "acknowledged": [],
        "summary": {"count": 1, "oldest_age_seconds": 0},
    }
    document = obligation_snapshot.document_for(
        payload,
        computed_at=datetime.now(tz=UTC),
        stream_offset=0,
        producer={
            "pid": os.getpid(),
            "pid_start_time": obligation_snapshot.process_start_time(os.getpid()),
            "started_at": datetime.now(tz=UTC).isoformat(),
            "code_stamp": obligation_snapshot.source_code_stamp(),
        },
    )
    obligation_snapshot.write_snapshot(PROJECT, SESSION, document)


def _off_node(tmp_path: Path) -> Path:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    return elsewhere


def test_a_fresh_producer_snapshot_is_served_over_a_stale_export(
    repository, tmp_path, runtime_root, config_home, follower
):
    _live_blocked_run(repository, tmp_path)
    _stale_export(runtime_root, config_home)
    _fresh_producer_snapshot()

    line = _injected(_drive(repository, runtime=_off_node(tmp_path)))

    assert "not current" not in line, line
    assert FRESH_RUN_ID in line, line


def test_a_stale_export_that_is_the_only_source_names_its_file(
    repository, tmp_path, runtime_root, config_home, follower
):
    _live_blocked_run(repository, tmp_path)
    exported = _stale_export(runtime_root, config_home)
    obligation_snapshot.snapshot_path(PROJECT, SESSION).unlink(missing_ok=True)

    line = _injected(_drive(repository, runtime=_off_node(tmp_path)))

    assert "not current" in line, line
    assert str(exported) in line, line
    assert BLOCKED_RUN_ID not in line
