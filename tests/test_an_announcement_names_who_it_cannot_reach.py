"""A reset announcement names the sessions its broadcast cannot reach.

When the account's limit resets early, the announcement is drawn by each
session's pane from a live follower registration, and a session without one is
never told. Recording the reset therefore also reports its reach: the sessions
holding a live registration, whose panes will draw the row, and the sessions
whose registrations are all dead, which the announcer has to message by hand.

The report derives no liveness of its own — it reads
``follower_registration.list_followers`` for every mounted project and folds the
verdicts by session — and it writes nothing.

Every test isolates the crew home through the autouse ``isolated_reckon_home``
fixture, so nothing here reads or writes this host's real state.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew import follower_registration

GROUP = "claude-sub"
PROJECT = "reckon"
OTHER_PROJECT = "imas-codex"

# A pid above Linux's ``pid_max`` ceiling names no process the kernel can have
# issued, so a registration carrying it is one whose follower has gone.
DEAD_PID = 2_147_483_647


def _mount(tmp_path: Path, monkeypatch, *projects: str) -> None:
    """Register ``projects`` in a mounts registry this test owns."""
    registry = tmp_path / "mounts.json"
    registry.write_text(
        json.dumps({name: str(tmp_path / name / "docs") for name in projects}),
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(registry))


def _write_registration(project: str, session: str, pid: int) -> None:
    """Leave a registration on disk exactly as a follower that has gone did.

    An acquired registration is never removed — it is a lock, and the file is
    left behind when the holder dies — so the residue that a departed session
    leaves is a record naming its dead pid.
    """
    path = follower_registration.follower_lock_path(project, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project": project,
                "session": session,
                "pid": pid,
                "delivery": "stream",
                "host": socket.gethostname(),
            }
        ),
        encoding="utf-8",
    )


def _budget(*flags: str):
    return CliRunner().invoke(cli_main, ["crew", "budget", *flags])


def _used(group: str = GROUP):
    return _budget(
        "--reset",
        "--group",
        group,
        "--used",
        "--reason",
        "the limit reset early",
    )


def _follower_tree(root: Path) -> set[tuple[str, int, int]]:
    return {
        (str(path.relative_to(root)), path.stat().st_mtime_ns, path.stat().st_size)
        for path in root.rglob("*")
    }


def test_a_live_registration_is_reached_and_a_stale_one_is_not(
    isolated_reckon_home, tmp_path, monkeypatch
) -> None:
    _mount(tmp_path, monkeypatch, PROJECT)
    _write_registration(PROJECT, "s-stale", DEAD_PID)

    with follower_registration.follower_registration(
        PROJECT, "s-live", delivery="stream"
    ):
        result = _used()

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    reach = payload["reach"]
    assert reach["reached"] == ["s-live"]
    assert reach["unreached"] == ["s-stale"]
    assert "1 of 2" in reach["detail"]


def test_the_reach_is_folded_across_every_mounted_project(
    isolated_reckon_home, tmp_path, monkeypatch
) -> None:
    _mount(tmp_path, monkeypatch, PROJECT, OTHER_PROJECT)
    # Each session is registered in only one of them: the announced row reaches
    # a pane through whichever project's ticker feeds it.
    _write_registration(OTHER_PROJECT, "s-other-stale", DEAD_PID)

    with follower_registration.follower_registration(
        PROJECT, "s-live", delivery="stream"
    ):
        result = _used()

    assert result.exit_code == 0, result.output
    reach = json.loads(result.output)["reach"]
    assert reach["reached"] == ["s-live"]
    assert reach["unreached"] == ["s-other-stale"]


def test_a_session_holding_one_live_registration_is_reached(
    isolated_reckon_home, tmp_path, monkeypatch
) -> None:
    _mount(tmp_path, monkeypatch, PROJECT, OTHER_PROJECT)
    # A session restarted across projects leaves a residue beside its live
    # registration; the live one is what its pane draws from.
    _write_registration(PROJECT, "s-both", DEAD_PID)

    with follower_registration.follower_registration(
        OTHER_PROJECT, "s-both", delivery="stream"
    ):
        result = _used()

    assert result.exit_code == 0, result.output
    reach = json.loads(result.output)["reach"]
    assert reach["reached"] == ["s-both"]
    assert reach["unreached"] == []


def test_no_registration_anywhere_reads_as_no_pane_reached(
    isolated_reckon_home, tmp_path, monkeypatch
) -> None:
    _mount(tmp_path, monkeypatch, PROJECT)

    result = _used()

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["reach"]["reached"] == []
    assert payload["reach"]["unreached"] == []
    assert "no pane will draw the row" in payload["reach"]["detail"]


def test_the_reach_report_writes_nothing(
    isolated_reckon_home, tmp_path, monkeypatch
) -> None:
    from reckon.crew_run_commands import _budget_reset_reach

    _mount(tmp_path, monkeypatch, PROJECT)
    _write_registration(PROJECT, "s-stale", DEAD_PID)

    before = _follower_tree(isolated_reckon_home)
    report = _budget_reset_reach()
    after = _follower_tree(isolated_reckon_home)

    assert report["reached"] == []
    assert report["unreached"] == ["s-stale"]
    assert after == before
