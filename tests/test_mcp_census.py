"""The MCP connection census names every failure's cause.

The census reads Claude Code's per-server connection logs over an explicit
window and groups the failures by cause, and reads the ``storage-slow`` tool
results per tool from the transcripts. The fixtures below hold one connection
per recorded signature, a clean connection, a failure no signature matches, and
a failure whose worktree directory has been removed.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import mcp_census
from reckon.project_maintenance_commands import main

WINDOW_START = "2026-10-01T01:00:00Z"
WINDOW_END = "2026-10-08T01:00:00Z"


def _instant(offset_seconds: int) -> tuple[str, str]:
    """A filename stamp and an in-window ISO instant, offset from the window start."""

    when = dt.datetime(2026, 10, 3, 12, 0, 0, tzinfo=dt.UTC) + dt.timedelta(
        seconds=offset_seconds
    )
    name = when.strftime("%Y-%m-%dT%H-%M-%S-") + f"{when.microsecond // 1000:03d}Z"
    return name, when.isoformat().replace("+00:00", "Z")


def _write_connection(
    log_root: Path,
    key: str,
    server: str,
    stamp: str,
    cwd: str,
    *,
    error: str | None = None,
) -> Path:
    directory = log_root / key / f"mcp-logs-{server}"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stamp}.jsonl"
    records = [{"debug": "Starting connection", "timestamp": stamp, "cwd": cwd}]
    if error is not None:
        records.append({"error": error, "timestamp": stamp, "cwd": cwd})
    else:
        records.append(
            {"debug": "Connection established", "timestamp": stamp, "cwd": cwd}
        )
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


@pytest.fixture
def tree(tmp_path: Path) -> dict:
    """A fixture log tree and transcript tree, with the analysis temp root."""

    log_root = tmp_path / "cache" / "claude-cli-nodejs"
    transcript_root = tmp_path / "projects"
    # The system temporary directory is another tree, so a fixture path under
    # tmp_path is classified by existence rather than read as temporary.
    temp_root = tmp_path / "sys-temp"
    temp_root.mkdir(parents=True)
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    # A representative connection per signature in the plan's table.
    _write_connection(
        log_root,
        "closed",
        "imas-cx",
        _instant(1)[0],
        str(checkout),
        error="Connection failed (CONNECTION_CLOSED): Connection closed",
    )
    _write_connection(
        log_root,
        "timeout",
        "imas-cx",
        _instant(2)[0],
        str(checkout),
        error='Connection failed (CONNECT_TIMEOUT): MCP server "imas-cx" connection '
        "timed out after 30000ms",
    )
    _write_connection(
        log_root,
        "metadata",
        "imas-cx",
        _instant(3)[0],
        str(checkout),
        error="Server stderr: error: Failed to generate package metadata for "
        "`imas-standard-names @ editable+../imas-standard-names`",
    )
    _write_connection(
        log_root,
        "missing",
        "imas-cx",
        _instant(4)[0],
        str(checkout),
        error="Server stderr: error: No such file or directory (os error 2)",
    )
    _write_connection(
        log_root,
        "readonly",
        "reckon",
        _instant(5)[0],
        str(checkout),
        error="Server stderr: error: failed to remove file `/x/.venv/bin/y`: "
        "Read-only file system (os error 30)",
    )
    _write_connection(
        log_root,
        "discover",
        "reckon",
        _instant(6)[0],
        str(checkout),
        error="Server stderr: Failed to validate request: 31 validation errors "
        "for ClientRequest\n  input_value='server/discover', input_type=str",
    )
    # A clean connection: no error record, in an existing main checkout.
    _write_connection(log_root, "clean", "imas-cx", _instant(7)[0], str(checkout))
    # A failure no signature matches.
    _write_connection(
        log_root,
        "unknown",
        "imas-cx",
        _instant(8)[0],
        str(checkout),
        error="Connection failed (SOMETHING_NEW): a cause the table does not know",
    )
    # A failure whose worktree directory has been removed; the newest occurrence,
    # so its kind is the one the CONNECTION_CLOSED cause records.
    removed = tmp_path / ".reckon-worktrees" / "imas-ambix-0405601943d2" / "session-x"
    _write_connection(
        log_root,
        "reaped",
        "imas-cx",
        _instant(9)[0],
        str(removed),
        error="Connection failed (CONNECTION_CLOSED): Connection closed",
    )

    # Transcripts: two storage-slow results from read_plan and one from roadmap.
    projects = transcript_root / "proj-a"
    projects.mkdir(parents=True)
    records = []

    def tool_use(uid: str, name: str) -> dict:
        return {
            "type": "assistant",
            "isSidechain": False,
            "timestamp": "2026-10-03T12:00:00.000Z",
            "message": {"content": [{"type": "tool_use", "id": uid, "name": name}]},
        }

    def tool_result(uid: str, payload: dict) -> dict:
        return {
            "type": "user",
            "isSidechain": False,
            "timestamp": "2026-10-03T12:00:01.000Z",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": uid,
                        "content": json.dumps(payload),
                    }
                ]
            },
            "mcpMeta": {"structuredContent": payload},
        }

    slow = {"ok": False, "error": "storage-slow", "kind": "read", "label": "read_plan"}
    records.append(tool_use("u1", "mcp__reckon__read_plan"))
    records.append(tool_result("u1", slow))
    records.append(tool_use("u2", "mcp__reckon__read_plan"))
    # No label in the payload: the tool name comes from the paired tool_use.
    records.append(tool_result("u2", {"ok": False, "error": "storage-slow"}))
    records.append(tool_use("u3", "mcp__reckon__roadmap"))
    records.append(
        tool_result("u3", {"ok": False, "error": "storage-slow", "label": "roadmap"})
    )
    # A non-storage-slow result, so the reader is shown to discriminate.
    records.append(tool_use("u4", "mcp__reckon__read_plan"))
    records.append(tool_result("u4", {"ok": True, "label": "read_plan"}))
    (projects / "session1.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )

    return {
        "log_root": log_root,
        "transcript_root": transcript_root,
        "temp_root": temp_root,
        "checkout": checkout,
    }


def _census(tree: dict) -> dict:
    return mcp_census.census(
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        log_root=tree["log_root"],
        transcript_root=tree["transcript_root"],
        temp_root=tree["temp_root"],
    )


def _cause(row: dict, name: str) -> dict:
    return next(c for c in row["causes"] if c["cause"] == name)


def test_every_signature_lands_in_its_cause(tree):
    report = _census(tree)
    imas = report["servers"]["imas-cx"]
    reckon = report["servers"]["reckon"]

    assert _cause(imas, "CONNECTION_CLOSED")["count"] == 2
    assert _cause(imas, "CONNECT_TIMEOUT")["count"] == 1
    assert _cause(imas, "package_metadata")["count"] == 1
    assert _cause(imas, "missing_directory")["count"] == 1
    assert _cause(reckon, "read_only_environment")["count"] == 1
    assert _cause(reckon, "server_discover")["count"] == 1


def test_unmatched_failure_is_unclassified(tree):
    report = _census(tree)
    row = report["servers"]["imas-cx"]
    unclassified = _cause(row, "unclassified")
    assert unclassified["count"] == 1
    assert "SOMETHING_NEW" in unclassified["first_line"]


def test_clean_connection_is_a_success(tree):
    report = _census(tree)
    row = report["servers"]["imas-cx"]
    assert row["connections"] == 7
    assert row["failed"] == 6
    assert row["succeeded"] == 1


def _recent(now: dt.datetime, seconds_ago: int) -> tuple[str, str]:
    """A connection-log filename stamp and ISO instant for a time before now."""

    when = now - dt.timedelta(seconds=seconds_ago)
    name = when.strftime("%Y-%m-%dT%H-%M-%S-") + f"{when.microsecond // 1000:03d}Z"
    return name, when.isoformat().replace("+00:00", "Z")


def test_doctor_prints_the_census_figures(tmp_path, monkeypatch):
    """The doctor's printed figures come from its own fixture, not the machine.

    The doctor censuses the last seven days, so the fixture connections and
    transcript blocks are stamped relative to now and the expected lines are
    derived from the same records written here. The census log and transcript
    roots are redirected at a temporary tree, and the real configuration home
    is asserted unchanged afterwards.
    """

    now = dt.datetime.now(dt.UTC)
    log_root = tmp_path / "cache" / "claude-cli-nodejs"
    transcript_root = tmp_path / "projects"
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    # The roots the doctor reads are module constants; redirect them and leave
    # the real configuration home untouched.
    real_log_root = mcp_census.LOG_ROOT
    real_before = sorted(os.listdir(real_log_root)) if real_log_root.is_dir() else None
    monkeypatch.setattr(mcp_census, "LOG_ROOT", log_root)
    monkeypatch.setattr(mcp_census, "TRANSCRIPT_ROOT", transcript_root)
    monkeypatch.setattr(
        "reckon.project_maintenance_commands._project_environment_drift",
        lambda: (None, []),
    )

    failures = [
        (
            "imas-cx",
            "CONNECTION_CLOSED",
            "Connection failed (CONNECTION_CLOSED): Connection closed",
        ),
        (
            "imas-cx",
            "CONNECTION_CLOSED",
            "Connection failed (CONNECTION_CLOSED): Connection closed",
        ),
        (
            "imas-cx",
            "CONNECT_TIMEOUT",
            (
                'Connection failed (CONNECT_TIMEOUT): MCP server "imas-cx" '
                "connection timed out after 30000ms"
            ),
        ),
        (
            "reckon",
            "server_discover",
            (
                "Server stderr: Failed to validate request: "
                "input_value='server/discover', input_type=str"
            ),
        ),
    ]
    for index, (server, _cause, error) in enumerate(failures, start=1):
        _write_connection(
            log_root,
            f"fail-{index}",
            server,
            _recent(now, index * 60)[0],
            str(checkout),
            error=error,
        )
    # One clean connection: no error record, in an existing directory.
    _write_connection(
        log_root,
        "clean",
        "imas-cx",
        _recent(now, (len(failures) + 1) * 60)[0],
        str(checkout),
    )

    connections = collections.Counter(server for server, _c, _e in failures)
    failed = collections.Counter(server for server, _c, _e in failures)
    connections["imas-cx"] += 1  # the clean connection
    cause_counts = collections.Counter(cause for _s, cause, _e in failures)

    # Transcripts carry the storage-slow results, stamped in the same window.
    projects = transcript_root / "proj-a"
    projects.mkdir(parents=True)
    records = []

    def tool_use(uid: str, name: str) -> dict:
        return {
            "type": "assistant",
            "isSidechain": False,
            "timestamp": _recent(now, 120)[1],
            "message": {"content": [{"type": "tool_use", "id": uid, "name": name}]},
        }

    def tool_result(uid: str, payload: dict) -> dict:
        return {
            "type": "user",
            "isSidechain": False,
            "timestamp": _recent(now, 119)[1],
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": uid,
                        "content": json.dumps(payload),
                    }
                ]
            },
            "mcpMeta": {"structuredContent": payload},
        }

    slow = {"ok": False, "error": "storage-slow", "kind": "read", "label": "read_plan"}
    records.append(tool_use("u1", "mcp__reckon__read_plan"))
    records.append(tool_result("u1", slow))
    records.append(tool_use("u2", "mcp__reckon__read_plan"))
    records.append(tool_result("u2", {"ok": False, "error": "storage-slow"}))
    records.append(tool_use("u3", "mcp__reckon__roadmap"))
    records.append(tool_result("u3", {"ok": False, "error": "storage-slow"}))
    (projects / "session1.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )
    storage = {"read_plan": 2, "roadmap": 1}

    result = CliRunner().invoke(main, ["doctor"])
    output = result.output

    assert "MCP connections" in output
    for server in mcp_census.SERVERS:
        line = (
            f"{server}: {connections[server]} connections, "
            f"{failed[server]} failed, {connections[server] - failed[server]} clean"
        )
        assert line in output
    for cause, count in cause_counts.items():
        assert f"{cause}  {count}" in output
    summary = ", ".join(f"{tool} {count}" for tool, count in sorted(storage.items()))
    assert f"storage-slow results: {summary}" in output

    # The redirect means the real configuration home saw no write from the test.
    real_after = sorted(os.listdir(real_log_root)) if real_log_root.is_dir() else None
    assert real_after == real_before


def test_removed_worktree_reads_as_worker_worktree(tree):
    report = _census(tree)
    closed = _cause(report["servers"]["imas-cx"], "CONNECTION_CLOSED")
    assert closed["directory_kind"] == mcp_census.WORKER_WORKTREE
    assert closed["repository"] == "imas-ambix"
    assert "reaped" in closed["example_path"]


def test_storage_slow_counts_per_tool(tree):
    report = _census(tree)
    assert report["storage_slow"]["read_plan"] == 2
    assert report["storage_slow"]["roadmap"] == 1
