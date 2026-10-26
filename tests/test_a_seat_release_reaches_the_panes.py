"""A producer's seat release reaches the project's watch stream and its panes.

A producer on a host that is not the fleet record's node gives up its seat, but
its announcement went only to its own log. A seat changing hands is a state
change a coordinator oversees, so the release writes one row to the project's
watch stream naming both hosts, and the row policy draws it under a pane word of
its own — ``seat moved``.

The row is written on the first asking only: a producer re-asks the question on
every lease renewal, and a renewal that repeats the announcement would draw the
same move a second time. The stream is temporary, so nothing under the
account's real configuration home is opened or written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon._store import _config_home
from reckon.crew import runs, ticker, watch_unit

PROJECT = "a-seat-release-reaches-the-panes"
FLEET_NODE = "98dci4-clu-2068"
FOREIGN_HOST = "98dci4-srv-1006"
UNIT = f"reckon-watch-{PROJECT}.service"
RUN_ID = "r-20261026T210458000000-a-seat-release-reaches-the-panes"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A temporary config home and a temporary fleet record.

    Every directory the decision and the stream resolve through points at a
    temp path, so the account's real seat and stream are never opened. The
    release guard is per project in one process, so it is cleared for this
    project exactly as a fresh producer process would find it.
    """
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(config / "mounts.json"))
    fleet_record = tmp_path / "state" / "fleet" / "record.json"
    fleet_record.parent.mkdir(parents=True)
    monkeypatch.setenv("RECKON_FLEET_RECORD", str(fleet_record))
    watch_unit._ANNOUNCED_RELEASES.discard(PROJECT)
    yield {"config": config, "fleet_record": fleet_record}
    watch_unit._ANNOUNCED_RELEASES.discard(PROJECT)


def _write_fleet_record(path: Path, node: str) -> None:
    path.write_text(
        json.dumps(
            {
                "job_id": "1291586",
                "node": node,
                "runtime_dir": str(path.parent / "run"),
                "started_at": "2026-10-26T21:04:58Z",
            }
        ),
        encoding="utf-8",
    )


def _write_run(project: str, run_id: str, launcher_host: str) -> None:
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": project,
            "launcher_host": launcher_host,
            "phase": "working",
            "pid": 4242,
            "session": "s",
        },
    )


def _write_seat(project: str, host: str) -> None:
    path = runs.watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"project": project, "host": host, "pid": 2147483646, "unit": UNIT}),
        encoding="utf-8",
    )


def _stream_rows(project: str) -> list[dict[str, object]]:
    path = runs.watch_stream_path(project)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _misplaced_release() -> tuple[dict[str, object], list[list[str]], list[int]]:
    """Drive one release-and-exit with both production seams recorded."""
    disabled: list[list[str]] = []
    exits: list[int] = []
    verdict = watch_unit.release_seat_when_misplaced(
        PROJECT,
        host=FOREIGN_HOST,
        environ={watch_unit.WATCH_UNIT_ENV: UNIT},
        runner=disabled.append,
        exit_process=exits.append,
    )
    return verdict, disabled, exits


def test_a_release_writes_one_row_naming_both_hosts(
    home: dict[str, Path],
) -> None:
    _write_fleet_record(home["fleet_record"], FLEET_NODE)
    _write_seat(PROJECT, FOREIGN_HOST)
    _write_run(PROJECT, RUN_ID, FLEET_NODE)

    verdict, disabled, exits = _misplaced_release()

    assert verdict["action"] == "release-and-exit"
    assert disabled == [["systemctl", "--user", "disable", "--now", UNIT]]
    assert exits == [0]

    rows = _stream_rows(PROJECT)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["to_state"] == "seat-moved"
    assert row["seat_from_host"] == FOREIGN_HOST
    assert row["seat_to_host"] == FLEET_NODE
    # Both hosts travel in the clause a person reads beside the structured
    # fields, so the row names them however it is displayed.
    assert FOREIGN_HOST in str(row["detail"])
    assert FLEET_NODE in str(row["detail"])


def test_the_row_policy_prints_the_seat_move_once_under_its_own_word(
    home: dict[str, Path],
) -> None:
    _write_fleet_record(home["fleet_record"], FLEET_NODE)
    _write_seat(PROJECT, FOREIGN_HOST)
    _write_run(PROJECT, RUN_ID, FLEET_NODE)
    _misplaced_release()

    rows = _stream_rows(PROJECT)
    assert len(rows) == 1, rows

    path = ticker.PaneRowPath(own_session="s-any")
    printed = path.feed(rows[0], now=0.0)

    assert [ticker._pane_word(row) for row in printed] == ["seat moved"]
    drawn = ticker.Ticker().render(printed[0])
    assert "seat moved" in drawn, drawn
    assert FLEET_NODE in drawn, drawn


def test_a_second_asking_writes_no_second_row(home: dict[str, Path]) -> None:
    """A lease renewal re-asks the question; the answer is news once."""
    _write_fleet_record(home["fleet_record"], FLEET_NODE)
    _write_seat(PROJECT, FOREIGN_HOST)
    _write_run(PROJECT, RUN_ID, FLEET_NODE)

    _misplaced_release()
    _misplaced_release()

    assert len(_stream_rows(PROJECT)) == 1


def test_a_producer_on_the_fleet_node_writes_no_row(home: dict[str, Path]) -> None:
    _write_fleet_record(home["fleet_record"], FLEET_NODE)
    _write_run(PROJECT, RUN_ID, FLEET_NODE)

    verdict = watch_unit.release_seat_when_misplaced(
        PROJECT,
        host=FLEET_NODE,
        environ={watch_unit.WATCH_UNIT_ENV: UNIT},
        exit_process=lambda code: None,
    )

    assert verdict["action"] == "hold"
    assert _stream_rows(PROJECT) == []


def test_the_real_stream_is_untouched(home: dict[str, Path]) -> None:
    # Read through the account's own configuration home rather than the patched
    # one, so this asserts the real stream rather than the temporary stream the
    # rest of the test writes to.
    real_stream = (
        _config_home({}) / "crew" / "watch" / runs.watch_stream_path(PROJECT).name
    )
    before = real_stream.read_bytes() if real_stream.exists() else None

    _write_fleet_record(home["fleet_record"], FLEET_NODE)
    _write_seat(PROJECT, FOREIGN_HOST)
    _write_run(PROJECT, RUN_ID, FLEET_NODE)
    _misplaced_release()

    assert (real_stream.read_bytes() if real_stream.exists() else None) == before