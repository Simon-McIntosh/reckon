"""The census reads a run's death from its own stream, not from its name."""

from __future__ import annotations

import json
import os
from pathlib import Path

from reckon.crew import death_census


def _write_stream(directory: Path, name: str, records: list[dict]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def _completed_records() -> list[dict]:
    return [
        {"type": "assistant", "message": {"content": "done"}},
        {"type": "result", "subtype": "success", "is_error": False},
    ]


def _mid_thinking_records() -> list[dict]:
    return [
        {"type": "assistant", "message": {"content": "thinking"}},
        {"type": "system", "subtype": "thinking"},
    ]


def _records() -> dict[str, dict]:
    return {
        "r-completed": {
            "backend": "clive",
            "role": "review",
            "effort": "high",
            "sandbox": "read-only",
        },
        "r-dead": {
            "backend": "clive",
            "role": "review",
            "effort": "xhigh",
            "sandbox": "read-only",
        },
    }


def test_stream_facts_names_result_presence_and_terminal_record(tmp_path: Path) -> None:
    stream = _write_stream(tmp_path, "stream.jsonl", _completed_records())
    facts = death_census.stream_facts(stream)
    assert facts["has_result_record"] is True
    assert facts["last_record_type"] == "result"
    assert facts["last_record_subtype"] == "success"
    assert facts["record_count"] == 2


def test_mid_thinking_stream_has_no_result_record(tmp_path: Path) -> None:
    stream = _write_stream(tmp_path, "stream.jsonl", _mid_thinking_records())
    facts = death_census.stream_facts(stream)
    assert facts["has_result_record"] is False
    assert facts["last_record_type"] == "system"
    assert facts["last_record_subtype"] == "thinking"


def test_census_classifies_completed_and_mid_thinking_death(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _write_stream(runs / "r-completed", "stream.jsonl", _completed_records())
    _write_stream(runs / "r-dead", "stream.jsonl", _mid_thinking_records())

    census = death_census.census_runs(runs, _records())

    by_id = {run["run_id"]: run for run in census["runs"]}
    assert by_id["r-completed"]["classification"] == death_census.CLASS_COMPLETED
    assert by_id["r-dead"]["classification"] == death_census.CLASS_DEAD
    assert by_id["r-dead"]["last_record_type"] == "system"
    assert by_id["r-dead"]["last_record_subtype"] == "thinking"
    assert census["totals"] == {"completed": 1, "dead": 1}


def test_cell_rates_carry_their_cell_size(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _write_stream(runs / "r-completed", "stream.jsonl", _completed_records())
    _write_stream(runs / "r-dead", "stream.jsonl", _mid_thinking_records())

    census = death_census.census_runs(runs, _records())

    cells = {(cell["role"], cell["effort"]): cell for cell in census["cells"]}
    assert cells[("review", "high")] == {
        "role": "review",
        "effort": "high",
        "size": 1,
        "completed": 1,
        "dead": 0,
        "running": 0,
        "unreadable": 0,
        "death_rate": 0.0,
        "completion_rate": 1.0,
    }
    assert cells[("review", "xhigh")]["size"] == 1
    assert cells[("review", "xhigh")]["dead"] == 1
    assert cells[("review", "xhigh")]["death_rate"] == 1.0


def _own_start_tick(pid: int) -> str:
    """The kernel start tick of a running process, as the live pointer stores it."""
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    return stat[stat.rfind(")") + 2 :].split()[19]


def test_running_run_is_not_counted_dead(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    live = tmp_path / "live"
    _write_stream(runs / "r-dead", "stream.jsonl", _mid_thinking_records())
    live.mkdir()
    # A pointer naming this live process, as the writer records one: a pid and
    # its start tick, with no stored process_alive field — the field the writer
    # does not populate. The census must derive liveness from the pid.
    own_pid = os.getpid()
    (live / "r-dead.json").write_text(
        json.dumps({"pid": own_pid, "pid_start_time": _own_start_tick(own_pid)}),
        encoding="utf-8",
    )

    census = death_census.census_runs(runs, _records(), live_dir=live)

    assert census["runs"][0]["classification"] == death_census.CLASS_RUNNING
    assert census["totals"] == {"running": 1}


def test_stored_process_alive_field_does_not_decide_liveness(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    live = tmp_path / "live"
    _write_stream(runs / "r-dead", "stream.jsonl", _mid_thinking_records())
    live.mkdir()
    # The stored field says alive but the named pid is not running, so a reader
    # that trusted the field would call this running; derived liveness calls it
    # dead. The inverse of the test above, so neither field read can pass both.
    (live / "r-dead.json").write_text(
        json.dumps({"process_alive": True, "pid": 2147483647}), encoding="utf-8"
    )

    census = death_census.census_runs(runs, _records(), live_dir=live)

    assert census["runs"][0]["classification"] == death_census.CLASS_DEAD


def test_terminal_stream_prefers_highest_resume_attempt(tmp_path: Path) -> None:
    run = tmp_path / "r"
    _write_stream(run, "stream.jsonl", _mid_thinking_records())
    _write_stream(run, "resume-1.jsonl", _mid_thinking_records())
    resumed = _write_stream(run, "resume-2.jsonl", _completed_records())

    assert death_census.terminal_stream_path(run) == resumed


def test_truncated_tail_is_a_parse_failure_not_a_record(tmp_path: Path) -> None:
    path = _write_stream(tmp_path, "stream.jsonl", _completed_records())
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"system","sub')

    facts = death_census.stream_facts(path)

    assert facts["parse_failures"] == 1
    assert facts["last_record_type"] == "result"
    assert facts["has_result_record"] is True


def test_non_clive_backend_is_excluded(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _write_stream(runs / "r-other", "stream.jsonl", _mid_thinking_records())
    records = {
        "r-other": {
            "backend": "codex",
            "role": "review",
            "effort": "high",
            "sandbox": "read-only",
        }
    }
    census = death_census.census_runs(runs, records)
    assert census["run_count"] == 0
