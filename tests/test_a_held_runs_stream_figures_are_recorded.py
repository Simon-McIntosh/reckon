"""A held run's stream figures are recorded before retention may reap it.

A ledger row written before promotion recorded the two figures the routing
derivation reads carries neither key, so retention holds the run directory as
the backstop for a figure that was never derived. The backfill derives both
figures from the run's own stream and records them on its row; where the stream
is already gone it records both as absent — a present null, never a zero,
because zero is a measurement — and retention then reaps the directory.

Each case is planted in a temporary crew home and read back through the readers
promotion and the reaper use, so a recorded figure and an absent one are told
apart by key presence rather than by value.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon import capabilities, ledger
from reckon.crew import routing

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "test"
DECLARED = ["reckon"]
OLD = timedelta(days=60)

# Two assistant messages carrying charged input (700 plus 300 cached on the
# first, 200 on the second) and three completed command items. The second
# message edits inside the declared scope, which is the point the orientation
# figure stops counting at.
STREAM_EVENTS = [
    {
        "type": "assistant",
        "message": {
            "id": "msg-1",
            "usage": {"input_tokens": 700, "cache_read_input_tokens": 300},
            "content": [
                {
                    "type": "tool_use",
                    "name": "Edit",
                    "input": {"file_path": "notes.md"},
                }
            ],
        },
    },
    {"type": "item.completed", "item": {"type": "command_execution", "id": "i1"}},
    {
        "type": "assistant",
        "message": {
            "id": "msg-2",
            "usage": {"input_tokens": 200},
            "content": [
                {
                    "type": "tool_use",
                    "name": "Edit",
                    "input": {"file_path": "reckon/crew/gc.py"},
                }
            ],
        },
    },
    {"type": "item.completed", "item": {"type": "command_execution", "id": "i2"}},
    {"type": "item.completed", "item": {"type": "agent_message", "id": "i3"}},
    {"type": "item.completed", "item": {"type": "command_execution", "id": "i4"}},
]

EXPECTED_FIGURES = {"orientation_input_tokens": 1200.0, "tool_steps": 3.0}
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def backfill() -> Any:
    """Import the backfill's command from ``scripts/`` beside the repository."""

    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import derive_run_figures

    return derive_run_figures


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "seed.txt").write_text("seed\n")
    git(repo, "add", "seed.txt")
    git(repo, "commit", "-q", "-m", "test: seed")
    return repo


def plant_run(
    repo: Path, home: Path, run_id: str, *, stream: bool, now: datetime = NOW
) -> Path:
    """Plant one old run directory whose ledger row carries neither figure."""

    run_dir = home / "crew" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.md").write_text(f"node: {run_id}\n")
    if stream:
        lines = [json.dumps(event) for event in STREAM_EVENTS]
        (run_dir / "stream.jsonl").write_text("\n".join(lines) + "\n")
    ledger.append_run(
        PROJECT,
        ledger.build_record(
            run_id=run_id,
            plan="plan-a",
            gate="passed",
            node="node-a",
            node_definition={"write_paths": DECLARED},
            manifest_path=str(run_dir / "manifest.md"),
        ),
        root=repo,
        allow_create=True,
    )
    stamp = (now - OLD).timestamp()
    os.utime(run_dir, (stamp, stamp))
    return run_dir


def row_for(repo: Path, run_id: str) -> dict[str, Any]:
    data, _version = ledger.load(PROJECT, root=repo)
    for row in data["runs"]:
        if isinstance(row, dict) and row.get("run_id") == run_id:
            return dict(row)
    raise AssertionError(f"no ledger row for {run_id}")


def retention_rows(repo: Path, now: datetime = NOW) -> dict[str, dict[str, Any]]:
    report = routing.garbage_collect(repo=repo, project=PROJECT, now=now)
    return {row["run_id"]: row for row in report["run_directories"]}


def test_a_stream_on_disk_records_the_figures_the_derivation_reads(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    plant_run(repo, home, "stream-on-disk", stream=True)

    row = row_for(repo, "stream-on-disk")
    assert "tool_steps" not in row
    assert "orientation_input_tokens" not in row

    # The figures the routing derivation computes from this stream are the ones
    # the row must carry afterwards, and they are also named here, so a
    # derivation that stopped reading the stream cannot pass by agreeing with
    # itself.
    derived = capabilities.derive_run_figures(row)
    assert derived == EXPECTED_FIGURES

    result = backfill().backfill_run_figures(PROJECT, root=repo)
    assert result["rows_filled"] == 1
    assert result["rows_skipped"] == 0

    filled = row_for(repo, "stream-on-disk")
    assert filled["tool_steps"] == derived["tool_steps"]
    assert filled["orientation_input_tokens"] == derived["orientation_input_tokens"]

    # A second pass is a no-op: the keys are present.
    again = backfill().backfill_run_figures(PROJECT, root=repo)
    assert again["rows_filled"] == 0
    assert again["rows_skipped"] == 0

    judged = retention_rows(repo)["stream-on-disk"]
    assert judged["action"] == "prune"
    assert judged["figure_status"] == {
        "orientation_input_tokens": "recorded",
        "tool_steps": "recorded",
    }


def test_a_stream_already_gone_is_recorded_absent_and_reapable(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    plant_run(repo, home, "stream-gone", stream=False)

    result = backfill().backfill_run_figures(PROJECT, root=repo)
    assert result["rows_skipped"] == 1
    assert result["rows_filled"] == 0

    gone = row_for(repo, "stream-gone")
    # Both figures are present and absent: a recorded absence, never a zero.
    assert "tool_steps" in gone and gone["tool_steps"] is None
    assert "orientation_input_tokens" in gone
    assert gone["orientation_input_tokens"] is None

    judged = retention_rows(repo)["stream-gone"]
    assert judged["action"] == "prune"
    assert sorted(judged["explicitly_absent_figures"]) == [
        "orientation_input_tokens",
        "tool_steps",
    ]


def test_a_row_with_no_recorded_figure_is_withheld(tmp_path: Path, monkeypatch) -> None:
    """Negative control: skipping the write leaves retention withholding."""

    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    plant_run(repo, home, "unfilled", stream=True)

    withheld = retention_rows(repo)["unfilled"]
    assert withheld["action"] == "withheld"
    assert withheld["withheld"] == "missing-derived-figure"
    assert sorted(withheld["missing_figures"]) == [
        "orientation_input_tokens",
        "tool_steps",
    ]

    done = backfill().backfill_run_figures(PROJECT, root=repo)
    assert done["rows_filled"] == 1
    assert retention_rows(repo)["unfilled"]["action"] == "prune"
